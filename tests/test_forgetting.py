"""Retention and forgetting.

Forgetting is soft: archived, never deleted. The research found no published decay
formula worth copying — only MemoryLLM's K/N replacement ratio — so the scoring here
is attributed to MemoryBank's composite of decay and access frequency rather than
presented as settled science, and nothing is destroyed automatically.
"""

from jnaapakam.retention import retention_score


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
