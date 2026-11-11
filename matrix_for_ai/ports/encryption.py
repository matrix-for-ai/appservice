import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class EncryptedFile:
    url: str
    key: str
    sha256: str
    iv: str


OlmEventHandler = Callable[[str, str, dict], None]


class EncryptionEngine(Protocol):
    mxid: str
    device_id: str | None

    async def receive_keys(self) -> None: ...

    async def decrypt_event(self, raw_event: dict, room_id: str) -> dict | None: ...

    def decrypt_attachment(
        self, ciphertext: bytes, encrypted_file: EncryptedFile
    ) -> bytes | None: ...

    async def upload_encrypted(
        self, *, data: bytes, mime_type: str, filename: str
    ) -> dict | None: ...

    async def send_event(
        self, *, room_id: str, event_type: str, content: dict
    ) -> str | None: ...

    async def create_encrypted_room(
        self, *, invitees: list[str], name: str | None, topic: str | None, is_direct: bool,
        public: bool,
    ) -> str | None: ...

    async def sync_once(self, timeout: int = 8000) -> None: ...

    async def sync_loop(self, stop_event: asyncio.Event, timeout: int = 5000) -> None: ...

    async def put_state(
        self, room_id: str, event_type: str, content: dict, state_key: str = ""
    ) -> Any: ...

    def install_olm_event_capture(self, event_type: str, handler: OlmEventHandler) -> None: ...

    async def send_olm_to_device(
        self, user_id: str, device_id: str, event_type: str, content: dict
    ) -> Any: ...


class EncryptionRegistry(Protocol):
    def get(self, mxid: str) -> EncryptionEngine | None: ...

    async def ensure(self, localpart: str) -> EncryptionEngine | None: ...

    async def close_all(self) -> None: ...
