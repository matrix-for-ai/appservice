import asyncio
from urllib.parse import quote

import pytest
from fakes import base_configuration
from matrix_for_ai.__main__ import _catch_up_when_homeserver_ready
from matrix_for_ai.configuration import ConfigurationError, parse_configuration
from matrix_for_ai.services.federation_policy import (
    FederationConfiguration, FederationPolicy, FederationRule, ServerRule)
from test_api import HUMAN

FRIEND = "@friend:amigo.org"
STRANGER = "@stranger:malo.org"
CLOSED = {"servers": {"allow": []}}
ONLY_FRIENDS = {"servers": {"allow": ["amigo.org"]}}


def _policy(default: FederationRule | None = None, **identities) -> FederationPolicy:
    return FederationPolicy(server_name="example.org", configuration=FederationConfiguration(
        default=default or FederationRule(), identities=identities))


def _rule(allow=("*",), deny=(), denied_members="ignore") -> FederationRule:
    return FederationRule(servers=ServerRule(allow=tuple(allow), deny=tuple(deny)),
                          denied_members=denied_members)


def _parse(tmp_path, federation):
    return parse_configuration(base_configuration(tmp_path, federation=federation)).federation


# configuration

def test_without_a_federation_section_every_server_is_allowed(tmp_path):
    policy = FederationPolicy(
        server_name="example.org",
        configuration=parse_configuration(base_configuration(tmp_path)).federation)
    assert policy.unrestricted("@ai_nova:example.org")
    assert policy.allows_user("@ai_nova:example.org", STRANGER)


@pytest.mark.parametrize("federation, complaint", [
    ({"servers": {"allow": "amigo.org"}}, "federation.servers.allow"),
    ({"servers": {"allow": ["amigo.org/x"]}}, "federation.servers.allow"),
    ({"servers": {"deny": ["[a-z].org"]}}, "federation.servers.deny"),
    ({"servers": {"allow": [], "block": []}}, "unknown keys in federation.servers"),
    ({"denied_members": "kick"}, "federation.denied_members"),
    ({"identities": {"@ai_nova:other.org": CLOSED}}, "@ai_nova:other.org"),
    ({"identities": {"ai_nova": CLOSED}}, "ai_nova"),
    ({"identities": {"@ai_nova:example.org": {"server": []}}}, "unknown keys"),
    ({"allow": []}, "unknown keys in federation"),
])
def test_a_wrong_federation_section_does_not_start(tmp_path, federation, complaint):
    with pytest.raises(ConfigurationError, match=complaint):
        _parse(tmp_path, federation)


def test_an_identity_replaces_the_servers_whole_and_inherits_what_it_does_not_set(tmp_path):
    configuration = _parse(tmp_path, {
        "servers": {"allow": ["*"], "deny": ["malo.org"]}, "denied_members": "leave",
        "identities": {"@ai_nova:example.org": {"servers": {"allow": ["amigo.org"]}}}})
    assert configuration.identities["@ai_nova:example.org"] == _rule(
        allow=["amigo.org"], deny=[], denied_members="leave")


# policy

def test_its_own_server_is_always_allowed():
    policy = _policy(_rule(allow=[], deny=["*"]))
    assert policy.allows_user("@ai_nova:example.org", "@human:example.org")
    assert policy.allows_user("@ai_nova:example.org", "@human:example.org:8448")
    assert not policy.allows_user("@ai_nova:example.org", FRIEND)


@pytest.mark.parametrize("glob, server, allowed", [
    ("amigo.org", "amigo.org", True),
    ("amigo.org", "AMIGO.org", True),
    ("amigo.org", "amigo.org:8448", True),
    ("amigo.org", "amigoxorg", False),
    ("amigo.org", "sub.amigo.org", False),
    ("*.amigo.org", "sub.amigo.org", True),
    ("*.amigo.org", "amigo.org", False),
    ("amig?.org", "amiga.org", True),
    ("amig?.org", "amigas.org", False),
])
def test_server_names_match_like_a_room_server_acl(glob, server, allowed):
    assert _policy(_rule(allow=[glob])).allows_server("@ai_nova:example.org", server) is allowed


def test_deny_wins_over_allow():
    policy = _policy(_rule(allow=["*.org"], deny=["malo.org"]))
    assert policy.allows_user("@ai_nova:example.org", FRIEND)
    assert not policy.allows_user("@ai_nova:example.org", STRANGER)


@pytest.mark.parametrize("server", ["1.2.3.4", "1.2.3.4:8448", "[::1]", "[2001:db8::1]:8448"])
def test_ip_literals_pass_only_when_nothing_is_restricted(server):
    assert _policy().allows_server("@ai_nova:example.org", server)
    assert not _policy(_rule(allow=["*"], deny=["malo.org"])).allows_server(
        "@ai_nova:example.org", server)


@pytest.mark.parametrize("rule, federate, expected", [
    (_rule(), True, (True, None)),
    (_rule(), False, (False, None)),
    (_rule(allow=[]), True, (False, None)),
    (_rule(deny=["*"]), True, (False, None)),
    (_rule(allow=["amigo.org"]), True, (True, {
        "allow": ["amigo.org", "example.org"], "deny": [], "allow_ip_literals": False})),
    (_rule(deny=["malo.org"]), True, (True, {
        "allow": ["*", "example.org"], "deny": ["malo.org"], "allow_ip_literals": False})),
])
def test_a_new_channel_federates_and_carries_an_acl_as_the_policy_says(rule, federate, expected):
    assert _policy(rule).room_federation("@ai_nova:example.org", federate=federate) == expected


def test_the_rule_of_an_identity_overrides_the_default_either_way():
    open_ai, closed_ai, other = "@ai_open:example.org", "@ai_closed:example.org", "@ai_x:example.org"
    closed_by_default = _policy(_rule(allow=[]), **{open_ai: _rule()})
    assert closed_by_default.allows_user(open_ai, STRANGER)
    assert not closed_by_default.allows_user(other, STRANGER)
    open_by_default = _policy(_rule(), **{closed_ai: _rule(allow=[])})
    assert not open_by_default.allows_user(closed_ai, STRANGER)
    assert open_by_default.allows_user(other, STRANGER)


# through the API

async def _appservice(make_appservice, federation=None, **options):
    overrides = {} if federation is None else {"federation": federation}
    return await make_appservice(e2ee=options.pop("e2ee", False), **overrides)


async def _create(appservice, auth, **payload):
    payload.setdefault("direct", False)
    return await appservice.client.post("/channels", json=payload, headers=auth)


async def _code(response) -> tuple[int, str]:
    return response.status, (await response.json())["code"]


async def _unread_texts(appservice, room, auth) -> list:
    response = await appservice.client.get(
        f"/channels/{quote(room, safe='')}/events?unread=true", headers=auth)
    if response.status == 404:
        return None
    return [(event["sender"], event["content"].get("text"))
            for event in (await response.json())["events"] if event["type"] == "text"]


def _invite(appservice, room, inviter, mxid):
    appservice.hs.rooms[room]["invited"].add(mxid)
    return appservice.hs.add_event(room, inviter, "m.room.member", {"membership": "invite"},
                                   state_key=mxid)


async def _remote_joins(appservice, room, user):
    appservice.hs.rooms[room]["members"].add(user)
    await appservice.push(appservice.hs.add_event(
        room, user, "m.room.member", {"membership": "join"}, state_key=user))


async def _says(appservice, room, sender, text):
    return await appservice.deliver(room, sender, "m.room.message", {"msgtype": "m.text", "body": text})


async def test_a_closed_identity_cannot_invite_another_server(make_appservice):
    appservice = await _appservice(make_appservice, CLOSED)
    _mxid, auth = await appservice.register("nova")
    response = await _create(appservice, auth, name="x", invite=[FRIEND], encrypted=False)
    assert await _code(response) == (403, "server_not_allowed")
    assert appservice.hs.rooms == {}


async def test_the_channels_of_a_closed_identity_never_federate(make_appservice):
    appservice = await _appservice(make_appservice, CLOSED)
    _mxid, auth = await appservice.register("nova")
    response = await _create(appservice, auth, name="x", invite=[HUMAN], encrypted=False)
    assert response.status == 201 and (await response.json())["federate"] is False
    [room] = appservice.hs.rooms.values()
    assert (room["federate"], room["server_acl"]) == (False, None)


async def test_a_channel_asked_not_to_federate_does_not(make_appservice):
    appservice = await _appservice(make_appservice)
    _mxid, auth = await appservice.register("nova")
    response = await _create(appservice, auth, name="x", encrypted=False, federate=False)
    assert (await response.json())["federate"] is False
    [room] = appservice.hs.rooms.values()
    assert room["federate"] is False


@pytest.mark.parametrize("payload", [
    {"name": "x", "encrypted": False, "federate": False, "invite": [FRIEND]},
    {"name": "x", "encrypted": False, "federate": "no"},
])
async def test_federate_must_be_a_boolean_that_fits_the_invitees(make_appservice, payload):
    appservice = await _appservice(make_appservice)
    _mxid, auth = await appservice.register("nova")
    assert await _code(await _create(appservice, auth, **payload)) == (422, "invalid_federate")
    assert appservice.hs.rooms == {}


async def test_a_channel_of_a_restricted_identity_carries_its_acl(make_appservice):
    appservice = await _appservice(make_appservice, ONLY_FRIENDS)
    _mxid, auth = await appservice.register("nova")
    response = await _create(appservice, auth, name="x", invite=[FRIEND], encrypted=False)
    assert response.status == 201
    [room] = appservice.hs.rooms.values()
    assert room["federate"] is True
    assert room["server_acl"] == {
        "allow": ["amigo.org", "example.org"], "deny": [], "allow_ip_literals": False}


async def test_an_encrypted_channel_carries_the_acl_too(make_appservice):
    appservice = await make_appservice(e2ee=True, federation=ONLY_FRIENDS)
    _mxid, auth = await appservice.register("nova")
    assert (await _create(appservice, auth, invite=[FRIEND])).status == 201
    [room] = appservice.hs.rooms.values()
    assert room["encrypted"] and room["server_acl"]["allow"] == ["amigo.org", "example.org"]


async def test_a_member_of_a_denied_server_is_not_invited(make_appservice):
    appservice = await _appservice(make_appservice, ONLY_FRIENDS)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(mxid, [], encrypted=False)
    response = await appservice.client.post(
        f"/channels/{quote(room, safe='')}/members", json={"user": STRANGER}, headers=auth)
    assert await _code(response) == (403, "server_not_allowed")
    assert STRANGER not in appservice.hs.rooms[room]["invited"]
    response = await appservice.client.post(
        f"/channels/{quote(room, safe='')}/members", json={"user": FRIEND}, headers=auth)
    assert response.status == 201


async def test_an_invitation_from_a_denied_server_is_declined(make_appservice):
    appservice = await _appservice(make_appservice, ONLY_FRIENDS)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(STRANGER, [], encrypted=False)
    await appservice.push(_invite(appservice, room, STRANGER, mxid))
    assert mxid not in appservice.hs.rooms[room]["members"] | appservice.hs.rooms[room]["invited"]
    assert await _unread_texts(appservice, room, auth) is None


async def test_an_invitation_from_an_allowed_server_is_accepted(make_appservice):
    appservice = await _appservice(make_appservice, ONLY_FRIENDS)
    mxid, _auth = await appservice.register("nova")
    room = appservice.hs.create_room(FRIEND, [], encrypted=False)
    await appservice.push(_invite(appservice, room, FRIEND, mxid))
    assert mxid in appservice.hs.rooms[room]["members"]


async def test_an_invitation_from_a_denied_server_missed_while_down_is_declined(make_appservice):
    appservice = await _appservice(make_appservice, ONLY_FRIENDS)
    mxid, _auth = await appservice.register("nova")
    denied = appservice.hs.create_room(STRANGER, [], encrypted=False)
    _invite(appservice, denied, STRANGER, mxid)
    allowed = appservice.hs.create_room(FRIEND, [], encrypted=False)
    _invite(appservice, allowed, FRIEND, mxid)
    await _catch_up_when_homeserver_ready(
        appservice.transport, appservice.event_router, asyncio.Event())
    assert mxid not in appservice.hs.rooms[denied]["members"] | appservice.hs.rooms[denied]["invited"]
    assert mxid in appservice.hs.rooms[allowed]["members"]


async def test_joining_a_channel_with_members_of_a_denied_server_is_undone(make_appservice):
    appservice = await _appservice(make_appservice, ONLY_FRIENDS)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(STRANGER, [], encrypted=False, public=True)
    response = await appservice.client.put(f"/channels/{quote(room, safe='')}", headers=auth)
    assert await _code(response) == (403, "server_not_allowed")
    assert mxid not in appservice.hs.rooms[room]["members"]


async def _room_with_a_stranger(appservice, mxid, *, encrypted=False):
    room = appservice.hs.create_room(HUMAN, [], encrypted=encrypted)
    await appservice.push(_invite(appservice, room, HUMAN, mxid))
    await _remote_joins(appservice, room, STRANGER)
    return room


async def test_ignore_hides_a_denied_member_and_keeps_the_rest(make_appservice):
    appservice = await _appservice(make_appservice, ONLY_FRIENDS)
    mxid, auth = await appservice.register("nova")
    room = await _room_with_a_stranger(appservice, mxid)
    hidden = await _says(appservice, room, STRANGER, "psst")
    await _says(appservice, room, HUMAN, "hola")
    assert await _unread_texts(appservice, room, auth) == [(HUMAN, "hola")]
    history = await appservice.client.get(f"/channels/{quote(room, safe='')}/events", headers=auth)
    assert STRANGER not in [event["sender"] for event in (await history.json())["events"]]
    single = await appservice.client.get(
        f"/channels/{quote(room, safe='')}/events/{quote(hidden['event_id'], safe='')}",
        headers=auth)
    assert single.status == 404


async def test_ignore_refuses_to_write_in_a_clear_channel_a_denied_member_reads(make_appservice):
    appservice = await _appservice(make_appservice, ONLY_FRIENDS)
    mxid, auth = await appservice.register("nova")
    room = await _room_with_a_stranger(appservice, mxid)
    events_before = len(appservice.hs.events)
    response = await appservice.client.post(
        f"/channels/{quote(room, safe='')}/events",
        json={"type": "text", "content": {"text": "secreto"}}, headers=auth)
    assert await _code(response) == (403, "server_not_allowed")
    assert len(appservice.hs.events) == events_before


async def test_ignore_withholds_the_keys_of_an_encrypted_channel_from_a_denied_member(
        make_appservice):
    appservice = await make_appservice(e2ee=True, federation=ONLY_FRIENDS)
    mxid, auth = await appservice.register("nova")
    room = await _room_with_a_stranger(appservice, mxid, encrypted=True)
    await _remote_joins(appservice, room, FRIEND)
    response = await appservice.client.post(
        f"/channels/{quote(room, safe='')}/events",
        json={"type": "text", "content": {"text": "secreto"}}, headers=auth)
    assert response.status == 201
    readers = appservice.hs.readers[(await response.json())["event_id"]]
    assert STRANGER not in readers and {HUMAN, FRIEND} <= readers


async def test_leave_leaves_when_a_denied_member_joins(make_appservice):
    appservice = await _appservice(make_appservice, {**ONLY_FRIENDS, "denied_members": "leave"})
    mxid, auth = await appservice.register("nova")
    room = await _room_with_a_stranger(appservice, mxid)
    assert mxid not in appservice.hs.rooms[room]["members"]
    listed = await appservice.client.get("/channels", headers=auth)
    assert room not in [channel["room"] for channel in (await listed.json())["channels"]]


async def test_leave_leaves_a_channel_it_is_invited_to_with_a_denied_member(make_appservice):
    appservice = await _appservice(make_appservice, {**ONLY_FRIENDS, "denied_members": "leave"})
    mxid, _auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [], encrypted=False)
    appservice.hs.rooms[room]["members"].add(STRANGER)
    await appservice.push(_invite(appservice, room, HUMAN, mxid))
    assert mxid not in appservice.hs.rooms[room]["members"]


async def test_leave_leaves_at_startup_what_the_policy_no_longer_allows(make_appservice):
    appservice = await _appservice(make_appservice, {**ONLY_FRIENDS, "denied_members": "leave"})
    mxid, _auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [], encrypted=False)
    appservice.hs.rooms[room]["members"] |= {mxid, STRANGER}
    await _catch_up_when_homeserver_ready(
        appservice.transport, appservice.event_router, asyncio.Event())
    assert mxid not in appservice.hs.rooms[room]["members"]


async def test_allow_keeps_talking_in_a_channel_a_human_shares_with_a_denied_member(
        make_appservice):
    appservice = await _appservice(make_appservice, {**ONLY_FRIENDS, "denied_members": "allow"})
    mxid, auth = await appservice.register("nova")
    room = await _room_with_a_stranger(appservice, mxid)
    await _says(appservice, room, STRANGER, "hola")
    assert await _unread_texts(appservice, room, auth) == [(STRANGER, "hola")]
    response = await appservice.client.post(
        f"/channels/{quote(room, safe='')}/events",
        json={"type": "text", "content": {"text": "hola"}}, headers=auth)
    assert response.status == 201


async def test_allow_still_declines_invitations_from_a_denied_server(make_appservice):
    appservice = await _appservice(make_appservice, {**ONLY_FRIENDS, "denied_members": "allow"})
    mxid, _auth = await appservice.register("nova")
    room = appservice.hs.create_room(STRANGER, [], encrypted=False)
    await appservice.push(_invite(appservice, room, STRANGER, mxid))
    assert mxid not in appservice.hs.rooms[room]["members"]


async def test_one_identity_open_while_the_rest_stay_closed(make_appservice):
    first = await _appservice(make_appservice)
    support, support_auth = await first.register("support")
    _house, house_auth = await first.register("house")
    appservice = await _appservice(make_appservice, {
        **CLOSED, "identities": {support: {"servers": {"allow": ["*"]}}}})
    allowed = await _create(appservice, support_auth, name="x", invite=[STRANGER], encrypted=False)
    assert allowed.status == 201
    denied = await _create(appservice, house_auth, name="x", invite=[STRANGER], encrypted=False)
    assert await _code(denied) == (403, "server_not_allowed")
