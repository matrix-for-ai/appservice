import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Protocol

from aiohttp import web


IncomingEventHandler = Callable[[dict], Awaitable[None]]


@dataclass(frozen=True)
class ApplicationServiceUserRegistration:
    outcome: Literal["created", "already_in_use"]


class HomeserverError(Exception):
    def __init__(self, status: int | None, body: Any = None) -> None:
        super().__init__(
            "the homeserver did not answer" if status is None else f"the homeserver answered {status}")
        self.status = status
        self.body = body


_SERVER_NAME = re.compile(
    r"(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*|\[[0-9A-Fa-f:.]{2,45}\])(?::[0-9]{1,5})?")
_MEDIA_ID = re.compile(r"[A-Za-z0-9_-]+")
_MAXIMUM_SERVER_NAME_LENGTH = 255


@dataclass(frozen=True)
class MediaUri:
    server_name: str
    media_id: str

    @classmethod
    def parse(cls, text) -> "MediaUri | None":
        if not isinstance(text, str) or not text.startswith("mxc://"):
            return None
        server_name, separator, media_id = text[len("mxc://"):].partition("/")
        if not separator or len(server_name) > _MAXIMUM_SERVER_NAME_LENGTH \
                or _SERVER_NAME.fullmatch(server_name) is None \
                or _MEDIA_ID.fullmatch(media_id) is None:
            return None
        return cls(server_name, media_id)

    def __str__(self) -> str:
        return f"mxc://{self.server_name}/{self.media_id}"


class MediaTooLarge(Exception):
    pass


@dataclass(frozen=True)
class PushCheck:
    supported: bool
    reachable: bool
    status: int
    errcode: str | None = None


@dataclass(frozen=True)
class EventContext:
    token_before: str
    token_after: str


@dataclass(frozen=True)
class TimelineChunk:
    events: list[dict]
    next_token: str | None


class MatrixTransport(Protocol):
    def set_incoming_event_handler(self, handler: IncomingEventHandler) -> None: ...

    def register_push_routes(self, application: web.Application) -> None: ...

    async def ping_from_homeserver(self, *, appservice_id: str) -> PushCheck: ...

    async def send_event(
        self, *, room_id: str, event_type: str, content: dict, as_identity: str
    ) -> str: ...

    async def create_room(
        self, *, as_identity: str, name: str | None, topic: str | None,
        invitees: list[str], is_direct: bool, public: bool,
    ) -> str: ...

    async def join_room(
        self, *, room_id: str, as_identity: str | None = None
    ) -> int: ...

    async def leave_and_forget_room(
        self, *, as_identity: str, room_id: str
    ) -> int: ...

    async def set_display_name(
        self, *, identity: str, display_name: str | None
    ) -> None: ...

    async def set_avatar_url(
        self, *, identity: str, avatar_url: str | None
    ) -> None: ...

    async def fetch_profile(self, *, identity: str) -> dict: ...

    async def register_application_service_user(
        self, *, localpart: str
    ) -> ApplicationServiceUserRegistration: ...

    async def fetch_event(
        self, *, room_id: str, event_id: str, as_identity: str | None = None
    ) -> dict | None: ...

    async def fetch_event_relations(
        self, *, room_id: str, event_id: str, as_identity: str | None = None,
        rel_type: str = "m.reference",
    ) -> list[dict]: ...

    async def fetch_event_context(
        self, *, room_id: str, event_id: str, as_identity: str
    ) -> EventContext | None: ...

    async def fetch_timeline(
        self, *, room_id: str, as_identity: str, backwards: bool, from_token: str | None,
        limit: int, event_types: tuple[str, ...],
    ) -> TimelineChunk: ...

    async def download_media(
        self, *, media_uri: MediaUri, as_identity: str, max_bytes: int | None
    ) -> bytes | None: ...

    async def upload_media(
        self, *, data: bytes, mime_type: str, filename: str
    ) -> str: ...

    async def mark_room_read(
        self, *, as_identity: str, room_id: str, event_id: str
    ) -> None: ...

    async def list_joined_rooms(self, *, identity: str) -> list[str]: ...

    async def list_invited_rooms(self, *, identity: str) -> list[str]: ...

    async def is_room_encrypted(self, *, room_id: str, as_identity: str) -> bool: ...

    async def membership_of(
        self, *, room_id: str, user_id: str, as_identity: str
    ) -> str | None: ...

    async def user_exists(self, *, user_id: str, as_identity: str) -> bool: ...

    async def invite_user(
        self, *, room_id: str, user_id: str, as_identity: str
    ) -> int: ...

    async def kick_user(
        self, *, room_id: str, user_id: str, as_identity: str, reason: str | None = None
    ) -> int: ...

    async def list_members(
        self, *, room_id: str, as_identity: str
    ) -> dict: ...
