"""Validity intervals, supersession, and usage tracking.

Phase 3 ships the *columns and their read semantics*; the LLM-driven
contradiction detection that decides when to supersede is phase 4. The research
was explicit that the schema is cheap now and a painful migration later, while the
detection pipeline has not yet earned its cost.
"""


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


# ---- supersession ------------------------------------------------------


def test_a_superseded_memory_drops_out_of_search(store):
    old = _ingest(store, "the user prefers vim as their editor")
    new = _ingest(store, "the user now prefers helix as their editor")

    store.supersede(old, new)

    found = [h["id"] for h in store.search("prefers editor")]
    assert new in found
    assert old not in found


def test_superseding_preserves_the_old_memory_rather_than_deleting_it(store):
    """Invalidate, never delete: history has to survive a correction."""
    old = _ingest(store, "the deadline is March 15")
    new = _ingest(store, "the deadline moved to April 2")

    store.supersede(old, new)

    row = store.get_memory(old)
    assert row is not None, "the superseded memory must still exist"
    assert row["superseded_by"] == new
    assert row["valid_to"] is not None


def test_a_superseded_memory_is_still_reachable_when_asking_about_the_past(store):
    old = _ingest(store, "the deadline is March 15")
    new = _ingest(store, "the deadline moved to April 2")
    store.supersede(old, new)

    assert old in [h["id"] for h in store.search("deadline", include_superseded=True)]


def test_a_chain_of_corrections_leaves_only_the_newest_active(store):
    first = _ingest(store, "release target is version one")
    second = _ingest(store, "release target is version two")
    third = _ingest(store, "release target is version three")
    store.supersede(first, second)
    store.supersede(second, third)

    active = [h["id"] for h in store.search("release target")]

    assert active == [third]


def test_superseding_across_namespaces_is_refused(store):
    """Cross-scope supersession would silently delete another project's memory."""
    a = _ingest(store, "the editor is vim", namespace="project-a")
    b = _ingest(store, "the editor is VS Code", namespace="project-b")

    assert store.supersede(a, b) is False
    assert store.get_memory(a)["superseded_by"] is None


def test_superseding_an_unknown_memory_reports_failure(store):
    live = _ingest(store, "a real memory")

    assert store.supersede(999999, live) is False


# ---- validity intervals ------------------------------------------------


def test_a_new_memory_is_valid_from_the_moment_it_is_stored(store):
    mid = _ingest(store, "a fact that holds from now on")

    row = store.get_memory(mid)

    assert row["valid_from"]
    assert row["valid_to"] is None, "an uncontradicted memory has no end of validity"


# ---- usage tracking ----------------------------------------------------


def test_returning_a_memory_records_that_it_was_used(store):
    mid = _ingest(store, "postgres connection pooling settings")
    assert store.get_memory(mid)["access_count"] == 0

    store.search("postgres pooling")

    assert store.get_memory(mid)["access_count"] == 1


def test_usage_is_only_recorded_for_memories_actually_returned(store):
    used = _ingest(store, "kubernetes ingress configuration")
    unused = _ingest(store, "an entirely unrelated note on payroll")

    store.search("kubernetes ingress")

    assert store.get_memory(used)["access_count"] == 1
    assert store.get_memory(unused)["access_count"] == 0


def test_repeated_recall_accumulates(store):
    mid = _ingest(store, "the on-call rotation starts on Monday")

    for _ in range(3):
        store.search("on-call rotation")

    row = store.get_memory(mid)
    assert row["access_count"] == 3
    assert row["last_accessed"]


# ---- kind is a label, not a code path ----------------------------------


def test_memories_carry_a_kind_that_defaults_to_factual(store):
    mid = _ingest(store, "a plain fact about the user")

    assert store.get_memory(mid)["kind"] == "factual"


def test_an_arbitrary_kind_is_stored_and_retrievable(store):
    """Taxonomy rides as a tag until a test proves a type needs its own path."""
    mid = store.add_memory(
        raw_text="tried the retry-with-backoff approach and it worked",
        summary="retry with backoff worked",
        entities=[],
        topics=[],
        importance=0.7,
        source="test",
        kind="experiential",
    )

    assert store.get_memory(mid)["kind"] == "experiential"
    assert mid in [h["id"] for h in store.search("retry backoff")]
