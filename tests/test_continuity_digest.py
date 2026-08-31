"""What the corpus digest must notice, and what it must ignore.

A digest that only covers memory *text* verifies the wrong thing. Two stores can
hold byte-identical text while disagreeing about which memories are still true,
which are archived, and how they connect — and an agent restored into the second
one behaves differently from the first.

So continuity is verified with two digests:

* **content** — what knowledge exists
* **semantic state** — how that knowledge is currently interpreted and retrieved

Both must survive a restore that renumbers every row, and neither may move
because a memory was merely read.
"""

import pytest

from jnaapakam import lineage
from jnaapakam.store import Store

# Fixed ids and timestamps: the point of every test here is that only the thing
# under test differs, so nothing may vary because two inserts landed a
# microsecond apart.
CORRECTION = [
    {
        "id": 17,
        "raw_text": "User's preferred database is PostgreSQL",
        "summary": "prefers PostgreSQL",
        "entities": ["postgresql"],
        "topics": ["database"],
        "created_at": "2026-01-01T00:00:00+00:00",
        "importance": 0.7,
    },
    {
        "id": 26,
        "raw_text": "User switched to ClickHouse",
        "summary": "switched to ClickHouse",
        "entities": ["clickhouse"],
        "topics": ["database"],
        "created_at": "2026-01-02T00:00:00+00:00",
        "importance": 0.7,
    },
]


def _store(tmp_path, name, rows=CORRECTION):
    store = Store(str(tmp_path / name)).initialize()
    store.import_all({"memories": rows, "consolidations": []})
    return store


def _renumbered(rows, offset=1000):
    """The same knowledge, with every row id changed, as a restore may produce."""
    remap = {row["id"]: row["id"] + offset for row in rows}
    out = []
    for row in rows:
        moved = dict(row, id=remap[row["id"]])
        if row.get("superseded_by") is not None:
            moved["superseded_by"] = remap[row["superseded_by"]]
        out.append(moved)
    return out


# ---- the failure the digest exists to catch ----------------------------


def test_losing_a_supersession_is_detected(tmp_path):
    """The correction chain is what makes a retracted fact stop being believed.

    Without this, a migration that drops `superseded_by` leaves the agent holding
    both "prefers PostgreSQL" and "switched to ClickHouse" as current.
    """
    intact = _store(tmp_path, "intact.db")
    lost = _store(tmp_path, "lost.db")
    try:
        intact.supersede(17, 26)

        assert len(intact.active_memories()) == 1
        assert len(lost.active_memories()) == 2, "the corruption must be real, not notional"
        assert intact.corpus_digests()["state"] != lost.corpus_digests()["state"]
    finally:
        intact.close()
        lost.close()


def test_archiving_a_memory_is_detected(tmp_path):
    """Archiving changes what the agent can retrieve, so it changes its state."""
    live = _store(tmp_path, "live.db")
    hidden = _store(tmp_path, "hidden.db")
    try:
        hidden.archive(17)

        assert live.corpus_digests()["state"] != hidden.corpus_digests()["state"]
    finally:
        live.close()
        hidden.close()


def test_corrupting_the_extracted_entities_is_detected(tmp_path):
    """Entities and topics are indexed, so corrupting them changes what is findable."""
    good = _store(tmp_path, "good.db")
    corrupt = _store(tmp_path, "corrupt.db")
    try:
        corrupt.db.execute(
            "UPDATE memories SET entities = ?, topics = ? WHERE id = 17",
            ('["unrelated"]', '["unrelated"]'),
        )
        corrupt.db.commit()

        assert good.corpus_digests()["content"] != corrupt.corpus_digests()["content"]
    finally:
        good.close()
        corrupt.close()


def test_losing_a_consolidation_link_is_detected(tmp_path):
    linked = _store(tmp_path, "linked.db")
    unlinked = _store(tmp_path, "unlinked.db")
    try:
        linked.record_consolidation(
            [17, 26], "summary", "insight",
            [{"from_id": 17, "to_id": 26, "relationship": "same subject"}],
        )
        unlinked.record_consolidation([17, 26], "summary", "insight", [])

        assert linked.corpus_digests()["state"] != unlinked.corpus_digests()["state"]
    finally:
        linked.close()
        unlinked.close()


def test_a_changed_validity_interval_is_detected(tmp_path):
    open_ended = _store(tmp_path, "open.db")
    closed = _store(tmp_path, "closed.db")
    try:
        closed.db.execute("UPDATE memories SET valid_to = ? WHERE id = 17", ("2026-06-01T00:00:00+00:00",))
        closed.db.commit()

        assert open_ended.corpus_digests()["state"] != closed.corpus_digests()["state"]
    finally:
        open_ended.close()
        closed.close()


# ---- what the digest must keep ignoring --------------------------------


def test_both_digests_survive_a_restore_that_renumbers_every_row(tmp_path):
    """Migration renumbers rows. Identity of knowledge must not depend on row ids."""
    original = _store(tmp_path, "original.db")
    try:
        original.supersede(17, 26)
        exported = [dict(row) for row in original.db.execute("SELECT * FROM memories ORDER BY id")]
        before = original.corpus_digests()
    finally:
        original.close()

    moved = _store(tmp_path, "moved.db", rows=_renumbered(exported))
    try:
        assert [m["id"] for m in moved.list_memories()] != [17, 26], "ids really did change"
        assert moved.corpus_digests() == before
    finally:
        moved.close()


def test_a_supersession_pointing_at_a_renumbered_row_still_matches(tmp_path):
    """Cross-row links are compared by what they point at, not by its row id."""
    original = _store(tmp_path, "original.db")
    try:
        original.supersede(17, 26)
        exported = [dict(row) for row in original.db.execute("SELECT * FROM memories ORDER BY id")]
        before = original.corpus_digests()["state"]
    finally:
        original.close()

    moved = _store(tmp_path, "moved.db", rows=_renumbered(exported, offset=5))
    try:
        # 17 -> 26 became 22 -> 31: both ends moved, the relationship did not.
        assert moved.get_memory(22)["superseded_by"] == 31
        assert moved.corpus_digests()["state"] == before
    finally:
        moved.close()


def test_reading_a_memory_moves_neither_digest(tmp_path):
    """Retrieval bumps access counters. Usage is not a change of knowledge or state."""
    store = _store(tmp_path, "read.db")
    try:
        store.supersede(17, 26)
        before = store.corpus_digests()

        assert store.search("ClickHouse"), "expected a hit"

        assert store.corpus_digests() == before
    finally:
        store.close()


def test_consolidation_bookkeeping_alone_moves_neither_digest(tmp_path):
    """Marking a memory consolidated changes no knowledge and hides nothing."""
    store = _store(tmp_path, "consolidated.db")
    try:
        before = store.corpus_digests()

        store.db.execute("UPDATE memories SET consolidated = 1")
        store.db.commit()

        assert store.corpus_digests() == before
    finally:
        store.close()


# ---- the two digests are genuinely different questions -----------------


def test_losing_a_supersession_leaves_the_content_digest_alone(tmp_path):
    """Content and state answer different questions, and must fail separately.

    Nothing was lost from the corpus here — every memory is still present and
    unaltered. What changed is which of them the agent still believes.
    """
    intact = _store(tmp_path, "intact.db")
    lost = _store(tmp_path, "lost.db")
    try:
        intact.supersede(17, 26)

        assert intact.corpus_digests()["content"] == lost.corpus_digests()["content"]
        assert intact.corpus_digests()["state"] != lost.corpus_digests()["state"]
    finally:
        intact.close()
        lost.close()


def test_two_memories_with_identical_content_are_both_counted(tmp_path):
    """Multiplicity matters: losing one of a duplicated pair is a real loss."""
    twice = [
        dict(CORRECTION[0], id=1),
        dict(CORRECTION[0], id=2),
    ]
    once = [dict(CORRECTION[0], id=1)]

    pair = _store(tmp_path, "pair.db", rows=twice)
    single = _store(tmp_path, "single.db", rows=once)
    try:
        assert pair.corpus_digests()["content"] != single.corpus_digests()["content"]
    finally:
        pair.close()
        single.close()


# ---- validation surfaces the distinction -------------------------------


def test_validation_reports_a_lost_supersession_as_a_state_failure(tmp_path):
    """The operator must be told the knowledge is intact but its reading changed."""
    store = _store(tmp_path, "agent.db")
    try:
        store.supersede(17, 26)
        generation = store.create_generation(manifest={})
        store.seal_corpus(generation["id"])

        # The link is lost after sealing — exactly the migration failure at issue.
        store.db.execute("UPDATE memories SET superseded_by = NULL, valid_to = NULL WHERE id = 17")
        store.db.commit()

        result = store.validate_continuity(generation["id"])

        assert result["passed"] is False
        assert result["checks"]["memory"]["status"] == "pass", "no knowledge was lost"
        assert result["checks"]["semantic_state"]["status"] == "fail"
    finally:
        store.close()


def test_validation_reports_lost_knowledge_as_a_memory_failure(tmp_path):
    store = _store(tmp_path, "agent.db")
    try:
        generation = store.create_generation(manifest={})
        store.seal_corpus(generation["id"])

        store.delete_memory(17)
        result = store.validate_continuity(generation["id"])

        assert result["passed"] is False
        assert result["checks"]["memory"]["status"] == "fail"
    finally:
        store.close()


def test_a_generation_sealed_without_a_state_digest_skips_that_check(tmp_path):
    """A generation sealed by an implementation that recorded no state digest.

    Reporting `skipped` is the honest answer: nothing was recorded to compare
    against, and claiming `pass` would assert a check that never ran.
    """
    store = _store(tmp_path, "agent.db")
    try:
        generation = store.create_generation(manifest={})
        store.seal_corpus(generation["id"])
        store.db.execute(
            "DELETE FROM generation_artifacts WHERE name = ?", (lineage.STATE_ARTIFACT,)
        )
        store.db.commit()

        result = store.validate_continuity(generation["id"])

        assert result["checks"]["semantic_state"]["status"] == "skipped"
        assert result["checks"]["memory"]["status"] == "pass"
    finally:
        store.close()


def test_a_full_migration_carries_both_digests_across_machines(tmp_path):
    """The end-to-end property: same knowledge, same reading of it, new database."""
    old = _store(tmp_path, "old.db")
    try:
        old.supersede(17, 26)
        old.archive(26)
        generation = old.create_generation(manifest={})
        old.seal_corpus(generation["id"])
        payload = old.export_all()
        expected = old.corpus_digests()
    finally:
        old.close()

    new = Store(str(tmp_path / "new.db")).initialize()
    try:
        new.import_all(payload)

        assert new.corpus_digests() == expected
        restored = new.list_generations()[0]
        result = new.validate_continuity(restored["id"])
        assert result["checks"]["memory"]["status"] == "pass"
        assert result["checks"]["semantic_state"]["status"] == "pass"
    finally:
        new.close()


def test_a_migration_that_drops_the_correction_chain_fails_validation(tmp_path):
    """The whole point, end to end: a lossy migration must not validate."""
    old = _store(tmp_path, "old.db")
    try:
        old.supersede(17, 26)
        generation = old.create_generation(manifest={})
        old.seal_corpus(generation["id"])
        payload = old.export_all()
    finally:
        old.close()

    # A migration path that forgets to carry supersession across.
    for memory in payload["memories"]:
        memory["superseded_by"] = None
        memory["valid_to"] = None

    new = Store(str(tmp_path / "new.db")).initialize()
    try:
        new.import_all(payload)
        restored = new.list_generations()[0]

        result = new.validate_continuity(restored["id"])

        assert result["passed"] is False
        assert result["checks"]["semantic_state"]["status"] == "fail"
        assert len(new.active_memories()) == 2, "both facts are now believed at once"
    finally:
        new.close()


# ---- digest hygiene ----------------------------------------------------


def test_tag_order_is_not_a_semantic_change(tmp_path):
    """Reordering extracted tags is not new knowledge, so it is not a new digest."""
    ordered = dict(CORRECTION[0], entities=["a", "b"], topics=["x", "y"])
    shuffled = dict(CORRECTION[0], entities=["b", "a"], topics=["y", "x"])
    forward = _store(tmp_path, "forward.db", rows=[ordered])
    reverse = _store(tmp_path, "reverse.db", rows=[shuffled])
    try:
        assert forward.corpus_digests()["content"] == reverse.corpus_digests()["content"]
    finally:
        forward.close()
        reverse.close()


@pytest.mark.parametrize("digest", ["content", "state"])
def test_both_digests_are_hex_sha256(store, digest):
    assert lineage.is_digest(store.corpus_digests()[digest])


def test_the_diff_reports_the_semantic_state_alongside_the_content(tmp_path):
    """An operator comparing generations needs to see both, not just the text."""
    store = _store(tmp_path, "agent.db")
    try:
        first = store.create_generation(manifest={})
        store.seal_corpus(first["id"])
        store.supersede(17, 26)
        second = store.create_generation(parent=first["id"], manifest={})
        store.seal_corpus(second["id"])

        memory = store.diff_generations(first["id"], second["id"])["memory"]

        assert memory["corpus"] == "unchanged", "no memory was added or altered"
        assert memory["state"] == "changed", "but one is now superseded"
    finally:
        store.close()
