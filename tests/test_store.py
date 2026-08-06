"""Storage and retrieval behaviour.

The headline defect these cover: v0.1 `query()` read the 50 most recent rows and
dumped them into a prompt, so anything older than the window was unreachable no
matter what it said.
"""


def _ingest(store, text, **kw):
    return store.add_memory(
        raw_text=text,
        summary=kw.get("summary", text[:120]),
        entities=kw.get("entities", []),
        topics=kw.get("topics", []),
        importance=kw.get("importance", 0.5),
        source=kw.get("source", "test"),
    )


def test_memory_far_outside_the_recency_window_is_still_found_by_content(store):
    """The bug that defines this release: memory #1 of 60 must still be findable."""
    target = _ingest(store, "the user prefers vim keybindings and a dark colour scheme")
    for i in range(59):
        _ingest(store, f"unrelated note number {i} about deployment pipelines")

    hits = store.search("vim keybindings", limit=5)

    assert target in [h["id"] for h in hits], "memory outside the recency window was not retrievable"


def test_search_matches_content_in_raw_text_not_only_the_summary(store):
    mid = _ingest(
        store,
        "Long transcript. Deep in the body the user mentions they use the Zig programming language.",
        summary="A conversation about tooling",
    )

    hits = store.search("Zig", limit=5)

    assert mid in [h["id"] for h in hits]


def test_more_relevant_memory_outranks_a_merely_recent_one(store):
    relevant = _ingest(store, "the deadline for the rust CLI tool is March 15")
    for i in range(10):
        _ingest(store, f"note {i} about unrelated meeting scheduling")

    hits = store.search("rust CLI deadline", limit=3)

    assert hits, "expected at least one result"
    assert hits[0]["id"] == relevant


def test_search_returns_provenance_for_every_hit(store):
    _ingest(store, "user timezone is UTC+5:30", source="conversation:2026-03-08")

    hits = store.search("timezone", limit=1)

    assert hits
    hit = hits[0]
    assert hit["raw_text"], "a hit with no raw text cannot be traced back to its origin"
    assert hit["source"] == "conversation:2026-03-08"
    assert hit["created_at"]


def test_query_syntax_characters_are_treated_as_text_not_as_operators(store):
    """FTS5 punctuation must not crash the search or leak query syntax."""
    _ingest(store, "the user asked about C++ templates and pointer arithmetic")

    for probe in ['C++ "templates"', "pointer OR", "NEAR(", "*", '"']:
        hits = store.search(probe, limit=5)
        assert isinstance(hits, list)


def test_empty_store_returns_no_results_rather_than_failing(store):
    assert store.search("anything at all", limit=10) == []


def test_deleting_a_memory_removes_it_from_search_results(store):
    mid = _ingest(store, "an ephemeral note about kubernetes ingress")
    assert store.search("kubernetes", limit=5)

    store.delete_memory(mid)

    assert mid not in [h["id"] for h in store.search("kubernetes", limit=5)]


def test_stored_memory_survives_reopening_the_database(config):
    from jnaapakam.store import Store

    first = Store(config.db_path)
    first.initialize()
    _ingest(first, "persistence across restarts is the whole point")
    first.close()

    second = Store(config.db_path)
    second.initialize()
    try:
        assert second.search("persistence restarts", limit=5)
    finally:
        second.close()
