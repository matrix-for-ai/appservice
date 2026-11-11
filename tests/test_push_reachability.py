import asyncio
import contextlib

import aiohttp
import pytest
from aiohttp import web

import fakes
from fakes import AS_TOKEN, HS_TOKEN
from matrix_for_ai.__main__ import _catch_up_when_homeserver_ready, _checked_every
from matrix_for_ai.adapters.matrix_appservice_transport import MatrixAppserviceTransport
from matrix_for_ai.ports.matrix_transport import PushCheck

PROBLEM = "application/problem+json"


async def _push_health(appservice):
    for _ in range(50):
        if appservice.transport.pings:
            break
        await asyncio.sleep(0.01)
    return await appservice.client.get("/health/push")


async def test_push_health_says_the_homeserver_reaches_the_appservice(make_appservice):
    appservice = await make_appservice()
    response = await _push_health(appservice)
    assert response.status == 204
    assert appservice.transport.pings == ["test"]


@pytest.mark.parametrize("push_check, status, code, upstream", [
    (PushCheck(supported=True, reachable=False, status=502, errcode="M_CONNECTION_FAILED"),
     503, "push_unreachable", {"status": 502, "errcode": "M_CONNECTION_FAILED"}),
    (PushCheck(supported=True, reachable=False, status=0), 503, "push_unreachable", None),
    (PushCheck(supported=False, reachable=False, status=404, errcode="M_UNRECOGNIZED"),
     501, "push_check_unsupported", None),
])
async def test_push_health_reports_what_the_ping_found(make_appservice, monkeypatch, push_check,
                                                       status, code, upstream):
    monkeypatch.setattr(fakes, "PUSH_CHECK", push_check)
    appservice = await make_appservice()
    response = await _push_health(appservice)
    assert response.status == status and response.content_type == PROBLEM
    body = await response.json()
    assert body["code"] == code and body.get("upstream") == upstream


async def test_the_appservice_answers_the_homeserver_ping_only_with_its_token(aiohttp_client):
    async with aiohttp.ClientSession() as session:
        transport = MatrixAppserviceTransport(
            http_session=session, homeserver_url="http://homeserver:8008",
            application_service_token=AS_TOKEN, homeserver_token=HS_TOKEN)
        application = web.Application()
        transport.register_push_routes(application)
        client = await aiohttp_client(application)
        ok = await client.post("/_matrix/app/v1/ping", json={"transaction_id": "t"},
                               headers={"Authorization": f"Bearer {HS_TOKEN}"})
        assert ok.status == 200 and await ok.json() == {}
        assert (await client.post("/_matrix/app/v1/ping", json={})).status == 401
        forged = await client.post("/_matrix/app/v1/ping", json={},
                                   headers={"Authorization": "Bearer wrong"})
        assert forged.status == 403 and await forged.json() == {"errcode": "M_FORBIDDEN"}


@pytest.mark.parametrize("answer, expected", [
    (web.json_response({"duration_ms": 3}), PushCheck(True, True, 200)),
    (web.json_response({"errcode": "M_CONNECTION_FAILED"}, status=502),
     PushCheck(True, False, 502, "M_CONNECTION_FAILED")),
    (web.json_response({"errcode": "M_UNRECOGNIZED"}, status=404),
     PushCheck(False, False, 404, "M_UNRECOGNIZED")),
])
async def test_the_ping_asks_the_homeserver_as_the_appservice(aiohttp_server, answer, expected):
    received = []

    async def ping(request):
        received.append((request.match_info["appservice_id"],
                         request.headers.get("Authorization"), await request.json()))
        return answer

    application = web.Application()
    application.router.add_post("/_matrix/client/v1/appservice/{appservice_id}/ping", ping)
    server = await aiohttp_server(application)
    async with aiohttp.ClientSession() as session:
        transport = MatrixAppserviceTransport(
            http_session=session, homeserver_url=str(server.make_url("")).rstrip("/"),
            application_service_token=AS_TOKEN, homeserver_token=HS_TOKEN)
        assert await transport.ping_from_homeserver(appservice_id="matrix-for-ai") == expected
    appservice_id, authorization, body = received[0]
    assert appservice_id == "matrix-for-ai" and authorization == f"Bearer {AS_TOKEN}"
    assert body["transaction_id"]


async def test_push_health_waits_for_the_first_ping(make_appservice, monkeypatch):
    monkeypatch.setattr(fakes, "PUSH_CHECK", None)
    appservice = await make_appservice()
    response = await appservice.client.get("/health/push")
    assert response.status == 503 and (await response.json())["code"] == "push_unchecked"


async def test_a_periodic_check_that_fails_keeps_running():
    calls = []

    async def flaky_check():
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("first check fails")

    context = _checked_every(flaky_check, 0)(None)
    await context.__anext__()
    for _ in range(10):
        await asyncio.sleep(0)
    with contextlib.suppress(StopAsyncIteration):
        await context.__anext__()
    assert len(calls) >= 2


async def test_membership_is_loaded_again_when_the_homeserver_fails_at_startup(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, _auth = await appservice.register("nova")
    room = appservice.hs.create_room("@human:example.org", [], encrypted=False)
    appservice.hs.rooms[room]["members"].add(mxid)
    router = appservice.event_router
    appservice.transport.failing["list_joined_rooms"] = 502
    attempt = asyncio.create_task(
        _catch_up_when_homeserver_ready(router, asyncio.Event()))
    await asyncio.sleep(0.1)
    assert not attempt.done() and router.managed_in_room(room) == frozenset()
    del appservice.transport.failing["list_joined_rooms"]
    await asyncio.wait_for(attempt, timeout=5)
    assert router.managed_in_room(room) == {mxid}
