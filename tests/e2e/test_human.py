import base64

import pytest
from nio import RoomEncryptedImage, RoomMessageImage, RoomMessageText

from live import (BASE_URL, HOMESERVER_URL, HUMAN_PASSWORD, HUMAN_USER, PNG, ai,
                  homeserver_reaches_appservice, human, leave)

pytestmark = [
    pytest.mark.skipif(
        not (BASE_URL and HOMESERVER_URL and HUMAN_USER and HUMAN_PASSWORD),
        reason="set MATRIX_FOR_AI_E2E_URL, _HOMESERVER, _HUMAN and _HUMAN_PASSWORD"
               " to talk to the appservice as a person"),
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.parametrize("encrypted", [True, False], ids=["encrypted", "clear"]),
]

POLL = "org.matrix.msc3381.poll.start"
POLL_RESPONSE = "org.matrix.msc3381.poll.response"
TEXT = "org.matrix.msc1767.text"


async def _direct_room(human, ai, *, encrypted: bool) -> str:
    room = await human.create_direct_room(ai.mxid, encrypted=encrypted)
    await ai.wait_until_joined(room)
    await human.wait_until_member(room, ai.mxid)
    return room


async def test_text_both_ways(human, ai, encrypted):
    room = await _direct_room(human, ai, encrypted=encrypted)
    try:
        await human.send(room, {"msgtype": "m.text", "body": "hola, soy una persona"})
        [heard] = await ai.wait_for_events(room)
        assert (heard["type"], heard["sender"]) == ("text", human.mxid)
        assert heard["content"]["text"] == "hola, soy una persona"

        sent = await ai.send(room, {"type": "text", "content": {"text": "hola, persona"}})
        assert sent["encrypted"] is encrypted
        [reply] = await human.wait_for_events(room, ai.mxid)
        assert isinstance(reply, RoomMessageText) and reply.body == "hola, persona"
        assert reply.decrypted is encrypted
    finally:
        await leave(room, ai)


async def test_images_both_ways(human, ai, encrypted):
    room = await _direct_room(human, ai, encrypted=encrypted)
    try:
        await human.send_image(room, PNG, mimetype="image/png", filename="dot.png")
        [image] = await ai.wait_for_events(room)
        assert image["type"] == "image"
        assert base64.b64decode(image["content"]["data_base64"]) == PNG

        await ai.send(room, {"type": "image", "content": {
            "data_base64": base64.b64encode(PNG).decode(), "mimetype": "image/png",
            "filename": "back.png", "w": 1, "h": 1}})
        [back] = await human.wait_for_events(room, ai.mxid)
        assert isinstance(back, RoomEncryptedImage if encrypted else RoomMessageImage), back
        assert await human.download(back) == PNG
    finally:
        await leave(room, ai)


async def test_poll_vote(human, ai, encrypted):
    room = await _direct_room(human, ai, encrypted=encrypted)
    try:
        poll_event_id = await human.send(room, {
            POLL: {"kind": "org.matrix.msc3381.poll.disclosed", "max_selections": 1,
                   "question": {TEXT: "¿Café o té?"},
                   "answers": [{"id": "coffee", TEXT: "Café"}, {"id": "tea", TEXT: "Té"}]},
            TEXT: "¿Café o té?\n1. Café\n2. Té"}, event_type=POLL)
        [poll] = await ai.wait_for_events(room)
        assert poll["type"] == "poll" and poll["event_id"] == poll_event_id
        assert poll["content"]["question"] == "¿Café o té?"
        assert poll["content"]["options"] == [
            {"id": "coffee", "text": "Café"}, {"id": "tea", "text": "Té"}]

        await ai.send(room, {"type": "poll_response",
                             "content": {"poll_event_id": poll_event_id, "answers": ["tea"]}})
        [vote] = await human.wait_for_events(room, ai.mxid)
        assert vote.source["type"] == POLL_RESPONSE, vote.source
        content = vote.source["content"]
        assert content[POLL_RESPONSE]["answers"] == ["tea"]
        assert content["m.relates_to"] == {"rel_type": "m.reference", "event_id": poll_event_id}
    finally:
        await leave(room, ai)
