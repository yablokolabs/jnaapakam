"""The operator dashboard.

A local read-only view of what the agent remembers. It is a tool for the person
running the server, not a feature of the deployment: on a public bind it is not
served at all, so exposing jnaapakam to a network never also exposes a browsable
window onto its memory.
"""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jnaapakam.server import build_app


@pytest.fixture
async def client(config, llm):
    async with TestClient(TestServer(build_app(config, chat=llm))) as c:
        yield c


async def test_the_dashboard_is_served_on_a_local_bind(client):
    resp = await client.get("/dashboard")

    assert resp.status == 200
    assert resp.content_type == "text/html"
    assert "jñāpakaṁ" in await resp.text()


async def test_the_dashboard_is_not_served_on_a_public_bind(config, llm):
    """Binding to a network must not also publish a window onto the memory.

    Holding the token is not enough: the page is withheld from a public bind
    entirely, so exposing the API never also exposes a browsable memory viewer.
    """
    config.host = "0.0.0.0"
    config.auth_token = "a-token-that-makes-the-public-bind-legal"

    async with TestClient(TestServer(build_app(config, chat=llm))) as client:
        anonymous = await client.get("/dashboard")
        credentialed = await client.get(
            "/dashboard",
            headers={"Authorization": "Bearer a-token-that-makes-the-public-bind-legal"},
        )

    assert anonymous.status == 401
    assert credentialed.status == 404


async def test_the_dashboard_needs_the_token_when_one_is_configured(config, llm):
    config.auth_token = "local-but-authenticated"

    async with TestClient(TestServer(build_app(config, chat=llm))) as client:
        unauthenticated = await client.get("/dashboard")
        authenticated = await client.get(
            "/dashboard", headers={"Authorization": "Bearer local-but-authenticated"}
        )

    assert unauthenticated.status == 401
    assert authenticated.status == 200


async def test_the_dashboard_reads_through_the_documented_endpoints(client):
    """It ships no private API: everything on the page is a documented route."""
    page = await (await client.get("/dashboard")).text()

    assert "/status" in page
    assert "/search" in page
