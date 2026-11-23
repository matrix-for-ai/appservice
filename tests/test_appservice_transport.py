import aiohttp
import pytest
from aiohttp import web

from fakes import AS_TOKEN, HS_TOKEN, new_transaction_log
from matrix_for_ai.adapters import transaction_log as transaction_log_module
from matrix_for_ai.adapters.matrix_appservice_transport import MatrixAppserviceTransport
from matrix_for_ai.adapters.transaction_log import TransactionLog
from matrix_for_ai.ports.matrix_transport import HomeserverError, MediaUri


def _homeserver_answering(status, body):
    async def answer(_request):
        return web.json_response(body, status=status)
    application = web.Application()
    application.router.add_route("*", "/{path:.*}", answer)
    return application


async def _transport_against(aiohttp_server, status, body):
    server = await aiohttp_server(_homeserver_answering(status, body))
    session = aiohttp.ClientSession()
    transport = MatrixAppserviceTransport(
        http_session=session, homeserver_url=str(server.make_url("")).rstrip("/"),
        application_service_token=AS_TOKEN, homeserver_token=HS_TOKEN,
            transaction_log=new_transaction_log())
    return transport, session


async def test_a_homeserver_failure_is_an_error_not_an_empty_answer(aiohttp_server):
    transport, session = await _transport_against(aiohttp_server, 500, {"errcode": "M_UNKNOWN"})
    async with session:
        calls = [
            transport.list_joined_rooms(identity="@ai_nova:example.org"),
            transport.list_invited_rooms(identity="@ai_nova:example.org"),
            transport.list_members(room_id="!r:example.org", as_identity="@ai_nova:example.org"),
            transport.fetch_event(room_id="!r:example.org", event_id="$e"),
            transport.fetch_event_relations(room_id="!r:example.org", event_id="$e"),
            transport.fetch_event_context(
                room_id="!r:example.org", event_id="$e", as_identity="@ai_nova:example.org"),
            transport.fetch_profile(identity="@ai_nova:example.org"),
            transport.send_event(room_id="!r:example.org", event_type="m.room.message",
                                 content={}, as_identity="@ai_nova:example.org"),
            transport.set_display_name(identity="@ai_nova:example.org", display_name="Nova"),
            transport.mark_room_read(
                as_identity="@ai_nova:example.org", room_id="!r:example.org", event_id="$e"),
        ]
        for call in calls:
            with pytest.raises(HomeserverError) as raised:
                await call
            assert raised.value.status == 500


async def test_an_event_the_homeserver_does_not_have_is_absent(aiohttp_server):
    transport, session = await _transport_against(aiohttp_server, 404, {"errcode": "M_NOT_FOUND"})
    async with session:
        assert await transport.fetch_event(room_id="!r:example.org", event_id="$e") is None
        assert await transport.fetch_event_context(
            room_id="!r:example.org", event_id="$e", as_identity="@ai_nova:example.org") is None
        assert await transport.fetch_event_relations(
            room_id="!r:example.org", event_id="$e") == []


async def test_a_direct_room_is_not_marked_over_account_data_that_failed_to_load(aiohttp_server):
    requests = []

    async def answer(request):
        requests.append(request.method)
        if request.path.endswith("/createRoom"):
            return web.json_response({"room_id": "!new:example.org"})
        return web.json_response({"errcode": "M_UNKNOWN"}, status=500)
    application = web.Application()
    application.router.add_route("*", "/{path:.*}", answer)
    server = await aiohttp_server(application)
    async with aiohttp.ClientSession() as session:
        transport = MatrixAppserviceTransport(
            http_session=session, homeserver_url=str(server.make_url("")).rstrip("/"),
            application_service_token=AS_TOKEN, homeserver_token=HS_TOKEN,
            transaction_log=new_transaction_log())
        with pytest.raises(HomeserverError):
            await transport.create_room(
                as_identity="@ai_nova:example.org", name=None, topic=None,
                invitees=["@human:example.org"], is_direct=True, public=False, federate=True,
                server_acl=None)
    assert "PUT" not in requests


@pytest.mark.parametrize("body, errcode", [
    (b"{not json", "M_NOT_JSON"),
    (b"\xff\xfe", "M_NOT_JSON"),
    (b"[]", "M_BAD_JSON"),
    (b"{}", "M_BAD_JSON"),
    (b'{"events": {}}', "M_BAD_JSON"),
    (b'{"events": ["text"]}', "M_BAD_JSON"),
])
async def test_a_transaction_that_is_not_a_list_of_events_is_refused_so_it_is_sent_again(
        aiohttp_client, body, errcode):
    delivered = []

    async def deliver(event):
        delivered.append(event)
    async with aiohttp.ClientSession() as session:
        transport = MatrixAppserviceTransport(
            http_session=session, homeserver_url="http://homeserver:8008",
            application_service_token=AS_TOKEN, homeserver_token=HS_TOKEN,
            transaction_log=new_transaction_log())
        transport.set_incoming_event_handler(deliver)
        application = web.Application()
        transport.register_push_routes(application)
        client = await aiohttp_client(application)
        response = await client.put(
            "/_matrix/app/v1/transactions/t1", data=body,
            headers={"Authorization": f"Bearer {HS_TOKEN}", "Content-Type": "application/json"})
        assert response.status == 400
        assert (await response.json())["errcode"] == errcode
    assert delivered == []


async def test_the_invited_rooms_come_from_a_sync_without_waiting(aiohttp_server):
    seen = []

    async def sync(request):
        seen.append(dict(request.query))
        return web.json_response({"next_batch": "s1", "rooms": {
            "join": {"!joined:example.org": {}},
            "invite": {"!invited:example.org": {"invite_state": {"events": [
                {"type": "m.room.name", "state_key": "", "sender": "@other:example.net"},
                {"type": "m.room.member", "state_key": "@ai_nova:example.org",
                 "sender": "@human:example.net", "content": {"membership": "invite"}}]}},
                       "!unknown_inviter:example.org": {}}}})
    application = web.Application()
    application.router.add_get("/_matrix/client/v3/sync", sync)
    server = await aiohttp_server(application)
    async with aiohttp.ClientSession() as session:
        transport = MatrixAppserviceTransport(
            http_session=session, homeserver_url=str(server.make_url("")).rstrip("/"),
            application_service_token=AS_TOKEN, homeserver_token=HS_TOKEN,
            transaction_log=new_transaction_log())
        assert await transport.list_invited_rooms(identity="@ai_nova:example.org") == {
            "!invited:example.org": "@human:example.net", "!unknown_inviter:example.org": None}
    assert seen[0]["timeout"] == "0" and seen[0]["user_id"] == "@ai_nova:example.org"


async def test_a_homeserver_that_does_not_answer_is_an_error_without_a_status(
        unused_tcp_port):
    async with aiohttp.ClientSession() as session:
        transport = MatrixAppserviceTransport(
            http_session=session, homeserver_url=f"http://127.0.0.1:{unused_tcp_port}",
            application_service_token=AS_TOKEN, homeserver_token=HS_TOKEN,
            transaction_log=new_transaction_log())
        calls = [
            transport.list_joined_rooms(identity="@ai_nova:example.org"),
            transport.download_media(media_uri=MediaUri("example.org", "a"),
                                     as_identity="@ai_nova:example.org", max_bytes=None),
            transport.upload_media(data=b"x", mime_type="text/plain", filename="x.txt"),
        ]
        for call in calls:
            with pytest.raises(HomeserverError) as raised:
                await call
            assert raised.value.status is None
        check = await transport.ping_from_homeserver(appservice_id="matrix-for-ai")
        assert (check.supported, check.reachable) == (True, False)


async def _push(client, transaction_id, events):
    return await client.put(
        f"/_matrix/app/v1/transactions/{transaction_id}", json={"events": events},
        headers={"Authorization": f"Bearer {HS_TOKEN}"})


async def _client_over(aiohttp_client, transaction_log, deliver):
    session = aiohttp.ClientSession()
    transport = MatrixAppserviceTransport(
        http_session=session, homeserver_url="http://homeserver:8008",
        application_service_token=AS_TOKEN, homeserver_token=HS_TOKEN,
        transaction_log=transaction_log)
    transport.set_incoming_event_handler(deliver)
    application = web.Application()
    application.on_cleanup.append(lambda _application: session.close())
    transport.register_push_routes(application)
    return await aiohttp_client(application)


async def test_a_transaction_sent_again_is_answered_without_handling_its_events_again(
        aiohttp_client, tmp_path):
    delivered = []

    async def deliver(event):
        delivered.append(event["event_id"])
    events = [{"event_id": "$1"}, {"event_id": "$2"}]
    client = await _client_over(aiohttp_client, TransactionLog(tmp_path), deliver)
    assert (await _push(client, "t1", events)).status == 200
    assert (await _push(client, "t1", events)).status == 200
    assert delivered == ["$1", "$2"]


async def test_a_transaction_id_used_again_for_other_events_is_a_new_transaction(
        aiohttp_client, tmp_path):
    delivered = []

    async def deliver(event):
        delivered.append(event["event_id"])
    client = await _client_over(aiohttp_client, TransactionLog(tmp_path), deliver)
    assert (await _push(client, "t1", [{"event_id": "$1"}])).status == 200
    assert (await _push(client, "t1", [{"event_id": "$2"}])).status == 200
    assert (await _push(client, "t1", [{"event_id": "$2", "room_id": "!other"}])).status == 200
    assert delivered == ["$1", "$2", "$2"]


async def test_a_transaction_that_failed_halfway_resumes_at_the_event_that_failed(
        aiohttp_client, tmp_path):
    delivered, failing = [], {"$2"}

    async def deliver(event):
        if event["event_id"] in failing:
            raise HomeserverError(0, None)
        delivered.append(event["event_id"])
    events = [{"event_id": "$1"}, {"event_id": "$2"}, {"event_id": "$3"}]
    client = await _client_over(aiohttp_client, TransactionLog(tmp_path), deliver)
    assert (await _push(client, "t1", events)).status == 500
    failing.clear()
    assert (await _push(client, "t1", events)).status == 200
    assert delivered == ["$1", "$2", "$3"]


async def test_what_a_transaction_handled_outlives_a_restart(aiohttp_client, tmp_path):
    delivered = []

    async def deliver(event):
        delivered.append(event["event_id"])
    events = [{"event_id": "$1"}]
    before_restart = TransactionLog(tmp_path)
    client = await _client_over(aiohttp_client, before_restart, deliver)
    assert (await _push(client, "t1", events)).status == 200
    before_restart.close()
    client = await _client_over(aiohttp_client, TransactionLog(tmp_path), deliver)
    assert (await _push(client, "t1", events)).status == 200
    assert delivered == ["$1"]


def test_the_transaction_log_keeps_only_the_newest_transactions(tmp_path, monkeypatch):
    monkeypatch.setattr(transaction_log_module, "TRANSACTIONS_KEPT", 2)
    log = TransactionLog(tmp_path)
    for transaction_id in ("t1", "t2", "t3"):
        log.record_events_done(transaction_id, 1)
    assert [log.events_done(t) for t in ("t1", "t2", "t3")] == [0, 1, 1]
