import json

import pytest

from fakes import FakeEngine
from test_api import HUMAN, path, read_events, unread_events

POLL = "org.matrix.msc3381.poll.start"
RESPONSE = "org.matrix.msc3381.poll.response"


async def _room_with(appservice, *, encrypted=False):
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=encrypted)
    await appservice.join(room, mxid)
    return room, auth


@pytest.mark.parametrize("event_type, content", [
    ("m.room.message", {"msgtype": "m.text", "body": {"nested": "object"}}),
    ("m.room.message", {"msgtype": "m.text", "body": None}),
    ("m.room.message", {"msgtype": "m.image", "body": "photo"}),
    ("m.room.message", {"msgtype": "m.image", "body": "photo", "url": 7}),
    ("m.room.message", {"msgtype": "m.file", "body": "f", "file": {"url": "mxc://example.org/a"}}),
    ("m.room.message", {"msgtype": "m.file", "body": "f", "file": "mxc://example.org/a"}),
    ("m.room.message", {POLL: "a poll"}),
    ("m.room.message", {POLL: {"question": {"m.text": "?"}, "answers": 3}}),
    ("m.room.message", {POLL: {"question": {"m.text": "?"}, "answers": [{"m.text": "no id"}]}}),
    ("m.room.message", {POLL: {"question": 5, "answers": []}}),
])
async def test_a_message_that_breaks_the_spec_is_read_as_malformed(
        make_appservice, event_type, content):
    appservice = await make_appservice(e2ee=False)
    room, auth = await _room_with(appservice)
    event = await appservice.deliver(room, HUMAN, event_type, content)
    for read in (unread_events, read_events):
        page = await read(appservice, room, auth)
        assert [(e["event_id"], e["type"], e["content"]) for e in page["events"]] == [
            (event["event_id"], "malformed", {})]


@pytest.mark.parametrize("content, expected_type", [
    ({"msgtype": "m.text", "body": "hi", "m.relates_to": "not an object"}, "text"),
    ({"msgtype": "m.text", "body": "hi", "m.relates_to": {"m.in_reply_to": "$x"}}, "text"),
    ({"msgtype": "m.text", "body": "hi", "m.relates_to": {"m.in_reply_to": {"event_id": 4}}},
     "text"),
    ({"msgtype": "m.image", "body": "p.png", "url": "mxc://example.org/a",
      "info": {"mimetype": 42, "size": "big"}}, "file"),
    ({"msgtype": "m.location", "body": "here", "geo_uri": 12}, "text"),
])
async def test_a_wrong_optional_field_is_left_out_and_the_message_still_reads(
        make_appservice, content, expected_type):
    appservice = await make_appservice(e2ee=False)
    room, auth = await _room_with(appservice)
    await appservice.deliver(room, HUMAN, "m.room.message", content)
    page = await read_events(appservice, room, auth)
    [event] = page["events"]
    assert event["type"] == expected_type and "reply_to" not in event


async def test_a_poll_end_whose_relation_is_not_an_object_closes_no_poll_in_particular(
        make_appservice):
    appservice = await make_appservice(e2ee=False)
    room, auth = await _room_with(appservice)
    await appservice.deliver(room, HUMAN, "m.poll.end", {"m.relates_to": "x", "body": "closed"})
    [event] = (await read_events(appservice, room, auth))["events"]
    assert (event["type"], event["content"]) == ("poll_end", {})


async def test_a_vote_with_answers_that_are_not_ids_counts_none_of_them(make_appservice):
    appservice = await make_appservice(e2ee=False)
    room, auth = await _room_with(appservice)
    poll = await appservice.deliver(room, HUMAN, "m.room.message", {POLL: {
        "question": {"m.text": [{"body": "Lunch?"}]},
        "answers": [{"id": "yes", "m.text": [{"body": "Yes"}]}]}})
    relation = {"rel_type": "m.reference", "event_id": poll["event_id"]}
    await appservice.deliver(room, HUMAN, RESPONSE, {
        "m.relates_to": relation, RESPONSE: {"answers": [{"id": "yes"}, ["yes"]]}})
    await appservice.deliver(room, "@other:example.org", RESPONSE, {
        "m.relates_to": relation, RESPONSE: {"answers": ["yes"]}})
    page = await read_events(appservice, room, auth, "?include=poll")
    [shown] = [event for event in page["events"] if event["type"] == "poll"]
    assert shown["content"]["question"] == "Lunch?"
    assert shown["content"]["options"] == [{"id": "yes", "text": "Yes"}]
    assert shown["content"]["results"]["counts"] == {"yes": 1}
    assert shown["content"]["results"]["total_voters"] == 2


async def test_a_reply_to_a_message_whose_body_is_not_text_quotes_no_text(make_appservice):
    appservice = await make_appservice(e2ee=False)
    room, auth = await _room_with(appservice)
    odd = await appservice.deliver(room, HUMAN, "m.room.message", {"body": ["a", "list"]})
    await appservice.deliver(room, HUMAN, "m.room.message", {
        "msgtype": "m.text", "body": "> quoted\n\nreply",
        "m.relates_to": {"m.in_reply_to": {"event_id": odd["event_id"]}}})
    reply = (await read_events(appservice, room, auth))["events"][-1]
    assert reply["reply_to"] == {"event_id": odd["event_id"], "sender": HUMAN}


@pytest.mark.parametrize("payload", [
    {"type": "m.room.message", "content": "not an object"},
    {"type": 7, "content": {"body": "hi"}},
])
async def test_an_encrypted_payload_that_breaks_the_spec_is_read_as_malformed(
        make_appservice, payload):
    appservice = await make_appservice()
    room, auth = await _room_with(appservice, encrypted=True)
    event = await appservice.deliver(
        room, HUMAN, "m.room.encrypted", {"ciphertext": json.dumps(payload)})
    [read] = (await unread_events(appservice, room, auth))["events"]
    assert (read["event_id"], read["type"]) == (event["event_id"], "malformed")


async def test_an_encrypted_attachment_that_does_not_decrypt_is_left_out(
        make_appservice, monkeypatch):
    appservice = await make_appservice()
    room, auth = await _room_with(appservice, encrypted=True)
    appservice.hs.media["mxc://example.org/a"] = b"cipher"
    attachment = {"msgtype": "m.file", "body": "f.bin", "file": {
        "url": "mxc://example.org/a", "key": {"k": "k"}, "iv": "iv",
        "hashes": {"sha256": "wrong"}, "v": "v2"}}
    event = await appservice.deliver(room, HUMAN, "m.room.encrypted", {
        "ciphertext": json.dumps({"type": "m.room.message", "content": attachment})})
    monkeypatch.setattr(FakeEngine, "decrypt_attachment", lambda *_args: None)
    page = await read_events(appservice, room, auth, "?include=media")
    [read] = page["events"]
    assert read["type"] == "file" and "data_base64" not in read["content"]
    response = await appservice.client.get(
        f"{path(room)}/events/{event['event_id']}/media", headers=auth)
    assert response.status == 404
