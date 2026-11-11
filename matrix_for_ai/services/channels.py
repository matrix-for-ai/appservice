import asyncio
import logging
import re
from dataclasses import dataclass

from ..api_errors import ApiError
from ..payload_fields import optional_flag, optional_string, reject_unknown_fields, required_string
from ..ports.matrix_transport import HomeserverError, MatrixTransport, MediaTooLarge
from .channel_membership import PRESENT, ChannelMembership
from .event_reader import (
    SKIP_EVENT, TIMELINE_EVENT_TYPES, EventReader, KeysReceivedOnce, MediaFile)
from .identity_registry import ResolvedIdentity
from .incoming_event_router import IncomingEventRouter
from .identity_store import ChannelState


logger = logging.getLogger(__name__)

USER_ID_PATTERN = re.compile(r"@[^:\s]+:\S+")
CHANNEL_FIELDS = ("name", "topic", "invite", "direct", "public", "encrypted")


@dataclass(frozen=True)
class Page:
    body: dict
    next_cursor: object = None


class ChannelService:
    def __init__(
        self, *, transport: MatrixTransport, membership: ChannelMembership,
        event_reader: EventReader, event_router: IncomingEventRouter, encryption_for,
        e2ee_enabled: bool,
    ) -> None:
        self._transport = transport
        self._membership = membership
        self._event_reader = event_reader
        self._event_router = event_router
        self._encryption_for = encryption_for
        self._e2ee_enabled = e2ee_enabled
        self._encrypted_rooms: set[str] = set()

    async def list(self, identity: ResolvedIdentity, *, unread_only: bool, limit: int,
             after: tuple | None) -> Page:
        channels = sorted(
            (_channel_view(state.room, state) for state in identity.mailbox.channels()
             if not unread_only or state.unread),
            key=recency_key)
        if after is not None:
            channels = [channel for channel in channels if recency_key(channel) > after]
        page = channels[:limit]
        encrypted = await asyncio.gather(
            *(self._is_encrypted(identity, channel["room"]) for channel in page))
        for channel, is_encrypted in zip(page, encrypted):
            channel["encrypted"] = is_encrypted
        return Page({"channels": page},
                    list(recency_key(page[-1])) if len(channels) > limit else None)

    async def channel(self, identity: ResolvedIdentity, room_id: str) -> dict:
        await self._membership.require(identity, room_id)
        return {**_channel_view(room_id, identity.mailbox.channel(room_id)),
                "encrypted": await self._is_encrypted(identity, room_id)}

    async def _is_encrypted(self, identity: ResolvedIdentity, room_id: str) -> bool:
        if room_id not in self._encrypted_rooms and await self._transport.is_room_encrypted(
                room_id=room_id, as_identity=identity.mxid):
            self._encrypted_rooms.add(room_id)
        return room_id in self._encrypted_rooms

    async def events(
        self, identity: ResolvedIdentity, room_id: str, *, include: frozenset, limit: int,
        unread: bool, before: str | None, after: str | None,
    ) -> dict:
        await self._membership.require(identity, room_id)
        engine = await self._reading_engine(identity.mxid)
        if unread:
            records, has_more = await self._unread(identity, room_id, limit, engine)
        else:
            records, has_more = await self._timeline(
                identity, room_id, limit, engine, before=before, after=after)
        if include:
            for record in records:
                if record["type"] != "undecryptable":
                    await self._event_reader.enrich(record, identity.mxid, engine, include)
        return {"events": records, "has_more": has_more}

    async def _unread(self, identity, room_id, limit, engine) -> tuple[list, bool]:
        pending = identity.mailbox.unread_event_ids(room_id)
        already_undecryptable = identity.mailbox.undecryptable_event_ids(room_id)
        records, undecryptable, not_messages, gone = [], [], [], []
        while pending and len(records) < limit:
            event_id = pending.pop(0)
            record = await self._event_reader.build_record(
                room_id, event_id, identity.mxid, engine)
            if record is None:
                gone.append(event_id)
            elif record is SKIP_EVENT:
                not_messages.append(event_id)
            else:
                if record["type"] == "undecryptable":
                    if event_id in already_undecryptable:
                        continue
                    undecryptable.append(event_id)
                records.append(record)
        if gone:
            logger.info("%s: %d unread events in %s no longer exist on the homeserver; forgotten",
                        identity.mxid, len(gone), room_id)
        identity.mailbox.forget(room_id, not_messages + gone)
        if undecryptable:
            identity.mailbox.mark_undecryptable(room_id, undecryptable)
        return records, bool(pending)

    async def _timeline(self, identity, room_id, limit, engine, *, before, after):
        token = None
        anchor = before or after
        if anchor is not None:
            context = await self._transport.fetch_event_context(
                room_id=room_id, event_id=anchor, as_identity=identity.mxid)
            if context is None:
                raise ApiError(404, "event_not_found", f"{anchor} is not an event of {room_id}")
            token = context.token_after if after else context.token_before
        backwards = after is None
        records, has_more = [], True
        while has_more and len(records) < limit:
            chunk = await self._transport.fetch_timeline(
                room_id=room_id, as_identity=identity.mxid, backwards=backwards,
                from_token=token, limit=limit - len(records), event_types=TIMELINE_EVENT_TYPES)
            for raw in chunk.events:
                record = await self._event_reader.record_of(raw, room_id, identity.mxid, engine)
                if record is not None and record is not SKIP_EVENT:
                    records.append(record)
            token, has_more = chunk.next_token, chunk.next_token is not None
        return (records[::-1] if backwards else records), has_more

    async def receipt(self, identity: ResolvedIdentity, room_id: str, payload: dict) -> None:
        reject_unknown_fields(payload, ("event_id",))
        event_id = required_string(payload, "event_id")
        if not event_id.startswith("$"):
            raise ApiError(422, "invalid_event_id", "event_id must be an event id ($…)")
        await self._membership.require(identity, room_id)
        if event_id in identity.mailbox.unread_event_ids(room_id):
            await self._transport.mark_room_read(
                as_identity=identity.mxid, room_id=room_id, event_id=event_id)
            identity.mailbox.ack_through(room_id, event_id)
            return
        if not await self._transport.fetch_event(
                room_id=room_id, event_id=event_id, as_identity=identity.mxid):
            raise ApiError(404, "event_not_found", f"{event_id} is not an event of {room_id}")

    async def event(self, identity: ResolvedIdentity, room_id: str, event_id: str, *,
                    include: frozenset) -> dict:
        await self._membership.require(identity, room_id)
        engine = await self._reading_engine(identity.mxid)
        record = await self._event_reader.build_record(room_id, event_id, identity.mxid, engine)
        if record is None or record is SKIP_EVENT:
            raise ApiError(404, "event_not_found", f"{event_id} cannot be resolved in {room_id}")
        if include and record["type"] != "undecryptable":
            await self._event_reader.enrich(record, identity.mxid, engine, include)
        return record

    async def media(self, identity: ResolvedIdentity, room_id: str, event_id: str) -> MediaFile:
        await self._membership.require(identity, room_id)
        engine = await self._reading_engine(identity.mxid)
        try:
            media = await self._event_reader.media_bytes(identity.mxid, room_id, event_id, engine)
        except MediaTooLarge:
            raise ApiError(502, "media_too_large",
                           f"the attachment of {event_id} is over this server's limit")
        if media is None:
            raise ApiError(404, "media_not_found", f"{event_id} has no attachment available")
        return media

    async def create(self, identity: ResolvedIdentity, payload: dict) -> dict:
        reject_unknown_fields(payload, CHANNEL_FIELDS)
        name = optional_string(payload, "name")
        topic = optional_string(payload, "topic")
        invitees = payload.get("invite") or []
        if not isinstance(invitees, list) or not all(is_user_id(i) for i in invitees):
            raise ApiError(422, "invalid_invite", "invite must be a list of user ids (@…:…)")
        is_direct = optional_flag(payload, "direct", default=len(invitees) == 1)
        public = optional_flag(payload, "public", default=False)
        encrypted = optional_flag(payload, "encrypted", default=self._e2ee_enabled)
        if public and is_direct:
            raise ApiError(422, "invalid_public", "a direct channel cannot be public")
        if not encrypted and not is_direct and not name:
            raise ApiError(422, "name_required", "an unencrypted group channel needs a name")
        if encrypted:
            room_id = await self._create_encrypted(
                identity, invitees, name, topic, is_direct, public)
        elif is_direct and not self._e2ee_enabled:
            raise ApiError(
                501, "no_e2ee_for_dm",
                "direct channels need end-to-end encryption, which this installation lacks: "
                "clients ignore an appservice's direct invite otherwise; create a group channel")
        else:
            room_id = await self._transport.create_room(
                as_identity=identity.mxid, name=name, topic=topic,
                invitees=invitees, is_direct=is_direct, public=public)
        return {"room": room_id, "name": name, "topic": topic, "direct": is_direct,
                "public": public, "encrypted": encrypted, "invited": invitees}

    async def join(self, identity: ResolvedIdentity, room_id: str) -> dict:
        membership = await self._transport.membership_of(
            room_id=room_id, user_id=identity.mxid, as_identity=identity.mxid)
        if membership == "join":
            raise ApiError(409, "already_member", f"{identity.mxid} is already in {room_id}")
        if membership == "ban":
            raise ApiError(409, "banned", f"{identity.mxid} is banned from {room_id}")
        status = await self._transport.join_room(room_id=room_id, as_identity=identity.mxid)
        if status == 404:
            raise ApiError(404, "channel_not_found", f"the homeserver does not know {room_id}")
        if status == 403:
            raise ApiError(403, "forbidden", f"{room_id} is not public and you are not invited")
        _require_success(status, "join")
        return {**_channel_view(room_id, identity.mailbox.channel(room_id)),
                "encrypted": await self._is_encrypted(identity, room_id)}

    async def leave(self, identity: ResolvedIdentity, room_id: str) -> None:
        membership = await self._transport.membership_of(
            room_id=room_id, user_id=identity.mxid, as_identity=identity.mxid)
        if membership not in PRESENT and identity.mailbox.channel(room_id) is None:
            raise ApiError(404, "not_member", f"{identity.mxid} is not in {room_id}")
        if membership in PRESENT:
            _require_success(await self._transport.leave_and_forget_room(
                as_identity=identity.mxid, room_id=room_id), "leave")
        self._event_router.discard_membership(room_id, identity.mxid)
        identity.mailbox.remove_channel(room_id)

    async def members(self, identity: ResolvedIdentity, room_id: str, *, limit: int,
                      after: str | None) -> Page:
        await self._membership.require(identity, room_id)
        members = await self._transport.list_members(room_id=room_id, as_identity=identity.mxid)
        ordered = sorted(user for user in members if after is None or user > after)
        page = ordered[:limit]
        return Page({"members": [
            {"user": user, "display_name": (members[user] or {}).get("display_name"),
             "avatar_url": (members[user] or {}).get("avatar_url")}
            for user in page]}, page[-1] if len(ordered) > limit else None)

    async def invite(self, identity: ResolvedIdentity, room_id: str, payload: dict) -> dict:
        reject_unknown_fields(payload, ("user",))
        user = required_string(payload, "user")
        if not is_user_id(user):
            raise ApiError(422, "invalid_user", "user must be a user id (@…:…)")
        await self._membership.require(identity, room_id)
        membership = await self._transport.membership_of(
            room_id=room_id, user_id=user, as_identity=identity.mxid)
        if membership == "join":
            raise ApiError(409, "already_member", f"{user} is already in {room_id}")
        if membership == "invite":
            raise ApiError(409, "already_invited", f"{user} is already invited to {room_id}")
        if membership == "ban":
            raise ApiError(409, "banned", f"{user} is banned from {room_id}")
        if not await self._transport.user_exists(user_id=user, as_identity=identity.mxid):
            raise ApiError(404, "user_not_found", f"the homeserver does not know {user}")
        status = await self._transport.invite_user(
            room_id=room_id, user_id=user, as_identity=identity.mxid)
        if status == 404:
            raise ApiError(404, "user_not_found", f"the homeserver does not know {user}")
        _require_success(status, "invite")
        return {"user": user, "membership": "invite"}

    async def remove(self, identity: ResolvedIdentity, room_id: str, user: str) -> None:
        await self._membership.require(identity, room_id)
        membership = await self._transport.membership_of(
            room_id=room_id, user_id=user, as_identity=identity.mxid) if is_user_id(user) else None
        if membership not in PRESENT:
            raise ApiError(404, "user_not_member", f"{user} is neither in nor invited to {room_id}")
        status = await self._transport.kick_user(
            room_id=room_id, user_id=user, as_identity=identity.mxid)
        _require_success(status, "remove")

    async def _create_encrypted(self, identity, invitees, name, topic, is_direct, public):
        engine = await self._engine_if_enabled(identity.mxid)
        if engine is None:
            raise ApiError(
                501, "e2ee_unavailable", "end-to-end encryption is unavailable for this identity")
        room_id = await engine.create_encrypted_room(
            invitees=invitees, name=name, topic=topic, is_direct=is_direct, public=public)
        if not room_id:
            raise ApiError(502, "homeserver_error", "the homeserver did not create the channel")
        return room_id

    async def _engine_if_enabled(self, mxid: str):
        return await self._encryption_for(mxid) if self._e2ee_enabled else None

    async def _reading_engine(self, mxid: str):
        engine = await self._engine_if_enabled(mxid)
        return None if engine is None else KeysReceivedOnce(engine)


def _channel_view(room_id: str, state: ChannelState | None) -> dict:
    newest = None if state is None else state.last_unread
    return {
        "room": room_id,
        "unread": 0 if state is None else state.unread,
        "last_unread": None if newest is None else {
            "event_id": newest.event_id, "sender": newest.sender, "ts": newest.ts},
        "last_ts": None if state is None else state.last_ts,
    }


def recency_key(channel: dict) -> tuple:
    return -(channel["last_ts"] or 0), channel["room"]


def _require_success(status: int, action: str) -> None:
    if status == 200:
        return
    if status == 403:
        raise ApiError(403, "forbidden", f"the homeserver does not allow this {action}")
    raise HomeserverError(status)


def is_user_id(value) -> bool:
    return isinstance(value, str) and USER_ID_PATTERN.fullmatch(value) is not None
