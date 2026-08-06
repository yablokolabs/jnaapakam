"""Regressions from the adversarial review of the v0.2 diff.

Each test here corresponds to a defect that was filed against this branch and
independently reproduced before being fixed. Numbers refer to the review findings.
"""

import sqlite3
import tempfile

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jnaapakam.config import Config, ConfigError, _is_loopback
from jnaapakam.server import build_app
from jnaapakam.store import Store, build_match_query


def _add(store, text, namespace="", **kw):
    return store.add_memory(
        raw_text=text,
        summary=kw.get("summary", text[:120]),
        entities=kw.get("entities", []),
        topics=kw.get("topics", []),
        importance=kw.get("importance", 0.5),
        source="test",
        namespace=namespace,
    )


# ---- [12] an empty bind address is not loopback -------------------------


@pytest.mark.parametrize("host", ["", "0.0.0.0", "::", "0", "127.0.0.1.evil.com", "example.com"])
def test_a_non_loopback_host_is_never_treated_as_local(host):
    assert not _is_loopback(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_genuine_loopback_hosts_are_recognised(host):
    assert _is_loopback(host)


def test_an_empty_host_is_refused_without_a_token():
    """`MEMORY_HOST=` in a compose file must not silently open the server to the LAN."""
    with pytest.raises(ConfigError):
        Config(db_path=":memory:", auth_token=None, host="", port=8889).validate()


def test_an_empty_host_environment_variable_falls_back_to_loopback(monkeypatch):
    monkeypatch.setenv("MEMORY_HOST", "")

    assert Config.from_env(db_path=":memory:").host == "127.0.0.1"


# ---- [20][4] backup/restore must preserve identity ----------------------


def test_a_backup_round_trip_preserves_memory_ids(store):
    first = _add(store, "emergency contact is Alice")
    second = _add(store, "emergency contact is Bob")

    backup = store.export_all()
    store.clear()
    store.import_all(backup)

    assert sorted(m["id"] for m in store.list_memories()) == sorted([first, second])


def test_a_backup_round_trip_preserves_supersession_chains(store):
    old = _add(store, "emergency contact is Alice")
    new = _add(store, "emergency contact is Bob")
    store.supersede(old, new)

    backup = store.export_all()
    store.clear()
    store.import_all(backup)

    assert store.get_memory(old)["superseded_by"] == new
    assert [m["id"] for m in store.search("emergency contact")] == [new]


def test_a_backup_round_trip_preserves_connections_and_provenance(store):
    a = _add(store, "the deploy pipeline uses github actions")
    b = _add(store, "the deploy pipeline caches rust builds")
    store.record_consolidation([a, b], "sum", "insight", [{"from_id": a, "to_id": b, "relationship": "r"}])

    backup = store.export_all()
    store.clear()
    store.import_all(backup)

    assert store.get_memory(a)["connections"][0]["linked_to"] == b
    assert store.consolidation_history()[0]["source_ids"] == [a, b]


def test_restoring_into_a_populated_store_does_not_hijack_existing_ids(store):
    """A merge must never make one namespace's memory supersede another's."""
    _add(store, "prod database is mysql", namespace="project-a")
    other = Store(tempfile.mktemp(suffix=".db"))
    other.initialize()
    try:
        old = _add(other, "my emergency contact is Alice", namespace="personal")
        new = _add(other, "my emergency contact is Bob", namespace="personal")
        other.supersede(old, new)
        backup = other.export_all()
    finally:
        other.close()

    store.import_all(backup)

    for memory in store.list_memories(namespace="personal"):
        pointer = memory["superseded_by"]
        if pointer is not None:
            assert store.get_memory(pointer)["namespace"] == "personal"


# ---- [6][21] a rejected restore must leave nothing behind ---------------


def test_a_restore_that_fails_midway_writes_nothing(store):
    _add(store, "a pre-existing memory")
    before = store.stats()["total_memories"]

    with pytest.raises(ValueError):
        store.import_all(
            {
                "memories": [
                    {"raw_text": "ok", "summary": "ok", "created_at": "2026-01-01T00:00:00+00:00"},
                    {
                        "raw_text": "bad",
                        "summary": "bad",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "importance": "not-a-number",
                    },
                ]
            }
        )

    assert store.stats()["total_memories"] == before


def test_a_failed_restore_does_not_leave_a_transaction_open(store):
    with pytest.raises(ValueError):
        store.import_all(
            {"memories": [{"raw_text": "x", "summary": "x", "created_at": "t", "importance": "bad"}]}
        )

    assert not store.db.in_transaction


def test_a_later_read_cannot_commit_a_rejected_restore(store):
    with pytest.raises(ValueError):
        store.import_all(
            {
                "memories": [
                    {"raw_text": "ok", "summary": "ok", "created_at": "2026-01-01T00:00:00+00:00"},
                    {
                        "raw_text": "b",
                        "summary": "b",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "importance": "x",
                    },
                ]
            }
        )

    store.search("ok")

    assert store.stats()["total_memories"] == 0


# ---- [16] restored JSON columns must be well-formed ---------------------


def test_a_restore_carrying_malformed_json_columns_is_rejected(store):
    with pytest.raises(ValueError):
        store.import_all(
            {
                "memories": [
                    {
                        "raw_text": "x",
                        "summary": "x",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "entities": "{not json",
                    }
                ]
            }
        )


# ---- [2][3][5] supersession guards -------------------------------------


def test_a_supersession_cycle_is_refused(store):
    a = _add(store, "the preferred editor is vim")
    b = _add(store, "the preferred editor is helix")
    assert store.supersede(a, b)

    assert store.supersede(b, a) is False
    assert store.search("preferred editor"), "a namespace must never be left with no live memory"


def test_an_already_superseded_memory_is_not_re_superseded(store):
    a = _add(store, "the contact is Alice")
    b = _add(store, "the contact is Bob")
    c = _add(store, "the contact is Carol")
    store.supersede(a, c)

    assert store.supersede(a, b) is False
    assert store.get_memory(a)["superseded_by"] == c, "a deliberate correction must not be overwritten"


def test_superseding_with_an_archived_memory_is_refused(store):
    a = _add(store, "the contact is Alice")
    b = _add(store, "the contact is Bob")
    store.archive(b)

    assert store.supersede(a, b) is False


def test_deleting_a_replacement_does_not_strand_the_memory_it_replaced(store):
    old = _add(store, "emergency contact is Alice")
    new = _add(store, "emergency contact is Bob")
    store.supersede(old, new)

    store.delete_memory(new)

    assert store.search("emergency contact"), "deleting a replacement must not hide its predecessor forever"


def test_a_supersession_never_produces_an_inverted_validity_interval(store):
    first = _add(store, "the deadline is March 15")
    second = _add(store, "the deadline is April 2")

    store.supersede(second, first)

    row = store.get_memory(second)
    if row["valid_to"] is not None:
        assert row["valid_to"] >= row["valid_from"]


# ---- [15] query cost must be bounded ------------------------------------


def test_an_enormous_query_is_capped_rather_than_expanded_unboundedly(store):
    match = build_match_query(" ".join(["alpha"] * 8000))

    assert match.count(" OR ") < 100, "an unbounded OR expansion stalls every other request"


def test_capping_does_not_break_ordinary_queries(store):
    mid = _add(store, "the rust CLI deadline is March 15")

    assert mid in [h["id"] for h in store.search("rust CLI deadline")]


# ---- [9] usage accounting must never break a read ----------------------


def test_a_search_still_returns_results_when_usage_accounting_fails(store, monkeypatch):
    _add(store, "postgres connection pooling settings")

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_record_access", explode)

    assert store.search("postgres pooling"), "a read must not fail because a counter could not be written"


# ---- [8] a half-configured connection must never be cached -------------


def test_a_connection_is_only_cached_once_fully_configured(store):
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# ---- HTTP-level regressions --------------------------------------------


@pytest.fixture
async def client(config, llm):
    async with TestClient(TestServer(build_app(config, chat=llm))) as c:
        yield c


async def test_status_is_scoped_to_the_requested_namespace(client):
    await client.post("/ingest", json={"text": "a scoped memory", "namespace": "team-x"})
    await client.post("/ingest", json={"text": "an unscoped memory"})

    scoped = await (await client.get("/status", params={"namespace": "team-x"})).json()

    assert scoped["total_memories"] == 1


async def test_pruning_without_a_keep_value_is_a_client_error(client):
    assert (await client.post("/prune")).status == 400


async def test_a_cross_origin_write_is_refused(client):
    """Loopback is not an authentication boundary against a browser."""
    resp = await client.post("/clear", headers={"Origin": "https://evil.example"})

    assert resp.status == 403


async def test_a_same_origin_write_still_works(client):
    assert (await client.post("/clear")).status == 200


async def test_an_internal_error_does_not_leak_details_over_mcp(config):
    async def leaky(model, system, message):
        raise RuntimeError("connect failed to /home/secret/path key=sk-ant-TOPSECRET")

    async with TestClient(TestServer(build_app(config, chat=leaky))) as c:
        resp = await c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ingest_memory", "arguments": {"text": "hello"}},
            },
        )

        message = (await resp.json())["error"]["message"]
        assert "TOPSECRET" not in message
        assert "/home/secret" not in message
