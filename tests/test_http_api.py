"""End-to-end HTTP behaviour against a live aiohttp server."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jnaapakam.server import STORE_KEY, build_app


@pytest.fixture
async def client(config, llm):
    app = build_app(config, chat=llm)
    async with TestClient(TestServer(app)) as c:
        yield c


async def _ingest(client, text, source="test"):
    resp = await client.post("/ingest", json={"text": text, "source": source})
    assert resp.status == 200, await resp.text()
    return (await resp.json())["memory_id"]


async def test_a_memory_ingested_long_ago_is_still_searchable(client):
    target = await _ingest(client, "the user prefers vim keybindings and dark mode")
    for i in range(59):
        await _ingest(client, f"routine note {i} covering unrelated build tooling")

    resp = await client.get("/search", params={"q": "vim keybindings"})

    assert resp.status == 200
    assert target in [m["id"] for m in (await resp.json())["memories"]]


async def test_search_returns_records_with_scores_rather_than_prose(client):
    await _ingest(client, "the user switched the project from React to Svelte")

    body = await (await client.get("/search", params={"q": "Svelte"})).json()

    assert body["memories"], "expected a hit"
    hit = body["memories"][0]
    assert {"id", "summary", "source", "created_at", "score"} <= set(hit)


async def test_a_non_numeric_limit_is_rejected_as_a_client_error(client):
    resp = await client.get("/memories", params={"limit": "abc"})

    assert resp.status == 400


async def test_a_negative_limit_is_rejected_as_a_client_error(client):
    resp = await client.get("/memories", params={"limit": "-5"})

    assert resp.status == 400


async def test_deleting_without_a_memory_id_is_a_client_error(client):
    resp = await client.post("/delete", json={})

    assert resp.status == 400


async def test_deleting_a_memory_that_does_not_exist_reports_not_found(client):
    resp = await client.post("/delete", json={"memory_id": 999999})

    assert resp.status == 404


async def test_restoring_a_malformed_backup_is_a_client_error_not_a_crash(client):
    resp = await client.post("/restore", json={"memories": [{"summary": "no raw text"}]})

    assert resp.status == 400


async def test_ingesting_empty_text_is_rejected(client):
    resp = await client.post("/ingest", json={"text": "   "})

    assert resp.status == 400


async def test_a_backup_round_trips_through_restore(client):
    await _ingest(client, "the user timezone is UTC+5:30")
    backup = await (await client.get("/backup")).json()

    await client.post("/clear")
    assert (await (await client.get("/status")).json())["total_memories"] == 0

    await client.post("/restore", json=backup)

    assert (await (await client.get("/status")).json())["total_memories"] == 1
    assert (await (await client.get("/search", params={"q": "timezone"})).json())["memories"]


async def test_an_llm_failure_surfaces_instead_of_silently_storing_a_degraded_memory(config):
    async def failing_llm(model, system, message):
        raise RuntimeError("model endpoint returned 404")

    app = build_app(config, chat=failing_llm)
    async with TestClient(TestServer(app)) as c:
        resp = await c.post("/ingest", json={"text": "something worth remembering"})

        assert resp.status >= 500, "a dead model must not be reported to the caller as success"


async def test_mcp_tools_listing_exposes_a_search_tool(client):
    resp = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )

    names = [t["name"] for t in (await resp.json())["result"]["tools"]]
    assert "search_memory" in names


async def test_mcp_search_tool_returns_the_stored_memory(client):
    await _ingest(client, "the deadline for the rust CLI tool is March 15")

    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "search_memory", "arguments": {"query": "rust deadline"}},
        },
    )

    result = (await resp.json())["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["memories"]


async def test_prune_requires_a_policy_to_apply(client):
    resp = await client.post("/prune")

    assert resp.status == 400


async def test_prune_applies_an_age_policy(client):
    old = await _ingest(client, "a note from a project that wound up years ago")
    client.app[STORE_KEY].db.execute(
        "UPDATE memories SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (old,)
    )
    client.app[STORE_KEY].db.commit()
    fresh = await _ingest(client, "a note from this week about the release")

    body = await (await client.post("/prune", params={"older_than_days": "90"})).json()

    assert body["archived"] == 1
    assert client.app[STORE_KEY].get_memory(old)["archived"] is True
    assert client.app[STORE_KEY].get_memory(fresh)["archived"] is False


async def test_an_age_policy_still_applies_when_the_count_policy_is_satisfied(client):
    """The two policies are independent: being under the cap does not make a memory fresh."""
    ids = [await _ingest(client, f"note number {i} about the build pipeline") for i in range(4)]
    store = client.app[STORE_KEY]
    store.db.execute(
        "UPDATE memories SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (ids[0],)
    )
    store.db.commit()

    body = await (
        await client.post("/prune", params={"older_than_days": "90", "keep": "4"})
    ).json()

    assert body["archived"] == 1, "keep=4 evicts nothing; the age policy still retires the stale one"
    assert store.get_memory(ids[0])["archived"] is True
