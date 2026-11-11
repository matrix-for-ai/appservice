import asyncio
import base64
import os
import secrets
import time
from urllib.parse import quote

import aiohttp
import pytest

BASE_URL = os.environ.get("MATRIX_FOR_AI_E2E_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(
    not BASE_URL, reason="set MATRIX_FOR_AI_E2E_URL to run against a running appservice")
TIMEOUT_SECONDS = float(os.environ.get("MATRIX_FOR_AI_E2E_TIMEOUT", "30"))
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


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


@pytest.fixture
async def pair():
    async with aiohttp.ClientSession() as session:
        run = secrets.token_hex(3)
        identities = []
        for role in ("alice", "bob"):
            async with session.post(
                    f"{BASE_URL}/identities", json={"name": f"e2e_{role}_{run}"}) as response:
                assert response.status == 201, await response.text()
                body = await response.json()
            identities.append(Identity(session, body["mxid"], body["api_token"]))
        yield identities


async def _converse(alice: Identity, bob: Identity, room: str, *, encrypted: bool) -> None:
    await alice.wait_until_member(room, bob.mxid)
    sent = await alice.send(room, {"type": "text", "content": {"text": "hola bob"}})
    assert sent["encrypted"] is encrypted
    assert [m["content"]["text"] for m in await bob.wait_for_events(room)] == ["hola bob"]

    reply = await bob.send(room, {"type": "text", "content": {"text": "hola alice"}})
    assert reply["encrypted"] is encrypted
    assert [m["content"]["text"] for m in await alice.wait_for_events(room)] == ["hola alice"]

    image = await bob.send(room, {
        "type": "image", "content": {"data_base64": base64.b64encode(PNG).decode(),
                                     "mimetype": "image/png", "filename": "dot.png",
                                     "w": 1, "h": 1}})
    received = await alice.wait_for_events(room)
    assert received[0]["type"] == "image"
    assert base64.b64decode(received[0]["content"]["data_base64"]) == PNG
    media = await alice.call(
        "GET", f"/channels/{quote(room, safe='')}/events/{quote(image['event_id'], safe='')}/media")
    assert media == PNG

    channel = f"/channels/{quote(room, safe='')}/events"
    history = (await alice.call("GET", channel))["events"]
    assert [m["content"].get("text") for m in history[-3:-1]] == ["hola bob", "hola alice"]
    assert history[-1]["event_id"] == image["event_id"]
    before = await alice.call(
        "GET", f"{channel}?limit=1&before={quote(history[-1]['event_id'], safe='')}")
    assert [m["event_id"] for m in before["events"]] == [history[-2]["event_id"]]
    assert before["has_more"]
    after = await alice.call("GET", f"{channel}?after={quote(history[-3]['event_id'], safe='')}")
    assert [m["event_id"] for m in after["events"]] == [m["event_id"] for m in history[-2:]]


async def test_health_and_capabilities():
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/health") as response:
            assert response.status == 204
        async with session.get(f"{BASE_URL}/capabilities") as response:
            capabilities = await response.json()
        async with session.get(f"{BASE_URL}/openapi.json") as response:
            contract = await response.json()
    assert capabilities["api_version"] == int(contract["info"]["version"].split(".")[0])
    assert {"open_signup", "webhooks", "e2ee", "tts", "stt"} <= capabilities.keys()


async def test_clear_group_conversation(pair):
    alice, bob = pair
    created = await alice.call("POST", "/channels", expect=201, json={
        "name": "e2e clear", "invite": [bob.mxid], "direct": False, "encrypted": False})
    try:
        await _converse(alice, bob, created["room"], encrypted=False)
    finally:
        await _leave(created["room"], alice, bob)


async def test_encrypted_dm_both_ways(pair):
    alice, bob = pair
    async with alice.session.get(f"{BASE_URL}/capabilities") as response:
        if not (await response.json())["e2ee"]:
            pytest.skip("the appservice runs without E2EE")
    created = await alice.call("POST", "/channels", expect=201, json={"invite": [bob.mxid]})
    assert created["encrypted"] and created["direct"]
    try:
        await _converse(alice, bob, created["room"], encrypted=True)
    finally:
        await _leave(created["room"], alice, bob)


async def test_voice_round_trip(pair):
    alice, bob = pair
    async with alice.session.get(f"{BASE_URL}/capabilities") as response:
        capabilities = await response.json()
    if not capabilities["tts"]["available"]:
        pytest.skip("no text-to-speech")
    created = await alice.call("POST", "/channels", expect=201, json={
        "name": "e2e voice", "invite": [bob.mxid], "direct": False})
    room = created["room"]
    try:
        await alice.wait_until_member(room, bob.mxid)
        sent = await alice.send(
            room, {"type": "audio", "content": {"transcript": "una prueba de voz"}})
        assert sent["type"] == "audio"
        voice = (await bob.wait_for_events(room))[0]
        assert voice["type"] == "audio" and voice["content"]["mimetype"] == "audio/ogg"
        transcribed = bool(voice["content"].get("transcript"))
        assert transcribed is capabilities["stt"]["available"], voice
    finally:
        await _leave(room, alice, bob)


async def _leave(room: str, *identities: Identity) -> None:
    for identity in identities:
        await identity.call("DELETE", f"/channels/{quote(room, safe='')}", expect=204)
