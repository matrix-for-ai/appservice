import asyncio
from urllib.parse import quote

from fakes import PUSH_CHECK
from matrix_for_ai.__main__ import _catch_up_when_homeserver_ready
from matrix_for_ai.ports.matrix_transport import PushCheck
from matrix_for_ai.services.push_reachability import PushReachability
from test_api import HUMAN


async def test_invitations_missed_while_the_appservice_was_down_are_accepted_at_startup(
        make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, _auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    await _catch_up_when_homeserver_ready(appservice.event_router, asyncio.Event())
    assert mxid in appservice.hs.rooms[room]["members"]


async def test_invitations_are_caught_up_when_the_homeserver_reaches_the_appservice_again():
    unreachable = PushCheck(supported=True, reachable=False, status=502)
    answers = [PUSH_CHECK, unreachable, unreachable, PUSH_CHECK, PUSH_CHECK]
    recoveries = []

    class Transport:
        async def ping_from_homeserver(self, *, appservice_id):
            return answers.pop(0)

    async def recovered():
        recoveries.append(len(answers))
    reachability = PushReachability(
        transport=Transport(), appservice_id="test", on_recovered=recovered)
    for _ in range(5):
        await reachability.check()
    assert recoveries == [1]


def _invite(appservice, room, mxid):
    appservice.hs.rooms[room]["invited"].add(mxid)
    return appservice.hs.add_event(room, HUMAN, "m.room.member", {"membership": "invite"},
                                   state_key=mxid)


async def _unread(appservice, room, auth):
    response = await appservice.client.get(
        f"/channels/{quote(room, safe='')}/events?unread=true", headers=auth)
    assert response.status == 200
    return [(event["type"], event["sender"], event["content"])
            for event in (await response.json())["events"]]


async def test_an_invitation_missed_while_down_is_unread_with_what_was_said_since(
        make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [], encrypted=False)
    appservice.hs.add_event(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "before"})
    _invite(appservice, room, mxid)
    appservice.hs.add_event(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "hola"})
    await _catch_up_when_homeserver_ready(appservice.event_router, asyncio.Event())
    assert await _unread(appservice, room, auth) == [
        ("membership", HUMAN, {"user": mxid, "membership": "invite"}),
        ("text", HUMAN, {"text": "hola"})]


async def test_an_invitation_that_arrives_by_push_is_unread(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [], encrypted=False)
    invite = _invite(appservice, room, mxid)
    await appservice.push(invite)
    await appservice.push(invite)
    assert await _unread(appservice, room, auth) == [
        ("membership", HUMAN, {"user": mxid, "membership": "invite"})]
    said = await appservice.deliver(room, HUMAN, "m.room.message",
                                    {"msgtype": "m.text", "body": "welcome"})
    receipt = await appservice.client.put(
        f"/channels/{quote(room, safe='')}/receipt", json={"event_id": said["event_id"]},
        headers=auth)
    assert receipt.status == 204
    await appservice.push(invite)
    assert await _unread(appservice, room, auth) == []


async def test_what_arrives_while_the_history_is_caught_up_stays_after_it(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [], encrypted=False)
    _invite(appservice, room, mxid)
    appservice.hs.add_event(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "first"})
    history_requested, release = asyncio.Event(), asyncio.Event()
    fetch_timeline = appservice.transport.fetch_timeline

    async def slow_fetch_timeline(**kwargs):
        history_requested.set()
        await release.wait()
        return await fetch_timeline(**kwargs)
    appservice.transport.fetch_timeline = slow_fetch_timeline
    catch_up = asyncio.create_task(appservice.event_router.accept_pending_invitations())
    await history_requested.wait()
    later = appservice.hs.add_event(
        room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "second"})
    push = asyncio.create_task(appservice.push(later))
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(catch_up, push)
    assert [content.get("text") for _type, _sender, content in
            await _unread(appservice, room, auth)] == [None, "first", "second"]


async def test_a_profile_change_after_joining_does_not_hide_the_invitation(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [], encrypted=False)
    _invite(appservice, room, mxid)
    appservice.hs.add_event(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "hola"})
    join_room = appservice.transport.join_room

    async def join_and_rename(**kwargs):
        status = await join_room(**kwargs)
        appservice.hs.add_event(room, mxid, "m.room.member",
                                {"membership": "join", "displayname": "Nova"}, state_key=mxid)
        return status
    appservice.transport.join_room = join_and_rename
    await appservice.event_router.accept_pending_invitations()
    assert [event_type for event_type, _sender, _content in
            await _unread(appservice, room, auth)] == ["membership", "text"]


async def test_without_a_visible_invitation_the_unread_starts_after_the_earlier_membership(
        make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [], encrypted=False)
    appservice.hs.add_event(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "old"})
    appservice.hs.add_event(room, mxid, "m.room.member", {"membership": "leave"}, state_key=mxid)
    appservice.hs.add_event(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "gap"})
    appservice.hs.rooms[room]["invited"].add(mxid)
    await appservice.event_router.accept_pending_invitations()
    assert [content.get("text") for _type, _sender, content in
            await _unread(appservice, room, auth)] == ["gap"]
