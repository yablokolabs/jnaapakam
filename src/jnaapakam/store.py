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


MAX_QUERY_TERMS = 32


def build_match_query(text: str, max_terms: int = MAX_QUERY_TERMS) -> str:
    """Turn arbitrary user text into a safe FTS5 MATCH expression.

    Every token is quoted, so FTS5 operators a user happens to type (``OR``,
    ``NEAR(``, ``*``, a stray quote) are searched for as text instead of being
    executed as query syntax or raising a parse error.
    """
    seen: dict[str, None] = {}
    for token in re.findall(r"\w+", text or "", re.UNICODE):
        seen.setdefault(token, None)
        if len(seen) >= max_terms:
            # Cost grows roughly quadratically in matching terms, and search holds a
            # process-wide lock, so an uncapped query stalls every other request.
            break
    return " OR ".join(f'"{token}"' for token in seen)


def _statements(script: str) -> list[str]:
    """Split a DDL script into statements.

    `executescript` cannot be used inside an explicit transaction — it issues an
    implicit COMMIT first — so the migration executes statements one at a time.
    `sqlite3.complete_statement` is what keeps CREATE TRIGGER ... BEGIN ... END
    intact, since a naive split on ';' would cut it apart.
    """
    statements, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip())
    return statements


def _synchronized(method):
    """Serialise access to the shared connection (see Store._lock)."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Store:
    def __init__(self, db_path: str, max_query_terms: int = MAX_QUERY_TERMS):
        self.db_path = db_path
        self.max_query_terms = max_query_terms
        self._db: sqlite3.Connection | None = None
        # The server runs store calls on a thread-pool worker, so the connection is
        # shared across threads and every statement is serialised by this lock.
        self._lock = threading.RLock()

    # ---- lifecycle -----------------------------------------------------

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            # Configure a local first and publish it only once every PRAGMA has
            # succeeded. Assigning before configuring meant a transient "database is
            # locked" on journal_mode=WAL left a permanently cached connection in
            # rollback-journal mode with foreign keys off, silently and forever.
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
            except BaseException:
                conn.close()
                raise
            self._db = conn
        return self._db

    def initialize(self) -> Store:
        """Create or upgrade the schema.

        The whole migration runs under BEGIN IMMEDIATE: the lock only serialises
        threads in this process, so without an exclusive transaction two processes
        opening the same file both read the pre-migration column set and the losers
        die on `duplicate column name`.
        """
        with self._lock:
            self.db.isolation_level = None  # explicit transaction control
            self.db.execute("BEGIN IMMEDIATE")
            try:
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                for statement in _statements(BASE_SCHEMA):
                    self.db.execute(statement)
                self._add_missing_columns()
                for statement in _statements(INDEXES) + _statements(FTS_SCHEMA):
                    self.db.execute(statement)
                if version < SCHEMA_VERSION:
                    self._backfill_full_text_index()
                    self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            finally:
                self.db.isolation_level = ""
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

        Refused when the result would be incoherent — an already-corrected memory
        (which would silently overwrite a deliberate correction), a dead replacement,
        a cycle (which would leave the namespace with no live head at all), or an
        inverted validity interval.
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
        if old["superseded_by"] is not None:
            log.warning(
                "Refusing to supersede #%s: already superseded by #%s", old_id, old["superseded_by"]
            )
            return False
        if new["superseded_by"] is not None or new["archived"]:
            log.warning("Refusing to point #%s at #%s: the replacement is not live", old_id, new_id)
            return False

        # Walk forward from the replacement; if the chain reaches `old`, this closes a
        # cycle and every memory in it would drop out of retrieval.
        seen, cursor = {new_id}, new["superseded_by"]
        while cursor is not None:
            if cursor == old_id or cursor in seen:
                log.warning("Refusing to supersede #%s with #%s: would close a cycle", old_id, new_id)
                return False
            seen.add(cursor)
            row = self.db.execute(
                "SELECT superseded_by FROM memories WHERE id = ?", (cursor,)
            ).fetchone()
            cursor = row["superseded_by"] if row else None

        boundary = new["valid_from"] or new["created_at"]
        start = old["valid_from"] or old["created_at"]
        end_at, start_at = retrieval._parse_time(boundary), retrieval._parse_time(start)
        if end_at and start_at and end_at < start_at:
            # The caller has the direction backwards: closing before opening would
            # make every point-in-time query over this memory return nothing.
            log.warning(
                "Refusing to supersede #%s with #%s: would invert its validity interval",
                old_id, new_id,
            )
            return False

        self.db.execute(
            "UPDATE memories SET superseded_by = ?, valid_to = ? WHERE id = ?",
            (new_id, boundary, old_id),
        )
        self.db.commit()
        return True

    @_synchronized
    def delete_memory(self, memory_id: int) -> bool:
        """Erase a memory, repairing any correction chain that ran through it.

        Without the repair, deleting a replacement strands the memory it replaced:
        that predecessor keeps `superseded_by` pointing at a row that no longer
        exists, and every default read path filters it out forever.
        """
        row = self.db.execute(
            "SELECT superseded_by FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return False
        successor = row["superseded_by"]
        self.db.execute(
            "UPDATE memories SET superseded_by = ?, valid_to = CASE WHEN ? IS NULL THEN NULL "
            "ELSE valid_to END WHERE superseded_by = ?",
            (successor, successor, memory_id),
        )
        cursor = self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self.db.commit()
        return cursor.rowcount > 0

    @_synchronized
    def clear(self, namespace: str | None = None) -> int:
        """Delete memories. Scoped to one namespace when given, otherwise everything."""
        if namespace is None:
            count = self.db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
            for table in ("memories", "consolidations", "processed_files"):
                self.db.execute(f"DELETE FROM {table}")
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
        weights: dict | None = None,
        halflife_days: float | None = None,
    ) -> list[dict]:
        """Rank memories by content relevance, then recency and importance.

        Replaces v0.1's `ORDER BY created_at DESC LIMIT 50`, under which anything
        older than the 50 most recent rows was unreachable regardless of content.
        """
        match = build_match_query(query, self.max_query_terms)
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

        ranked = retrieval.rank(
            candidates,
            now=_now(),
            weights=weights,
            halflife_days=halflife_days or retrieval.DEFAULT_HALFLIFE_DAYS,
            limit=limit,
        )
        if record_access:
            try:
                self._record_access([m["id"] for m in ranked])
            except sqlite3.Error as exc:
                # A counter is not worth failing a read for: a read-only database or a
                # write lock held by another process must still return results.
                log.warning("Could not record access counts: %s", exc)
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
    def was_processed(self, path: str) -> bool:
        return (
            self.db.execute("SELECT 1 FROM processed_files WHERE path = ?", (path,)).fetchone()
            is not None
        )

    @_synchronized
    def mark_processed(self, path: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO processed_files (path, processed_at) VALUES (?, ?)",
            (path, _now()),
        )
        self.db.commit()

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
        """Restore a backup atomically, preserving identity and repairing references.

        Three properties the first implementation lacked:

        * **Atomic.** Everything runs in one transaction. Previously a row that failed
          coercion left earlier rows uncommitted but visible on the shared connection,
          and the next `commit()` from any read made the rejected data permanent.
        * **Identity-preserving.** Rows keep their original ids where those ids are
          free, so `superseded_by`, `connections[].linked_to`, and
          `consolidations.source_ids` still refer to the memories they were written
          about. Reassigning ids silently repointed every correction chain.
        * **Reference-repairing.** Where an id must change (a merge into a populated
          store), references are remapped through the same mapping, and references
          that resolve to nothing are dropped rather than left dangling.
        """
        if not isinstance(data, dict):
            raise ValueError("backup must be a JSON object")
        memories = data.get("memories", [])
        consolidations = data.get("consolidations", [])
        if not isinstance(memories, list) or not isinstance(consolidations, list):
            raise ValueError("'memories' and 'consolidations' must be arrays")

        def as_json_text(value, default, field, index):
            """Normalise a JSON column, rejecting anything unparseable."""
            if value is None:
                return default
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except (TypeError, ValueError):
                    raise ValueError(f"memories[{index}].{field} is not valid JSON") from None
            else:
                parsed = value
            if not isinstance(parsed, list):
                raise ValueError(f"memories[{index}].{field} must be a JSON array")
            return json.dumps(parsed)

        # ---- validate everything before writing anything ----
        prepared = []
        for index, memory in enumerate(memories):
            if not isinstance(memory, dict):
                raise ValueError(f"memories[{index}] must be an object")
            for required in ("raw_text", "summary", "created_at"):
                if not memory.get(required):
                    raise ValueError(f"memories[{index}] is missing '{required}'")
            try:
                importance = float(memory.get("importance", 0.5))
                consolidated = int(memory.get("consolidated", 0))
                archived = int(memory.get("archived", 0))
                access_count = int(memory.get("access_count", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"memories[{index}] has a non-numeric field: {exc}") from None
            prepared.append(
                {
                    "old_id": memory.get("id"),
                    "row": memory,
                    "entities": as_json_text(memory.get("entities"), "[]", "entities", index),
                    "topics": as_json_text(memory.get("topics"), "[]", "topics", index),
                    "connections": as_json_text(memory.get("connections"), "[]", "connections", index),
                    "importance": importance,
                    "consolidated": consolidated,
                    "archived": archived,
                    "access_count": access_count,
                }
            )

        prepared_consolidations = []
        for index, consolidation in enumerate(consolidations):
            if not isinstance(consolidation, dict):
                raise ValueError(f"consolidations[{index}] must be an object")
            for required in ("source_ids", "summary", "insight", "created_at"):
                if consolidation.get(required) is None:
                    raise ValueError(f"consolidations[{index}] is missing '{required}'")
            raw_ids = consolidation["source_ids"]
            if isinstance(raw_ids, str):
                try:
                    raw_ids = json.loads(raw_ids)
                except (TypeError, ValueError):
                    raise ValueError(f"consolidations[{index}].source_ids is not valid JSON") from None
            if not isinstance(raw_ids, list):
                raise ValueError(f"consolidations[{index}].source_ids must be a JSON array")
            prepared_consolidations.append({"row": consolidation, "source_ids": raw_ids})

        # ---- write, atomically ----
        taken = {r["id"] for r in self.db.execute("SELECT id FROM memories")}
        id_map: dict[int, int] = {}
        try:
            for item in prepared:
                memory = item["row"]
                old_id = item["old_id"]
                keep_id = (
                    isinstance(old_id, int) and old_id > 0 and old_id not in taken
                )
                cursor = self.db.execute(
                    "INSERT INTO memories (id, source, raw_text, summary, entities, topics, "
                    "connections, importance, created_at, consolidated, namespace, kind, "
                    "event_time, valid_from, valid_to, superseded_by, access_count, "
                    "last_accessed, archived) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        old_id if keep_id else None,
                        memory.get("source", ""),
                        memory["raw_text"],
                        memory["summary"],
                        item["entities"],
                        item["topics"],
                        item["connections"],
                        item["importance"],
                        memory["created_at"],
                        item["consolidated"],
                        memory.get("namespace") or "",
                        memory.get("kind") or "factual",
                        memory.get("event_time"),
                        memory.get("valid_from") or memory["created_at"],
                        memory.get("valid_to"),
                        memory.get("superseded_by"),
                        item["access_count"],
                        memory.get("last_accessed"),
                        item["archived"],
                    ),
                )
                new_id = cursor.lastrowid
                taken.add(new_id)
                if isinstance(old_id, int):
                    id_map[old_id] = new_id

            def remap(reference):
                """Translate a backup id, or None if it no longer resolves."""
                if not isinstance(reference, int):
                    return None
                return id_map.get(reference)

            # Second pass: rewrite every id reference through the mapping.
            for new_id in id_map.values():
                row = self.db.execute(
                    "SELECT superseded_by, connections FROM memories WHERE id = ?", (new_id,)
                ).fetchone()
                pointer = remap(row["superseded_by"])
                links = [
                    {**link, "linked_to": remap(link.get("linked_to"))}
                    for link in json.loads(row["connections"])
                    if remap(link.get("linked_to")) is not None
                ]
                self.db.execute(
                    "UPDATE memories SET superseded_by = ?, connections = ?, "
                    "valid_to = CASE WHEN ? IS NULL THEN NULL ELSE valid_to END WHERE id = ?",
                    (pointer, json.dumps(links), pointer, new_id),
                )

            for item in prepared_consolidations:
                consolidation = item["row"]
                mapped = [remap(i) for i in item["source_ids"]]
                self.db.execute(
                    "INSERT INTO consolidations (source_ids, summary, insight, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        json.dumps([i for i in mapped if i is not None]),
                        consolidation["summary"],
                        consolidation["insight"],
                        consolidation["created_at"],
                    ),
                )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

        return {
            "status": "restored",
            "memories_imported": len(prepared),
            "consolidations_imported": len(prepared_consolidations),
        }
