import asyncio
import hmac
import itertools
import json
import logging
import os
import time
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote

import aiohttp
from aiohttp import web
from yarl import URL

from ..ports.matrix_transport import (
    ApplicationServiceUserRegistration, EventContext, HomeserverError, IncomingEventHandler,
    MediaTooLarge, PushCheck, TimelineChunk)


logger = logging.getLogger(__name__)

_transaction_counter = itertools.count()


class MatrixAppserviceTransport:
    def __init__(
        self, *,
        http_session: aiohttp.ClientSession,
        homeserver_url: str,
        application_service_token: str,
        homeserver_token: str,
    ) -> None:
        self._http_session = http_session
        self._homeserver_url = homeserver_url
        self._application_service_token = application_service_token
        self._homeserver_token = homeserver_token
        self._incoming_event_handler: IncomingEventHandler | None = None

    def set_incoming_event_handler(self, handler: IncomingEventHandler) -> None:
        self._incoming_event_handler = handler

    def register_push_routes(self, application: web.Application) -> None:
        application.router.add_put(
            "/_matrix/app/v1/transactions/{transaction_id}",
            self._from_homeserver(self._handle_transaction))
        application.router.add_post("/_matrix/app/v1/ping", self._from_homeserver(self._handle_ping))
        application.router.add_get(
            "/_matrix/app/v1/users/{user_id}", self._from_homeserver(self._handle_namespace_query))
        application.router.add_get(
            "/_matrix/app/v1/rooms/{room_alias}",
            self._from_homeserver(self._handle_namespace_query))
        application.router.add_route("*", "/{path:.*}", _handle_unrecognized)

    def _from_homeserver(self, handler):
        async def authenticated(request: web.Request) -> web.Response:
            authorization_header = request.headers.get("Authorization")
            if authorization_header is None:
                return web.json_response({"errcode": "M_UNAUTHORIZED"}, status=401)
            if not authorization_header.startswith("Bearer ") or not hmac.compare_digest(
                    authorization_header[len("Bearer "):].encode(),
                    self._homeserver_token.encode()):
                return web.json_response({"errcode": "M_FORBIDDEN"}, status=403)
            return await handler(request)
        return authenticated

    async def _handle_transaction(self, request: web.Request) -> web.Response:
        transaction_id = request.match_info["transaction_id"]
        try:
            body = json.loads(await request.read())
        except ValueError:
            logger.warning("transaction %s rejected: not JSON", transaction_id)
            return web.json_response(
                {"errcode": "M_NOT_JSON", "error": "the transaction is not JSON"}, status=400)
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
            logger.warning("transaction %s rejected: no list of events", transaction_id)
            return web.json_response(
                {"errcode": "M_BAD_JSON", "error": "the transaction needs a list of events"},
                status=400)
        logger.info("transaction %s events=%d", transaction_id, len(events))
        if self._incoming_event_handler is not None:
            for event in events:
                await self._incoming_event_handler(event)
        return web.json_response({})

    async def _handle_ping(self, request: web.Request) -> web.Response:
        return web.json_response({})

    async def ping_from_homeserver(self, *, appservice_id) -> PushCheck:
        try:
            status, body = await self._request(
                "POST", f"/_matrix/client/v1/appservice/{quote(appservice_id, safe='')}/ping",
                json_body={"transaction_id": f"ping_{_current_milliseconds()}"},
                timeout_seconds=45)
        except HomeserverError:
            return PushCheck(supported=True, reachable=False, status=0)
        errcode = body.get("errcode") if isinstance(body, dict) else None
        if status in (404, 405) or errcode == "M_UNRECOGNIZED":
            return PushCheck(supported=False, reachable=False, status=status, errcode=errcode)
        return PushCheck(supported=True, reachable=status == 200, status=status,
                         errcode=errcode)

    async def _handle_namespace_query(self, request: web.Request) -> web.Response:
        return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)

    async def send_event(self, *, room_id, event_type, content, as_identity) -> str:
        transaction_id = f"send_{_current_milliseconds()}_{os.getpid()}_{next(_transaction_counter)}"
        encoded_room_id = quote(room_id, safe="")
        encoded_event_type = quote(event_type, safe="")
        status, body = await self._request(
            "PUT",
            f"/_matrix/client/v3/rooms/{encoded_room_id}/send/{encoded_event_type}/{transaction_id}",
            impersonated_user_id=as_identity, json_body=content, timeout_seconds=30,
        )
        if status != 200 or not isinstance(body, dict) \
                or not isinstance(body.get("event_id"), str):
            raise HomeserverError(status, body)
        return body["event_id"]

    async def create_room(
        self, *, as_identity, name, topic, invitees, is_direct, public,
    ) -> str:
        request_body: dict[str, Any] = {"invite": invitees}
        if public:
            request_body["preset"] = "public_chat"
        elif is_direct:
            request_body["preset"] = "trusted_private_chat"
        else:
            request_body["preset"] = "private_chat"
        if is_direct:
            request_body["is_direct"] = True
        elif name:
            request_body["name"] = name
        if topic:
            request_body["topic"] = topic
        status, body = await self._request(
            "POST", "/_matrix/client/v3/createRoom",
            impersonated_user_id=as_identity, json_body=request_body, timeout_seconds=30,
        )
        if status != 200 or not isinstance(body, dict) or not body.get("room_id"):
            raise HomeserverError(status, body)
        created_room_id = body["room_id"]
        if is_direct and invitees:
            await self._mark_direct_room_for_ghost(as_identity, created_room_id, invitees)
        return created_room_id

    async def join_room(self, *, room_id, as_identity=None) -> int:
        encoded_room_id = quote(room_id, safe="")
        status, _ = await self._request(
            "POST", f"/_matrix/client/v3/rooms/{encoded_room_id}/join",
            impersonated_user_id=as_identity, timeout_seconds=30,
        )
        if status >= 400:
            logger.warning("join_room failed %s room=%s", status, room_id)
        return status

    async def leave_and_forget_room(self, *, as_identity, room_id) -> int:
        encoded_room_id = quote(room_id, safe="")
        status, body = await self._request(
            "POST", f"/_matrix/client/v3/rooms/{encoded_room_id}/leave",
            impersonated_user_id=as_identity, timeout_seconds=30,
        )
        if status >= 400:
            logger.warning("leave failed %s room=%s: %s", status, room_id, body)
            return status
        forget_status, forget_body = await self._request(
            "POST", f"/_matrix/client/v3/rooms/{encoded_room_id}/forget",
            impersonated_user_id=as_identity, timeout_seconds=30,
        )
        if forget_status != 200:
            raise HomeserverError(forget_status, forget_body)
        return status

    async def set_display_name(self, *, identity, display_name) -> None:
        await self._set_profile_field(identity, "displayname", display_name)

    async def set_avatar_url(self, *, identity, avatar_url) -> None:
        await self._set_profile_field(identity, "avatar_url", avatar_url)

    async def _set_profile_field(self, identity, field, value) -> None:
        path = f"/_matrix/client/v3/profile/{quote(identity, safe='')}/{field}"
        if value is None:
            status, body = await self._request(
                "DELETE", path, impersonated_user_id=identity, timeout_seconds=15)
        else:
            status, body = await self._request(
                "PUT", path, impersonated_user_id=identity,
                json_body={field: value}, timeout_seconds=15)
        if status != 200:
            raise HomeserverError(status, body)

    async def fetch_profile(self, *, identity) -> dict:
        encoded_user_id = quote(identity, safe="")
        status, body = await self._request(
            "GET", f"/_matrix/client/v3/profile/{encoded_user_id}",
            impersonated_user_id=identity, timeout_seconds=15,
        )
        if status != 200 or not isinstance(body, dict):
            raise HomeserverError(status, body)
        return body

    async def register_application_service_user(
        self, *, localpart
    ) -> ApplicationServiceUserRegistration:
        status, body = await self._request(
            "POST", "/_matrix/client/v3/register",
            json_body={"type": "m.login.application_service", "username": localpart},
            timeout_seconds=15,
        )
        if status == 200:
            return ApplicationServiceUserRegistration(outcome="created")
        if isinstance(body, dict) and body.get("errcode") == "M_USER_IN_USE":
            return ApplicationServiceUserRegistration(outcome="already_in_use")
        raise HomeserverError(status, body)

    async def fetch_event(self, *, room_id, event_id, as_identity=None) -> dict | None:
        encoded_room_id = quote(room_id, safe="")
        encoded_event_id = quote(event_id, safe="")
        status, body = await self._request(
            "GET", f"/_matrix/client/v3/rooms/{encoded_room_id}/event/{encoded_event_id}",
            impersonated_user_id=as_identity, timeout_seconds=30,
        )
        if status == 404:
            return None
        if status != 200 or not isinstance(body, dict):
            raise HomeserverError(status, body)
        return body

    async def fetch_event_relations(
        self, *, room_id, event_id, as_identity=None, rel_type="m.reference"
    ) -> list[dict]:
        encoded_room_id = quote(room_id, safe="")
        encoded_event_id = quote(event_id, safe="")
        encoded_rel_type = quote(rel_type, safe="")
        collected: list[dict] = []
        from_token: str | None = None
        for _ in range(20):
            query: dict[str, Any] = {"limit": 100}
            if from_token:
                query["from"] = from_token
            status, body = await self._request(
                "GET",
                f"/_matrix/client/v1/rooms/{encoded_room_id}/relations/"
                f"{encoded_event_id}/{encoded_rel_type}",
                impersonated_user_id=as_identity, query_parameters=query, timeout_seconds=30,
            )
            if status == 404:
                break
            if status != 200 or not isinstance(body, dict):
                raise HomeserverError(status, body)
            collected.extend(body.get("chunk", []))
            from_token = body.get("next_batch")
            if not from_token:
                break
        return collected

    async def fetch_event_context(self, *, room_id, event_id, as_identity) -> EventContext | None:
        encoded_room_id = quote(room_id, safe="")
        encoded_event_id = quote(event_id, safe="")
        status, body = await self._request(
            "GET", f"/_matrix/client/v3/rooms/{encoded_room_id}/context/{encoded_event_id}",
            impersonated_user_id=as_identity, query_parameters={"limit": 0}, timeout_seconds=30,
        )
        if status == 404:
            return None
        if status != 200 or not isinstance(body, dict) or not body.get("start") \
                or not body.get("end"):
            raise HomeserverError(status, body)
        return EventContext(token_before=body["start"], token_after=body["end"])

    async def fetch_timeline(
        self, *, room_id, as_identity, backwards, from_token, limit, event_types
    ) -> TimelineChunk:
        encoded_room_id = quote(room_id, safe="")
        query: dict[str, Any] = {
            "dir": "b" if backwards else "f", "limit": limit,
            "filter": json.dumps({"types": list(event_types)}, separators=(",", ":")),
        }
        if from_token is not None:
            query["from"] = from_token
        status, body = await self._request(
            "GET", f"/_matrix/client/v3/rooms/{encoded_room_id}/messages",
            impersonated_user_id=as_identity, query_parameters=query, timeout_seconds=30,
        )
        if status != 200 or not isinstance(body, dict):
            raise HomeserverError(status, body)
        return TimelineChunk(events=body.get("chunk") or [], next_token=body.get("end"))

    async def download_media(self, *, media_uri, as_identity, max_bytes) -> bytes | None:
        download_url = (
            URL(self._homeserver_url) / "_matrix" / "client" / "v1" / "media" / "download"
            / media_uri.server_name / media_uri.media_id
        ).with_query(user_id=as_identity)
        with _homeserver_answer():
            async with self._http_session.get(
                download_url,
                headers={"Authorization": f"Bearer {self._application_service_token}"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                if response.status in (403, 404):
                    return None
                if response.status != 200:
                    raise HomeserverError(response.status, await _body_of(response))
                if max_bytes is None:
                    return await response.read()
                if response.content_length is not None and response.content_length > max_bytes:
                    raise MediaTooLarge()
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data += chunk
                    if len(data) > max_bytes:
                        raise MediaTooLarge()
                return bytes(data)

    async def upload_media(self, *, data, mime_type, filename) -> str:
        upload_headers = {
            "Authorization": f"Bearer {self._application_service_token}",
            "Content-Type": mime_type,
        }
        with _homeserver_answer():
            async with self._http_session.post(
                f"{self._homeserver_url}/_matrix/media/v3/upload",
                params={"filename": filename}, headers=upload_headers, data=data,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                body = await _body_of(response)
                if response.status != 200 or not isinstance(body, dict) \
                        or not isinstance(body.get("content_uri"), str):
                    raise HomeserverError(response.status, body)
                return body["content_uri"]

    async def mark_room_read(self, *, as_identity, room_id, event_id) -> None:
        encoded_room_id = quote(room_id, safe="")
        status, body = await self._request(
            "POST", f"/_matrix/client/v3/rooms/{encoded_room_id}/read_markers",
            impersonated_user_id=as_identity,
            json_body={"m.fully_read": event_id, "m.read": event_id}, timeout_seconds=15,
        )
        if status != 200:
            raise HomeserverError(status)

    async def list_joined_rooms(self, *, identity) -> list[str]:
        status, body = await self._request(
            "GET", "/_matrix/client/v3/joined_rooms",
            impersonated_user_id=identity, timeout_seconds=15,
        )
        if status != 200 or not isinstance(body, dict):
            raise HomeserverError(status, body)
        return body.get("joined_rooms", [])

    async def list_invited_rooms(self, *, identity) -> list[str]:
        status, body = await self._request(
            "GET", "/_matrix/client/v3/sync", impersonated_user_id=identity,
            query_parameters={"timeout": 0, "filter": _INVITATIONS_ONLY_FILTER},
            timeout_seconds=30,
        )
        if status != 200 or not isinstance(body, dict):
            raise HomeserverError(status, body)
        rooms = body.get("rooms")
        invited = rooms.get("invite") if isinstance(rooms, dict) else None
        return list(invited) if isinstance(invited, dict) else []

    async def is_room_encrypted(self, *, room_id, as_identity) -> bool:
        encoded_room_id = quote(room_id, safe="")
        status, _ = await self._request(
            "GET", f"/_matrix/client/v3/rooms/{encoded_room_id}/state/m.room.encryption",
            impersonated_user_id=as_identity, timeout_seconds=15)
        if status == 200:
            return True
        if status == 404:
            return False
        raise HomeserverError(status)

    async def membership_of(self, *, room_id, user_id, as_identity) -> str | None:
        encoded_room_id = quote(room_id, safe="")
        encoded_user_id = quote(user_id, safe="")
        status, body = await self._request(
            "GET",
            f"/_matrix/client/v3/rooms/{encoded_room_id}/state/m.room.member/{encoded_user_id}",
            impersonated_user_id=as_identity, timeout_seconds=15)
        if status == 200 and isinstance(body, dict):
            return body.get("membership")
        if status in (403, 404):
            return None
        raise HomeserverError(status)

    async def user_exists(self, *, user_id, as_identity) -> bool:
        status, _body = await self._request(
            "GET", f"/_matrix/client/v3/profile/{quote(user_id, safe='')}",
            impersonated_user_id=as_identity, timeout_seconds=15)
        if status == 404:
            return False
        if status in (200, 403):
            return True
        raise HomeserverError(status)

    async def invite_user(self, *, room_id, user_id, as_identity) -> int:
        encoded_room_id = quote(room_id, safe="")
        status, body = await self._request(
            "POST", f"/_matrix/client/v3/rooms/{encoded_room_id}/invite",
            impersonated_user_id=as_identity, json_body={"user_id": user_id}, timeout_seconds=15)
        if status != 200:
            logger.warning("invite failed %s room=%s user=%s: %s", status, room_id, user_id, body)
        return status

    async def kick_user(self, *, room_id, user_id, as_identity, reason=None) -> int:
        encoded_room_id = quote(room_id, safe="")
        json_body = {"user_id": user_id}
        if reason:
            json_body["reason"] = reason
        status, body = await self._request(
            "POST", f"/_matrix/client/v3/rooms/{encoded_room_id}/kick",
            impersonated_user_id=as_identity, json_body=json_body, timeout_seconds=15)
        if status != 200:
            logger.warning("kick failed %s room=%s user=%s: %s", status, room_id, user_id, body)
        return status

    async def list_members(self, *, room_id, as_identity) -> dict:
        encoded_room_id = quote(room_id, safe="")
        status, body = await self._request(
            "GET", f"/_matrix/client/v3/rooms/{encoded_room_id}/joined_members",
            impersonated_user_id=as_identity, timeout_seconds=15)
        if status != 200 or not isinstance(body, dict):
            raise HomeserverError(status, body)
        return body.get("joined", {})

    async def _mark_direct_room_for_ghost(
        self, ghost_mxid: str, room_id: str, invitees: list[str]
    ) -> None:
        encoded_user_id = quote(ghost_mxid, safe="")
        status, current = await self._request(
            "GET", f"/_matrix/client/v3/user/{encoded_user_id}/account_data/m.direct",
            impersonated_user_id=ghost_mxid, timeout_seconds=15,
        )
        if status == 404:
            current = {}
        elif status != 200 or not isinstance(current, dict):
            raise HomeserverError(status, current)
        direct_rooms_by_user = current
        for invited_user in invitees:
            direct_rooms_by_user.setdefault(invited_user, [])
            if room_id not in direct_rooms_by_user[invited_user]:
                direct_rooms_by_user[invited_user].append(room_id)
        status, body = await self._request(
            "PUT", f"/_matrix/client/v3/user/{encoded_user_id}/account_data/m.direct",
            impersonated_user_id=ghost_mxid, json_body=direct_rooms_by_user, timeout_seconds=15,
        )
        if status != 200:
            raise HomeserverError(status, body)

    async def _request(
        self, method: str, path: str, *,
        impersonated_user_id: str | None = None,
        query_parameters: dict | None = None,
        json_body: dict | None = None,
        timeout_seconds: int = 60,
    ) -> tuple[int, Any]:
        request_headers = {"Authorization": f"Bearer {self._application_service_token}"}
        if json_body is not None:
            request_headers["Content-Type"] = "application/json"
        effective_query = dict(query_parameters or {})
        if impersonated_user_id is not None:
            effective_query["user_id"] = impersonated_user_id
        with _homeserver_answer():
            async with self._http_session.request(
                method, f"{self._homeserver_url}{path}",
                headers=request_headers,
                params=effective_query or None,
                json=json_body,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as response:
                return response.status, await _body_of(response)


@contextmanager
def _homeserver_answer():
    try:
        yield
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise HomeserverError(None) from error


async def _body_of(response: aiohttp.ClientResponse) -> Any:
    raw_body = await response.read()
    try:
        return json.loads(raw_body)
    except ValueError:
        return raw_body.decode("utf-8", errors="replace")


_INVITATIONS_ONLY_FILTER = json.dumps({
    "presence": {"types": []}, "account_data": {"types": []},
    "room": {"timeline": {"limit": 0}, "state": {"types": []}, "ephemeral": {"types": []},
             "account_data": {"types": []}},
}, separators=(",", ":"))


async def _handle_unrecognized(request: web.Request) -> web.Response:
    return web.json_response(
        {"errcode": "M_UNRECOGNIZED", "error": "the appservice does not implement this endpoint"},
        status=404)


def _current_milliseconds() -> int:
    return int(time.time() * 1000)
