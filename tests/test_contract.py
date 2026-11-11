import ast
from fnmatch import fnmatch
from pathlib import Path

import pytest

from urllib.parse import quote

from contract import SPEC, declared
from fakes import ADMIN_TOKEN
from matrix_for_ai.ports.matrix_transport import HomeserverError

HUMAN = "@human:example.org"
_VALID_BODIES = {"/channels/{room}/events": {"type": "text", "content": {"text": "hi"}},
                 "/channels/{room}/receipt": {"event_id": "$e1:example.org"},
                 "/channels/{room}/members": {"user": "@x:example.org"}}

PACKAGE = Path(__file__).resolve().parent.parent / "matrix_for_ai"
_FILLERS = {"{id}": "me", "{room}": "%21r1%3Aexample.org", "{event}": "%24e1%3Aexample.org",
            "{user}": "%40u%3Aexample.org"}


def raised_errors() -> set:
    raised = set()
    for source in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ApiError":
                status, code = node.args[:2]
                if isinstance(code, ast.JoinedStr):
                    raised.add((status.value, "".join(
                        part.value if isinstance(part, ast.Constant) else "*"
                        for part in code.values)))
                else:
                    raised.add((status.value, code.value))
    return raised


def test_every_error_the_code_raises_is_documented():
    documented = {(status, code) for _m, _p, status, code in declared() if code}
    undocumented = {
        (status, code) for status, code in raised_errors()
        if not any(status == s and fnmatch(c, code) for s, c in documented)}
    assert not undocumented, f"raised but not in openapi.json: {sorted(undocumented)}"


def _url(route: str, **overrides) -> str:
    for placeholder, value in {**_FILLERS, **overrides}.items():
        route = route.replace(placeholder, value)
    return route


def _declaring(status: int, code: str) -> list:
    return sorted({(method, route) for method, route, s, c in declared()
                   if (s, c) == (status, code)})


async def _problem(response, status, code):
    assert response.status == status, await response.text()
    assert (await response.json())["code"] == code


SIGNUP = ("post", "/identities")


@pytest.mark.parametrize("method, route", _declaring(401, "unauthorized"))
async def test_every_call_without_a_token_is_unauthorized(make_appservice, method, route):
    appservice = await make_appservice()
    await _problem(await appservice.client.request(method, _url(route)), 401, "unauthorized")


@pytest.mark.parametrize("method, route", _declaring(400, "invalid_room"))
async def test_every_call_takes_only_a_room_id(make_appservice, method, route):
    appservice = await make_appservice()
    _mxid, auth = await appservice.register("nova")
    response = await appservice.client.request(
        method, _url(route, **{"{room}": "%40someone%3Aexample.org"}), headers=auth)
    await _problem(response, 400, "invalid_room")


@pytest.mark.parametrize("method, route", _declaring(400, "unknown_parameter"))
async def test_every_call_refuses_unknown_query_parameters(make_appservice, method, route):
    appservice = await make_appservice()
    _mxid, auth = await appservice.register("nova")
    await _problem(await appservice.client.request(method, _url(route) + "?nope=1", headers=auth),
                   400, "unknown_parameter")


def _takes_json(method: str, route: str) -> bool:
    body = SPEC["paths"][route][method].get("requestBody", {}).get("content", {})
    return "application/json" in body


@pytest.mark.parametrize("method, route", [
    call for call in _declaring(400, "body_required") if _takes_json(*call)])
async def test_every_json_body_must_be_a_json_object(make_appservice, method, route):
    appservice = await make_appservice()
    _mxid, auth = await appservice.register("nova")
    url = _url(route)
    await _problem(await appservice.client.request(method, url, headers=auth), 400, "body_required")
    await _problem(await appservice.client.request(
        method, url, data="x", headers={**auth, "Content-Type": "text/plain"}),
        415, "unsupported_media_type")
    await _problem(await appservice.client.request(
        method, url, data="{nope", headers={**auth, "Content-Type": "application/json"}),
        400, "bad_json")


@pytest.mark.parametrize("method, route", [
    (method, route) for method, route in _declaring(403, "forbidden")
    if route.startswith("/identities")])
async def test_a_token_reaches_only_its_own_identity(make_appservice, method, route):
    appservice = await make_appservice()
    other, _ = await appservice.register("bob")
    _mxid, auth = await appservice.register("alice")
    url = _url(route, **{"{id}": other[1:].split(":")[0]})
    await _problem(await appservice.client.request(method, url, headers=auth), 403, "forbidden")


@pytest.mark.parametrize("method, route", [
    call for call in _declaring(429, "rate_limited") if call != SIGNUP])
async def test_every_call_with_a_token_is_rate_limited_before_anything_else(
        make_appservice, method, route):
    appservice = await make_appservice(limits={"requests_per_minute": 1})
    _mxid, auth = await appservice.register("nova")
    assert (await appservice.client.get("/identities/me", headers=auth)).status == 200
    response = await appservice.client.request(method, _url(route), headers=auth)
    await _problem(response, 429, "rate_limited")
    assert 1 <= int(response.headers["Retry-After"]) <= 60


@pytest.mark.parametrize("method, route", _declaring(404, "identity_not_found"))
async def test_the_admin_naming_a_missing_identity_is_not_found(make_appservice, method, route):
    appservice = await make_appservice()
    response = await appservice.client.request(method, _url(route, **{"{id}": "ai_ghost"}),
                                        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    await _problem(response, 404, "identity_not_found")


def _channel_url(route: str, room: str) -> str:
    return _url(route, **{"{room}": quote(room, safe="")})


def _valid_body(route: str):
    return _VALID_BODIES.get(route)


@pytest.mark.parametrize("method, route", _declaring(404, "not_member"))
async def test_every_channel_call_answers_not_member_outside_the_channel(make_appservice, method, route):
    appservice = await make_appservice(e2ee=False)
    _mxid, auth = await appservice.register("nova")
    outsider = appservice.hs.create_room(HUMAN, [], encrypted=False)
    await _problem(await appservice.client.request(
        method, _channel_url(route, outsider), json=_valid_body(route), headers=auth),
        404, "not_member")


@pytest.mark.parametrize("method, route", [
    (method, route) for method, route in _declaring(502, "homeserver_error")
    if (method, route) in _declaring(404, "not_member")])
async def test_every_channel_call_answers_502_when_the_homeserver_fails(make_appservice, method, route):
    appservice = await make_appservice(e2ee=False)
    mxid, auth = await appservice.register("nova")
    room = appservice.hs.create_room(HUMAN, [mxid], encrypted=False)

    async def failing(**_kwargs):
        raise HomeserverError(500)
    appservice.transport.membership_of = failing
    await _problem(await appservice.client.request(
        method, _channel_url(route, room), json=_valid_body(route), headers=auth),
        502, "homeserver_error")


@pytest.mark.parametrize("method, route", _declaring(400, "invalid_event"))
async def test_every_call_takes_only_an_event_id(make_appservice, method, route):
    appservice = await make_appservice()
    _mxid, auth = await appservice.register("nova")
    await _problem(await appservice.client.request(method, _url(route, **{"{event}": "nope"}),
                                            headers=auth), 400, "invalid_event")


def test_the_version_header_is_the_contracts_major():
    major = int(SPEC["info"]["version"].split(".")[0])
    assert SPEC["components"]["headers"]["X-API-Version"]["schema"]["const"] == major
