import ipaddress
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from contract import RECORDER, declared  # noqa: E402
from fakes import (  # noqa: E402
    ADMIN_TOKEN, AS_TOKEN, HS_TOKEN, SERVER, FakeEncryptionRegistry, FakeHomeserver,
    FakeTransport)
from matrix_for_ai.__main__ import compose  # noqa: E402
from matrix_for_ai.services import webhook_notifier  # noqa: E402
from matrix_for_ai.configuration import parse_configuration  # noqa: E402


def base_configuration(tmp_path, **overrides) -> dict:
    raw = {
        "homeserver": {"url": "http://homeserver:8008", "server_name": SERVER},
        "appservice": {"id": "test", "as_token": AS_TOKEN, "hs_token": HS_TOKEN, "e2ee": True},
        "admin_token": ADMIN_TOKEN,
        "storage": {"mailbox_dir": str(tmp_path / "mailboxes"),
                    "crypto_dir": str(tmp_path / "crypto")},
    }
    raw.update(overrides)
    return raw


@dataclass
class RunningAppservice:
    client: object
    push_client: object
    hs: FakeHomeserver
    transport: FakeTransport
    encryption: FakeEncryptionRegistry | None
    event_router: object
    speech: dict

    async def register(self, name: str) -> tuple[str, dict]:
        response = await self.client.post("/identities", json={"name": name})
        assert response.status == 201, await response.text()
        body = await response.json()
        return body["mxid"], {"Authorization": f"Bearer {body['api_token']}"}

    async def push(self, *events: dict) -> None:
        response = await self.push_client.put(
            "/_matrix/app/v1/transactions/t1", json={"events": list(events)},
            headers={"Authorization": f"Bearer {HS_TOKEN}"})
        assert response.status == 200

    async def deliver(self, room_id, sender, event_type, content) -> dict:
        event = self.hs.add_event(room_id, sender, event_type, content)
        await self.push(event)
        return event

    async def join(self, room_id: str, mxid: str) -> None:
        self.hs.rooms[room_id]["invited"].discard(mxid)
        self.hs.rooms[room_id]["members"].add(mxid)
        await self.push(self.hs.add_event(
            room_id, mxid, "m.room.member", {"membership": "join"}, state_key=mxid))


@pytest.fixture
def make_appservice(aiohttp_client, tmp_path):
    async def factory(*, e2ee: bool = True, speech: dict | None = None,
                      **overrides) -> RunningAppservice:
        raw = base_configuration(tmp_path, **overrides)
        raw["appservice"]["e2ee"] = e2ee
        configuration = parse_configuration(raw)
        hs = FakeHomeserver()
        session = aiohttp.ClientSession()
        speech = speech or {"tts": None, "stt": None}
        transport = FakeTransport(hs)
        encryption = FakeEncryptionRegistry(hs) if e2ee else None
        application, appservice_application, _registry, event_router = compose(
            configuration, session, transport, encryption, speech=speech)
        application.middlewares.insert(0, RECORDER.middleware)
        application.on_cleanup.append(lambda _app: session.close())
        return RunningAppservice(
            client=await aiohttp_client(application),
            push_client=await aiohttp_client(appservice_application),
            hs=hs, transport=transport, encryption=encryption, event_router=event_router,
            speech=speech)
    return factory


@pytest.fixture(autouse=True)
def dns(monkeypatch) -> dict:
    names: dict[str, list[str]] = {}

    async def resolve(host: str, port: int) -> list:
        if host not in names:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [ipaddress.ip_address(address) for address in names[host]]

    monkeypatch.setattr(webhook_notifier, "resolve_addresses", resolve)
    return names


@pytest.fixture(autouse=True)
def follows_the_contract():
    start = len(RECORDER.violations)
    yield
    broken = RECORDER.violations[start:]
    assert not broken, "the API strayed from openapi.json:\n" + "\n".join(broken)


def pytest_sessionfinish(session, exitstatus):
    option = session.config.option
    if option.keyword or option.markexpr or any("::" in arg for arg in session.config.args):
        return
    if not any(item.module.__name__ == "test_api" for item in session.items):
        return
    missing = sorted(declared() - RECORDER.seen, key=str)
    if missing:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        reporter.write_line(f"\nopenapi.json documents {len(missing)} outcomes no test produced:",
                            red=True)
        for method, route, status, code in missing:
            reporter.write_line(f"  {method.upper()} {route} → {status} {code or ''}")
        session.exitstatus = 1
