"""Authentication and bind-address safety.

v0.1 bound 0.0.0.0 with no auth and an unauthenticated destructive /clear, while
PROTOCOL.md:353 said it SHOULD bind localhost.
"""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jnaapakam.config import Config, ConfigError
from jnaapakam.server import build_app


@pytest.fixture
async def secured(config, llm):
    cfg = Config(db_path=config.db_path, auth_token="s3cret-token", host="0.0.0.0", port=0)
    async with TestClient(TestServer(build_app(cfg, chat=llm))) as c:
        yield c


async def test_a_request_without_a_token_is_refused(secured):
    assert (await secured.get("/status")).status == 401


async def test_the_health_endpoint_is_public_when_auth_is_required(secured):
    """Platform startup probes must not need credentials to learn the server is up."""
    resp = await secured.get("/health")
    assert resp.status == 200
    assert (await resp.json())["status"] == "ok"


async def test_the_info_endpoints_are_public_when_auth_is_required(secured):
    """Server metadata (name/version/tools) is not memory data and stays public."""
    assert (await secured.get("/")).status == 200
    assert (await secured.get("/mcp")).status == 200


async def test_a_request_with_the_wrong_token_is_refused(secured):
    resp = await secured.get("/status", headers={"Authorization": "Bearer wrong"})

    assert resp.status == 401


async def test_a_request_bearing_the_configured_token_succeeds(secured):
    resp = await secured.get("/status", headers={"Authorization": "Bearer s3cret-token"})

    assert resp.status == 200


async def test_the_destructive_clear_endpoint_is_also_protected(secured):
    assert (await secured.post("/clear")).status == 401


async def test_the_mcp_surface_is_public_when_auth_is_required(secured):
    """The MCP bridge cannot send credentials; the MCP tools hold no destructive ops."""
    resp = await secured.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert resp.status == 200
    body = await resp.json()
    assert "tools" in body.get("result", {})


async def test_loopback_binding_without_a_token_is_allowed_for_local_development(config, llm):
    async with TestClient(TestServer(build_app(config, chat=llm))) as c:
        assert (await c.get("/status")).status == 200


def test_binding_a_public_interface_without_a_token_is_refused_at_startup():
    """Refusing to start beats silently exposing a wipeable memory store."""
    with pytest.raises(ConfigError):
        Config(db_path=":memory:", auth_token=None, host="0.0.0.0", port=8080).validate()


def test_binding_a_public_interface_with_a_token_is_accepted_at_startup():
    Config(db_path=":memory:", auth_token="tok", host="0.0.0.0", port=8080).validate()
