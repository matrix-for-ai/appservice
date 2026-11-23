import secrets

import aiohttp
import pytest
import pytest_asyncio
from live import (
    BASE_URL,
    HOMESERVER_URL,
    HUMAN_PASSWORD,
    HUMAN_USER,
    REMOTE_URL,
    Human,
    register_identity,
    wait_until_homeserver_reaches_appservice,
)
from nio import AsyncClient, AsyncClientConfig, LoginResponse


@pytest.fixture(scope="session", autouse=True)
def homeserver_reaches_appservice():
    if BASE_URL:
        for base_url in {BASE_URL, REMOTE_URL}:
            wait_until_homeserver_reaches_appservice(base_url)


@pytest.fixture
async def pair():
    async with aiohttp.ClientSession() as session:
        run = secrets.token_hex(3)
        yield [await register_identity(session, "alice", run),
               await register_identity(session, "bob", run, REMOTE_URL)]


@pytest_asyncio.fixture(loop_scope="session")
async def ai():
    async with aiohttp.ClientSession() as session:
        yield await register_identity(session, "ai", secrets.token_hex(3))


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def human(tmp_path_factory):
    client = AsyncClient(
        HOMESERVER_URL, HUMAN_USER, store_path=str(tmp_path_factory.mktemp("human")),
        config=AsyncClientConfig(encryption_enabled=True))
    login = await client.login(HUMAN_PASSWORD, device_name="e2e")
    assert isinstance(login, LoginResponse), login
    try:
        async with aiohttp.ClientSession() as session:
            human = Human(client, session)
            await human.sync(full_state=True)
            yield human
    finally:
        await client.logout()
        await client.close()
