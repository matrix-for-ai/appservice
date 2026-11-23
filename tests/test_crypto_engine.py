import asyncio
import logging
import types

import aiohttp
import pytest
from aiohttp import web

from matrix_for_ai.adapters.nio import crypto_registry
from matrix_for_ai.adapters.nio.crypto_engine import CryptoEngine, Ed25519SigningKey
from matrix_for_ai.adapters.nio.crypto_registry import CryptoEngineRegistry
from matrix_for_ai.adapters.nio.stored_secrets import StoredSecrets

MXID = "@ai_nova:example.org"


async def test_requests_for_the_same_identity_at_once_share_one_engine(monkeypatch, tmp_path):
    started = []
    release = asyncio.Event()

    class SlowEngine:
        device_id = "DEVICE"

        def __init__(self, **_options):
            pass

        async def start(self):
            started.append(self)
            await release.wait()
    monkeypatch.setattr(crypto_registry, "CryptoEngine", SlowEngine)
    registry = CryptoEngineRegistry(
        homeserver_url="http://homeserver:8008", server_name="example.org", as_token="as",
        store_root=str(tmp_path), stored_secrets=StoredSecrets(None))
    both = asyncio.gather(registry.ensure("ai_nova"), registry.ensure("ai_nova"))
    await asyncio.sleep(0)
    release.set()
    first, second = await both
    assert len(started) == 1
    assert first is second is registry.get(MXID)


async def _engine_against(aiohttp_server, tmp_path, routes):
    application = web.Application()
    for path, answer in routes.items():
        application.router.add_post(path, answer)
    server = await aiohttp_server(application)
    engine = CryptoEngine(
        homeserver_url=str(server.make_url("")).rstrip("/"), server_name="example.org",
        as_token="as", localpart="ai_nova", store_root=str(tmp_path),
        stored_secrets=StoredSecrets(None))
    engine._access_token = "token"
    engine.device_id = "DEVICE"
    return engine


def _signing_keys():
    return {name: Ed25519SigningKey(bytes([number]) * Ed25519SigningKey.SEED_BYTES)
            for number, name in enumerate(("master", "ssk", "usk"))}


async def test_cross_signing_keys_the_homeserver_rejects_are_an_error_not_a_success(
        aiohttp_server, tmp_path, caplog):
    async def rejected(_request):
        return web.json_response({"errcode": "M_FORBIDDEN"}, status=403)
    engine = await _engine_against(
        aiohttp_server, tmp_path, {"/_matrix/client/v3/keys/device_signing/upload": rejected})
    caplog.set_level(logging.INFO)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(RuntimeError, match="403"):
            await engine._upload_cross_signing_keys(session, _signing_keys())
    assert "uploaded" not in caplog.text


async def test_a_device_signature_the_homeserver_rejects_is_an_error_not_a_success(
        aiohttp_server, tmp_path, caplog):
    async def device(_request):
        return web.json_response({"device_keys": {MXID: {"DEVICE": {
            "user_id": MXID, "device_id": "DEVICE", "keys": {}}}}})

    async def failures(_request):
        return web.json_response({"failures": {MXID: {"DEVICE": {"errcode": "M_INVALID_SIGNATURE"}}}})
    engine = await _engine_against(aiohttp_server, tmp_path, {
        "/_matrix/client/v3/keys/query": device,
        "/_matrix/client/v3/keys/signatures/upload": failures})
    caplog.set_level(logging.INFO)
    async with aiohttp.ClientSession() as session:
        with pytest.raises(RuntimeError, match="M_INVALID_SIGNATURE"):
            await engine._self_sign_device(session, _signing_keys()["ssk"])
    assert "self-signed" not in caplog.text


class _Device:
    def __init__(self, user_id: str, device_id: str) -> None:
        self.user_id, self.id, self.blacklisted = user_id, device_id, False


def _engine_over_a_room(tmp_path, devices, shared_with):
    session = types.SimpleNamespace(users_shared_with=set(shared_with), users_ignored=set())
    sent = []

    async def sync(**_options):
        return None

    async def room_send(room_id, event_type, content, ignore_unverified_devices):
        sent.append(room_id)
        return types.SimpleNamespace(event_id="$sent")

    def set_blacklisted(value):
        def change(device):
            device.blacklisted = value
        return change
    engine = CryptoEngine(homeserver_url="http://hs", server_name="example.org", as_token="t" * 16,
                          localpart="nova", store_root=str(tmp_path),
                          stored_secrets=StoredSecrets(""))
    engine.device_id = "NOVA"
    engine._client = types.SimpleNamespace(
        rooms={"!room:example.org": types.SimpleNamespace(
            users={user for user, _device in devices})},
        device_store=types.SimpleNamespace(active_user_devices=lambda user: [
            device for owner, device in devices if owner == user]),
        olm=types.SimpleNamespace(outbound_group_sessions={"!room:example.org": session}),
        should_query_keys=False, sync=sync, room_send=room_send,
        blacklist_device=set_blacklisted(True), unblacklist_device=set_blacklisted(False))
    return engine, sent


async def test_the_keys_are_withheld_from_the_devices_of_a_denied_user(tmp_path):
    human, stranger = _Device("@human:example.org", "H"), _Device("@stranger:malo.org", "S")
    engine, sent = _engine_over_a_room(
        tmp_path, [(human.user_id, human), (stranger.user_id, stranger)],
        shared_with=[(human.user_id, "H")])
    await engine.send_event(room_id="!room:example.org", event_type="m.room.message",
                            content={}, withholds_keys_from=lambda user: user == stranger.user_id)
    assert stranger.blacklisted and not human.blacklisted
    assert "!room:example.org" in engine._client.olm.outbound_group_sessions
    assert sent == ["!room:example.org"]


async def test_a_session_a_denied_user_already_holds_is_replaced(tmp_path):
    stranger = _Device("@stranger:malo.org", "S")
    engine, _sent = _engine_over_a_room(
        tmp_path, [(stranger.user_id, stranger)], shared_with=[(stranger.user_id, "S")])
    await engine.send_event(room_id="!room:example.org", event_type="m.room.message",
                            content={}, withholds_keys_from=lambda user: True)
    assert "!room:example.org" not in engine._client.olm.outbound_group_sessions


async def test_a_user_allowed_again_gets_the_keys_again(tmp_path):
    friend = _Device("@friend:amigo.org", "F")
    friend.blacklisted = True
    engine, _sent = _engine_over_a_room(tmp_path, [(friend.user_id, friend)], shared_with=[])
    await engine.send_event(room_id="!room:example.org", event_type="m.room.message",
                            content={}, withholds_keys_from=lambda user: False)
    assert not friend.blacklisted
