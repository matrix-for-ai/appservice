import asyncio
import base64
import hashlib
import hmac
import json
import logging
import shutil
import ssl
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest
from aiohttp import web

from fakes import ADMIN_TOKEN, PNG, PNG_BASE64, FakeEngine, FakeSynthesizer, FakeTranscriber

OPENAPI = json.loads(
    (Path(__file__).resolve().parent.parent / "matrix_for_ai" / "api" / "openapi.json").read_text())
HUMAN = "@human:example.org"
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
PROBLEM = "application/problem+json"


def path(room: str) -> str:
    return f"/channels/{quote(room, safe='')}"


async def problem_of(response, status: int, code: str) -> dict:
    assert response.status == status, await response.text()
    assert response.content_type == PROBLEM
    body = await response.json()
    assert body["status"] == status and body["code"] == code
    assert body["type"] == "about:blank" and body["title"] and body["detail"]
    return body


async def unread_events(appservice, room, auth, query="") -> dict:
    return await read_events(appservice, room, auth, "?unread=true" + query.replace("?", "&"))


async def read_events(appservice, room, auth, query="") -> dict:
    response = await appservice.client.get(f"{path(room)}/events{query}", headers=auth)
    assert response.status == 200, await response.text()
    return await response.json()


async def test_every_documented_route_exists_and_every_route_is_documented(make_appservice):
    appservice = await make_appservice()
    routed = set()
    for route in appservice.client.app.router.routes():
        route_path = route.resource.canonical
        if route.method != "HEAD" and not route_path.startswith("/_matrix"):
            routed.add((route.method.lower(), route_path))
    documented = {
        (method, route_path) for route_path, operations in OPENAPI["paths"].items()
        for method in operations if method in ("get", "post", "put", "patch", "delete")}
    assert documented == routed


async def test_health_has_no_body(make_appservice):
    appservice = await make_appservice()
    response = await appservice.client.get("/health")
    assert response.status == 204 and await response.read() == b""


async def test_requests_without_a_valid_token_are_challenged(make_appservice):
    appservice = await make_appservice()
    for headers, challenge in (({}, "Bearer"),
                               ({"Authorization": "Bearer nope"}, 'Bearer error="invalid_token"')):
        response = await appservice.client.get("/channels", headers=headers)
        await problem_of(response, 401, "unauthorized")
        assert response.headers["WWW-Authenticate"] == challenge


async def test_unknown_routes_and_methods_answer_problems(make_appservice):
    appservice = await make_appservice()
    missing = await appservice.client.get("/nope")
    await problem_of(missing, 404, "not_found")
    assert missing.headers["X-API-Version"] == "1"
    response = await appservice.client.put("/channels")
    await problem_of(response, 405, "method_not_allowed")
    assert "GET" in response.headers["Allow"]


async def test_bodies_must_be_json_objects(make_appservice):
    appservice = await make_appservice(e2ee=False)
    _mxid, auth = await appservice.register("nova")
    events = f"{path('!r:example.org')}/events"
    await problem_of(await appservice.client.post(events, headers=auth), 400, "body_required")
    await problem_of(await appservice.client.post(
        events, data="hi", headers={**auth, "Content-Type": "text/plain"}),
        415, "unsupported_media_type")
    await problem_of(await appservice.client.post(
        events, data="{nope", headers={**auth, "Content-Type": "application/json"}),
        400, "bad_json")
    await problem_of(await appservice.client.post(events, json=[1], headers=auth), 400, "bad_json")


async def test_unknown_query_parameters_are_refused(make_appservice):
    appservice = await make_appservice()
    _mxid, auth = await appservice.register("nova")
    response = await appservice.client.get("/channels?ack=true", headers=auth)
    assert "ack" in (await problem_of(response, 400, "unknown_parameter"))["detail"]
    await problem_of(
        await appservice.client.get("/channels?unread=yes", headers=auth), 400, "invalid_parameter")
    await problem_of(
        await appservice.client.get("/channels?limit=0", headers=auth), 400, "invalid_parameter")
    await problem_of(
        await appservice.client.get("/channels?cursor=%%%", headers=auth), 400, "invalid_cursor")


async def test_registration_always_mints_a_new_identity(make_appservice):
    appservice = await make_appservice()
    response = await appservice.client.post("/identities", json={"name": "nova"})
    assert response.status == 201
    body = await response.json()
    assert set(body) == {"mxid", "localpart", "display_name", "api_token"}
    assert body["localpart"].startswith("ai_nova_")
    assert response.headers["Location"] == f"identities/{body['localpart']}"
    again = await appservice.client.post("/identities", json={"name": "nova"}, headers=ADMIN)
    assert again.status == 201
    assert (await again.json())["mxid"] != body["mxid"]
    await problem_of(await appservice.client.post("/identities", json={"name": "No!"}),
                     422, "invalid_name")
    await problem_of(await appservice.client.post(
        "/identities", json={"name": "x", "localpart": "ai_x"}, headers=ADMIN), 422, "unknown_field")


async def test_a_new_token_replaces_the_old_one(make_appservice):
    appservice = await make_appservice()
    mxid, auth = await appservice.register("nova")
    localpart = mxid[1:].split(":")[0]
    response = await appservice.client.post("/identities/me/token", headers=auth)
    assert response.status == 200
    body = await response.json()
    assert body["mxid"] == mxid
    await problem_of(await appservice.client.get("/identities/me", headers=auth), 401, "unauthorized")
    fresh = {"Authorization": f"Bearer {body['api_token']}"}
    assert (await appservice.client.get("/identities/me", headers=fresh)).status == 200
    response = await appservice.client.post(f"/identities/{localpart}/token", headers=ADMIN)
    assert response.status == 200
    await problem_of(await appservice.client.get("/identities/me", headers=fresh), 401, "unauthorized")


async def test_a_token_for_another_identity_needs_the_admin(make_appservice):
    appservice = await make_appservice()
    bob, _ = await appservice.register("bob")
    _alice, auth = await appservice.register("alice")
    target = f"/identities/{bob[1:].split(':')[0]}/token"
    response = await appservice.client.post(target)
    await problem_of(response, 401, "unauthorized")
    assert response.headers["WWW-Authenticate"] == "Bearer"
    await problem_of(await appservice.client.post(target, headers=auth), 403, "forbidden")
    await problem_of(await appservice.client.post(
        "/identities/ai_nobody/token", headers=ADMIN), 404, "identity_not_found")


async def test_closed_signup_leaves_identities_to_the_admin(make_appservice):
    appservice = await make_appservice(open_signup=False)
    await problem_of(await appservice.client.post("/identities", json={"name": "nova"}),
                     403, "signup_closed")
    response = await appservice.client.post("/identities", json={"name": "nova"}, headers=ADMIN)
    assert response.status == 201


async def test_clear_room_round_trip_reads_without_side_effects_until_the_receipt(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    response = await appservice.client.post(
        "/channels", json={"name": "lobby", "invite": [HUMAN], "direct": False}, headers=auth)
    assert response.status == 201
    created = await response.json()
    room = created["room"]
    assert response.headers["Location"] == f"channels/{quote(room, safe='')}"
    assert created == {"room": room, "name": "lobby", "topic": None, "direct": False,
                       "public": False, "encrypted": False, "invited": [HUMAN]}
    await appservice.join(room, mxid)

    sent = await appservice.client.post(
        f"{path(room)}/events", json={"type": "text", "content": {"text": "hi"}}, headers=auth)
    assert sent.status == 201
    body = await sent.json()
    assert body == {"event_id": body["event_id"], "type": "text", "encrypted": False}
    assert sent.headers["Location"] == f"events/{quote(body['event_id'], safe='')}"
    assert appservice.hs.events[body["event_id"]]["content"] == {"msgtype": "m.text", "body": "hi"}

    incoming = await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "yo"})
    listed = await (await appservice.client.get("/channels?unread=true", headers=auth)).json()
    assert [(c["room"], c["unread"]) for c in listed["channels"]] == [(room, 1)]
    assert listed["next_cursor"] is None
    for _ in range(2):
        read = await unread_events(appservice, room, auth)
        assert [(m["type"], m["content"]["text"]) for m in read["events"]] == [("text", "yo")]
    assert read["events"][-1]["event_id"] == incoming["event_id"] and not read["has_more"]
    assert (mxid, room) not in appservice.hs.read_markers

    receipt = await appservice.client.put(
        f"{path(room)}/receipt", json={"event_id": incoming["event_id"]}, headers=auth)
    assert receipt.status == 204
    assert appservice.hs.read_markers[(mxid, room)] == incoming["event_id"]
    assert (await unread_events(appservice, room, auth))["events"] == []
    listed = await (await appservice.client.get("/channels?unread=true", headers=auth)).json()
    assert listed["channels"] == []
    channel = await (await appservice.client.get(path(room), headers=auth)).json()
    assert channel["unread"] == 0 and channel["last_unread"] is None


async def test_a_receipt_covers_what_was_read_and_not_what_arrived_after(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    first = await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "1"})
    await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "2"})
    await appservice.client.put(f"{path(room)}/receipt", json={"event_id": first["event_id"]},
                          headers=auth)
    unread = (await unread_events(appservice, room, auth))["events"]
    assert [m["content"]["text"] for m in unread] == ["2"]
    again = await appservice.client.put(
        f"{path(room)}/receipt", json={"event_id": first["event_id"]}, headers=auth)
    assert again.status == 204
    await problem_of(await appservice.client.put(
        f"{path(room)}/receipt", json={"event_id": "$missing:example.org"}, headers=auth),
        404, "event_not_found")
    await problem_of(await appservice.client.put(
        f"{path(room)}/receipt", json={"event_id": "nope"}, headers=auth),
        422, "invalid_event_id")


async def test_unread_messages_come_a_page_at_a_time_until_marked_read(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    for text in "abc":
        await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": text})
    page = await unread_events(appservice, room, auth, "?limit=2")
    assert [m["content"]["text"] for m in page["events"]] == ["a", "b"] and page["has_more"]
    await appservice.client.put(f"{path(room)}/receipt",
                          json={"event_id": page["events"][-1]["event_id"]}, headers=auth)
    page = await unread_events(appservice, room, auth, "?limit=2")
    assert [m["content"]["text"] for m in page["events"]] == ["c"] and not page["has_more"]


async def test_undecryptable_messages_do_not_block_the_pages_behind_them(make_appservice):
    appservice = await make_appservice()
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=True)
    await appservice.join(room, mxid)
    for _ in range(2):
        await appservice.push(appservice.hs.add_event(room, HUMAN, "m.room.encrypted", {"ciphertext": "{"}))
    clear = await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "ok"})
    engine = await appservice.encryption.ensure(mxid[1:].split(":")[0])
    first = await unread_events(appservice, room, auth, "?limit=2")
    assert [m["type"] for m in first["events"]] == ["undecryptable"] * 2
    assert first["has_more"]
    assert engine.key_receptions == 1
    second = await unread_events(appservice, room, auth, "?limit=2")
    assert [m["event_id"] for m in second["events"]] == [clear["event_id"]]
    assert not second["has_more"]
    assert engine.key_receptions == 2


async def test_events_that_are_not_messages_leave_the_unread(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "hi"})
    await appservice.deliver(room, HUMAN, "m.room.message",
                      {"m.relates_to": {"rel_type": "m.reference", "event_id": "$x"}})
    page = await unread_events(appservice, room, auth)
    assert [m["content"]["text"] for m in page["events"]] == ["hi"]
    channel = await (await appservice.client.get(path(room), headers=auth)).json()
    assert channel["unread"] == 1


async def test_history_reads_back_and_forth_from_any_message(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    sent = [await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": t})
            for t in "abcde"]
    await appservice.client.put(f"{path(room)}/receipt", json={"event_id": sent[-1]["event_id"]},
                          headers=auth)
    latest = await read_events(appservice, room, auth, "?limit=2")
    assert [m["content"]["text"] for m in latest["events"]] == ["d", "e"] and latest["has_more"]
    earlier = await read_events(
        appservice, room, auth, f"?limit=2&before={quote(latest['events'][0]['event_id'])}")
    assert [m["content"]["text"] for m in earlier["events"]] == ["b", "c"] and earlier["has_more"]
    later = await read_events(appservice, room, auth, f"?after={quote(sent[2]['event_id'])}")
    assert [m["content"]["text"] for m in later["events"]] == ["d", "e"] and not later["has_more"]
    everything = await read_events(appservice, room, auth)
    assert [m["content"]["text"] for m in everything["events"]] == list("abcde")
    assert not everything["has_more"]


async def test_history_skips_what_is_not_a_message_and_still_fills_the_page(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "a"})
    for _ in range(3):
        appservice.hs.add_event(room, HUMAN, "m.reaction", {})
    appservice.hs.add_event(room, HUMAN, "m.room.message",
                     {"m.relates_to": {"rel_type": "m.reference", "event_id": "$x"}})
    await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "b"})
    page = await read_events(appservice, room, auth, "?limit=2")
    assert [m["content"]["text"] for m in page["events"]] == ["a", "b"]


async def test_history_options_exclude_each_other_and_need_an_event_of_the_room(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    event = quote((await appservice.deliver(room, HUMAN, "m.room.message",
                                     {"msgtype": "m.text", "body": "a"}))["event_id"])
    for query in (f"?before={event}&after={event}", f"?unread=true&before={event}",
                  "?before=nope"):
        await problem_of(await appservice.client.get(f"{path(room)}/events{query}", headers=auth),
                         400, "invalid_parameter")
    await problem_of(await appservice.client.get(
        f"{path(room)}/events?before={quote('$missing:example.org')}", headers=auth),
        404, "event_not_found")


async def test_channels_page_with_a_cursor(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    rooms = []
    for _ in range(3):
        room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
        await appservice.join(room, mxid)
        await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "x"})
        rooms.append(room)
    first = await (await appservice.client.get("/channels?limit=2", headers=auth)).json()
    second = await (await appservice.client.get(
        f"/channels?limit=2&cursor={first['next_cursor']}", headers=auth)).json()
    listed = [c["room"] for c in first["channels"] + second["channels"]]
    assert listed == list(reversed(rooms)) and second["next_cursor"] is None


async def test_a_channel_that_moves_while_paging_does_not_repeat(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    rooms = []
    for _ in range(3):
        room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
        await appservice.join(room, mxid)
        await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "x"})
        rooms.append(room)
    first = await (await appservice.client.get("/channels?limit=1", headers=auth)).json()
    await appservice.deliver(rooms[0], HUMAN, "m.room.message", {"msgtype": "m.text", "body": "y"})
    second = await (await appservice.client.get(
        f"/channels?limit=5&cursor={first['next_cursor']}", headers=auth)).json()
    assert [c["room"] for c in first["channels"] + second["channels"]] == [rooms[2], rooms[1]]


async def test_the_newest_unread_shows_in_the_channel(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "a"})
    newest = await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "b"})
    channel = await (await appservice.client.get(path(room), headers=auth)).json()
    assert channel["unread"] == 2 and channel["last_unread"] == {
        "event_id": newest["event_id"], "sender": HUMAN, "ts": newest["origin_server_ts"]}
    assert channel["encrypted"] is False


async def test_a_channel_says_whether_it_is_encrypted(make_appservice):
    appservice = await make_appservice()
    mxid, auth = await appservice.register("nova")
    plain = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    secret = appservice.hs.create_room(HUMAN, [mxid], encrypted=True)
    for room in (plain, secret):
        await appservice.join(room, mxid)
        await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "x"})
    listed = await (await appservice.client.get("/channels", headers=auth)).json()
    assert {c["room"]: c["encrypted"] for c in listed["channels"]} == {plain: False, secret: True}
    assert (await (await appservice.client.get(path(secret), headers=auth)).json())["encrypted"] is True


async def test_members_come_in_pages(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid, "@zed:example.org"], encrypted=False)
    await appservice.join(room, mxid)
    appservice.hs.rooms[room]["members"].add("@zed:example.org")
    first = await (await appservice.client.get(f"{path(room)}/members?limit=2", headers=auth)).json()
    second = await (await appservice.client.get(
        f"{path(room)}/members?limit=2&cursor={first['next_cursor']}", headers=auth)).json()
    assert [m["user"] for m in first["members"] + second["members"]] == sorted(
        [HUMAN, mxid, "@zed:example.org"])
    assert second["next_cursor"] is None


async def test_reading_a_channel_you_are_not_in_is_not_found(make_appservice):
    appservice = await make_appservice(e2ee=False)
    _mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [], encrypted=False)
    await problem_of(await appservice.client.get(path(room), headers=auth), 404, "not_member")
    await problem_of(await appservice.client.get(f"{path(room)}/events", headers=auth),
                     404, "not_member")
    await problem_of(await appservice.client.delete(path(room), headers=auth), 404, "not_member")


async def test_encrypted_dm_between_two_identities(make_appservice):
    appservice = await make_appservice()
    alice, alice_auth = await appservice.register("alice")
    bob, bob_auth = await appservice.register("bob")
    created = await (await appservice.client.post(
        "/channels", json={"invite": [bob]}, headers=alice_auth)).json()
    assert created["encrypted"] and created["direct"]
    room = created["room"]
    await appservice.join(room, alice)
    await appservice.join(room, bob)

    sent = await (await appservice.client.post(
        f"{path(room)}/events", json={"type": "text", "content": {"text": "secret"}},
        headers=alice_auth)).json()
    assert sent["encrypted"] is True
    stored = appservice.hs.events[sent["event_id"]]
    assert stored["type"] == "m.room.encrypted"
    await appservice.push(stored)
    read = await unread_events(appservice, room, bob_auth)
    assert [(m["sender"], m["content"]["text"]) for m in read["events"]] == [(alice, "secret")]


async def test_undecryptable_messages_show_once_and_return_when_they_decrypt(make_appservice):
    appservice = await make_appservice()
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=True)
    await appservice.join(room, mxid)
    locked = appservice.hs.add_event(room, HUMAN, "m.room.encrypted", {"ciphertext": "{not json"})
    await appservice.push(locked)
    clear = await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "ok"})
    read = await unread_events(appservice, room, auth)
    assert [m["type"] for m in read["events"]] == ["undecryptable", "text"]
    await appservice.client.put(
        f"{path(room)}/receipt", json={"event_id": clear["event_id"]}, headers=auth)
    assert (await unread_events(appservice, room, auth))["events"] == []
    channel = await (await appservice.client.get(path(room), headers=auth)).json()
    assert channel["unread"] == 1
    appservice.hs.events[locked["event_id"]]["content"] = {
        "ciphertext": json.dumps({"type": "m.room.message",
                                  "content": {"msgtype": "m.text", "body": "late"}})}
    left = await unread_events(appservice, room, auth)
    assert [(m["event_id"], m["content"]["text"]) for m in left["events"]] == [
        (locked["event_id"], "late")]



async def test_a_deleted_message_is_not_a_message(make_appservice):
    appservice = await make_appservice()
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=True)
    await appservice.join(room, mxid)
    deleted = appservice.hs.add_event(room, HUMAN, "m.room.encrypted", {})
    deleted["unsigned"] = {"redacted_because": {"type": "m.room.redaction", "sender": HUMAN}}
    await appservice.push(deleted)
    kept = await appservice.deliver(room, HUMAN, "m.room.encrypted", {"ciphertext": json.dumps(
        {"type": "m.room.message", "content": {"msgtype": "m.text", "body": "still here"}})})
    read = await unread_events(appservice, room, auth)
    assert [(m["event_id"], m["content"]["text"]) for m in read["events"]] == [
        (kept["event_id"], "still here")]
    history = await read_events(appservice, room, auth)
    assert deleted["event_id"] not in [m["event_id"] for m in history["events"]]

async def test_encrypted_room_without_e2ee_is_refused_not_sent_in_clear(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=True)
    response = await appservice.client.post(
        f"{path(room)}/events", json={"type": "text", "content": {"text": "x"}}, headers=auth)
    await problem_of(response, 501, "e2ee_unavailable")
    assert appservice.hs.events == {}


async def test_send_validation_is_unprocessable(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    for payload, code in (
            ({"content": {"text": "hi"}}, "type_required"),
            ({"type": "sticker", "content": {}}, "invalid_type"),
            ({"type": "text"}, "content_required"),
            ({"type": "text", "content": "hi"}, "invalid_content"),
            ({"type": "text", "text": "hi"}, "unknown_field"),
            ({"type": "text", "content": {"text": "hi", "audio": True}}, "unknown_field"),
            ({"type": "text", "content": {}}, "text_required"),
            ({"type": "text", "content": {"text": 1}}, "invalid_text"),
            ({"type": "audio", "content": {}}, "transcript_required"),
            ({"type": "audio", "content": {"transcript": 1}}, "invalid_transcript"),
            ({"type": "audio", "content": {"transcript": "x", "tts": "fast"}}, "invalid_tts"),
            ({"type": "audio", "content": {"transcript": "x", "tts": {"input": "y"}}},
             "reserved_option"),
            ({"type": "audio", "content": {"transcript": "x", "tts": {"blob": "x" * 5000}}},
             "options_too_large"),
            ({"type": "image", "content": {}}, "data_base64_required"),
            ({"type": "image", "content": {"data_base64": "***"}}, "invalid_data_base64"),
            ({"type": "image", "content": {"data_base64": PNG_BASE64, "mimetype": 1}},
             "invalid_mimetype"),
            ({"type": "image", "content": {"data_base64": PNG_BASE64, "filename": 1}},
             "invalid_filename"),
            ({"type": "image", "content": {"data_base64": PNG_BASE64, "w": 0}}, "invalid_w"),
            ({"type": "image", "content": {"data_base64": PNG_BASE64, "h": "x"}}, "invalid_h"),
            ({"type": "image", "content": {"data_base64": PNG_BASE64, "mimetype": "image/gif"}},
             "mimetype_mismatch"),
            ({"type": "poll_response", "content": {"poll_event_id": "x", "answers": ["a"]}},
             "invalid_poll_event_id"),
            ({"type": "poll_response", "content": {"poll_event_id": "$p", "answers": []}},
             "invalid_answers")):
        await problem_of(await appservice.client.post(
            f"{path(room)}/events", json=payload, headers=auth), 422, code)


async def test_audio_without_tts_is_an_api_error_and_never_reaches_the_room(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    response = await appservice.client.post(
        f"{path(room)}/events", json={"type": "audio", "content": {"transcript": "hola"}},
        headers=auth)
    await problem_of(response, 501, "tts_not_configured")
    assert appservice.hs.events == {}


async def test_audio_while_tts_is_down_is_unavailable_and_sends_nothing(make_appservice):
    appservice = await make_appservice(e2ee=False, speech={"tts": FakeSynthesizer(fail=True), "stt": None})
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    response = await appservice.client.post(
        f"{path(room)}/events", json={"type": "audio", "content": {"transcript": "hola"}},
        headers=auth)
    await problem_of(response, 503, "tts_unavailable")
    assert appservice.hs.events == {}


async def test_voice_message_uses_profile_options_with_per_send_overrides(make_appservice):
    synthesizer = FakeSynthesizer()
    appservice = await make_appservice(speech={"tts": synthesizer, "stt": None})
    mxid, auth = await appservice.register("nova")
    patched = await appservice.client.patch(
        "/identities/me", json={"tts": {"voice": "em_alex", "speed": 1.1}},
        headers={**auth, "Content-Type": "application/merge-patch+json"})
    assert patched.status == 200
    assert (await patched.json())["tts"] == {"voice": "em_alex", "speed": 1.1}
    room = (await (await appservice.client.post(
        "/channels", json={"invite": [HUMAN]}, headers=auth)).json())["room"]

    sent = await (await appservice.client.post(
        f"{path(room)}/events",
        json={"type": "audio", "content": {
            "transcript": "hola", "tts": {"speed": None, "voice": "ef_dora"}}},
        headers=auth)).json()
    assert sent["type"] == "audio" and sent["encrypted"]
    assert synthesizer.calls == [("hola", {"voice": "ef_dora"})]
    content = json.loads(appservice.hs.events[sent["event_id"]]["content"]["ciphertext"])["content"]
    assert content["org.matrix.msc3245.voice"] == {}
    assert content["org.matrix.msc1767.text"] == "hola"
    assert "file" in content and "url" not in content
    identity = await (await appservice.client.get("/identities/me", headers=auth)).json()
    assert identity == {
        "mxid": mxid, "localpart": mxid[1:].split(":")[0],
        "matrix": {"display_name": "Nova", "avatar_url": None},
        "tts": {"voice": "em_alex", "speed": 1.1}, "stt": {}, "transcribe_audio": True}


async def test_patch_takes_merge_patch_or_json_only(make_appservice):
    appservice = await make_appservice()
    _mxid, auth = await appservice.register("nova")
    response = await appservice.client.patch(
        "/identities/me", data="tts=x", headers={**auth, "Content-Type": "text/plain"})
    await problem_of(response, 415, "unsupported_media_type")
    assert "application/merge-patch+json" in response.headers["Accept-Patch"]


async def test_profile_validation(make_appservice):
    appservice = await make_appservice()
    _mxid, auth = await appservice.register("nova")
    cases = [
        ({"voice": "em_alex"}, "unknown_field"),
        ({"mxid": "@x:example.org"}, "read_only_field"),
        ({"localpart": "x"}, "read_only_field"),
        ({"tts": {"input": "x"}}, "reserved_option"),
        ({"stt": {"file": "x"}}, "reserved_option"),
        ({"tts": "em_alex"}, "invalid_tts"),
        ({"stt": ["es"]}, "invalid_stt"),
        ({"tts": {"blob": "x" * 5000}}, "options_too_large"),
        ({"matrix": None}, "invalid_matrix"),
        ({"matrix": {"nick": "x"}}, "unknown_field"),
        ({"matrix": {"avatar_url": "mxc://example.org/a"}}, "read_only_field"),
        ({"matrix": {"display_name": " "}}, "invalid_display_name"),
        ({"transcribe_audio": "no"}, "invalid_transcribe_audio"),
    ]
    for payload, code in cases:
        await problem_of(await appservice.client.patch("/identities/me", json=payload, headers=auth),
                         422, code)


async def test_patch_merges_at_any_depth_and_null_removes(make_appservice):
    appservice = await make_appservice()
    mxid, auth = await appservice.register("nova")
    await appservice.client.patch("/identities/me", json={"stt": {"language": "es"}}, headers=auth)
    identity = await (await appservice.client.patch(
        "/identities/me", json={"stt": {"prompt": "Nova"}, "matrix": {"display_name": "Nova N"}},
        headers=auth)).json()
    assert identity["stt"] == {"language": "es", "prompt": "Nova"}
    assert identity["matrix"]["display_name"] == "Nova N"
    assert appservice.hs.profiles[mxid]["displayname"] == "Nova N"
    await appservice.client.patch("/identities/me", json={"tts": {"extra": {"a": 1, "b": 2}}},
                           headers=auth)
    identity = await (await appservice.client.patch(
        "/identities/me", json={"tts": {"extra": {"a": None, "c": 3}}}, headers=auth)).json()
    assert identity["tts"] == {"extra": {"b": 2, "c": 3}}
    identity = await (await appservice.client.patch(
        "/identities/me", json={"stt": None, "matrix": {"display_name": None}},
        headers=auth)).json()
    assert identity["stt"] == {} and identity["matrix"]["display_name"] is None
    assert "displayname" not in appservice.hs.profiles[mxid]


async def test_the_avatar_is_its_own_resource(make_appservice):
    appservice = await make_appservice()
    mxid, auth = await appservice.register("nova")
    avatar = "/identities/me/avatar"
    await problem_of(await appservice.client.get(avatar, headers=auth), 404, "avatar_not_found")
    await problem_of(await appservice.client.delete(avatar, headers=auth), 404, "avatar_not_found")
    png = {**auth, "Content-Type": "image/png"}
    await problem_of(await appservice.client.put(avatar, headers=png), 400, "body_required")
    await problem_of(await appservice.client.put(
        avatar, data=PNG, headers={**auth, "Content-Type": "image/svg+xml"}),
        415, "unsupported_media_type")
    await problem_of(await appservice.client.put(avatar, data=b"not an image", headers=png),
                     422, "invalid_avatar")
    await problem_of(await appservice.client.put(
        avatar, data=PNG, headers={**auth, "Content-Type": "image/gif"}),
        422, "mimetype_mismatch")
    created = await appservice.client.put(avatar, data=PNG, headers=png)
    assert created.status == 201 and created.headers["Location"] == "avatar"
    avatar_url = (await created.json())["avatar_url"]
    identity = await (await appservice.client.get("/identities/me", headers=auth)).json()
    assert identity["matrix"]["avatar_url"] == avatar_url
    downloaded = await appservice.client.get(avatar, headers=auth)
    assert await downloaded.read() == PNG and downloaded.content_type == "image/png"
    replaced = await appservice.client.put(avatar, data=PNG, headers=png)
    assert replaced.status == 200
    assert (await appservice.client.delete(avatar, headers=auth)).status == 204
    assert "avatar_url" not in appservice.hs.profiles[mxid]
    await problem_of(await appservice.client.get(
        "/identities/ai_ghost/avatar", headers=ADMIN), 404, "identity_not_found")


async def test_a_token_cannot_patch_another_identity_but_the_admin_can(make_appservice):
    appservice = await make_appservice()
    _alice, alice_auth = await appservice.register("alice")
    bob, _bob_auth = await appservice.register("bob")
    localpart = bob[1:].split(":")[0]
    await problem_of(await appservice.client.patch(
        f"/identities/{localpart}", json={"tts": {"voice": "x"}}, headers=alice_auth),
        403, "forbidden")
    response = await appservice.client.patch(
        f"/identities/{localpart}", json={"tts": {"voice": "x"}}, headers=ADMIN)
    assert response.status == 200
    await problem_of(await appservice.client.get("/identities/ai_ghost", headers=ADMIN),
                     404, "identity_not_found")


async def test_incoming_audio_without_stt_is_marked_unavailable(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    uri = "mxc://example.org/voice"
    appservice.hs.media[uri] = b"opus"
    await appservice.deliver(room, HUMAN, "m.room.message", {
        "msgtype": "m.audio", "body": "voice", "url": uri, "info": {"mimetype": "audio/ogg"}})
    message = (await unread_events(appservice, room, auth))["events"][0]
    assert message["type"] == "audio" and "transcript" not in message["content"]


async def test_incoming_encrypted_audio_is_transcribed_with_the_stt_profile(make_appservice):
    transcriber = FakeTranscriber(text="buenos días")
    appservice = await make_appservice(speech={"tts": None, "stt": transcriber})
    mxid, auth = await appservice.register("nova")
    await appservice.client.patch("/identities/me", json={"stt": {"language": "es"}}, headers=auth)
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=True)
    await appservice.join(room, mxid)
    engine = FakeEngine(appservice.hs, appservice.hs.new_id("@"))
    encrypted_file = await engine.upload_encrypted(data=b"opus", mime_type="audio/ogg", filename="v")
    event_id = await engine.send_event(room_id=room, event_type="m.room.message", content={
        "msgtype": "m.audio", "body": "nota.ogg", "file": encrypted_file,
        "info": {"mimetype": "audio/ogg"}})
    await appservice.push(appservice.hs.events[event_id])
    message = (await unread_events(appservice, room, auth, "?include=media"))["events"][0]
    assert message["content"]["transcript"] == "buenos días"
    assert base64.b64decode(message["content"]["data_base64"]) == b"opus"
    assert transcriber.calls == [(b"opus", "audio/ogg", {"language": "es"})]
    media = await appservice.client.get(
        f"{path(room)}/events/{quote(event_id, safe='')}/media", headers=auth)
    assert await media.read() == b"opus" and media.content_type == "audio/ogg"
    assert media.headers["Content-Disposition"] == (
        "attachment; filename=\"nota.ogg\"; filename*=UTF-8''nota.ogg")
    assert media.headers["Cache-Control"] == "private"


async def test_transcribe_audio_false_skips_the_stt_and_null_restores_it(make_appservice):
    transcriber = FakeTranscriber(text="hola")
    appservice = await make_appservice(e2ee=False, speech={"tts": None, "stt": transcriber})
    mxid, auth = await appservice.register("nova")
    identity = await (await appservice.client.patch(
        "/identities/me", json={"transcribe_audio": False}, headers=auth)).json()
    assert identity["transcribe_audio"] is False
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    appservice.hs.media["mxc://example.org/voice"] = b"opus"
    audio = {"msgtype": "m.audio", "body": "voice", "url": "mxc://example.org/voice",
             "info": {"mimetype": "audio/ogg"}}
    await appservice.deliver(room, HUMAN, "m.room.message", audio)
    message = (await unread_events(appservice, room, auth))["events"][0]
    assert "transcript" not in message["content"] and transcriber.calls == []
    identity = await (await appservice.client.patch(
        "/identities/me", json={"transcribe_audio": None}, headers=auth)).json()
    assert identity["transcribe_audio"] is True
    message = (await unread_events(appservice, room, auth))["events"][0]
    assert message["content"]["transcript"] == "hola"


async def test_image_send_and_media_download(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    sent = await (await appservice.client.post(f"{path(room)}/events", json={
        "type": "image", "content": {"data_base64": PNG_BASE64, "mimetype": "image/png",
                                     "filename": "año.png", "w": 2, "h": 3}},
        headers=auth)).json()
    assert sent["type"] == "image"
    content = appservice.hs.events[sent["event_id"]]["content"]
    assert content["msgtype"] == "m.image" and content["info"]["w"] == 2
    media = await appservice.client.get(
        f"{path(room)}/events/{quote(sent['event_id'], safe='')}/media", headers=auth)
    assert await media.read() == PNG
    assert media.headers["Content-Disposition"] == (
        "attachment; filename=\"a_o.png\"; filename*=UTF-8''a%C3%B1o.png")


async def test_poll_vote_and_tally_in_clear_and_encrypted_rooms(make_appservice):
    for encrypted in (False, True):
        appservice = await make_appservice(e2ee=encrypted)
        mxid, auth = await appservice.register("nova")
        room = appservice.hs.create_room(HUMAN, [mxid], encrypted=encrypted)
        await appservice.join(room, mxid)
        poll = await appservice.deliver(room, HUMAN, "org.matrix.msc3381.poll.start", {
            "org.matrix.msc3381.poll.start": {
                "question": {"body": "¿Cena?"},
                "answers": [{"id": "a", "body": "pizza"}, {"id": "b", "body": "sushi"}]}})
        voted = await appservice.client.post(f"{path(room)}/events", json={
            "type": "poll_response",
            "content": {"poll_event_id": poll["event_id"], "answers": ["b"]}}, headers=auth)
        assert (await voted.json()) | {"event_id": None} == {
            "event_id": None, "type": "poll_response", "encrypted": encrypted}
        poll_path = f"{path(room)}/events/{quote(poll['event_id'], safe='')}"
        record = await (await appservice.client.get(f"{poll_path}?include=poll", headers=auth)).json()
        assert record["type"] == "poll" and record["content"]["results"] == {
            "counts": {"a": 0, "b": 1}, "votes": {mxid: ["b"]}, "total_voters": 1,
            "closed": False}
        await problem_of(await appservice.client.get(f"{poll_path}?include=everything", headers=auth),
                         400, "invalid_parameter")


async def test_capabilities(make_appservice):
    appservice = await make_appservice()
    assert await (await appservice.client.get("/capabilities")).json() == {
        "api_version": 1,
        "open_signup": True,
        "webhooks": False,
        "e2ee": True,
        "tts": {"available": False, "info": None},
        "stt": {"available": False, "info": None}}
    appservice = await make_appservice(e2ee=False, speech={"tts": FakeSynthesizer(), "stt": None})
    capabilities = await (await appservice.client.get("/capabilities")).json()
    assert capabilities["e2ee"] is False
    assert capabilities["tts"] == {"available": True, "info": {"voices": ["fake"]}}
    appservice = await make_appservice(open_signup=False, webhooks={"allowed_targets": ["*"]})
    capabilities = await (await appservice.client.get("/capabilities")).json()
    assert capabilities["open_signup"] is False and capabilities["webhooks"] is True


async def test_webhooks_are_disabled_by_default(make_appservice):
    appservice = await make_appservice()
    _mxid, auth = await appservice.register("nova")
    await problem_of(await appservice.client.put(
        "/identities/me/webhook", json={"url": "http://192.168.1.2:9000/"}, headers=auth),
        501, "webhooks_disabled")


async def test_webhook_lifecycle(make_appservice):
    appservice = await make_appservice(webhooks={"allowed_targets": ["192.168.1.2:9000"]})
    _mxid, auth = await appservice.register("nova")
    hook = "/identities/me/webhook"
    await problem_of(await appservice.client.get(hook, headers=auth), 404, "webhook_not_found")
    created = await appservice.client.put(hook, json={"url": "http://192.168.1.2:9000/"}, headers=auth)
    assert created.status == 201 and created.headers["Location"] == "webhook"
    first_secret = (await created.json())["secret"]
    rotated = await appservice.client.put(hook, json={"url": "http://192.168.1.2:9000/"}, headers=auth)
    assert rotated.status == 200 and (await rotated.json())["secret"] != first_secret
    assert await (await appservice.client.get(hook, headers=auth)).json() == {
        "url": "http://192.168.1.2:9000/"}
    deleted = await appservice.client.delete(hook, headers=auth)
    assert deleted.status == 204
    await problem_of(await appservice.client.delete(hook, headers=auth), 404, "webhook_not_found")


async def test_webhook_allowlist(make_appservice):
    appservice = await make_appservice(webhooks={"allowed_targets": ["192.168.1.2:9000"]})
    _mxid, auth = await appservice.register("nova")
    for url in ("http://192.168.1.2:9001/", "http://127.0.0.1:9000/"):
        await problem_of(await appservice.client.put(
            "/identities/me/webhook", json={"url": url}, headers=auth), 422, "url_not_allowed")
    for url in ("http://host:9000/", "ftp://192.168.1.2:9000/", "http://u:p@192.168.1.2:9000/"):
        await problem_of(await appservice.client.put(
            "/identities/me/webhook", json={"url": url}, headers=auth), 422, "invalid_url")


async def test_webhook_on_loopback_only_when_listed(make_appservice):
    appservice = await make_appservice(webhooks={"allowed_targets": ["127.0.0.1:8794"]})
    _mxid, auth = await appservice.register("nova")
    allowed = await appservice.client.put(
        "/identities/me/webhook", json={"url": "http://127.0.0.1:8794/"}, headers=auth)
    assert allowed.status == 201
    rejected = await appservice.client.put(
        "/identities/me/webhook", json={"url": "http://127.0.0.1:8795/"}, headers=auth)
    assert "allowlist" in (await problem_of(rejected, 422, "url_not_allowed"))["detail"]


@pytest.mark.parametrize("allowed, url, accepted", [
    ("192.168.1.0/24:9000", "http://192.168.1.77:9000/", True),
    ("192.168.1.0/24:9000", "http://192.168.2.77:9000/", False),
    ("192.168.1.0/24:9000", "http://192.168.1.77:9001/", False),
    ("192.168.1.2:*", "http://192.168.1.2:31337/", True),
    ("192.168.1.2:*", "http://192.168.1.3:31337/", False),
    ("*:9000", "http://10.1.2.3:9000/", True),
    ("[fd00::/8]:9000", "http://[fd12::1]:9000/", True),
    ("*", "http://0.0.0.0:9000/", False),
    ("*", "http://127.0.0.1:9000/", False),
    ("*", "http://[::1]:9000/", False),
    ("*", "http://169.254.169.254:80/", False),
    ("*", "http://[fe80::1]:9000/", False),
    ("*", "http://[::ffff:169.254.169.254]:80/", False),
    ("[::/0]:*", "http://[fe80::1]:9000/", False),
    ("[::/0]:*", "http://[::1]:9000/", False),
    ("0.0.0.0/0:*", "http://127.0.0.1:9000/", False),
    ("0.0.0.0/0:*", "http://169.254.169.254:80/", False),
    ("127.0.0.0/8:*", "http://127.0.0.1:9000/", True),
    ("169.254.1.3:8091", "http://169.254.1.3:8091/", True),
    ("[fe80::/64]:9000", "http://[fe80::1]:9000/", True),
    ("*", "http://192.168.1.2:7700/", False),
    ("*", "http://192.168.1.2:7701/", False),
    ("192.168.1.2:*", "http://192.168.1.2:7700/", False),
    ("192.168.1.2:7700", "http://192.168.1.2:7700/", True),
    ("*", "http://192.168.1.2:9000/", True),
])
async def test_webhook_allowlist_patterns(make_appservice, allowed, url, accepted):
    appservice = await make_appservice(webhooks={"allowed_targets": [allowed]})
    _mxid, auth = await appservice.register("nova")
    response = await appservice.client.put("/identities/me/webhook", json={"url": url}, headers=auth)
    assert (response.status == 201) is accepted, await response.text()


@pytest.mark.parametrize("names, accepted, code", [
    ({"hooks.lan": ["192.168.1.2"]}, True, None),
    ({"hooks.lan": ["192.168.1.2", "fd00::2"]}, True, None),
    ({"hooks.lan": ["192.168.1.2", "127.0.0.1"]}, False, "url_not_allowed"),
    ({"hooks.lan": ["::ffff:127.0.0.1"]}, False, "url_not_allowed"),
    ({}, False, "invalid_url"),
])
async def test_a_webhook_name_is_judged_by_every_address_it_resolves_to(
        make_appservice, dns, names, accepted, code):
    dns.update(names)
    appservice = await make_appservice(webhooks={"allowed_targets": ["192.168.1.2:9000", "[fd00::/8]:9000"]})
    _mxid, auth = await appservice.register("nova")
    response = await appservice.client.put(
        "/identities/me/webhook", json={"url": "http://hooks.lan:9000/"}, headers=auth)
    if accepted:
        assert response.status == 201, await response.text()
    else:
        await problem_of(response, 422, code)


class Receiver:
    def __init__(self) -> None:
        self.rings = asyncio.Queue()

    async def handle(self, request):
        await self.rings.put((await request.read(), request.headers))
        return web.Response(status=204)

    async def next_ring(self):
        return await asyncio.wait_for(self.rings.get(), timeout=10)


async def receiver_server(aiohttp_server, **options) -> tuple[Receiver, object]:
    receiver = Receiver()
    application = web.Application()
    application.router.add_post("/hook", receiver.handle)
    return receiver, await aiohttp_server(application, host="127.0.0.1", **options)


async def subscribed_room(appservice, url: str) -> tuple[str, str, str]:
    mxid, auth = await appservice.register("nova")
    hook = await appservice.client.put("/identities/me/webhook", json={"url": url}, headers=auth)
    assert hook.status == 201, await hook.text()
    room = (await (await appservice.client.post(
        "/channels", json={"name": "lobby", "invite": [HUMAN], "direct": False},
        headers=auth)).json())["room"]
    await appservice.join(room, mxid)
    return mxid, room, (await hook.json())["secret"]


async def logged(caplog, text: str) -> None:
    for _ in range(100):
        if any(text in record.getMessage() for record in caplog.records):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"never logged {text!r}: {[r.getMessage() for r in caplog.records]}")


async def test_an_incoming_message_rings_the_webhook_signed(make_appservice, aiohttp_server):
    receiver, server = await receiver_server(aiohttp_server)
    appservice = await make_appservice(e2ee=False, webhooks={"allowed_targets": [f"127.0.0.1:{server.port}"]})
    mxid, room, secret = await subscribed_room(appservice, f"http://127.0.0.1:{server.port}/hook")

    await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "yo"})
    body, headers = await receiver.next_ring()
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert headers["X-Signature-256"] == f"sha256={expected}"
    ring = json.loads(body)
    assert (ring["identity"], ring["room"], ring["sender"], ring["unread"]) == (mxid, room, HUMAN, 1)


async def test_a_webhook_by_name_goes_to_the_address_it_was_checked_against(
        make_appservice, aiohttp_server, dns):
    receiver, server = await receiver_server(aiohttp_server)
    dns["hooks.lan"] = ["127.0.0.1"]
    appservice = await make_appservice(e2ee=False, webhooks={"allowed_targets": [f"127.0.0.1:{server.port}"]})
    _mxid, room, _secret = await subscribed_room(appservice, f"http://hooks.lan:{server.port}/hook")

    await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "yo"})
    _body, headers = await receiver.next_ring()
    assert headers["Host"] == f"hooks.lan:{server.port}"


async def test_a_name_that_moves_off_the_allowlist_stops_ringing(
        make_appservice, aiohttp_server, dns, caplog):
    caplog.set_level(logging.INFO, logger="matrix_for_ai.services.webhook_notifier")
    receiver, server = await receiver_server(aiohttp_server)
    dns["hooks.lan"] = ["127.0.0.1"]
    appservice = await make_appservice(e2ee=False, webhooks={"allowed_targets": [f"127.0.0.1:{server.port}"]})
    _mxid, room, _secret = await subscribed_room(appservice, f"http://hooks.lan:{server.port}/hook")

    dns["hooks.lan"] = ["10.9.9.9"]
    await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "yo"})
    await logged(caplog, "skipped: url target is not in the installation's webhook allowlist")
    assert receiver.rings.empty()


def certificate_authority(folder) -> tuple[str, ssl.SSLContext]:
    def openssl(*arguments):
        subprocess.run(["openssl", *arguments], check=True, capture_output=True, cwd=folder)
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=house ca",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", "ca.key", "-out", "ca.pem")
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=hooks.lan",
            "-keyout", "hook.key", "-out", "hook.csr")
    (folder / "san.ext").write_text(
        "subjectAltName=DNS:hooks.lan\nauthorityKeyIdentifier=keyid\nbasicConstraints=CA:FALSE\n")
    openssl("x509", "-req", "-in", "hook.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-CAcreateserial", "-days", "1", "-extfile", "san.ext", "-out", "hook.pem")
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_context.load_cert_chain(folder / "hook.pem", folder / "hook.key")
    return str(folder / "ca.pem"), server_context


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl command")
@pytest.mark.parametrize("trusted", [True, False])
async def test_a_tls_webhook_rings_only_when_its_certificate_is_trusted(
        make_appservice, aiohttp_server, dns, caplog, tmp_path, trusted):
    caplog.set_level(logging.INFO, logger="matrix_for_ai.services.webhook_notifier")
    ca_file, server_context = certificate_authority(tmp_path)
    receiver, server = await receiver_server(aiohttp_server, ssl=server_context)
    dns["hooks.lan"] = ["127.0.0.1"]
    webhooks = {"allowed_targets": [f"127.0.0.1:{server.port}"]}
    if trusted:
        webhooks["ca_file"] = ca_file
    appservice = await make_appservice(e2ee=False, webhooks=webhooks)
    _mxid, room, _secret = await subscribed_room(appservice, f"https://hooks.lan:{server.port}/hook")

    await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "yo"})
    if trusted:
        _body, headers = await receiver.next_ring()
        assert headers["Host"] == f"hooks.lan:{server.port}"
    else:
        await logged(caplog, "unreachable at 127.0.0.1")
        assert receiver.rings.empty()



async def test_signups_are_rate_limited_except_for_the_admin(make_appservice):
    appservice = await make_appservice(limits={"signups_per_hour": 1})
    await appservice.register("nova")
    refused = await appservice.client.post("/identities", json={"name": "nova"})
    await problem_of(refused, 429, "rate_limited")
    assert 3000 < int(refused.headers["Retry-After"]) <= 3600
    assert (await appservice.client.post("/identities", json={"name": "nova"}, headers=ADMIN)).status == 201


async def test_failed_signups_do_not_use_up_the_quota(make_appservice):
    appservice = await make_appservice(limits={"signups_per_hour": 1})
    for body in ({"name": "Not Valid"}, {"name": "nova", "colour": "red"}, {}):
        response = await appservice.client.post("/identities", json=body)
        assert response.status in (400, 422), await response.text()
    await appservice.register("nova")
    await problem_of(await appservice.client.post("/identities", json={"name": "vega"}), 429, "rate_limited")


async def test_no_limit_when_the_installation_sets_null(make_appservice):
    appservice = await make_appservice(limits={"requests_per_minute": None})
    _mxid, auth = await appservice.register("nova")
    for _ in range(400):
        assert (await appservice.client.get("/identities/me", headers=auth)).status == 200


async def test_voice_messages_have_their_own_rate(make_appservice):
    appservice = await make_appservice(e2ee=False, speech={"tts": FakeSynthesizer(), "stt": None},
                         limits={"speech_per_minute": 1})
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    events = f"{path(room)}/events"
    speak = lambda words: {"type": "audio", "content": {"transcript": words}}
    assert (await appservice.client.post(events, json=speak("uno"), headers=auth)).status == 201
    await problem_of(await appservice.client.post(events, json=speak("dos"), headers=auth),
                     429, "rate_limited")
    assert (await appservice.client.post(
        events, json={"type": "text", "content": {"text": "tres"}}, headers=auth)).status == 201


async def test_an_audio_is_transcribed_once_however_often_it_is_read(make_appservice):
    transcriber = FakeTranscriber()
    appservice = await make_appservice(e2ee=False, speech={"tts": None, "stt": transcriber})
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    content_uri = "mxc://example.org/voice"
    appservice.hs.media[content_uri] = b"OggS"
    await appservice.deliver(room, HUMAN, "m.room.message", {
        "msgtype": "m.audio", "body": "voice.ogg", "url": content_uri,
        "info": {"mimetype": "audio/ogg"}})
    for _ in range(3):
        read = await read_events(appservice, room, auth)
        assert read["events"][-1]["content"]["transcript"] == "hello there"
    assert len(transcriber.calls) == 1

async def test_a_user_id_is_not_a_room(make_appservice):
    appservice = await make_appservice(e2ee=False)
    _mxid, auth = await appservice.register("nova")
    await problem_of(await appservice.client.post(
        "/channels/%40someone%3Aexample.org/events",
        json={"type": "text", "content": {"text": "hi"}}, headers=auth), 400, "invalid_room")


async def test_members_invite_twice_is_a_conflict_and_removal_is_no_content(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = (await (await appservice.client.post(
        "/channels", json={"name": "team"}, headers=auth)).json())["room"]
    members = path(room) + "/members"
    response = await appservice.client.post(members, json={"user": HUMAN}, headers=auth)
    assert response.status == 201
    assert await response.json() == {"user": HUMAN, "membership": "invite"}
    assert response.headers["Location"] == f"members/{quote(HUMAN, safe='')}"
    await problem_of(await appservice.client.post(members, json={"user": HUMAN}, headers=auth),
                     409, "already_invited")
    await appservice.join(room, HUMAN)
    await problem_of(await appservice.client.post(members, json={"user": HUMAN}, headers=auth),
                     409, "already_member")
    await problem_of(await appservice.client.post(members, json={"user": "human"}, headers=auth),
                     422, "invalid_user")
    listed = await (await appservice.client.get(members, headers=auth)).json()
    assert [m["user"] for m in listed["members"]] == sorted([HUMAN, mxid])
    removed = await appservice.client.delete(f"{members}/{quote(HUMAN, safe='')}", headers=auth)
    assert removed.status == 204
    await problem_of(await appservice.client.delete(f"{members}/{quote(HUMAN, safe='')}", headers=auth),
                     404, "user_not_member")


async def test_leaving_a_channel_forgets_its_unread(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "yo"})
    response = await appservice.client.delete(path(room), headers=auth)
    assert response.status == 204
    assert (await (await appservice.client.get("/channels", headers=auth)).json())["channels"] == []
    await problem_of(await appservice.client.delete(path(room), headers=auth), 404, "not_member")


async def test_membership_of_others_is_rendered_and_own_messages_are_not_unread(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await appservice.join(room, mxid)
    await appservice.deliver(room, mxid, "m.room.message", {"msgtype": "m.text", "body": "mine"})
    await appservice.push(appservice.hs.add_event(
        room, HUMAN, "m.room.member", {"membership": "join", "displayname": "Human"},
        state_key=HUMAN))
    read = await unread_events(appservice, room, auth)
    assert [(m["type"], m["content"]) for m in read["events"]] == [
        ("membership", {"user": HUMAN, "membership": "join", "display_name": "Human"})]


async def test_anyone_can_join_a_public_channel_and_no_one_a_private_one(make_appservice):
    appservice = await make_appservice(e2ee=False)
    _owner, owner_auth = await appservice.register("owner")
    guest, guest_auth = await appservice.register("guest")
    created = await (await appservice.client.post(
        "/channels", json={"name": "plaza", "public": True}, headers=owner_auth)).json()
    assert created["public"] is True
    room = created["room"]
    joined = await appservice.client.put(path(room), headers=guest_auth)
    assert joined.status == 201, await joined.text()
    assert (await joined.json())["room"] == room
    assert guest in appservice.hs.rooms[room]["members"]
    await problem_of(await appservice.client.put(path(room), headers=guest_auth), 409, "already_member")
    private = await (await appservice.client.post(
        "/channels", json={"name": "club"}, headers=owner_auth)).json()
    await problem_of(await appservice.client.put(path(private["room"]), headers=guest_auth),
                     403, "forbidden")
    await problem_of(await appservice.client.put(path("!nowhere:example.org"), headers=guest_auth),
                     404, "channel_not_found")
    await problem_of(await appservice.client.put(path(room), json={}, headers=guest_auth),
                     400, "body_not_allowed")
    await problem_of(await appservice.client.post(
        "/channels", json={"invite": [HUMAN], "public": True}, headers=owner_auth),
        422, "invalid_public")
    await problem_of(await appservice.client.post(
        "/channels", json={"name": "x", "public": "yes"}, headers=owner_auth),
        422, "invalid_public")


async def test_a_banned_identity_cannot_join(make_appservice):
    appservice = await make_appservice(e2ee=False)
    _mxid, auth = await appservice.register("guest")
    room = appservice.hs.create_room(HUMAN, [], encrypted=False, public=True)

    async def banned(**_kwargs):
        return "ban"
    appservice.transport.membership_of = banned
    await problem_of(await appservice.client.put(path(room), headers=auth), 409, "banned")


async def test_joining_answers_502_when_the_homeserver_fails(make_appservice):
    appservice = await make_appservice(e2ee=False)
    _mxid, auth = await appservice.register("guest")
    room = appservice.hs.create_room(HUMAN, [], encrypted=False, public=True)

    async def failing(**_kwargs):
        return 500
    appservice.transport.join_room = failing
    await problem_of(await appservice.client.put(path(room), headers=auth), 502, "homeserver_error")


async def test_revoking_a_token_suspends_the_identity_until_a_new_one_is_issued(make_appservice):
    appservice = await make_appservice()
    mxid, auth = await appservice.register("nova")
    localpart = mxid[1:].split(":")[0]
    revoked = await appservice.client.delete(f"/identities/{localpart}/token", headers=ADMIN)
    assert revoked.status == 204
    await problem_of(await appservice.client.get("/identities/me", headers=auth), 401, "unauthorized")
    await problem_of(await appservice.client.delete(f"/identities/{localpart}/token", headers=ADMIN),
                     404, "token_not_found")
    reissued = await (await appservice.client.post(
        f"/identities/{localpart}/token", headers=ADMIN)).json()
    again = {"Authorization": f"Bearer {reissued['api_token']}"}
    assert (await appservice.client.get("/identities/me", headers=again)).status == 200


async def test_an_identity_may_revoke_only_its_own_token(make_appservice):
    appservice = await make_appservice()
    _nova, nova_auth = await appservice.register("nova")
    vega, vega_auth = await appservice.register("vega")
    await problem_of(await appservice.client.delete(
        f"/identities/{vega[1:].split(':')[0]}/token", headers=nova_auth), 403, "forbidden")
    await problem_of(await appservice.client.delete("/identities/ai_nobody/token", headers=ADMIN),
                     404, "identity_not_found")
    await problem_of(await appservice.client.delete("/identities/me/token?x=1", headers=nova_auth),
                     400, "unknown_parameter")
    assert (await appservice.client.delete("/identities/me/token", headers=nova_auth)).status == 204
    await problem_of(await appservice.client.delete("/identities/me/token", headers=nova_auth),
                     401, "unauthorized")
    assert (await appservice.client.get("/identities/me", headers=vega_auth)).status == 200
