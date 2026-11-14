import asyncio
import json
import secrets
import sys
from urllib.parse import quote

import aiohttp

from live import (HOMESERVER_URL, HUMAN_PASSWORD, HUMAN_USER, Identity, leave, register_identity,
                  wait_until_homeserver_reaches_appservice)

WHILE_DOWN = "sent while the appservice was down"


async def call_client_server_api(session, method, path, token=None, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with session.request(
            method, f"{HOMESERVER_URL}/_matrix/client/v3{path}", headers=headers, **kwargs) as response:
        body = await response.json()
        assert response.status == 200, f"{method} {path} -> {response.status}: {body}"
        return body


async def before(session):
    ai = await register_identity(session, "restart", secrets.token_hex(3))
    login = await call_client_server_api(session, "POST", "/login", json={
        "type": "m.login.password", "identifier": {"type": "m.id.user", "user": HUMAN_USER},
        "password": HUMAN_PASSWORD})
    room = (await call_client_server_api(session, "POST", "/createRoom", login["access_token"],
                              json={"invite": [ai.mxid], "is_direct": True}))["room_id"]
    await ai.wait_until_joined(room)
    return {"ai": {"mxid": ai.mxid, "token": ai.token},
            "human_token": login["access_token"], "room": room}


async def while_down(session, state):
    await call_client_server_api(
        session, "PUT",
        f"/rooms/{quote(state['room'], safe='')}/send/m.room.message/{secrets.token_hex(8)}",
        state["human_token"], json={"msgtype": "m.text", "body": WHILE_DOWN})


async def after(session, state):
    wait_until_homeserver_reaches_appservice()
    ai = Identity(session, state["ai"]["mxid"], state["ai"]["token"])
    try:
        [missed] = await ai.wait_for_events(state["room"])
        assert missed["content"]["text"] == WHILE_DOWN, missed
    finally:
        await leave(state["room"], ai)
        await call_client_server_api(session, "POST", "/logout", state["human_token"], json={})


async def main(phase):
    async with aiohttp.ClientSession() as session:
        if phase == "before":
            print(json.dumps(await before(session)))
        else:
            await {"while_down": while_down, "after": after}[phase](session, json.load(sys.stdin))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
