import asyncio
import inspect
import logging
from typing import Awaitable, Callable

from ..ports.matrix_transport import MatrixTransport
from .event_reader import MESSAGE_EVENT_TYPES, TIMELINE_EVENT_TYPES, is_rendered_membership
from .identity_registry import IdentityRegistry


logger = logging.getLogger(__name__)

EventObserver = Callable[[dict, frozenset], Awaitable[None] | None]
HISTORY_PAGE_SIZE = 100


class IncomingEventRouter:
    def __init__(
        self, *, identity_registry: IdentityRegistry, transport: MatrixTransport,
        webhook_notifier=None,
    ) -> None:
        self._identity_registry = identity_registry
        self._transport = transport
        self._webhook_notifier = webhook_notifier
        self._managed_mxids_by_room: dict[str, set[str]] = {}
        self._observers: list[EventObserver] = []
        self._room_locks: dict[str, asyncio.Lock] = {}

    def add_observer(self, observer: EventObserver) -> None:
        self._observers.append(observer)

    def managed_in_room(self, room_id: str) -> frozenset:
        return frozenset(self._managed_mxids_by_room.get(room_id, ()))

    async def bootstrap_membership(self) -> None:
        self._managed_mxids_by_room.clear()
        for mxid in self._identity_registry.managed_mxids():
            for room_id in await self._transport.list_joined_rooms(identity=mxid):
                self._managed_mxids_by_room.setdefault(room_id, set()).add(mxid)
        logger.info("membership bootstrap: %d rooms", len(self._managed_mxids_by_room))

    async def accept_pending_invitations(self) -> None:
        for mxid in self._identity_registry.managed_mxids():
            for room_id in await self._transport.list_invited_rooms(identity=mxid):
                logger.info("%s accepts the invitation to %s it missed", mxid, room_id)
                async with self._in_order(room_id):
                    await self._accept_invitation(room_id, mxid)

    async def route(self, event: dict) -> None:
        room_id = event.get("room_id")
        if not room_id:
            return
        async with self._in_order(room_id):
            await self._route_in_order(room_id, event)

    def _in_order(self, room_id: str) -> asyncio.Lock:
        return self._room_locks.setdefault(room_id, asyncio.Lock())

    async def _route_in_order(self, room_id: str, event: dict) -> None:
        self._touch_managed(room_id, event.get("origin_server_ts"))
        event_type = event.get("type")
        if event_type == "m.room.member":
            await self._route_membership_change(room_id, event)
        elif event_type in MESSAGE_EVENT_TYPES:
            self._note_for_managed(room_id, event)
        await self._notify_observers(room_id, event)

    def discard_membership(self, room_id: str, mxid: str) -> None:
        self._managed_mxids_by_room.get(room_id, set()).discard(mxid)

    async def _notify_observers(self, room_id: str, event: dict) -> None:
        managed = self.managed_in_room(room_id)
        for observer in self._observers:
            try:
                outcome = observer(event, managed)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                logger.exception("event observer failed")

    def _touch_managed(self, room_id: str, ts) -> None:
        if ts is None:
            return
        for mxid in self.managed_in_room(room_id):
            mailbox = self._identity_registry.mailbox_for(mxid)
            if mailbox is not None:
                mailbox.touch(room_id, ts)

    def _note_for_managed(self, room_id: str, event: dict) -> None:
        for mxid in self.managed_in_room(room_id):
            if mxid != event.get("sender"):
                self._note_for(mxid, room_id, event)

    def _note_for(self, mxid: str, room_id: str, event: dict) -> None:
        mailbox = self._identity_registry.mailbox_for(mxid)
        if mailbox is None:
            return
        mailbox.note_unread(room_id, event)
        if self._webhook_notifier is not None:
            self._webhook_notifier.notify(mxid, room_id, event.get("sender") or "")

    async def _route_membership_change(self, room_id: str, event: dict) -> None:
        subject_mxid = event.get("state_key") or ""
        membership = (event.get("content") or {}).get("membership")
        if not self._identity_registry.is_managed(subject_mxid):
            if is_rendered_membership(membership):
                self._note_for_managed(room_id, event)
            return
        present_mxids = self._managed_mxids_by_room.setdefault(room_id, set())
        if membership == "join":
            present_mxids.add(subject_mxid)
        elif membership in ("leave", "ban"):
            present_mxids.discard(subject_mxid)
        elif membership == "invite":
            await self._accept_invitation(room_id, subject_mxid)

    async def _accept_invitation(self, room_id: str, mxid: str) -> None:
        if mxid in self.managed_in_room(room_id):
            return
        if await self._transport.join_room(room_id=room_id, as_identity=mxid) != 200:
            return
        self._managed_mxids_by_room.setdefault(room_id, set()).add(mxid)
        for event in await self._unread_since_invitation(room_id, mxid):
            self._note_for(mxid, room_id, event)

    async def _unread_since_invitation(self, room_id: str, mxid: str) -> list[dict]:
        newest_first, token = [], None
        while True:
            chunk = await self._transport.fetch_timeline(
                room_id=room_id, as_identity=mxid, backwards=True, from_token=token,
                limit=HISTORY_PAGE_SIZE, event_types=TIMELINE_EVENT_TYPES)
            for event in chunk.events:
                if event.get("type") == "m.room.member" and event.get("state_key") == mxid:
                    membership = (event.get("content") or {}).get("membership")
                    if membership == "join":
                        continue
                    if membership == "invite":
                        newest_first.append(event)
                    return newest_first[::-1]
                if self._is_unread_for(mxid, event):
                    newest_first.append(event)
            if chunk.next_token is None:
                return newest_first[::-1]
            token = chunk.next_token

    def _is_unread_for(self, mxid: str, event: dict) -> bool:
        if event.get("sender") == mxid:
            return False
        if event.get("type") == "m.room.member":
            return not self._identity_registry.is_managed(event.get("state_key") or "") \
                and is_rendered_membership((event.get("content") or {}).get("membership"))
        return event.get("type") in MESSAGE_EVENT_TYPES
