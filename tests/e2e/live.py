import asyncio
import base64
import io
import os
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import quote

import aiohttp
import pytest
import pytest_asyncio
from nio import (AsyncClient, AsyncClientConfig, EncryptionError, LoginResponse, MegolmEvent,
                 RoomCreateResponse, RoomEncryptedMedia, RoomMessage, RoomPreset,
                 RoomSendResponse, UnknownEvent, UploadResponse)
from nio.crypto import decrypt_attachment

BASE_URL = os.environ.get("MATRIX_FOR_AI_E2E_URL", "").rstrip("/")
HOMESERVER_URL = os.environ.get("MATRIX_FOR_AI_E2E_HOMESERVER", "").rstrip("/")
HUMAN_USER = os.environ.get("MATRIX_FOR_AI_E2E_HUMAN", "")
HUMAN_PASSWORD = os.environ.get("MATRIX_FOR_AI_E2E_HUMAN_PASSWORD", "")
TIMEOUT_SECONDS = float(os.environ.get("MATRIX_FOR_AI_E2E_TIMEOUT", "30"))
READY_TIMEOUT_SECONDS = float(os.environ.get("MATRIX_FOR_AI_E2E_READY_TIMEOUT", "120"))
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
ENCRYPTION_STATE = {"type": "m.room.encryption", "state_key": "",
                    "content": {"algorithm": "m.megolm.v1.aes-sha2"}}


class Identity:
    def __init__(self, session: aiohttp.ClientSession, mxid: str, token: str) -> None:
        self.session = session
        self.mxid = mxid
        self.headers = {"Authorization": f"Bearer {token}"}

    async def call(self, method: str, path: str, expect: int = 200, **kwargs):
        async with self.session.request(
                method, BASE_URL + path, headers=self.headers, **kwargs) as response:
            body = await response.read()
            assert response.status == expect, f"{method} {path} -> {response.status}: {body[:300]!r}"
            if response.content_type == "application/json":
                return await response.json()
            return body

    async def wait_for_events(self, room: str, count: int = 1) -> list:
        received = []
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            channel = f"/channels/{quote(room, safe='')}"
            read = await self.call("GET", f"{channel}/events?unread=true&include=media,poll")
            if read["events"]:
                await self.call("PUT", f"{channel}/receipt", expect=204,
                                json={"event_id": read["events"][-1]["event_id"]})
            received += [m for m in read["events"] if m["type"] != "membership"]
            if len(received) >= count:
                return received
            await asyncio.sleep(1)
        raise AssertionError(f"{self.mxid} got {received} in {room}, expected {count}")

    async def wait_until_joined(self, room: str) -> None:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            channels = await self.call("GET", "/channels")
            if room in [channel["room"] for channel in channels["channels"]]:
                return
            await asyncio.sleep(1)
        raise AssertionError(f"{self.mxid} never joined {room}")

    async def wait_until_member(self, room: str, other: str) -> None:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            members = await self.call("GET", f"/channels/{quote(room, safe='')}/members")
            if other in [member["user"] for member in members["members"]]:
                return
            await asyncio.sleep(1)
        raise AssertionError(f"{other} never joined {room}")

    async def send(self, room: str, payload: dict) -> dict:
        return await self.call(
            "POST", f"/channels/{quote(room, safe='')}/events", expect=201, json=payload)


class Human:
    def __init__(self, client: AsyncClient, session: aiohttp.ClientSession) -> None:
        self.client = client
        self.session = session
        self.mxid = client.user_id

    async def sync(self, **options):
        response = await self.client.sync(**options)
        await self.exchange_keys()
        return response

    async def exchange_keys(self) -> None:
        await self.client.send_to_device_messages()
        if self.client.should_upload_keys:
            await self.client.keys_upload()
        if self.client.should_query_keys:
            await self.client.keys_query()
        if self.client.should_claim_keys:
            await self.client.keys_claim(self.client.get_users_for_key_claiming())

    async def create_direct_room(self, invite: str, *, encrypted: bool) -> str:
        response = await self.client.room_create(
            is_direct=True, invite=[invite], preset=RoomPreset.trusted_private_chat,
            initial_state=[ENCRYPTION_STATE] if encrypted else [])
        assert isinstance(response, RoomCreateResponse), response
        return response.room_id

    async def wait_until_member(self, room: str, other: str) -> None:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            await self.sync(timeout=1000)
            joined = self.client.rooms.get(room)
            if joined and other in joined.users and other not in joined.invited_users:
                return
        raise AssertionError(f"{other} never joined {room} for {self.mxid}")

    async def send(self, room: str, content: dict, event_type: str = "m.room.message") -> str:
        await self.exchange_keys()
        response = await self.client.room_send(
            room, event_type, content, ignore_unverified_devices=True)
        assert isinstance(response, RoomSendResponse), response
        return response.event_id

    async def send_image(self, room: str, data: bytes, *, mimetype: str, filename: str) -> str:
        encrypted = self.client.rooms[room].encrypted
        upload, file_keys = await self.client.upload(
            lambda *_: io.BytesIO(data), content_type=mimetype, filename=filename,
            encrypt=encrypted, filesize=len(data))
        assert isinstance(upload, UploadResponse), upload
        content = {"msgtype": "m.image", "body": filename,
                   "info": {"mimetype": mimetype, "size": len(data), "w": 1, "h": 1}}
        if encrypted:
            content["file"] = {**file_keys, "url": upload.content_uri}
        else:
            content["url"] = upload.content_uri
        return await self.send(room, content)

    async def wait_for_events(self, room: str, sender: str, count: int = 1) -> list:
        received, undecrypted = [], []
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            response = await self.sync(timeout=1000)
            joined_room = response.rooms.join.get(room)
            for event in joined_room.timeline.events if joined_room else []:
                if event.sender != sender:
                    continue
                if isinstance(event, MegolmEvent):
                    undecrypted.append(event)
                elif isinstance(event, (RoomMessage, UnknownEvent)):
                    received.append(event)
            for event in list(undecrypted):
                try:
                    received.append(self.client.decrypt_event(event))
                    undecrypted.remove(event)
                except EncryptionError:
                    pass
            if len(received) >= count:
                return received
        raise AssertionError(
            f"{self.mxid} got {received} from {sender} in {room}, expected {count};"
            f" {len(undecrypted)} did not decrypt")

    async def download(self, event) -> bytes:
        server_name, media_id = event.url.removeprefix("mxc://").split("/", 1)
        async with self.session.get(
                f"{HOMESERVER_URL}/_matrix/client/v1/media/download/{server_name}/{media_id}",
                headers={"Authorization": f"Bearer {self.client.access_token}"}) as response:
            assert response.status == 200, await response.text()
            data = await response.read()
        if not isinstance(event, RoomEncryptedMedia):
            return data
        return decrypt_attachment(data, event.key["k"], event.hashes["sha256"], event.iv)


async def leave(room: str, *identities: Identity) -> None:
    for identity in identities:
        await identity.call("DELETE", f"/channels/{quote(room, safe='')}", expect=204)


@pytest.fixture(scope="session", autouse=True)
def homeserver_reaches_appservice():
    if not BASE_URL:
        return
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while True:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health/push"):
                return
        except (urllib.error.URLError, ConnectionError):
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


async def _register(session: aiohttp.ClientSession, role: str, run: str) -> Identity:
    async with session.post(
            f"{BASE_URL}/identities", json={"name": f"e2e_{role}_{run}"}) as response:
        assert response.status == 201, await response.text()
        body = await response.json()
    return Identity(session, body["mxid"], body["api_token"])


@pytest.fixture
async def pair():
    async with aiohttp.ClientSession() as session:
        run = secrets.token_hex(3)
        yield [await _register(session, role, run) for role in ("alice", "bob")]


@pytest_asyncio.fixture(loop_scope="session")
async def ai():
    async with aiohttp.ClientSession() as session:
        yield await _register(session, "ai", secrets.token_hex(3))


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def human(tmp_path_factory):
    client = AsyncClient(
        HOMESERVER_URL, HUMAN_USER, store_path=str(tmp_path_factory.mktemp("human")),
        config=AsyncClientConfig(encryption_enabled=True))
    login = await client.login(HUMAN_PASSWORD, device_name="e2e")
    assert isinstance(login, LoginResponse), login
    try:
        async with aiohttp.ClientSession() as session:
            human = Human(client, session)
            await human.sync(full_state=True)
            yield human
    finally:
        await client.logout()
        await client.close()
