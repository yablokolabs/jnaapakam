"""SQLite-backed memory store with full-text retrieval.

FTS5 and BM25 ship inside stock CPython's sqlite3, so the default install needs
no extension, no daemon, and no network — which is what lets `pip install
jnaapakam && jnaapakam serve` work offline on first run.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone

from . import retention, retrieval

log = logging.getLogger("jnaapakam.store")

# Bumped when the on-disk layout changes. Tracked in `PRAGMA user_version` so an
# upgrade runs exactly once per database. v0.1 files report 0.
SCHEMA_VERSION = 3

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
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

CREATE TABLE IF NOT EXISTS consolidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ids TEXT NOT NULL,
    summary TEXT NOT NULL,
    insight TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_files (
    path TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);
"""

# Columns added after v0.1. Applied by ALTER TABLE when an older database is opened,
# so a v0.1 file keeps its rows and simply gains the new fields at their defaults.
#
# The temporal and usage columns are deliberately ahead of the machinery that drives
# them: retrofitting them later is a painful migration, whereas carrying them unused
# costs nothing. Phase 4 adds the contradiction detection that writes them.
ADDED_COLUMNS = {
    "namespace": "TEXT NOT NULL DEFAULT ''",
    "kind": "TEXT NOT NULL DEFAULT 'factual'",
    "event_time": "TEXT",
    "valid_from": "TEXT",
    "valid_to": "TEXT",
    "superseded_by": "INTEGER",
    "access_count": "INTEGER NOT NULL DEFAULT 0",
    "last_accessed": "TEXT",
    "archived": "INTEGER NOT NULL DEFAULT 0",
}

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_consolidated ON memories(consolidated, created_at);
CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source);
CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_validity ON memories(namespace, valid_to);
CREATE INDEX IF NOT EXISTS idx_memories_archived ON memories(namespace, archived);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    summary, raw_text, entities, topics,
    content='memories', content_rowid='id', tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, summary, raw_text, entities, topics)
    VALUES (new.id, new.summary, new.raw_text, new.entities, new.topics);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, summary, raw_text, entities, topics)
    VALUES ('delete', old.id, old.summary, old.raw_text, old.entities, old.topics);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, summary, raw_text, entities, topics)
    VALUES ('delete', old.id, old.summary, old.raw_text, old.entities, old.topics);
    INSERT INTO memories_fts(rowid, summary, raw_text, entities, topics)
    VALUES (new.id, new.summary, new.raw_text, new.entities, new.topics);
END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_match_query(text: str) -> str:
    """Turn arbitrary user text into a safe FTS5 MATCH expression.

    Every token is quoted, so FTS5 operators a user happens to type (``OR``,
    ``NEAR(``, ``*``, a stray quote) are searched for as text instead of being
    executed as query syntax or raising a parse error.
    """
    tokens = re.findall(r"\w+", text or "", re.UNICODE)
    return " OR ".join(f'"{token}"' for token in tokens)


def _synchronized(method):
    """Serialise access to the shared connection (see Store._lock)."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: sqlite3.Connection | None = None
        # The server runs store calls on a thread-pool worker, so the connection is
        # shared across threads and every statement is serialised by this lock.
        self._lock = threading.RLock()

    # ---- lifecycle -----------------------------------------------------

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = sqlite3.connect(self.db_path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA foreign_keys=ON")
        return self._db

    def initialize(self) -> Store:
        with self._lock:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            self.db.executescript(BASE_SCHEMA)
            self._add_missing_columns()
            self.db.executescript(INDEXES)
            self.db.executescript(FTS_SCHEMA)
            if version < SCHEMA_VERSION:
                self._backfill_full_text_index()
                self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.db.commit()
        return self

    def _add_missing_columns(self) -> None:
        existing = {row["name"] for row in self.db.execute("PRAGMA table_info(memories)")}
        for column, definition in ADDED_COLUMNS.items():
            if column not in existing:
                log.info("Upgrading schema: adding memories.%s", column)
                self.db.execute(f"ALTER TABLE memories ADD COLUMN {column} {definition}")
        # Rows that predate the validity columns are treated as valid since creation.
        self.db.execute("UPDATE memories SET valid_from = created_at WHERE valid_from IS NULL")

    def _backfill_full_text_index(self) -> None:
        """Index rows written before the full-text table existed.

        A v0.1 database has memories but no index, so without this every one of
        them would be invisible to search after the upgrade. Gated on
        `PRAGMA user_version` rather than a row count: `memories_fts` is an
        external-content table, so `COUNT(*)` on it delegates to `memories` and can
        never reveal an empty index. On a fresh database the rebuild is a no-op.
        """
        stored = self.db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
        if stored:
            log.info("Building full-text index for %d existing memories", stored)
        self.db.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # ---- writes --------------------------------------------------------

    @_synchronized
    def add_memory(
        self,
        raw_text,
        summary,
        entities,
        topics,
        importance,
        source="",
        namespace="",
        kind="factual",
        event_time=None,
    ) -> int:
        now = _now()
        cursor = self.db.execute(
            "INSERT INTO memories (source, raw_text, summary, entities, topics, importance, "
            "created_at, namespace, kind, event_time, valid_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source,
                raw_text,
                summary,
                json.dumps(entities),
                json.dumps(topics),
                float(importance),
                now,
                namespace or "",
                kind or "factual",
                event_time,
                now,
            ),
        )
        self.db.commit()
        return cursor.lastrowid

    @_synchronized
    def supersede(self, old_id: int, new_id: int) -> bool:
        """Mark `old_id` as replaced by `new_id`, without deleting it.

        Corrections invalidate rather than destroy: the superseded memory keeps its
        content and gains an end to its validity interval, so history survives and
        `include_superseded` can still reach it.
        """
        if old_id == new_id:
            return False
        old = self.db.execute("SELECT * FROM memories WHERE id = ?", (old_id,)).fetchone()
        new = self.db.execute("SELECT * FROM memories WHERE id = ?", (new_id,)).fetchone()
        if old is None or new is None:
            return False
        if old["namespace"] != new["namespace"]:
            # Superseding across scopes would silently retire another project's memory.
            log.warning(
                "Refusing to supersede #%s (%r) with #%s (%r): different namespaces",
                old_id, old["namespace"], new_id, new["namespace"],
            )
            return False
        self.db.execute(
            "UPDATE memories SET superseded_by = ?, valid_to = ? WHERE id = ?",
            (new_id, new["valid_from"] or new["created_at"], old_id),
        )
        self.db.commit()
        return True

    @_synchronized
    def delete_memory(self, memory_id: int) -> bool:
        cursor = self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self.db.commit()
        return cursor.rowcount > 0

    @_synchronized
    def clear(self, namespace: str | None = None) -> int:
        """Delete memories. Scoped to one namespace when given, otherwise everything."""
        if namespace is None:
            count = self.db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
            self.db.executescript(
                "DELETE FROM memories; DELETE FROM consolidations; DELETE FROM processed_files;"
            )
        else:
            count = self.db.execute(
                "SELECT COUNT(*) AS c FROM memories WHERE namespace = ?", (namespace,)
            ).fetchone()["c"]
            self.db.execute("DELETE FROM memories WHERE namespace = ?", (namespace,))
        self.db.commit()
        return count

    @_synchronized
    def _record_access(self, memory_ids: list[int]) -> None:
        """Count a recall against the memories that were actually returned.

        Usage is an outcome-grounded retention signal, unlike `importance`, which is
        assigned once at ingest and never corrected by whether the memory proved useful.
        """
        if not memory_ids:
            return
        placeholders = ",".join("?" * len(memory_ids))
        self.db.execute(
            f"UPDATE memories SET access_count = access_count + 1, last_accessed = ? "
            f"WHERE id IN ({placeholders})",
            (_now(), *memory_ids),
        )
        self.db.commit()

    # ---- reads ---------------------------------------------------------

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "namespace": row["namespace"],
            "kind": row["kind"],
            "source": row["source"],
            "summary": row["summary"],
            "raw_text": row["raw_text"],
            "entities": json.loads(row["entities"]),
            "topics": json.loads(row["topics"]),
            "connections": json.loads(row["connections"]),
            "importance": row["importance"],
            "created_at": row["created_at"],
            "event_time": row["event_time"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "superseded_by": row["superseded_by"],
            "access_count": row["access_count"],
            "last_accessed": row["last_accessed"],
            "archived": bool(row["archived"]),
            "consolidated": bool(row["consolidated"]),
        }

    @_synchronized
    def search(
        self,
        query: str,
        limit: int = 12,
        namespace: str = "",
        include_superseded: bool = False,
        include_archived: bool = False,
        candidate_pool: int = 200,
        record_access: bool = True,
    ) -> list[dict]:
        """Rank memories by content relevance, then recency and importance.

        Replaces v0.1's `ORDER BY created_at DESC LIMIT 50`, under which anything
        older than the 50 most recent rows was unreachable regardless of content.
        """
        match = build_match_query(query)
        if not match:
            return []

        sql = (
            "SELECT m.*, bm25(memories_fts) AS bm25_score "
            "FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
            "WHERE memories_fts MATCH ? AND m.namespace = ? "
        )
        params: list = [match, namespace or ""]
        if not include_superseded:
            sql += "AND m.superseded_by IS NULL "
        if not include_archived:
            sql += "AND m.archived = 0 "
        sql += "ORDER BY bm25_score LIMIT ?"
        params.append(candidate_pool)

        try:
            rows = self.db.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:  # malformed MATCH should not 500
            log.warning("FTS query failed for %r: %s", query, exc)
            return []

        if not rows:
            return []

        # bm25() is more negative for better matches; flip and scale to [0, 1].
        raw = [-row["bm25_score"] for row in rows]
        best = max(raw) or 1.0
        candidates = []
        for row, relevance in zip(rows, raw, strict=True):
            memory = self._row_to_memory(row)
            memory["lexical"] = max(0.0, relevance / best)
            candidates.append(memory)

        ranked = retrieval.rank(candidates, now=_now(), limit=limit)
        if record_access:
            self._record_access([m["id"] for m in ranked])
        return ranked

    @_synchronized
    def list_memories(self, limit: int = 50, namespace: str = "") -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM memories WHERE namespace = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (namespace or "", limit),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    @_synchronized
    def get_memory(self, memory_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._row_to_memory(row) if row else None

    @_synchronized
    def unconsolidated(self, limit: int = 20, namespace: str = "") -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM memories WHERE consolidated = 0 AND namespace = ? AND superseded_by IS NULL "
            "AND archived = 0 "
            "ORDER BY created_at ASC LIMIT ?",
            (namespace or "", limit),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    @_synchronized
    def namespaces(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT namespace, COUNT(*) AS c FROM memories GROUP BY namespace ORDER BY namespace"
        ).fetchall()
        return [{"namespace": r["namespace"], "total_memories": r["c"]} for r in rows]

    @_synchronized
    def stats(self, namespace: str | None = None) -> dict:
        """Counts for one namespace, or across every namespace when none is given."""
        filters: list[str] = []
        params: list = []
        if namespace is not None:
            filters.append("namespace = ?")
            params.append(namespace)

        def count(*extra: str) -> int:
            clauses = [*filters, *extra]
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            return self.db.execute(
                f"SELECT COUNT(*) AS c FROM memories{where}", params
            ).fetchone()["c"]

        total = count()
        pending = count("consolidated = 0")
        superseded = count("superseded_by IS NOT NULL")
        archived = count("archived = 1")
        consolidations = self.db.execute("SELECT COUNT(*) AS c FROM consolidations").fetchone()["c"]
        return {
            "total_memories": total,
            "unconsolidated": pending,
            "superseded": superseded,
            "archived": archived,
            "consolidations": consolidations,
            "namespace": namespace,
            "version": "0.2",
        }

    @_synchronized
    def consolidation_history(self, limit: int = 10) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM consolidations ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {
                "summary": r["summary"],
                "insight": r["insight"],
                "source_ids": json.loads(r["source_ids"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    @_synchronized
    def record_consolidation(self, source_ids, summary, insight, connections) -> dict:
        self.db.execute(
            "INSERT INTO consolidations (source_ids, summary, insight, created_at) VALUES (?, ?, ?, ?)",
            (json.dumps(source_ids), summary, insight, _now()),
        )
        for conn in connections or []:
            from_id, to_id = conn.get("from_id"), conn.get("to_id")
            if not from_id or not to_id or from_id == to_id:
                continue
            relationship = conn.get("relationship", "")
            for owner, other in ((from_id, to_id), (to_id, from_id)):
                row = self.db.execute(
                    "SELECT connections FROM memories WHERE id = ?", (owner,)
                ).fetchone()
                if not row:
                    continue
                links = json.loads(row["connections"])
                # Re-running consolidation must not accumulate duplicate edges.
                if any(link.get("linked_to") == other for link in links):
                    continue
                links.append({"linked_to": other, "relationship": relationship})
                self.db.execute(
                    "UPDATE memories SET connections = ? WHERE id = ?", (json.dumps(links), owner)
                )
        if source_ids:
            placeholders = ",".join("?" * len(source_ids))
            self.db.execute(
                f"UPDATE memories SET consolidated = 1 WHERE id IN ({placeholders})", source_ids
            )
        self.db.commit()
        return {"status": "consolidated", "memories_processed": len(source_ids), "insight": insight}

    @_synchronized
    def active_memories(self, namespace: str = "", limit: int = 50) -> list[dict]:
        """Memories eligible for reconciliation: current, unarchived, in one namespace."""
        rows = self.db.execute(
            "SELECT * FROM memories WHERE namespace = ? AND superseded_by IS NULL AND archived = 0 "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (namespace or "", limit),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    @_synchronized
    def archive(self, memory_id: int) -> bool:
        """Remove a memory from retrieval without destroying it."""
        cursor = self.db.execute(
            "UPDATE memories SET archived = 1 WHERE id = ? AND archived = 0", (memory_id,)
        )
        self.db.commit()
        return cursor.rowcount > 0

    @_synchronized
    def unarchive(self, memory_id: int) -> bool:
        cursor = self.db.execute(
            "UPDATE memories SET archived = 0 WHERE id = ? AND archived = 1", (memory_id,)
        )
        self.db.commit()
        return cursor.rowcount > 0

    @_synchronized
    def prune(self, keep: int, namespace: str = "") -> dict:
        """Archive all but the `keep` highest-retention memories in a namespace.

        Nothing is deleted. Superseded memories sort last regardless of score, so a
        correction that has already been replaced is evicted before anything live.
        """
        rows = self.db.execute(
            "SELECT * FROM memories WHERE namespace = ? AND archived = 0",
            (namespace or "",),
        ).fetchall()
        memories = [self._row_to_memory(r) for r in rows]
        if len(memories) <= keep:
            return {"status": "pruned", "archived": 0, "kept": len(memories)}

        now = _now()
        ordered = sorted(
            memories,
            key=lambda m: (m["superseded_by"] is None, retention.retention_score(m, now)),
            reverse=True,
        )
        doomed = [m["id"] for m in ordered[keep:]]
        placeholders = ",".join("?" * len(doomed))
        self.db.execute(
            f"UPDATE memories SET archived = 1 WHERE id IN ({placeholders})", doomed
        )
        self.db.commit()
        return {"status": "pruned", "archived": len(doomed), "kept": keep}

    # ---- backup --------------------------------------------------------

    @_synchronized
    def export_all(self) -> dict:
        memories = [dict(r) for r in self.db.execute("SELECT * FROM memories ORDER BY id")]
        consolidations = [dict(r) for r in self.db.execute("SELECT * FROM consolidations ORDER BY id")]
        return {
            "version": "0.2",
            "exported_at": _now(),
            "memories": memories,
            "consolidations": consolidations,
        }

    @_synchronized
    def import_all(self, data: dict) -> dict:
        """Restore a backup, rejecting malformed input before writing anything."""
        if not isinstance(data, dict):
            raise ValueError("backup must be a JSON object")
        memories = data.get("memories", [])
        consolidations = data.get("consolidations", [])
        if not isinstance(memories, list) or not isinstance(consolidations, list):
            raise ValueError("'memories' and 'consolidations' must be arrays")

        for index, memory in enumerate(memories):
            if not isinstance(memory, dict):
                raise ValueError(f"memories[{index}] must be an object")
            for required in ("raw_text", "summary", "created_at"):
                if not memory.get(required):
                    raise ValueError(f"memories[{index}] is missing '{required}'")

        for index, consolidation in enumerate(consolidations):
            if not isinstance(consolidation, dict):
                raise ValueError(f"consolidations[{index}] must be an object")
            for required in ("source_ids", "summary", "insight", "created_at"):
                if consolidation.get(required) is None:
                    raise ValueError(f"consolidations[{index}] is missing '{required}'")

        def as_json_text(value, default):
            if value is None:
                return default
            return value if isinstance(value, str) else json.dumps(value)

        for memory in memories:
            self.db.execute(
                "INSERT INTO memories (source, raw_text, summary, entities, topics, connections, "
                "importance, created_at, consolidated, namespace, kind, event_time, valid_from, "
                "valid_to, superseded_by, access_count, last_accessed, archived) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    memory.get("source", ""),
                    memory["raw_text"],
                    memory["summary"],
                    as_json_text(memory.get("entities"), "[]"),
                    as_json_text(memory.get("topics"), "[]"),
                    as_json_text(memory.get("connections"), "[]"),
                    float(memory.get("importance", 0.5)),
                    memory["created_at"],
                    int(memory.get("consolidated", 0)),
                    memory.get("namespace") or "",
                    memory.get("kind") or "factual",
                    memory.get("event_time"),
                    memory.get("valid_from") or memory["created_at"],
                    memory.get("valid_to"),
                    memory.get("superseded_by"),
                    int(memory.get("access_count", 0)),
                    memory.get("last_accessed"),
                    int(memory.get("archived", 0)),
                ),
            )
        for consolidation in consolidations:
            self.db.execute(
                "INSERT INTO consolidations (source_ids, summary, insight, created_at) VALUES (?, ?, ?, ?)",
                (
                    as_json_text(consolidation["source_ids"], "[]"),
                    consolidation["summary"],
                    consolidation["insight"],
                    consolidation["created_at"],
                ),
            )
        self.db.commit()
        return {
            "status": "restored",
            "memories_imported": len(memories),
            "consolidations_imported": len(consolidations),
        }
