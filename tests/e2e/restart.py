import asyncio
import json
import secrets
import sys
from urllib.parse import quote

import aiohttp

from live import (BASE_URL, HOMESERVER_URL, HUMAN_PASSWORD, HUMAN_USER, REMOTE_URL, Identity, leave,
                  register_identity, wait_until_homeserver_reaches_appservice)

WHILE_DOWN = "sent while the appservice was down"


async def call_client_server_api(session, method, path, token=None, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with session.request(
            method, f"{HOMESERVER_URL}/_matrix/client/v3{path}", headers=headers, **kwargs) as response:
        body = await response.json()
        assert response.status == 200, f"{method} {path} -> {response.status}: {body}"
        return body


def _saved(identity: Identity) -> dict:
    return {"mxid": identity.mxid, "token": identity.token, "base_url": identity.base_url}


def _identity(session, saved: dict) -> Identity:
    return Identity(session, saved["mxid"], saved["token"], saved["base_url"])


async def before(session):
    run = secrets.token_hex(3)
    ai = await register_identity(session, "restart", run)
    login = await call_client_server_api(session, "POST", "/login", json={
        "type": "m.login.password", "identifier": {"type": "m.id.user", "user": HUMAN_USER},
        "password": HUMAN_PASSWORD})
    state = {"ai": _saved(ai), "human_token": login["access_token"]}
    state["human_room"] = (await call_client_server_api(
        session, "POST", "/createRoom", state["human_token"],
        json={"invite": [ai.mxid], "is_direct": True}))["room_id"]
    await ai.wait_until_joined(state["human_room"])
    if REMOTE_URL != BASE_URL:
        peer = await register_identity(session, "restart_peer", run, REMOTE_URL)
        state["peer"] = _saved(peer)
        state["peer_room"] = (await peer.call(
            "POST", "/channels", expect=201, json={"invite": [ai.mxid]}))["room"]
        await ai.wait_until_joined(state["peer_room"])
        await peer.wait_until_member(state["peer_room"], ai.mxid)
    return state


async def while_down(session, state):
    await call_client_server_api(
        session, "PUT",
        f"/rooms/{quote(state['human_room'], safe='')}/send/m.room.message/{secrets.token_hex(8)}",
        state["human_token"], json={"msgtype": "m.text", "body": WHILE_DOWN})
    if "peer" in state:
        await _identity(session, state["peer"]).send(
            state["peer_room"], {"type": "text", "content": {"text": WHILE_DOWN}})
    else:
        state["invited_room"] = (await call_client_server_api(
            session, "POST", "/createRoom", state["human_token"],
            json={"invite": [state["ai"]["mxid"]]}))["room_id"]
    return state


async def after(session, state):
    ai = _identity(session, state["ai"])
    wait_until_homeserver_reaches_appservice(ai.base_url)
    rooms_with_a_message = [state["human_room"]] + ([state["peer_room"]] if "peer" in state else [])
    try:
        for room in rooms_with_a_message:
            [missed] = await ai.wait_for_events(room)
            assert missed["content"]["text"] == WHILE_DOWN, missed
        if "invited_room" in state:
            await ai.wait_until_joined(state["invited_room"])
    finally:
        for room in rooms_with_a_message + [state[key] for key in ("invited_room",) if key in state]:
            await leave(room, ai)
        if "peer" in state:
            await leave(state["peer_room"], _identity(session, state["peer"]))
        await call_client_server_api(session, "POST", "/logout", state["human_token"], json={})


async def main(phase):
    async with aiohttp.ClientSession() as session:
        if phase == "before":
            print(json.dumps(await before(session)))
        elif phase == "while_down":
            print(json.dumps(await while_down(session, json.load(sys.stdin))))
        else:
            await after(session, json.load(sys.stdin))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
