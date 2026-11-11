import array
import io
import math
import os
import shutil
import stat
import struct
import sys
import types
import wave

import aiohttp
import pytest
from aiohttp import web

from conftest import base_configuration
from matrix_for_ai.configuration import ConfigurationError, load_configuration, parse_configuration
from matrix_for_ai.adapters.nio.stored_secrets import StoredSecrets
from matrix_for_ai.adapters.openai_speech import OpenAiSpeechSynthesizer, OpenAiTranscriber, waveform_of
from matrix_for_ai.ports.speech import SpeechServiceConfiguration, SpeechUnavailable


def test_minimal_configuration_has_safe_defaults(tmp_path):
    configuration = parse_configuration(base_configuration(tmp_path))
    assert configuration.e2ee is True
    assert configuration.webhook_allowed_targets == ()
    assert configuration.open_signup is True
    assert configuration.tts is None and configuration.stt is None
    assert (configuration.listen_host, configuration.listen_port) == ("0.0.0.0", 7700)


@pytest.mark.parametrize("mutate, message", [
    (lambda raw: raw.pop("homeserver"), "homeserver is required"),
    (lambda raw: raw["appservice"].pop("hs_token"), "appservice.hs_token is required"),
    (lambda raw: raw["appservice"].update(as_token="short"), "shorter than 32 characters"),
    (lambda raw: raw["appservice"].update(hs_token="x" * 31), "shorter than 32 characters"),
    (lambda raw: raw.update(admin_token="x" * 31), "admin_token is shorter than 32"),
    (lambda raw: raw.update(admin_token="CHANGE_ME"), "admin_token is still the example's placeholder"),
    (lambda raw: raw["appservice"].update(as_token="CHANGE_ME_AS_TOKEN" + "x" * 20),
     "openssl rand -hex 32"),
    (lambda raw: raw["homeserver"].update(url="tuwunel:6167"), "http(s) URL"),
    (lambda raw: raw.update(listen="7700"), "HOST:PORT"),
    (lambda raw: raw.update(stt_url="http://x"), "unknown keys in configuration: stt_url"),
    (lambda raw: raw["appservice"].update(e2ee="yes"), "true or false"),
    (lambda raw: raw.update(webhooks={"allowed_targets": ["host:80"]}), "is not IP:PORT"),
    (lambda raw: raw.update(webhooks={"allowed_targets": ["10.0.0.1/8:80"]}), "is not IP:PORT"),
    (lambda raw: raw.update(webhooks={"allowed_targets": ["10.0.0.1:99999"]}), "is not IP:PORT"),
    (lambda raw: raw.update(webhooks={"allowed_targets": ["10.0.0.1"]}), "is not IP:PORT"),
    (lambda raw: raw.update(open_signup="no"), "open_signup must be true or false"),
    (lambda raw: raw.update(webhooks={"ca_file": "/nonexistent.pem"}),
     "webhooks.ca_file is not a readable PEM certificate"),
    (lambda raw: raw.update(speech={"tts": {"url": "x"}}), "speech.tts.url"),
])
def test_invalid_configuration_is_reported_clearly(tmp_path, mutate, message):
    raw = base_configuration(tmp_path)
    mutate(raw)
    with pytest.raises(ConfigurationError, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse_configuration(raw)


def test_environment_placeholders(tmp_path, monkeypatch):
    raw = base_configuration(tmp_path)
    raw["appservice"]["as_token"] = "${TEST_AS_TOKEN}"
    path = tmp_path / "config.json"
    import json
    path.write_text(json.dumps(raw))
    with pytest.raises(ConfigurationError, match="TEST_AS_TOKEN"):
        load_configuration(path)
    monkeypatch.setenv("TEST_AS_TOKEN", "x" * 32)
    assert load_configuration(path).as_token == "x" * 32


def test_speech_service_without_url_counts_as_absent(tmp_path):
    raw = base_configuration(tmp_path, speech={"tts": {"url": ""}, "stt": None})
    configuration = parse_configuration(raw)
    assert configuration.tts is None and configuration.stt is None


def test_waveform_is_normalized_to_1024():
    samples = array.array("h", [0] * 50 + [1000] * 50 + [-2000] * 50)
    waveform = waveform_of(samples, bucket_count=3)
    assert waveform == [0, 512, 1024]
    assert waveform_of(array.array("h"), bucket_count=2) == [0, 0]


def _sine_wav(seconds: float = 0.5, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = int(seconds * rate)
        handle.writeframes(b"".join(
            struct.pack("<h", int(8000 * math.sin(i / 10))) for i in range(frames)))
    return buffer.getvalue()


async def _info(_request):
    return web.json_response({"voices": ["a", "b"]})


async def _provider(aiohttp_server, handler, path):
    application = web.Application()
    application.router.add_post(path, handler)
    application.router.add_get("/info", _info)
    return await aiohttp_server(application)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
async def test_openai_synthesizer_request_and_voice_encoding(aiohttp_server):
    received = {}

    async def speech(request):
        received["body"] = await request.json()
        received["authorization"] = request.headers.get("Authorization")
        return web.Response(body=_sine_wav(), content_type="audio/wav")

    server = await _provider(aiohttp_server, speech, "/v1/audio/speech")
    async with aiohttp.ClientSession() as session:
        synthesizer = OpenAiSpeechSynthesizer(session, SpeechServiceConfiguration(
            url=str(server.make_url("/v1/audio/speech")), api_key="k",
            info_url=str(server.make_url("/info"))))
        voice = await synthesizer.synthesize("hola", {"voice": "x", "speed": 1.2})
        await synthesizer.refresh_description()
        description = synthesizer.describe()
    assert received["body"] == {
        "voice": "x", "speed": 1.2, "input": "hola", "response_format": "wav"}
    assert received["authorization"] == "Bearer k"
    assert voice.opus_ogg_bytes.startswith(b"OggS")
    assert 450 <= voice.duration_milliseconds <= 550
    assert len(voice.waveform) == 30 and max(voice.waveform) == 1024
    assert description == {"available": True, "info": {"voices": ["a", "b"]}}


async def test_openai_synthesizer_reports_provider_errors(aiohttp_server):
    async def rejecting(_request):
        return web.Response(status=400, text="unknown voice")

    server = await _provider(aiohttp_server, rejecting, "/v1/audio/speech")
    async with aiohttp.ClientSession() as session:
        synthesizer = OpenAiSpeechSynthesizer(session, SpeechServiceConfiguration(
            url=str(server.make_url("/v1/audio/speech"))))
        with pytest.raises(SpeechUnavailable, match="unknown voice"):
            await synthesizer.synthesize("hola", {})


async def test_openai_transcriber_sends_file_and_options(aiohttp_server):
    received = {}

    async def transcriptions(request):
        form = await request.post()
        received["fields"] = {k: v for k, v in form.items() if k != "file"}
        received["file"] = (form["file"].filename, form["file"].file.read())
        return web.json_response({"text": " hola \n"})

    server = await _provider(aiohttp_server, transcriptions, "/v1/audio/transcriptions")
    async with aiohttp.ClientSession() as session:
        transcriber = OpenAiTranscriber(session, SpeechServiceConfiguration(
            url=str(server.make_url("/v1/audio/transcriptions"))))
        text = await transcriber.transcribe(b"opus", "audio/ogg", {"language": "es", "temperature": 0})
    assert text == "hola"
    assert received["file"] == ("audio.ogg", b"opus")
    assert received["fields"] == {"language": "es", "temperature": "0", "response_format": "json"}


async def test_extensions_add_routes_and_observe_events(make_appservice):
    seen = []
    module = types.ModuleType("example_extension")

    def setup(context):
        assert context.options == {"greeting": "hi"}

        async def hello(_request):
            return web.json_response({"greeting": context.options["greeting"]})

        context.add_routes(lambda router: router.add_get("/hello", hello))
        context.add_event_observer(lambda event, managed: seen.append((event["type"], managed)))

    module.setup = setup
    sys.modules["example_extension"] = module
    try:
        appservice = await make_appservice(extensions={"example_extension": {"greeting": "hi"}})
        assert await (await appservice.client.get("/hello")).json() == {"greeting": "hi"}
        room = appservice.hs.create_room("@human:example.org", [], encrypted=False)
        await appservice.deliver(room, "@human:example.org", "m.custom", {})
        assert seen == [("m.custom", frozenset())]
    finally:
        del sys.modules["example_extension"]


def test_nio_adapters_implement_the_encryption_ports():
    import inspect

    from matrix_for_ai.adapters.nio.crypto_engine import CryptoEngine
    from matrix_for_ai.adapters.nio.crypto_registry import CryptoEngineRegistry
    from matrix_for_ai.ports.encryption import EncryptionEngine, EncryptionRegistry

    for port, adapter in ((EncryptionEngine, CryptoEngine), (EncryptionRegistry, CryptoEngineRegistry)):
        for name, member in inspect.getmembers(port, inspect.isfunction):
            if name.startswith("_"):
                continue
            implemented = getattr(adapter, name, None)
            assert implemented is not None, f"{adapter.__name__} lacks {name}"
            assert inspect.signature(implemented).parameters.keys() == (
                inspect.signature(member).parameters.keys()), name


@pytest.mark.parametrize("content_without", [("sender_key",), ("sender_key", "device_id")])
async def test_megolm_events_without_the_deprecated_sender_fields_still_decrypt(
        tmp_path, content_without):
    from nio.crypto.memorystores import GroupSessionStore

    from matrix_for_ai.adapters.nio.crypto_engine import CryptoEngine

    room, session_id, sender_key = "!room:example.org", "SESSION", "CURVE25519KEY"
    store = GroupSessionStore()
    store.add(types.SimpleNamespace(room_id=room, sender_key=sender_key, id=session_id))
    seen = []

    def decrypt_megolm_event(event, room_id):
        seen.append(event.sender_key)
        return types.SimpleNamespace(source={"type": "m.room.message", "content": {"body": "hi"}})

    engine = CryptoEngine(homeserver_url="http://hs", server_name="example.org",
                          as_token="t" * 16, localpart="nova", store_root=str(tmp_path),
                          stored_secrets=StoredSecrets(""))
    engine._client = types.SimpleNamespace(olm=types.SimpleNamespace(
        inbound_group_store=store, decrypt_megolm_event=decrypt_megolm_event))
    content = {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "x", "session_id": session_id,
               "sender_key": sender_key, "device_id": "DEVICE"}
    for key in content_without:
        del content[key]
    raw = {"type": "m.room.encrypted", "event_id": "$e", "sender": "@human:example.org",
           "origin_server_ts": 1, "room_id": room, "content": content}

    assert await engine.decrypt_event(raw, room) == {
        "type": "m.room.message", "content": {"body": "hi"}}
    assert seen == [sender_key]


@pytest.mark.parametrize("key, sha256, iv", [
    ("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "wrong", "AAAAAAAAAAAAAAAAAAAAAA"),
    ("not base64!", "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU", "AAAAAAAAAAAAAAAAAAAAAA"),
    ("AAAA", "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU", "AAAAAAAAAAAAAAAAAAAAAA"),
])
def test_an_attachment_that_does_not_decrypt_gives_nothing(tmp_path, key, sha256, iv):
    from matrix_for_ai.adapters.nio.crypto_engine import CryptoEngine
    from matrix_for_ai.ports.encryption import EncryptedFile

    engine = CryptoEngine(homeserver_url="http://hs", server_name="example.org",
                          as_token="t" * 16, localpart="nova", store_root=str(tmp_path),
                          stored_secrets=StoredSecrets(""))
    assert engine.decrypt_attachment(
        b"", EncryptedFile("mxc://example.org/a", key, sha256, iv)) is None


def test_the_access_log_never_writes_the_query_where_the_homeserver_puts_its_token(caplog):
    import logging

    from aiohttp.test_utils import make_mocked_request

    from matrix_for_ai.api.access_log import AccessLoggerWithoutQuery

    caplog.set_level(logging.INFO, logger="aiohttp.access")
    access_log = AccessLoggerWithoutQuery(logging.getLogger("aiohttp.access"), "")
    request = make_mocked_request(
        "PUT", "/_matrix/app/v1/transactions/t1?access_token=hs-secret-token",
        headers={"User-Agent": "tuwunel"})
    access_log.log(request, web.Response(status=200, body=b"{}"), 0.01)
    line = caplog.records[-1].getMessage()
    assert "/_matrix/app/v1/transactions/t1" in line and " 200 " in line
    assert "hs-secret-token" not in line and "access_token" not in line


@pytest.mark.parametrize("is_direct, public, preset", [(True, False, "trusted_private_chat"),
                                                       (False, False, "private_chat"),
                                                       (False, True, "public_chat")])
async def test_encrypted_groups_keep_the_creator_above_the_invitees(
        tmp_path, is_direct, public, preset):
    from matrix_for_ai.adapters.nio.crypto_engine import CryptoEngine

    created = {}

    async def room_create(**options):
        created.update(options)
        return types.SimpleNamespace(room_id="!new:example.org")

    engine = CryptoEngine(homeserver_url="http://hs", server_name="example.org",
                          as_token="t" * 16, localpart="nova", store_root=str(tmp_path),
                          stored_secrets=StoredSecrets(""))
    engine._client = types.SimpleNamespace(room_create=room_create)
    await engine.create_encrypted_room(
        invitees=["@human:example.org"], name="lobby", topic=None, is_direct=is_direct,
        public=public)
    assert created["preset"].value == preset


def test_the_appservice_listens_apart_from_the_api(tmp_path):
    configuration = parse_configuration(base_configuration(tmp_path))
    assert (configuration.listen_port, configuration.appservice_listen_port) == (7700, 7701)
    raw = base_configuration(tmp_path, listen="0.0.0.0:7701", appservice_listen="0.0.0.0:7701")
    with pytest.raises(ConfigurationError, match="a port other than listen's"):
        parse_configuration(raw)


@pytest.mark.parametrize("limits, message", [
    ({"requests_per_minute": 0}, "limits.requests_per_minute must be a positive integer"),
    ({"speech_per_minute": "10"}, "limits.speech_per_minute must be a positive integer"),
    ({"bursts": 3}, "unknown keys in limits: bursts"),
])
def test_limits_are_positive_integers_or_null(tmp_path, limits, message):
    with pytest.raises(ConfigurationError, match=message):
        parse_configuration(base_configuration(tmp_path, limits=limits))


async def test_the_api_does_not_serve_the_appservice_routes(make_appservice):
    appservice = await make_appservice()
    response = await appservice.client.put("/_matrix/app/v1/transactions/t1", json={"events": []})
    assert response.status == 404


def test_the_appservice_keeps_what_it_writes_private(tmp_path):
    from matrix_for_ai.__main__ import PRIVATE_FILES_UMASK

    previous = os.umask(PRIVATE_FILES_UMASK)
    try:
        folder = tmp_path / "crypto" / "nova"
        folder.mkdir(parents=True)
        (folder / "keys.db").write_bytes(b"secret")
    finally:
        os.umask(previous)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "keys.db").stat().st_mode) == 0o600


def test_the_appservice_listener_defaults_to_loopback(tmp_path):
    configuration = parse_configuration(base_configuration(tmp_path))
    assert (configuration.appservice_listen_host,
            configuration.appservice_listen_port) == ("127.0.0.1", 7701)


@pytest.mark.parametrize("path, headers, status", [
    ("/_matrix/app/v1/transactions/t1", {"Authorization": "Bearer hs-" + "h" * 40}, 200),
    ("/_matrix/app/v1/transactions/t1?access_token=hs-" + "h" * 40, {}, 401),
    ("/_matrix/app/v1/transactions/t1", {"Authorization": "Bearer wrong"}, 403),
    ("/_matrix/app/v1/transactions/t1", {"Authorization": "hs-" + "h" * 40}, 403),
])
async def test_the_homeserver_token_is_accepted_only_in_the_authorization_header(
        aiohttp_client, path, headers, status):
    from matrix_for_ai.adapters.matrix_appservice_transport import MatrixAppserviceTransport

    async with aiohttp.ClientSession() as session:
        transport = MatrixAppserviceTransport(
            http_session=session, homeserver_url="http://homeserver:8008",
            application_service_token="as-" + "a" * 40, homeserver_token="hs-" + "h" * 40)
        application = web.Application()
        transport.register_push_routes(application)
        client = await aiohttp_client(application)
        response = await client.put(path, json={"events": []}, headers=headers)
        assert response.status == status


async def test_speech_description_is_served_from_memory_and_keeps_the_last_good_one(aiohttp_server):
    answers = [web.json_response({"voices": ["a"]}), web.Response(status=503)]
    requests = []

    async def info(_request):
        requests.append(1)
        return answers.pop(0)

    application = web.Application()
    application.router.add_get("/info", info)
    server = await aiohttp_server(application)
    async with aiohttp.ClientSession() as session:
        synthesizer = OpenAiSpeechSynthesizer(session, SpeechServiceConfiguration(
            url=str(server.make_url("/v1/audio/speech")), info_url=str(server.make_url("/info"))))
        assert synthesizer.describe() == {"available": True, "info": None}
        await synthesizer.refresh_description()
        synthesizer.describe()
        synthesizer.describe()
        assert len(requests) == 1
        await synthesizer.refresh_description()
        assert synthesizer.describe() == {"available": True, "info": {"voices": ["a"]}}


async def test_speech_description_reports_the_error_until_a_good_one_arrives(aiohttp_server):
    application = web.Application()
    application.router.add_get("/info", _unavailable)
    server = await aiohttp_server(application)
    async with aiohttp.ClientSession() as session:
        transcriber = OpenAiTranscriber(session, SpeechServiceConfiguration(
            url=str(server.make_url("/v1/audio/transcriptions")),
            info_url=str(server.make_url("/info"))))
        await transcriber.refresh_description()
    assert transcriber.describe() == {
        "available": True, "info": None, "info_error": "provider answered 503"}


async def _unavailable(_request):
    return web.Response(status=503)


@pytest.mark.parametrize("path", [
    "/_matrix/app/v1/users/@ai_ghost:example.org", "/_matrix/app/v1/rooms/%23ghost:example.org"])
async def test_namespace_queries_answer_that_the_appservice_creates_nothing_on_demand(
        aiohttp_client, path):
    from matrix_for_ai.adapters.matrix_appservice_transport import MatrixAppserviceTransport

    async with aiohttp.ClientSession() as session:
        transport = MatrixAppserviceTransport(
            http_session=session, homeserver_url="http://homeserver:8008",
            application_service_token="as-" + "a" * 40, homeserver_token="hs-" + "h" * 40)
        application = web.Application()
        transport.register_push_routes(application)
        client = await aiohttp_client(application)
        response = await client.get(path, headers={"Authorization": "Bearer hs-" + "h" * 40})
        assert response.status == 404
        assert (await response.json())["errcode"] == "M_NOT_FOUND"
        refused = await client.get(path)
        assert refused.status == 401


@pytest.mark.parametrize("method, path", [
    ("POST", "/_matrix/app/unstable/org.matrix.msc3984/keys/query"),
    ("PUT", "/transactions/t1"),
    ("GET", "/_matrix/app/v1/ping"),
])
async def test_endpoints_the_appservice_does_not_implement_answer_m_unrecognized(
        aiohttp_client, method, path):
    from matrix_for_ai.adapters.matrix_appservice_transport import MatrixAppserviceTransport

    async with aiohttp.ClientSession() as session:
        transport = MatrixAppserviceTransport(
            http_session=session, homeserver_url="http://homeserver:8008",
            application_service_token="as-" + "a" * 40, homeserver_token="hs-" + "h" * 40)
        application = web.Application()
        transport.register_push_routes(application)
        client = await aiohttp_client(application)
        response = await client.request(method, path, json={})
        assert response.status == 404
        assert (await response.json())["errcode"] == "M_UNRECOGNIZED"
