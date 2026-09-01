"""Retention and forgetting.

Forgetting is soft: archived, never deleted. The research found no published decay
formula worth copying — only MemoryLLM's K/N replacement ratio — so the scoring here
is attributed to MemoryBank's composite of decay and access frequency rather than
presented as settled science, and nothing is destroyed automatically.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from aiohttp import web

from jnaapakam.cli import _background_loops
from jnaapakam.retention import retention_score
from jnaapakam.server import STORE_KEY


def _ingest(store, text, namespace="", **kw):
    return store.add_memory(
        raw_text=text,
        summary=kw.get("summary", text[:120]),
        entities=[],
        topics=[],
        importance=kw.get("importance", 0.5),
        source="test",
        namespace=namespace,
    )


NOW = "2026-08-05T12:00:00+00:00"


def candidate(**kw):
    base = {
        "importance": 0.5,
        "access_count": 0,
        "created_at": "2026-08-05T11:00:00+00:00",
        "last_accessed": None,
    }
    base.update(kw)
    return base


# ---- scoring is a pure function ----------------------------------------


def test_a_frequently_recalled_memory_outranks_an_ignored_one():
    used = candidate(access_count=10)
    ignored = candidate(access_count=0)

    assert retention_score(used, now=NOW) > retention_score(ignored, now=NOW)


def test_an_important_memory_outranks_a_trivial_one_of_the_same_age():
    assert retention_score(candidate(importance=0.9), now=NOW) > retention_score(
        candidate(importance=0.1), now=NOW
    )


def test_an_old_memory_decays_below_a_fresh_one():
    old = candidate(created_at="2024-01-01T00:00:00+00:00")
    fresh = candidate(created_at=NOW)

    assert retention_score(fresh, now=NOW) > retention_score(old, now=NOW)


def test_use_outweighs_age_for_an_old_but_frequently_recalled_memory():
    """A rarely-touched-but-load-bearing memory is exactly what naive LRU discards."""
    old_and_used = candidate(created_at="2024-01-01T00:00:00+00:00", access_count=25)
    fresh_and_unused = candidate(created_at=NOW, access_count=0, importance=0.2)

    assert retention_score(old_and_used, now=NOW) > retention_score(fresh_and_unused, now=NOW)


# ---- archiving ---------------------------------------------------------


def test_an_archived_memory_disappears_from_search(store):
    mid = _ingest(store, "an obsolete note about the old build system")

    store.archive(mid)

    assert mid not in [h["id"] for h in store.search("obsolete build system")]


def test_an_archived_memory_is_not_deleted(store):
    mid = _ingest(store, "an obsolete note about the old build system")

    store.archive(mid)

    row = store.get_memory(mid)
    assert row is not None
    assert row["archived"] is True


def test_an_archived_memory_can_be_retrieved_deliberately(store):
    mid = _ingest(store, "an obsolete note about the old build system")
    store.archive(mid)

    assert mid in [h["id"] for h in store.search("obsolete build system", include_archived=True)]


def test_archiving_can_be_undone(store):
    mid = _ingest(store, "a note archived by mistake")
    store.archive(mid)

    store.unarchive(mid)

    assert mid in [h["id"] for h in store.search("archived mistake")]


# ---- pruning -----------------------------------------------------------


def test_pruning_keeps_the_highest_retention_memories(store):
    keep = _ingest(store, "critical production runbook step", importance=0.95)
    drop = _ingest(store, "a passing thought about lunch", importance=0.05)

    store.prune(keep=1)

    assert store.get_memory(keep)["archived"] is False
    assert store.get_memory(drop)["archived"] is True


def test_pruning_archives_rather_than_deletes(store):
    for i in range(5):
        _ingest(store, f"note number {i}", importance=0.1 * i)

    store.prune(keep=2)

    assert store.stats()["total_memories"] == 5, "prune must not destroy anything"
    assert store.stats()["archived"] == 3


def test_pruning_is_scoped_to_a_namespace(store):
    other = _ingest(store, "a memory belonging to another project", namespace="other", importance=0.01)
    _ingest(store, "one", namespace="target", importance=0.1)
    _ingest(store, "two", namespace="target", importance=0.9)

    store.prune(keep=1, namespace="target")

    assert store.get_memory(other)["archived"] is False


def test_pruning_below_the_threshold_does_nothing(store):
    a = _ingest(store, "first note")
    b = _ingest(store, "second note")

    store.prune(keep=10)

    assert store.get_memory(a)["archived"] is False
    assert store.get_memory(b)["archived"] is False


def test_a_superseded_memory_is_archived_before_a_live_one(store):
    """Corrected memories are the natural first candidates for eviction."""
    old = _ingest(store, "the deadline is March 15", importance=0.9)
    new = _ingest(store, "the deadline moved to April 2", importance=0.9)
    store.supersede(old, new)

    store.prune(keep=1)

    assert store.get_memory(old)["archived"] is True
    assert store.get_memory(new)["archived"] is False


# ---- expiry ------------------------------------------------------------


def _backdate(store, memory_id, days):
    """Move a memory's clock back, the way real elapsed time would."""
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    store.db.execute(
        "UPDATE memories SET created_at = ?, valid_from = ? WHERE id = ?", (when, when, memory_id)
    )
    store.db.commit()


def test_a_memory_untouched_for_longer_than_the_policy_is_archived(store):
    stale = _ingest(store, "the standup link for a project that ended", importance=0.9)
    _backdate(store, stale, days=120)

    store.expire(max_age_days=90)

    assert store.get_memory(stale)["archived"] is True


def test_a_memory_inside_the_policy_window_survives(store):
    recent = _ingest(store, "the deploy key rotates on the first of the month")
    _backdate(store, recent, days=30)

    store.expire(max_age_days=90)

    assert store.get_memory(recent)["archived"] is False


def test_recall_resets_the_expiry_clock(store):
    """Use is the signal that matters: a memory still being read is not stale."""
    used = _ingest(store, "the postgres connection string lives in vault")
    _backdate(store, used, days=200)
    store.search("postgres connection string")

    store.expire(max_age_days=90)

    assert store.get_memory(used)["archived"] is False


def test_expiry_archives_rather_than_deletes(store):
    old = _ingest(store, "a note from another era")
    _backdate(store, old, days=365)

    store.expire(max_age_days=90)

    assert store.stats()["total_memories"] == 1, "expiry must not destroy anything"
    assert store.get_memory(old)["raw_text"] == "a note from another era"


def test_expiry_is_scoped_to_a_namespace(store):
    other = _ingest(store, "an ancient note in another project", namespace="other")
    target = _ingest(store, "an ancient note in this project", namespace="target")
    _backdate(store, other, days=365)
    _backdate(store, target, days=365)

    store.expire(max_age_days=90, namespace="target")

    assert store.get_memory(target)["archived"] is True
    assert store.get_memory(other)["archived"] is False


def test_expiry_reports_what_it_archived_and_what_remains(store):
    old = _ingest(store, "one for the archive")
    _ingest(store, "one that stays")
    _backdate(store, old, days=365)

    assert store.expire(max_age_days=90) == {"status": "pruned", "archived": 1, "kept": 1}


# ---- the policy applies without anyone calling the endpoint ------------


async def test_a_configured_expiry_policy_runs_on_its_own(config, store):
    """A documented flag whose loop is never scheduled looks exactly like a working one.

    That regression has shipped here before (--watch and --consolidate-every were
    inert for a release), so this drives the serve-time wiring, not `expire` directly.
    """
    config.expire_after_days = 90
    config.consolidate_every_minutes = 0
    app = web.Application()
    app[STORE_KEY] = store
    stale = _ingest(store, "a note from a project that wound up")
    _backdate(store, stale, days=365)

    tasks = [asyncio.create_task(loop) for loop in _background_loops(config, app)]
    try:
        for _ in range(200):
            if store.get_memory(stale)["archived"]:
                break
            await asyncio.sleep(0.01)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert store.get_memory(stale)["archived"] is True
