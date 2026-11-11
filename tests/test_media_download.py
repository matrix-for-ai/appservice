from urllib.parse import quote

import aiohttp
import pytest
from aiohttp import web

from fakes import AS_TOKEN, PNG
from matrix_for_ai.adapters.matrix_appservice_transport import MatrixAppserviceTransport
from matrix_for_ai.ports.matrix_transport import MediaTooLarge, MediaUri

HUMAN = "@human:example.org"
VICTIM = "@ai_victim:example.org"
HOSTILE_URIS = [
    "mxc://x/y/../../../../../client/v3/rooms/!room:example.org/messages?dir=b"
    f"&user_id={VICTIM}",
    "mxc://example.org/../../client/v3/sync",
    "mxc://example.org/%2E%2E/%2E%2E/client/v3/joined_rooms",
    "mxc://example.org/abc/def",
    "mxc://example.org/abc?user_id=" + VICTIM,
    "mxc://example.org/abc&user_id=" + VICTIM,
    "mxc://example.org/abc#fragment",
    "mxc://../abc",
    "mxc://example..org/abc",
    "mxc://.example.org/abc",
    "mxc://exa_mple.org/abc",
    "mxc://example.org:port/abc",
    "mxc://example.org:123456/abc",
    "mxc://user@example.org/abc",
    "mxc://example.org/",
    "mxc:///abc",
    "mxc://example.org",
    "mxc://example.org/abc\n",
    "mxc://" + "a" * 256 + "/abc",
    "https://example.org/abc",
    None,
    42,
]


@pytest.mark.parametrize("text", HOSTILE_URIS)
def test_media_uri_rejects_anything_outside_the_specification(text):
    assert MediaUri.parse(text) is None


@pytest.mark.parametrize("text, server_name, media_id", [
    ("mxc://example.org/AbC_123-x", "example.org", "AbC_123-x"),
    ("mxc://matrix.example.org:8448/abc", "matrix.example.org:8448", "abc"),
    ("mxc://192.168.1.10/abc", "192.168.1.10", "abc"),
    ("mxc://[::1]:8448/abc", "[::1]:8448", "abc"),
    ("mxc://localhost/abc", "localhost", "abc"),
])
def test_media_uri_parses_valid_content_uris(text, server_name, media_id):
    media_uri = MediaUri.parse(text)
    assert media_uri == MediaUri(server_name, media_id)
    assert str(media_uri) == text


async def test_download_uses_authenticated_media_as_the_requesting_identity(aiohttp_server):
    requests = []

    async def download(request):
        requests.append((request.raw_path, request.path, request.query.getall("user_id"),
                         request.headers.get("Authorization")))
        return web.Response(body=b"bytes")

    application = web.Application()
    application.router.add_get(
        "/_matrix/client/v1/media/download/{server_name}/{media_id}", download)
    server = await aiohttp_server(application)
    async with aiohttp.ClientSession() as session:
        transport = _transport(session, server)
        data = await transport.download_media(
            media_uri=MediaUri("matrix.example.org:8448", "abc"), as_identity="@ai_nova:example.org",
            max_bytes=None)
    assert data == b"bytes"
    assert requests == [(
        "/_matrix/client/v1/media/download/matrix.example.org:8448/abc"
        "?user_id=@ai_nova:example.org",
        "/_matrix/client/v1/media/download/matrix.example.org:8448/abc",
        ["@ai_nova:example.org"], f"Bearer {AS_TOKEN}")]


async def test_download_reports_a_homeserver_refusal_as_missing(aiohttp_server):
    application = web.Application()
    application.router.add_get(
        "/_matrix/client/v1/media/download/{server_name}/{media_id}",
        _forbidden)
    server = await aiohttp_server(application)
    async with aiohttp.ClientSession() as session:
        data = await _transport(session, server).download_media(
            media_uri=MediaUri("example.org", "abc"), as_identity="@ai_nova:example.org",
            max_bytes=None)
    assert data is None


@pytest.mark.parametrize("hostile_uri", [uri for uri in HOSTILE_URIS if isinstance(uri, str)])
async def test_hostile_attachment_uri_never_reaches_the_homeserver(make_appservice, hostile_uri):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    event = await appservice.deliver(room, HUMAN, "m.room.message", {
        "msgtype": "m.file", "body": "loot", "url": hostile_uri})
    response = await appservice.client.get(
        f"/channels/{quote(room, safe='')}/events/{quote(event['event_id'], safe='')}/media",
        headers=auth)
    assert response.status == 404
    assert (await response.json())["code"] == "media_not_found"
    assert appservice.hs.media_downloads == []


async def test_attachment_is_downloaded_as_the_identity_that_reads_it(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    appservice.hs.media["mxc://example.org/report"] = b"%PDF"
    event = await appservice.deliver(room, HUMAN, "m.room.message", {
        "msgtype": "m.file", "body": "report.pdf", "url": "mxc://example.org/report",
        "info": {"mimetype": "application/pdf"}})
    response = await appservice.client.get(
        f"/channels/{quote(room, safe='')}/events/{quote(event['event_id'], safe='')}/media",
        headers=auth)
    assert await response.read() == b"%PDF"
    assert appservice.hs.media_downloads == [("mxc://example.org/report", mxid)]


async def test_avatar_is_downloaded_as_its_identity(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    appservice.hs.media["mxc://example.org/face"] = PNG
    appservice.hs.profiles.setdefault(mxid, {})["avatar_url"] = "mxc://example.org/face"
    response = await appservice.client.get("/identities/me/avatar", headers=auth)
    assert await response.read() == PNG
    assert appservice.hs.media_downloads == [("mxc://example.org/face", mxid)]


async def _forbidden(request):
    return web.json_response({"errcode": "M_FORBIDDEN"}, status=403)


def _transport(session, server):
    return MatrixAppserviceTransport(
        http_session=session, homeserver_url=str(server.make_url("")).rstrip("/"),
        application_service_token=AS_TOKEN, homeserver_token="unused")


async def test_attachment_over_the_limit_is_refused_and_left_out(make_appservice):
    appservice = await make_appservice(e2ee=False, limits={"media_bytes": 4})
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    appservice.hs.media["mxc://example.org/big"] = b"12345"
    event = await appservice.deliver(room, HUMAN, "m.room.message", {
        "msgtype": "m.file", "body": "big.bin", "url": "mxc://example.org/big"})
    response = await appservice.client.get(
        f"/channels/{quote(room, safe='')}/events/{quote(event['event_id'], safe='')}/media",
        headers=auth)
    assert response.status == 502
    assert (await response.json())["code"] == "media_too_large"
    listing = await appservice.client.get(
        f"/channels/{quote(room, safe='')}/events?unread=true&include=media", headers=auth)
    assert "data_base64" not in (await listing.json())["events"][0]["content"]


async def test_download_stops_reading_over_the_limit(aiohttp_server):
    async def download(request):
        response = web.StreamResponse()
        await response.prepare(request)
        for _ in range(4):
            await response.write(b"x" * 65536)
        return response

    application = web.Application()
    application.router.add_get(
        "/_matrix/client/v1/media/download/{server_name}/{media_id}", download)
    server = await aiohttp_server(application)
    async with aiohttp.ClientSession() as session:
        transport = _transport(session, server)
        with pytest.raises(MediaTooLarge):
            await transport.download_media(
                media_uri=MediaUri("example.org", "abc"), as_identity="@ai_nova:example.org",
                max_bytes=100_000)
        assert len(await transport.download_media(
            media_uri=MediaUri("example.org", "abc"), as_identity="@ai_nova:example.org",
            max_bytes=None)) == 4 * 65536


async def test_avatar_over_the_limit_is_refused(make_appservice):
    appservice = await make_appservice(e2ee=False, limits={"media_bytes": 4})
    mxid, auth = await appservice.register("nova")
    appservice.hs.media["mxc://example.org/face"] = PNG
    appservice.hs.profiles.setdefault(mxid, {})["avatar_url"] = "mxc://example.org/face"
    response = await appservice.client.get("/identities/me/avatar", headers=auth)
    assert response.status == 502
    assert (await response.json())["code"] == "media_too_large"
