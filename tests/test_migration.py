"""Opening a database written by an older version.

Migration is greenfield by decision — there is no promise of a lossless upgrade
from every v0.1 edge case — but an existing file must not be destroyed or become
unreadable, and the rows in it have to end up searchable.
"""

import sqlite3

from jnaapakam.store import Store

V01_SCHEMA = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL,
    summary TEXT NOT NULL,
    entities TEXT NOT NULL DEFAULT '[]',
    topics TEXT NOT NULL DEFAULT '[]',
    connections TEXT NOT NULL DEFAULT '[]',
    importance REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    consolidated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE consolidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ids TEXT NOT NULL,
    summary TEXT NOT NULL,
    insight TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE processed_files (path TEXT PRIMARY KEY, processed_at TEXT NOT NULL);
"""


def _write_v01_database(path):
    db = sqlite3.connect(path)
    db.executescript(V01_SCHEMA)
    db.execute(
        "INSERT INTO memories (source, raw_text, summary, entities, topics, importance, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "conversation",
            "The user said they prefer vim keybindings and a dark colour scheme.",
            "User prefers vim keybindings and dark mode",
            '["user"]',
            '["preferences"]',
            0.6,
            "2026-03-08T01:50:00+00:00",
        ),
    )
    db.commit()
    db.close()


def test_an_existing_v01_database_still_opens(tmp_path):
    path = str(tmp_path / "old.db")
    _write_v01_database(path)

    store = Store(path)
    store.initialize()
    try:
        assert store.stats()["total_memories"] == 1
    finally:
        store.close()


def test_rows_written_by_v01_become_searchable_after_upgrade(tmp_path):
    """v0.1 had no full-text index, so existing rows must be backfilled into one."""
    path = str(tmp_path / "old.db")
    _write_v01_database(path)

    store = Store(path)
    store.initialize()
    try:
        assert store.search("vim keybindings"), "pre-existing memories were not indexed"
    finally:
        store.close()


def test_v01_rows_land_in_the_default_namespace(tmp_path):
    path = str(tmp_path / "old.db")
    _write_v01_database(path)

    store = Store(path)
    store.initialize()
    try:
        memory = store.list_memories()[0]
        assert memory["namespace"] == ""
        assert memory["kind"] == "factual"
        assert memory["superseded_by"] is None
    finally:
        store.close()


def test_original_content_survives_the_upgrade(tmp_path):
    path = str(tmp_path / "old.db")
    _write_v01_database(path)

    store = Store(path)
    store.initialize()
    try:
        memory = store.list_memories()[0]
        assert memory["summary"] == "User prefers vim keybindings and dark mode"
        assert memory["source"] == "conversation"
        assert memory["created_at"] == "2026-03-08T01:50:00+00:00"
        assert memory["entities"] == ["user"]
    finally:
        store.close()


def test_upgrading_twice_is_a_no_op(tmp_path):
    path = str(tmp_path / "old.db")
    _write_v01_database(path)

    for _ in range(3):
        store = Store(path)
        store.initialize()
        count = store.stats()["total_memories"]
        store.close()
        assert count == 1, "re-opening duplicated or dropped rows"


V04_ARTIFACTS = """
CREATE TABLE generation_artifacts (
    generation_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    algorithm TEXT NOT NULL DEFAULT 'sha256',
    digest TEXT NOT NULL,
    bytes INTEGER,
    records INTEGER,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (generation_id, name)
);
"""


def test_a_v04_seal_survives_the_upgrade_to_signed_artifacts(tmp_path):
    """Seals written before signing existed must read back as unsigned, not broken."""
    path = str(tmp_path / "v04.db")
    _write_v01_database(path)
    db = sqlite3.connect(path)
    db.executescript(V04_ARTIFACTS)
    db.execute(
        "INSERT INTO generation_artifacts (generation_id, name, digest, records, recorded_at) "
        "VALUES (1, 'memory_corpus', ?, 3, '2026-08-31T00:00:00+00:00')",
        ("a" * 64,),
    )
    db.commit()
    db.close()

    store = Store(path).initialize()
    recorded = store.artifacts(1)
    store.close()

    assert recorded[0]["digest"] == "a" * 64
    assert recorded[0]["signature"] is None
