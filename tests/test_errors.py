import base64
import json
from pathlib import Path
from urllib.parse import quote

from fakes import PNG, FakeSynthesizer
from matrix_for_ai.ports.matrix_transport import ApplicationServiceUserRegistration, HomeserverError
from test_api import HUMAN, path, problem_of

OPENAPI_PATH = Path(__file__).resolve().parent.parent / "matrix_for_ai" / "api" / "openapi.json"


def returning(value):
    async def stub(**_kwargs):
        return value
    return stub


def failing(status=500):
    async def stub(**_kwargs):
        raise HomeserverError(status)
    return stub


async def joined_room(appservice, mxid, *, encrypted=False):
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=encrypted)
    await appservice.join(room, mxid)
    return room


async def test_the_contract_is_served(make_appservice):
    appservice = await make_appservice()
    response = await appservice.client.get("/openapi.json")
    assert response.status == 200
    assert await response.json() == json.loads(OPENAPI_PATH.read_text())


async def test_registration_fields_must_be_strings(make_appservice):
    appservice = await make_appservice()
    await problem_of(await appservice.client.post("/identities", json={"name": 1}), 422, "invalid_name")
    await problem_of(await appservice.client.post(
        "/identities", json={"name": "nova", "display_name": 1}), 422, "invalid_display_name")


async def test_registration_reports_the_homeserver_refusal(make_appservice):
    appservice = await make_appservice()
    async def refuse(**_kwargs):
        raise HomeserverError(400, {"errcode": "M_EXCLUSIVE", "error": "internal detail",
                                    "admin_contact": "ops@example.org"})
    appservice.transport.register_application_service_user = refuse
    body = await problem_of(await appservice.client.post("/identities", json={"name": "nova"}),
                            502, "homeserver_error")
    assert body["upstream"] == {"status": 400, "errcode": "M_EXCLUSIVE"}
    assert "internal detail" not in json.dumps(body)


async def test_registration_gives_up_when_every_id_is_taken(make_appservice):
    appservice = await make_appservice()
    appservice.transport.register_application_service_user = returning(
        ApplicationServiceUserRegistration(outcome="already_in_use"))
    await problem_of(await appservice.client.post("/identities", json={"name": "nova"}),
                     503, "no_free_id")


async def test_a_new_token_takes_no_body(make_appservice):
    appservice = await make_appservice()
    _mxid, auth = await appservice.register("nova")
    await problem_of(await appservice.client.post("/identities/me/token", json={}, headers=auth),
                     400, "body_not_allowed")


async def test_reading_an_identity_needs_the_homeserver_profile(make_appservice):
    appservice = await make_appservice()
    _mxid, auth = await appservice.register("nova")
    appservice.transport.fetch_profile = failing()
    await problem_of(await appservice.client.get("/identities/me", headers=auth),
                     502, "homeserver_error")


async def test_patch_matrix_must_be_an_object_and_reach_the_homeserver(make_appservice):
    appservice = await make_appservice()
    _mxid, auth = await appservice.register("nova")
    await problem_of(await appservice.client.patch(
        "/identities/me", json={"matrix": "Nova"}, headers=auth), 422, "invalid_matrix")
    appservice.transport.set_display_name = failing()
    await problem_of(await appservice.client.patch(
        "/identities/me", json={"matrix": {"display_name": "N"}}, headers=auth),
        502, "homeserver_error")


async def test_a_webhook_needs_a_url(make_appservice):
    appservice = await make_appservice(webhooks={"allowed_targets": ["*"]})
    _mxid, auth = await appservice.register("nova")
    await problem_of(await appservice.client.put(
        "/identities/me/webhook", json={"url": ""}, headers=auth), 422, "url_required")


async def test_channel_creation_validation(make_appservice):
    appservice = await make_appservice(e2ee=False)
    _mxid, auth = await appservice.register("nova")
    for payload, code in (({"name": 1}, "invalid_name"),
                          ({"topic": 1}, "invalid_topic"),
                          ({"invite": ["human"]}, "invalid_invite"),
                          ({"direct": "yes"}, "invalid_direct"),
                          ({"encrypted": "yes"}, "invalid_encrypted"),
                          ({"direct": False}, "name_required")):
        await problem_of(await appservice.client.post("/channels", json=payload, headers=auth),
                         422, code)


async def test_channel_creation_without_encryption(make_appservice):
    appservice = await make_appservice(e2ee=False)
    _mxid, auth = await appservice.register("nova")
    await problem_of(await appservice.client.post("/channels", json={"invite": [HUMAN]}, headers=auth),
                     501, "no_e2ee_for_dm")
    await problem_of(await appservice.client.post(
        "/channels", json={"name": "team", "encrypted": True}, headers=auth),
        501, "e2ee_unavailable")
    appservice.transport.create_room = failing()
    await problem_of(await appservice.client.post("/channels", json={"name": "team"}, headers=auth),
                     502, "homeserver_error")


async def test_leaving_reports_what_the_homeserver_answered(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = await joined_room(appservice, mxid)
    for status, expected, code in ((403, 403, "forbidden"), (500, 502, "homeserver_error")):
        appservice.transport.leave_and_forget_room = returning(status)
        await problem_of(await appservice.client.delete(path(room), headers=auth), expected, code)


async def test_inviting_reports_what_the_homeserver_answered(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    outsider = appservice.hs.create_room(HUMAN, [], encrypted=False)
    await problem_of(await appservice.client.post(
        f"{path(outsider)}/members", json={"user": "@x:example.org"}, headers=auth),
        404, "not_member")
    room = await joined_room(appservice, mxid)
    members = f"{path(room)}/members"
    for status, expected, code in ((403, 403, "forbidden"), (404, 404, "user_not_found"),
                                   (500, 502, "homeserver_error")):
        appservice.transport.invite_user = returning(status)
        await problem_of(await appservice.client.post(members, json={"user": "@x:example.org"},
                                               headers=auth), expected, code)
    appservice.transport.invite_user = returning(200)
    appservice.transport.user_exists = returning(False)
    await problem_of(await appservice.client.post(members, json={"user": "@nobody:example.org"},
                                           headers=auth), 404, "user_not_found")

    async def profile_lookup_fails(*, user_id, as_identity):
        raise HomeserverError(500)
    appservice.transport.user_exists = profile_lookup_fails
    await problem_of(await appservice.client.post(members, json={"user": "@x:example.org"},
                                           headers=auth), 502, "homeserver_error")
    del appservice.transport.user_exists
    async def banned(*, room_id, user_id, as_identity):
        return "ban" if user_id == "@x:example.org" else "join"
    appservice.transport.membership_of = banned
    await problem_of(await appservice.client.post(members, json={"user": "@x:example.org"},
                                           headers=auth), 409, "banned")


async def test_removing_reports_what_the_homeserver_answered(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = await joined_room(appservice, mxid)
    target = f"{path(room)}/members/{quote(HUMAN, safe='')}"
    for status, expected, code in ((403, 403, "forbidden"), (500, 502, "homeserver_error")):
        appservice.transport.kick_user = returning(status)
        await problem_of(await appservice.client.delete(target, headers=auth), expected, code)


async def test_listing_members_needs_the_homeserver(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = await joined_room(appservice, mxid)
    appservice.transport.list_members = failing()
    await problem_of(await appservice.client.get(f"{path(room)}/members", headers=auth),
                     502, "homeserver_error")


async def test_messages_query_validation(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = await joined_room(appservice, mxid)
    for query in ("?limit=0", "?include=everything", "?unread=maybe"):
        await problem_of(await appservice.client.get(f"{path(room)}/events{query}", headers=auth),
                         400, "invalid_parameter")


async def test_history_needs_the_homeserver(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = await joined_room(appservice, mxid)
    appservice.transport.fetch_timeline = failing()
    await problem_of(await appservice.client.get(f"{path(room)}/events", headers=auth),
                     502, "homeserver_error")


async def test_cursors_are_only_the_ones_the_api_issued(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = await joined_room(appservice, mxid)
    forged = base64.urlsafe_b64encode(b"7").decode()
    for url in (f"/channels?cursor={forged}", "/channels?cursor=%%%",
                f"{path(room)}/members?cursor={forged}"):
        await problem_of(await appservice.client.get(url, headers=auth), 400, "invalid_cursor")
    await problem_of(await appservice.client.get(f"{path(room)}/members?limit=0", headers=auth),
                     400, "invalid_parameter")


async def test_events_that_are_not_there_are_not_found(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = await joined_room(appservice, mxid)
    text = await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "x"})
    event = f"{path(room)}/events/{quote(text['event_id'], safe='')}"
    missing = f"{path(room)}/events/{quote('$missing:example.org', safe='')}"
    await problem_of(await appservice.client.get(missing, headers=auth), 404, "event_not_found")
    await problem_of(await appservice.client.get(f"{event}/media", headers=auth),
                     404, "media_not_found")
    outsider = appservice.hs.create_room(HUMAN, [], encrypted=False)
    await problem_of(await appservice.client.get(
        f"{path(outsider)}/events/{quote(text['event_id'], safe='')}", headers=auth),
        404, "not_member")
    await problem_of(await appservice.client.get(f"{path(room)}/events/nope", headers=auth),
                     400, "invalid_event")


async def test_an_image_is_judged_by_its_bytes_not_its_name(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    svg = base64.b64encode(b"<svg/>").decode()
    await problem_of(await appservice.client.post(f"{path(room)}/events", json={
        "type": "image", "content": {"data_base64": svg, "filename": "a.png"}}, headers=auth),
        422, "invalid_data_base64")


async def test_send_reports_speech_and_homeserver_failures(make_appservice):
    appservice = await make_appservice(e2ee=False, speech={"tts": FakeSynthesizer(fail=True), "stt": None})
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    events = f"{path(room)}/events"
    await problem_of(await appservice.client.post(
        events, json={"type": "audio", "content": {"transcript": "hola"}}, headers=auth),
        503, "tts_unavailable")
    appservice.transport.send_event = failing()
    await problem_of(await appservice.client.post(
        events, json={"type": "text", "content": {"text": "hola"}}, headers=auth),
        502, "homeserver_error")


async def test_the_body_is_checked_before_the_channel(make_appservice):
    appservice = await make_appservice(e2ee=False)
    _mxid, auth = await appservice.register("nova")
    outsider = path(appservice.hs.create_room(HUMAN, [], encrypted=False))
    for method, url, payload, code in (
            ("POST", f"{outsider}/events", {"type": "sticker"}, "invalid_type"),
            ("PUT", f"{outsider}/receipt", {}, "event_id_required"),
            ("POST", f"{outsider}/members", {"user": "x"}, "invalid_user")):
        await problem_of(await appservice.client.request(method, url, json=payload, headers=auth),
                         422, code)


async def test_bodies_take_only_their_fields(make_appservice):
    appservice = await make_appservice(e2ee=False, webhooks={"allowed_targets": ["*"]})
    mxid, auth = await appservice.register("nova")
    room = path(await joined_room(appservice, mxid))
    for method, url, payload in (
            ("POST", "/channels", {"name": "team", "colour": "red"}),
            ("POST", f"{room}/events", {"type": "text", "content": {"text": "hi"}, "to": "x"}),
            ("POST", f"{room}/events", {"type": "poll_response", "content": {
                "poll_event_id": "$p", "answers": ["a"], "extra": 1}}),
            ("PUT", f"{room}/receipt", {"event_id": "$e", "thread": "main"}),
            ("POST", f"{room}/members", {"user": HUMAN, "reason": "x"}),
            ("PUT", "/identities/me/webhook", {"url": "http://10.0.0.5/", "events": []}),
            ("PATCH", "/identities/me", {"matrix": {"bio": "x"}})):
        await problem_of(await appservice.client.request(method, url, json=payload, headers=auth),
                         422, "unknown_field")


async def test_a_failing_homeserver_is_a_bad_gateway_not_an_absence(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)

    appservice.transport.membership_of = failing()
    body = await problem_of(await appservice.client.get(path(room), headers=auth),
                            502, "homeserver_error")
    assert body["upstream"] == {"status": 500}


async def test_a_receipt_the_homeserver_refuses_stays_unread(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = await joined_room(appservice, mxid)
    text = await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "x"})

    appservice.transport.mark_room_read = failing()
    await problem_of(await appservice.client.put(f"{path(room)}/receipt",
                                           json={"event_id": text["event_id"]}, headers=auth),
                     502, "homeserver_error")
    channel = await (await appservice.client.get(path(room), headers=auth)).json()
    assert channel["unread"] == 1


async def test_profile_values_are_checked_before_anything_changes(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    for payload, code in (({"matrix": {"display_name": 7}}, "invalid_display_name"),
                          ({"matrix": {"display_name": "N"}, "tts": "x"}, "invalid_tts"),
                          ({"matrix": {"display_name": "N"}, "stt": {"file": "x"}},
                           "reserved_option"),
                          ({"tts": {f"k{n}": n for n in range(65)}}, "options_too_large")):
        await problem_of(await appservice.client.patch("/identities/me", json=payload, headers=auth),
                         422, code)
    identity = await (await appservice.client.get("/identities/me", headers=auth)).json()
    assert identity["matrix"]["display_name"] == "Nova"


async def test_required_fields_and_stt_options(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = await joined_room(appservice, mxid)
    await problem_of(await appservice.client.post("/identities", json={}), 422, "name_required")
    await problem_of(await appservice.client.post(f"{path(room)}/members", json={}, headers=auth),
                     422, "user_required")
    await problem_of(await appservice.client.patch("/identities/me", json={"stt": "es"}, headers=auth),
                     422, "invalid_stt")


async def test_listing_needs_the_homeserver_to_say_what_is_encrypted(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = await joined_room(appservice, mxid)
    await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "x"})

    appservice.transport.is_room_encrypted = failing()
    await problem_of(await appservice.client.get("/channels", headers=auth), 502, "homeserver_error")


async def test_the_avatar_answers_502_when_the_homeserver_fails(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    avatar = "/identities/me/avatar"
    png = {**auth, "Content-Type": "image/png"}
    appservice.transport.upload_media = failing()
    await problem_of(await appservice.client.put(avatar, data=PNG, headers=png), 502, "homeserver_error")
    appservice.hs.profiles.setdefault(mxid, {})["avatar_url"] = "mxc://example.org/gone"
    await problem_of(await appservice.client.get(avatar, headers=auth), 502, "homeserver_error")
    appservice.transport.set_avatar_url = failing()
    await problem_of(await appservice.client.delete(avatar, headers=auth), 502, "homeserver_error")


async def test_a_speech_provider_refusal_gives_its_status_and_keeps_its_answer_in_the_log(
        make_appservice):
    synthesizer = FakeSynthesizer(fail=True, provider_status=400)
    appservice = await make_appservice(e2ee=False, speech={"tts": synthesizer, "stt": None})
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)
    body = await problem_of(await appservice.client.post(
        f"{path(room)}/events", json={"type": "audio", "content": {"transcript": "hola"}},
        headers=auth), 503, "tts_unavailable")
    assert body["upstream"] == {"status": 400}
    assert "alba" not in json.dumps(body)


async def test_registration_survives_a_homeserver_that_refuses_the_display_name(
        make_appservice):
    appservice = await make_appservice(e2ee=False)
    appservice.transport.failing["set_display_name"] = 500
    mxid, auth = await appservice.register("nova")
    identity = await (await appservice.client.get("/identities/me", headers=auth)).json()
    assert identity["mxid"] == mxid


async def test_an_unread_event_the_homeserver_no_longer_has_is_forgotten(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = await joined_room(appservice, mxid)
    gone = await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "x"})
    del appservice.hs.events[gone["event_id"]]
    page = await (await appservice.client.get(
        f"{path(room)}/events?unread=true", headers=auth)).json()
    assert page["events"] == []
    channel = await (await appservice.client.get(path(room), headers=auth)).json()
    assert channel["unread"] == 0


async def test_an_unread_event_stays_when_the_homeserver_fails_to_return_it(make_appservice):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = await joined_room(appservice, mxid)
    await appservice.deliver(room, HUMAN, "m.room.message", {"msgtype": "m.text", "body": "x"})
    appservice.transport.failing["fetch_event"] = 500
    await problem_of(await appservice.client.get(
        f"{path(room)}/events?unread=true", headers=auth), 502, "homeserver_error")
    del appservice.transport.failing["fetch_event"]
    channel = await (await appservice.client.get(path(room), headers=auth)).json()
    assert channel["unread"] == 1


async def test_a_homeserver_that_does_not_answer_is_a_bad_gateway_with_no_upstream(
        make_appservice):
    appservice = await make_appservice()
    _mxid, auth = await appservice.register("nova")
    appservice.transport.fetch_profile = failing(None)
    body = await problem_of(await appservice.client.get("/identities/me", headers=auth),
                            502, "homeserver_error")
    assert body["detail"] == "the homeserver did not answer" and "upstream" not in body
