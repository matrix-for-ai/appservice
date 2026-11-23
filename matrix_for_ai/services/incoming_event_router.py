import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable

from ..ports.matrix_transport import HomeserverError, MatrixTransport
from .event_reader import MESSAGE_EVENT_TYPES, TIMELINE_EVENT_TYPES, is_rendered_membership
from .federation_policy import FederationPolicy
from .identity_registry import IdentityRegistry


logger = logging.getLogger(__name__)

EventObserver = Callable[[dict, frozenset], Awaitable[None] | None]
MembershipAction = Callable[[str, str], Awaitable[bool]]
HISTORY_PAGE_SIZE = 100
MEMBERSHIP_RETRY_DELAYS_SECONDS = (1, 2, 4, 8, 16, 32, 64)


class IncomingEventRouter:
    def __init__(
        self, *, identity_registry: IdentityRegistry, transport: MatrixTransport,
        federation_policy: FederationPolicy, webhook_notifier=None,
    ) -> None:
        self._identity_registry = identity_registry
        self._federation_policy = federation_policy
        self._transport = transport
        self._webhook_notifier = webhook_notifier
        self._managed_mxids_by_room: dict[str, set[str]] = {}
        self._observers: list[EventObserver] = []
        self._room_locks: dict[str, asyncio.Lock] = {}
        self._membership_tasks: dict[tuple[str, str], asyncio.Task] = {}

    def add_observer(self, observer: EventObserver) -> None:
        self._observers.append(observer)

    def managed_in_room(self, room_id: str) -> frozenset:
        return frozenset(self._managed_mxids_by_room.get(room_id, ()))

    async def bootstrap_membership(self) -> None:
        self._managed_mxids_by_room.clear()
        for mxid in self._identity_registry.managed_mxids():
            for room_id in await self._transport.list_joined_rooms(identity=mxid):
                if not await self._left_for_denied_members(room_id, mxid):
                    self._managed_mxids_by_room.setdefault(room_id, set()).add(mxid)
        logger.info("membership bootstrap: %d rooms", len(self._managed_mxids_by_room))

    async def accept_pending_invitations(self) -> None:
        for mxid in self._identity_registry.managed_mxids():
            for room_id, inviter in (await self._transport.list_invited_rooms(identity=mxid)).items():
                logger.info("%s answers the invitation to %s it missed", mxid, room_id)
                async with self._in_order(room_id):
                    await self._answer_invitation(room_id, mxid, inviter)

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
            sender = event.get("sender") or ""
            if mxid != sender and not self._federation_policy.ignores(mxid, sender):
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
            if membership in ("invite", "join"):
                for mxid in self.managed_in_room(room_id):
                    if self._federation_policy.leaves_for(mxid, subject_mxid):
                        self._in_background(room_id, mxid, self._leave_for_denied_members)
            return
        present_mxids = self._managed_mxids_by_room.setdefault(room_id, set())
        if membership == "join":
            present_mxids.add(subject_mxid)
        elif membership in ("leave", "ban"):
            present_mxids.discard(subject_mxid)
        elif membership == "invite":
            inviter = event.get("sender")
            self._in_background(
                room_id, subject_mxid,
                lambda room, mxid: self._answer_invitation(room, mxid, inviter))

    def _in_background(self, room_id: str, mxid: str, action: MembershipAction) -> None:
        key = (room_id, mxid)
        if key not in self._membership_tasks:
            task = asyncio.create_task(self._with_retries(room_id, mxid, action))
            self._membership_tasks[key] = task
            task.add_done_callback(lambda _task: self._membership_tasks.pop(key, None))

    async def _with_retries(self, room_id: str, mxid: str, action: MembershipAction) -> None:
        for delay_seconds in (0, *MEMBERSHIP_RETRY_DELAYS_SECONDS):
            await asyncio.sleep(delay_seconds)
            try:
                async with self._in_order(room_id):
                    if await action(room_id, mxid):
                        return
            except HomeserverError as error:
                logger.warning("%s could not change its membership of %s yet: %s",
                               mxid, room_id, error)
            except Exception:
                logger.exception("%s failed to change its membership of %s", mxid, room_id)
                return
        logger.warning("%s gave up changing its membership of %s; it retries at the next catch-up",
                       mxid, room_id)

    async def wait_for_memberships(self) -> None:
        await asyncio.gather(*self._membership_tasks.values())

    async def close(self) -> None:
        tasks = list(self._membership_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _answer_invitation(self, room_id: str, mxid: str, inviter: str | None) -> bool:
        if inviter is None or self._federation_policy.allows_user(mxid, inviter):
            return await self._accept_invitation(room_id, mxid)
        logger.info("%s declines the invitation of %s to %s: that server is not allowed",
                    mxid, inviter, room_id)
        return await self._transport.leave_and_forget_room(as_identity=mxid, room_id=room_id) == 200

    async def _accept_invitation(self, room_id: str, mxid: str) -> bool:
        if mxid in self.managed_in_room(room_id):
            return True
        if await self._transport.join_room(room_id=room_id, as_identity=mxid) != 200:
            return False
        if await self._left_for_denied_members(room_id, mxid):
            return True
        self._managed_mxids_by_room.setdefault(room_id, set()).add(mxid)
        mailbox = self._identity_registry.mailbox_for(mxid)
        if mailbox is not None:
            mailbox.add_channel(room_id)
        for event in await self._unread_since_invitation(room_id, mxid):
            self._note_for(mxid, room_id, event)
        return True

    async def _left_for_denied_members(self, room_id: str, mxid: str) -> bool:
        policy = self._federation_policy
        if policy.rule_for(mxid).denied_members != "leave" or policy.unrestricted(mxid):
            return False
        members = await self._transport.list_members(room_id=room_id, as_identity=mxid)
        if not policy.denied_among(mxid, members):
            return False
        return await self._leave_for_denied_members(room_id, mxid)

    async def _leave_for_denied_members(self, room_id: str, mxid: str) -> bool:
        logger.info("%s leaves %s: a member's server is not allowed", mxid, room_id)
        if await self._transport.leave_and_forget_room(as_identity=mxid, room_id=room_id) != 200:
            return False
        self.discard_membership(room_id, mxid)
        mailbox = self._identity_registry.mailbox_for(mxid)
        if mailbox is not None:
            mailbox.remove_channel(room_id)
        return True

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
        if self._federation_policy.ignores(mxid, event.get("sender") or ""):
            return False
        if event.get("type") == "m.room.member":
            return not self._identity_registry.is_managed(event.get("state_key") or "") \
                and is_rendered_membership((event.get("content") or {}).get("membership"))
        return event.get("type") in MESSAGE_EVENT_TYPES
