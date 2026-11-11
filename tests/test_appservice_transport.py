import aiohttp
import pytest
from aiohttp import web

from fakes import AS_TOKEN, HS_TOKEN
from matrix_for_ai.adapters.matrix_appservice_transport import MatrixAppserviceTransport
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
        application_service_token=AS_TOKEN, homeserver_token=HS_TOKEN)
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
            application_service_token=AS_TOKEN, homeserver_token=HS_TOKEN)
        with pytest.raises(HomeserverError):
            await transport.create_room(
                as_identity="@ai_nova:example.org", name=None, topic=None,
                invitees=["@human:example.org"], is_direct=True, public=False)
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
            application_service_token=AS_TOKEN, homeserver_token=HS_TOKEN)
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
            "join": {"!joined:example.org": {}}, "invite": {"!invited:example.org": {}}}})
    application = web.Application()
    application.router.add_get("/_matrix/client/v3/sync", sync)
    server = await aiohttp_server(application)
    async with aiohttp.ClientSession() as session:
        transport = MatrixAppserviceTransport(
            http_session=session, homeserver_url=str(server.make_url("")).rstrip("/"),
            application_service_token=AS_TOKEN, homeserver_token=HS_TOKEN)
        assert await transport.list_invited_rooms(identity="@ai_nova:example.org") == [
            "!invited:example.org"]
    assert seen[0]["timeout"] == "0" and seen[0]["user_id"] == "@ai_nova:example.org"


async def test_a_homeserver_that_does_not_answer_is_an_error_without_a_status(
        unused_tcp_port):
    async with aiohttp.ClientSession() as session:
        transport = MatrixAppserviceTransport(
            http_session=session, homeserver_url=f"http://127.0.0.1:{unused_tcp_port}",
            application_service_token=AS_TOKEN, homeserver_token=HS_TOKEN)
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
