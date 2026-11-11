import base64
import binascii
import hmac
import json
import logging
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from ..api_errors import PROBLEM_CONTENT_TYPE, ApiError, homeserver_problem, problem
from ..payload_fields import optional_string, reject_unknown_fields, required_string
from ..ports.matrix_transport import HomeserverError, MatrixTransport
from ..services.channels import ChannelService, Page
from ..services.event_reader import MediaFile
from ..services.identity_registry import NAME_PATTERN, IdentityRegistry, ResolvedIdentity
from ..services.image_types import SUPPORTED as SUPPORTED_IMAGES
from ..services.message_sender import MessageSender
from ..services.profiles import ProfileService
from ..services.push_reachability import PushReachability
from ..services.rate_limits import RateLimit
from ..services.webhook_notifier import WebhookNotifier


logger = logging.getLogger(__name__)

_OPENAPI_PATH = Path(__file__).with_name("openapi.json")
API_VERSION = int(json.loads(_OPENAPI_PATH.read_text())["info"]["version"].split(".")[0])
API_VERSION_HEADER = "X-API-Version"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
INCLUDABLE = frozenset({"media", "poll"})
JSON = "application/json"
MERGE_PATCH = "application/merge-patch+json"
_HTTP_ERROR_CODES = {
    400: "bad_request", 404: "not_found", 405: "method_not_allowed",
    413: "payload_too_large", 415: "unsupported_media_type",
}


@web.middleware
async def _version_middleware(request: web.Request, handler):
    response = await handler(request)
    response.headers[API_VERSION_HEADER] = str(API_VERSION)
    return response


@web.middleware
async def _problem_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except ApiError as error:
        return _problem_response(error.as_problem(), error.headers)
    except HomeserverError as error:
        logger.warning("%s to %s %s: %s",
                       error, request.method, request.rel_url.path, error.body)
        return _problem_response(homeserver_problem(error.status, error.body), {})
    except web.HTTPException as error:
        if error.status < 400:
            raise
        return _problem_response(
            problem(error.status, _HTTP_ERROR_CODES.get(error.status, "http_error"),
                    _http_error_detail(request, error)),
            {name: value for name, value in error.headers.items() if name == "Allow"})
    except Exception:
        logger.exception("unhandled error on %s %s", request.method, request.path)
        return _problem_response(problem(500, "internal_error", "unexpected server error"), {})


def build_application() -> web.Application:
    return web.Application(
        middlewares=[_version_middleware, _problem_middleware], client_max_size=MAX_REQUEST_BYTES)


class HttpApi:
    def __init__(
        self, *,
        transport: MatrixTransport,
        identity_registry: IdentityRegistry,
        channels: ChannelService,
        sender: MessageSender,
        profiles: ProfileService,
        webhook_notifier: WebhookNotifier,
        speech_services: dict,
        encryption_for,
        e2ee_enabled: bool,
        request_limit: RateLimit,
        signup_limit: RateLimit,
        push_reachability: PushReachability,
        admin_token: str = "",
        open_signup: bool = True,
    ) -> None:
        self._push_reachability = push_reachability
        self._transport = transport
        self._identity_registry = identity_registry
        self._channels = channels
        self._sender = sender
        self._profiles = profiles
        self._webhook_notifier = webhook_notifier
        self._speech_services = speech_services
        self._encryption_for = encryption_for
        self._e2ee_enabled = e2ee_enabled
        self._admin_token = admin_token
        self._open_signup = open_signup
        self._request_limit = request_limit
        self._signup_limit = signup_limit

    def register_routes(self, application: web.Application) -> None:
        router = application.router
        router.add_get("/health", self._health)
        router.add_get("/health/push", self._push_health)
        router.add_get("/openapi.json", self._openapi)
        router.add_get("/capabilities", self._capabilities)
        router.add_post("/identities", self._register)
        router.add_get("/identities/{id}", self._get_identity)
        router.add_patch("/identities/{id}", self._patch_identity)
        router.add_post("/identities/{id}/token", self._issue_token)
        router.add_delete("/identities/{id}/token", self._revoke_token)
        router.add_get("/identities/{id}/avatar", self._get_avatar)
        router.add_put("/identities/{id}/avatar", self._put_avatar)
        router.add_delete("/identities/{id}/avatar", self._delete_avatar)
        router.add_get("/identities/{id}/webhook", self._get_webhook)
        router.add_put("/identities/{id}/webhook", self._put_webhook)
        router.add_delete("/identities/{id}/webhook", self._delete_webhook)
        router.add_get("/channels", self._list_channels)
        router.add_post("/channels", self._create_channel)
        router.add_get("/channels/{room}", self._get_channel)
        router.add_put("/channels/{room}", self._join_channel)
        router.add_delete("/channels/{room}", self._leave_channel)
        router.add_put("/channels/{room}/receipt", self._receipt)
        router.add_get("/channels/{room}/events", self._events)
        router.add_post("/channels/{room}/events", self._send)
        router.add_get("/channels/{room}/events/{event}", self._event)
        router.add_get("/channels/{room}/events/{event}/media", self._media)
        router.add_get("/channels/{room}/members", self._list_members)
        router.add_post("/channels/{room}/members", self._invite_member)
        router.add_delete("/channels/{room}/members/{user}", self._remove_member)

    async def _health(self, request: web.Request) -> web.Response:
        return web.Response(status=204)

    async def _push_health(self, request: web.Request) -> web.Response:
        _query(request)
        self._push_reachability.require_reachable()
        return web.Response(status=204)

    async def _openapi(self, request: web.Request) -> web.Response:
        return web.Response(body=_OPENAPI_PATH.read_bytes(), content_type=JSON)

    async def _capabilities(self, request: web.Request) -> web.Response:
        capabilities: dict = {
            "api_version": API_VERSION,
            "open_signup": self._open_signup,
            "webhooks": self._webhook_notifier.enabled,
            "e2ee": self._e2ee_enabled,
        }
        for name, service in self._speech_services.items():
            capabilities[name] = (
                service.describe() if service is not None
                else {"available": False, "info": None})
        return web.json_response(capabilities)

    async def _register(self, request: web.Request) -> web.Response:
        if self._admin_authorized(request):
            return await self._create_identity(request)
        if not self._open_signup:
            raise ApiError(403, "signup_closed", "signup is closed: an admin creates identities")
        with self._signup_limit.counted_if_successful("signup"):
            return await self._create_identity(request)

    async def _create_identity(self, request: web.Request) -> web.Response:
        _query(request)
        payload = await _json_body(request)
        reject_unknown_fields(payload, ("name", "display_name"))
        name = required_string(payload, "name")
        if not NAME_PATTERN.fullmatch(name):
            raise ApiError(422, "invalid_name", "name must match [a-z0-9_]+")
        registration = await self._identity_registry.register_identity(
            transport=self._transport, name=name,
            display_name=optional_string(payload, "display_name"))
        if self._e2ee_enabled:
            await self._encryption_for(registration.mxid)
        logger.info("register %s", registration.mxid)
        return web.json_response({
            "mxid": registration.mxid,
            "localpart": registration.localpart,
            "display_name": registration.display_name,
            "api_token": registration.api_token,
        }, status=201, headers={"Location": f"identities/{registration.localpart}"})

    async def _issue_token(self, request: web.Request) -> web.Response:
        identity = self._target_identity(request)
        _query(request)
        if request.body_exists:
            raise ApiError(400, "body_not_allowed", "this request takes no body")
        localpart = self._identity_registry.localpart_of(identity.mxid)
        logger.info("token issued for %s", identity.mxid)
        return web.json_response(
            {"mxid": identity.mxid, "api_token": self._identity_registry.issue_token(localpart)})

    async def _revoke_token(self, request: web.Request) -> web.Response:
        identity = self._target_identity(request)
        _query(request)
        localpart = self._identity_registry.localpart_of(identity.mxid)
        if not self._identity_registry.revoke_token(localpart):
            raise ApiError(404, "token_not_found", f"{identity.mxid} has no token")
        logger.info("token revoked for %s", identity.mxid)
        return web.Response(status=204)

    async def _get_identity(self, request: web.Request) -> web.Response:
        identity = self._target_identity(request)
        _query(request)
        return web.json_response(await self._profiles.describe(identity.mxid))

    async def _patch_identity(self, request: web.Request) -> web.Response:
        identity = self._target_identity(request)
        _query(request)
        await self._profiles.update(
            identity.mxid, await _json_body(request, media_types=(MERGE_PATCH, JSON)))
        return web.json_response(await self._profiles.describe(identity.mxid))

    async def _get_avatar(self, request: web.Request) -> web.Response:
        identity = self._target_identity(request)
        _query(request)
        data, mimetype = await self._profiles.avatar(identity.mxid)
        return web.Response(body=data, content_type=mimetype, headers={
            "Cache-Control": "private", "X-Content-Type-Options": "nosniff"})

    async def _put_avatar(self, request: web.Request) -> web.Response:
        identity = self._target_identity(request)
        _query(request)
        if not request.body_exists:
            raise ApiError(400, "body_required", "this request needs the image as its body")
        if request.content_type not in SUPPORTED_IMAGES:
            raise ApiError(415, "unsupported_media_type",
                           f"Content-Type must be one of {', '.join(SUPPORTED_IMAGES)}")
        avatar_url, replaced = await self._profiles.set_avatar(
            identity.mxid, await request.read(), request.content_type)
        if replaced:
            return web.json_response({"avatar_url": avatar_url})
        return web.json_response({"avatar_url": avatar_url}, status=201,
                                 headers={"Location": "avatar"})

    async def _delete_avatar(self, request: web.Request) -> web.Response:
        identity = self._target_identity(request)
        _query(request)
        await self._profiles.remove_avatar(identity.mxid)
        return web.Response(status=204)

    async def _get_webhook(self, request: web.Request) -> web.Response:
        identity = self._own_identity(request)
        _query(request)
        webhook = self._webhook_notifier.describe(identity.mxid)
        if webhook is None:
            raise ApiError(404, "webhook_not_found", f"{identity.mxid} has no webhook")
        return web.json_response(webhook)

    async def _put_webhook(self, request: web.Request) -> web.Response:
        identity = self._own_identity(request)
        _query(request)
        payload = await _json_body(request)
        secret, replaced = await self._webhook_notifier.register(identity.mxid, payload)
        body = {"url": payload["url"].strip(), "secret": secret}
        if replaced:
            return web.json_response(body)
        return web.json_response(body, status=201, headers={"Location": "webhook"})

    async def _delete_webhook(self, request: web.Request) -> web.Response:
        identity = self._own_identity(request)
        _query(request)
        if not self._webhook_notifier.unregister(identity.mxid):
            raise ApiError(404, "webhook_not_found", f"{identity.mxid} has no webhook")
        return web.Response(status=204)

    async def _list_channels(self, request: web.Request) -> web.Response:
        identity = self._caller(request)
        _query(request, "unread", "limit", "cursor")
        unread_only, limit = _boolean_query(request, "unread"), _limit(request)
        after = _cursor(request)
        if after is not None and not _is_recency_key(after):
            raise ApiError(400, "invalid_cursor", "cursor is not one this API issued")
        return _page_response(await self._channels.list(
            identity, unread_only=unread_only, limit=limit,
            after=None if after is None else tuple(after)))

    async def _create_channel(self, request: web.Request) -> web.Response:
        identity = self._caller(request)
        _query(request)
        created = await self._channels.create(identity, await _json_body(request))
        return web.json_response(
            created, status=201,
            headers={"Location": f"channels/{quote(created['room'], safe='')}"})

    async def _get_channel(self, request: web.Request) -> web.Response:
        identity = self._caller(request)
        _query(request)
        return web.json_response(await self._channels.channel(identity, _room(request)))

    async def _join_channel(self, request: web.Request) -> web.Response:
        identity = self._caller(request)
        _query(request)
        if request.body_exists:
            raise ApiError(400, "body_not_allowed", "this request takes no body")
        joined = await self._channels.join(identity, _room(request))
        return web.json_response(
            joined, status=201, headers={"Location": quote(joined["room"], safe="")})

    async def _leave_channel(self, request: web.Request) -> web.Response:
        identity = self._caller(request)
        _query(request)
        await self._channels.leave(identity, _room(request))
        return web.Response(status=204)

    async def _events(self, request: web.Request) -> web.Response:
        identity = self._caller(request)
        _query(request, "unread", "before", "after", "include", "limit")
        unread = _boolean_query(request, "unread")
        before, after = _event_query(request, "before"), _event_query(request, "after")
        if unread + (before is not None) + (after is not None) > 1:
            raise ApiError(400, "invalid_parameter",
                           "unread, before and after are mutually exclusive")
        include, limit = _include(request), _limit(request)
        return web.json_response(await self._channels.events(
            identity, _room(request), include=include, limit=limit,
            unread=unread, before=before, after=after))

    async def _receipt(self, request: web.Request) -> web.Response:
        identity = self._caller(request)
        _query(request)
        room = _room(request)
        await self._channels.receipt(identity, room, await _json_body(request))
        return web.Response(status=204)

    async def _send(self, request: web.Request) -> web.Response:
        identity = self._caller(request)
        _query(request)
        room = _room(request)
        sent = await self._sender.send(identity, room, await _json_body(request))
        return web.json_response(
            sent, status=201,
            headers={"Location": f"events/{quote(sent['event_id'], safe='')}"})

    async def _event(self, request: web.Request) -> web.Response:
        identity = self._caller(request)
        _query(request, "include")
        include = _include(request)
        return web.json_response(await self._channels.event(
            identity, _room(request), _event(request), include=include))

    async def _media(self, request: web.Request) -> web.Response:
        identity = self._caller(request)
        _query(request)
        media: MediaFile = await self._channels.media(identity, _room(request), _event(request))
        return web.Response(body=media.data, content_type=media.mimetype, headers={
            "Content-Disposition": _attachment(media.filename),
            "Cache-Control": "private",
            "X-Content-Type-Options": "nosniff",
        })

    async def _list_members(self, request: web.Request) -> web.Response:
        identity = self._caller(request)
        _query(request, "limit", "cursor")
        limit = _limit(request)
        after = _cursor(request)
        if after is not None and not isinstance(after, str):
            raise ApiError(400, "invalid_cursor", "cursor is not one this API issued")
        return _page_response(await self._channels.members(
            identity, _room(request), limit=limit, after=after))

    async def _invite_member(self, request: web.Request) -> web.Response:
        identity = self._caller(request)
        _query(request)
        room = _room(request)
        invited = await self._channels.invite(identity, room, await _json_body(request))
        return web.json_response(
            invited, status=201,
            headers={"Location": f"members/{quote(invited['user'], safe='')}"})

    async def _remove_member(self, request: web.Request) -> web.Response:
        identity = self._caller(request)
        _query(request)
        room = _room(request)
        await self._channels.remove(identity, room, request.match_info["user"])
        return web.Response(status=204)

    def _caller(self, request: web.Request) -> ResolvedIdentity:
        token = _bearer_token(request)
        identity = self._identity_registry.identity_for_token(token or "")
        if identity is None:
            raise ApiError(
                401, "unauthorized", "a valid identity token is required",
                headers={"WWW-Authenticate": _challenge(token)})
        self._request_limit.take(identity.mxid)
        return identity

    def _own_identity(self, request: web.Request) -> ResolvedIdentity:
        identity = self._caller(request)
        if request.match_info["id"] not in _names_of(identity.mxid):
            raise ApiError(403, "forbidden", "the token does not own this identity")
        return identity

    def _target_identity(self, request: web.Request) -> ResolvedIdentity:
        target = request.match_info["id"]
        if self._admin_authorized(request) and target != "me":
            identity = self._identity_registry.resolve(target)
            if identity is None:
                raise ApiError(404, "identity_not_found", f"no identity {target}")
            return identity
        return self._own_identity(request)

    def _admin_authorized(self, request: web.Request) -> bool:
        return bool(self._admin_token) and hmac.compare_digest(
            (_bearer_token(request) or "").encode(), self._admin_token.encode())


def _problem_response(body: dict, headers: dict) -> web.Response:
    return web.json_response(
        body, status=body["status"], headers=headers, content_type=PROBLEM_CONTENT_TYPE)


def _http_error_detail(request: web.Request, error: web.HTTPException) -> str:
    if error.status == 404:
        return f"no resource at {request.path}"
    if error.status == 405:
        return f"{request.method} is not allowed on {request.path}"
    if error.status == 413:
        return f"the body exceeds {MAX_REQUEST_BYTES} bytes"
    if error.text and not error.text.startswith(f"{error.status}:"):
        return error.text
    return error.reason


def _page_response(page: Page) -> web.Response:
    cursor = None if page.next_cursor is None else _encode_cursor(page.next_cursor)
    return web.json_response({**page.body, "next_cursor": cursor})


def _encode_cursor(value) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _cursor(request: web.Request):
    raw = request.query.get("cursor")
    if raw is None:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ApiError(400, "invalid_cursor", "cursor is not one this API issued")


def _query(request: web.Request, *allowed: str) -> None:
    unknown = sorted(set(request.query) - set(allowed))
    if unknown:
        accepted = ", ".join(allowed) or "none"
        raise ApiError(400, "unknown_parameter",
                       f"unknown query parameter {', '.join(unknown)} (accepted: {accepted})")


def _is_recency_key(value) -> bool:
    return (isinstance(value, list) and len(value) == 2 and isinstance(value[0], int)
            and not isinstance(value[0], bool) and isinstance(value[1], str))


def _event_query(request: web.Request, name: str) -> str | None:
    value = request.query.get(name)
    if value is not None and not value.startswith("$"):
        raise ApiError(400, "invalid_parameter", f"{name} must be an event id ($…)")
    return value


def _boolean_query(request: web.Request, name: str) -> bool:
    value = request.query.get(name, "false")
    if value not in ("true", "false"):
        raise ApiError(400, "invalid_parameter", f"{name} must be true or false")
    return value == "true"


def _limit(request: web.Request) -> int:
    raw = request.query.get("limit", str(DEFAULT_PAGE_SIZE))
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_PAGE_SIZE:
        raise ApiError(400, "invalid_parameter", f"limit must be an integer from 1 to {MAX_PAGE_SIZE}")
    return int(raw)


def _include(request: web.Request) -> frozenset:
    raw = request.query.get("include", "")
    included = frozenset(item for item in raw.split(",") if item)
    if not included <= INCLUDABLE:
        raise ApiError(400, "invalid_parameter",
                       f"include accepts {', '.join(sorted(INCLUDABLE))}, comma-separated")
    return included


def _room(request: web.Request) -> str:
    room = request.match_info["room"]
    if not room.startswith("!"):
        raise ApiError(
            400, "invalid_room",
            f"{room} is not a room id (!…); to write to a user, POST /channels with invite")
    return room


def _event(request: web.Request) -> str:
    event = request.match_info["event"]
    if not event.startswith("$"):
        raise ApiError(400, "invalid_event", f"{event} is not an event id ($…)")
    return event


def _names_of(mxid: str) -> tuple:
    return ("me", mxid, mxid[1:].split(":", 1)[0])


def _bearer_token(request: web.Request) -> str | None:
    scheme, _, credentials = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and credentials.strip():
        return credentials.strip()
    return None


def _challenge(token: str | None) -> str:
    return 'Bearer error="invalid_token"' if token else "Bearer"


def _attachment(filename: str) -> str:
    fallback = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


async def _json_body(request: web.Request, *, media_types: tuple = (JSON,)) -> dict:
    if not request.body_exists:
        raise ApiError(400, "body_required", "this request needs a JSON object body")
    if request.content_type not in media_types:
        headers = {"Accept-Patch": ", ".join(media_types)} if request.method == "PATCH" else {}
        raise ApiError(
            415, "unsupported_media_type",
            f"Content-Type must be {' or '.join(media_types)}", headers=headers)
    try:
        body = json.loads(await request.read())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(400, "bad_json", "the body is not valid JSON")
    if not isinstance(body, dict):
        raise ApiError(400, "bad_json", "the body must be a JSON object")
    return body
