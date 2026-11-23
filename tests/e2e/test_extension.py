import asyncio
import secrets
import time
from urllib.parse import quote

import pytest

from live import (BASE_URL, EXTENSION, HOMESERVER_URL, HUMAN_PASSWORD, HUMAN_USER, TIMEOUT_SECONDS,
                  leave)

pytestmark = pytest.mark.skipif(
    not (BASE_URL and EXTENSION),
    reason="set MATRIX_FOR_AI_E2E_URL and _EXTENSION=true, for an appservice with E2EE that loads"
           " tests/e2e/encryption_extension.py")

PING = "org.example.e2e.ping"
STATE = "org.example.e2e.state"


async def _extension(identity, method, action, expect=200, **kwargs):
    return await identity.call(method, f"/e2e/{quote(identity.mxid, safe='')}/{action}",
                               expect=expect, **kwargs)


async def test_an_extension_sends_and_captures_olm_to_device_events(pair):
    alice, bob = pair
    run = secrets.token_hex(3)
    await _extension(bob, "POST", f"capture?type={PING}", expect=204)
    bob_device = (await _extension(bob, "GET", "device"))["device_id"]
    await _extension(alice, "POST", "to_device", expect=204, json={
        "user_id": bob.mxid, "device_id": bob_device, "type": PING, "content": {"run": run}})
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        captured = await _extension(bob, "GET", "captured")
        if captured:
            [event] = captured
            assert event["sender"] == alice.mxid
            assert (event["payload"]["type"], event["payload"]["content"]) == (PING, {"run": run})
            return
        await asyncio.sleep(1)
    raise AssertionError(f"{bob.mxid} captured nothing from {alice.mxid}")


@pytest.mark.skipif(
    not (HOMESERVER_URL and HUMAN_USER and HUMAN_PASSWORD),
    reason="set MATRIX_FOR_AI_E2E_HOMESERVER, _HUMAN and _HUMAN_PASSWORD")
@pytest.mark.asyncio(loop_scope="session")
async def test_an_extension_puts_room_state(human, ai):
    room = await human.create_direct_room(ai.mxid, encrypted=True)
    try:
        await ai.wait_until_joined(room)
        run = secrets.token_hex(3)
        await _extension(ai, "PUT", "state", json={
            "room": room, "type": STATE, "state_key": ai.mxid, "content": {"run": run}})
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while True:
            async with human.session.get(
                    f"{HOMESERVER_URL}/_matrix/client/v3/rooms/{quote(room, safe='')}/state/"
                    f"{STATE}/{quote(ai.mxid, safe='')}",
                    headers={"Authorization": f"Bearer {human.client.access_token}"}) as response:
                if response.status == 200 or time.monotonic() > deadline:
                    assert response.status == 200, await response.text()
                    assert await response.json() == {"run": run}
                    return
            await asyncio.sleep(1)
    finally:
        await leave(room, ai)
