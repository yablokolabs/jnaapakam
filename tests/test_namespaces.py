"""Namespace isolation.

PROTOCOL v0.2 §6 recorded the gap this closes: `source` was a free-text label, so
agents on unrelated projects retrieved each other's memories. That also blocks
memory correction — a fact true in one project and false in another is
indistinguishable from a genuine contradiction until scopes are enforceable.
"""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jnaapakam.server import build_app


def _ingest(store, text, namespace="", **kw):
    return store.add_memory(
        raw_text=text,
        summary=kw.get("summary", text[:120]),
        entities=[],
        topics=[],
        importance=kw.get("importance", 0.5),
        source=kw.get("source", "test"),
        namespace=namespace,
    )


def test_a_memory_in_one_namespace_is_invisible_to_another(store):
    _ingest(store, "the user prefers vim keybindings", namespace="project-a")

    assert store.search("vim", namespace="project-a")
    assert store.search("vim", namespace="project-b") == []


def test_the_same_claim_can_be_true_in_two_namespaces_at_once(store):
    """Conflict precision: a scope difference is not a contradiction."""
    a = _ingest(store, "the preferred editor is vim", namespace="project-a")
    b = _ingest(store, "the preferred editor is VS Code", namespace="project-b")

    assert [h["id"] for h in store.search("preferred editor", namespace="project-a")] == [a]
    assert [h["id"] for h in store.search("preferred editor", namespace="project-b")] == [b]


def test_memories_default_to_the_shared_namespace(store):
    """Single-tenant users who never pass a namespace keep working unchanged."""
    mid = _ingest(store, "a memory with no namespace given")

    hits = store.search("namespace given")

    assert [h["id"] for h in hits] == [mid]
    assert hits[0]["namespace"] == ""


def test_listing_memories_is_scoped_to_its_namespace(store):
    _ingest(store, "note in alpha", namespace="alpha")
    _ingest(store, "note in beta", namespace="beta")

    assert len(store.list_memories(namespace="alpha")) == 1
    assert len(store.list_memories(namespace="beta")) == 1


def test_counts_are_reported_per_namespace(store):
    _ingest(store, "one", namespace="alpha")
    _ingest(store, "two", namespace="alpha")
    _ingest(store, "three", namespace="beta")

    assert store.stats(namespace="alpha")["total_memories"] == 2
    assert store.stats(namespace="beta")["total_memories"] == 1
    assert store.stats()["total_memories"] == 3, "an unscoped call still sees everything"


def test_clearing_one_namespace_leaves_the_others_intact(store):
    _ingest(store, "disposable note", namespace="scratch")
    keeper = _ingest(store, "important note", namespace="keep")

    store.clear(namespace="scratch")

    assert store.get_memory(keeper) is not None
    assert store.stats(namespace="scratch")["total_memories"] == 0


def test_consolidation_does_not_mix_memories_across_namespaces(store):
    _ingest(store, "alpha one", namespace="alpha")
    _ingest(store, "alpha two", namespace="alpha")
    _ingest(store, "beta one", namespace="beta")

    pending = store.unconsolidated(namespace="alpha")

    assert {m["namespace"] for m in pending} == {"alpha"}


# ---- HTTP surface ------------------------------------------------------


@pytest.fixture
async def client(config, llm):
    async with TestClient(TestServer(build_app(config, chat=llm))) as c:
        yield c


async def test_namespaced_ingest_is_only_visible_in_that_namespace(client):
    await client.post(
        "/ingest", json={"text": "the deploy target is staging", "namespace": "project-a"}
    )

    async def search_in(ns):
        resp = await client.get("/search", params={"q": "deploy target", "namespace": ns})
        return await resp.json()

    in_scope = await search_in("project-a")
    out_of_scope = await search_in("project-b")

    assert in_scope["memories"]
    assert out_of_scope["memories"] == []


async def test_search_without_a_namespace_sees_only_unscoped_memories(client):
    await client.post("/ingest", json={"text": "scoped note about kafka", "namespace": "team-x"})
    await client.post("/ingest", json={"text": "unscoped note about kafka"})

    body = await (await client.get("/search", params={"q": "kafka"})).json()

    assert len(body["memories"]) == 1
    assert body["memories"][0]["namespace"] == ""


async def test_mcp_search_honours_the_namespace_argument(client):
    await client.post("/ingest", json={"text": "rust toolchain pinned to 1.9", "namespace": "svc"})

    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_memory",
                "arguments": {"query": "rust toolchain", "namespace": "svc"},
            },
        },
    )

    assert (await resp.json())["result"]["structuredContent"]["memories"]
