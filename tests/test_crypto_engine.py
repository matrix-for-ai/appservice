import asyncio
import logging

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
