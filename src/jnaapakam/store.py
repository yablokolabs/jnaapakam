"""SQLite-backed memory store with full-text retrieval.

FTS5 and BM25 ship inside stock CPython's sqlite3, so the default install needs
no extension, no daemon, and no network — which is what lets `pip install
jnaapakam && jnaapakam serve` work offline on first run.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from . import embeddings, lineage, retention, retrieval, signing

log = logging.getLogger("jnaapakam.store")

# Bumped when the on-disk layout changes. Tracked in `PRAGMA user_version` so an
# upgrade runs exactly once per database. v0.1 files report 0.
SCHEMA_VERSION = 6

PROTOCOL_VERSION = "0.5"

GENERATION_STATUSES = ("staged", "promoted", "rejected")

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

# Signatures are additive: a store written before signing existed keeps its seals,
# and they verify as unsigned rather than as failures.
ADDED_ARTIFACT_COLUMNS = {
    "signature": "TEXT",
    "public_key": "TEXT",
}

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_consolidated ON memories(consolidated, created_at);
CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source);
CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_validity ON memories(namespace, valid_to);
CREATE INDEX IF NOT EXISTS idx_memories_archived ON memories(namespace, archived);
"""

# v0.3. Continuity state lives alongside the memories rather than in a second
# database: an agent's identity, its generations, and the transitions between them
# have to survive or fail together with the memories they describe.
#
# No foreign keys, matching the rest of the schema — `/restore` inserts children
# before their parents exist and repairs the references in a second pass, exactly
# as it already does for memory correction chains.
GENERATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    parent_id INTEGER,
    status TEXT NOT NULL DEFAULT 'staged',
    created_at TEXT NOT NULL,
    promoted_at TEXT,
    label TEXT NOT NULL DEFAULT '',
    manifest TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    from_generation INTEGER,
    to_generation INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    memory_records INTEGER,
    corpus_digest_before TEXT,
    corpus_digest_after TEXT,
    checks TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS generation_artifacts (
    generation_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    algorithm TEXT NOT NULL DEFAULT 'sha256',
    digest TEXT NOT NULL,
    bytes INTEGER,
    records INTEGER,
    recorded_at TEXT NOT NULL,
    signature TEXT,
    public_key TEXT,
    PRIMARY KEY (generation_id, name)
);

-- One vector per memory. The model is stored with it because vectors from
-- different models are not comparable, and a changed model must not silently
-- rank yesterday's embeddings against today's queries.
CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector BLOB NOT NULL,
    embedded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_embeddings_model ON memory_embeddings(model);

CREATE INDEX IF NOT EXISTS idx_generations_parent ON generations(parent_id);
CREATE INDEX IF NOT EXISTS idx_migrations_target ON migrations(to_generation, id DESC);
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
    def __init__(
        self,
        db_path: str,
        max_query_terms: int = MAX_QUERY_TERMS,
        signing_key: str | None = None,
    ):
        self.db_path = db_path
        self.max_query_terms = max_query_terms
        # A path, not key material: the key is read per seal and not kept resident.
        self.signing_key = signing_key
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
                for statement in (
                    _statements(INDEXES) + _statements(FTS_SCHEMA) + _statements(GENERATION_SCHEMA)
                ):
                    self.db.execute(statement)
                self._add_missing_artifact_columns()
                # Gated on 3, not on SCHEMA_VERSION: the index is only missing in
                # files written before v0.2, and rebuilding it on every future
                # schema bump would cost every user a full reindex for nothing.
                if version < 3:
                    self._backfill_full_text_index()
                self._ensure_agent_id()
                if version < SCHEMA_VERSION:
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

    def _add_missing_artifact_columns(self) -> None:
        existing = {row["name"] for row in self.db.execute("PRAGMA table_info(generation_artifacts)")}
        for column, definition in ADDED_ARTIFACT_COLUMNS.items():
            if column not in existing:
                log.info("Upgrading schema: adding generation_artifacts.%s", column)
                self.db.execute(
                    f"ALTER TABLE generation_artifacts ADD COLUMN {column} {definition}"
                )

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

    def _ensure_agent_id(self) -> None:
        """Give the agent a permanent identity the first time this file is opened.

        A v0.2 database gains one on upgrade, which is the honest outcome: that
        agent always had a continuous identity, there was simply no name for it.
        `INSERT OR IGNORE` is what makes it mint-once — reopening the file, or two
        processes racing the same migration, cannot produce a second identity.
        """
        self.db.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('agent_id', ?)",
            (lineage.new_agent_id(),),
        )
        self.db.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('agent_created_at', ?)", (_now(),)
        )

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
        query_vector=None,
        embedding_model: str | None = None,
        semantic_weight: float = retrieval.DEFAULT_SEMANTIC_WEIGHT,
    ) -> list[dict]:
        """Rank memories by content relevance, then recency and importance.

        Replaces v0.1's `ORDER BY created_at DESC LIMIT 50`, under which anything
        older than the 50 most recent rows was unreachable regardless of content.

        With a `query_vector`, semantically similar memories are added to the pool
        rather than merely reordering it: re-ranking what BM25 returned could never
        surface the memory that shares no words with the query, which is the only
        reason to run an embedding model at all.
        """
        match = build_match_query(query, self.max_query_terms)
        semantic = self._semantic_matches(
            query_vector, embedding_model, namespace, include_superseded, include_archived,
            candidate_pool,
        )
        if not match:
            return self._rank_semantic_only(
                semantic, limit, weights, halflife_days, semantic_weight, record_access
            )

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

        if not rows and not semantic:
            return []

        # bm25() is more negative for better matches; flip and scale to [0, 1].
        raw = [-row["bm25_score"] for row in rows]
        best = max(raw, default=0.0) or 1.0
        candidates = []
        seen = set()
        for row, relevance in zip(rows, raw, strict=True):
            memory = self._row_to_memory(row)
            memory["lexical"] = max(0.0, relevance / best)
            if semantic:
                memory["semantic"] = semantic.get(memory["id"], 0.0)
            candidates.append(memory)
            seen.add(memory["id"])

        # Semantic hits BM25 never saw: no shared words, so lexical relevance is 0.
        for memory_id, score in semantic.items():
            if memory_id in seen:
                continue
            memory = self.get_memory(memory_id)
            if memory is None:
                continue
            memory["lexical"] = 0.0
            memory["semantic"] = score
            candidates.append(memory)

        ranked = retrieval.rank(
            candidates,
            now=_now(),
            weights=weights,
            halflife_days=halflife_days or retrieval.DEFAULT_HALFLIFE_DAYS,
            limit=limit,
            semantic_weight=semantic_weight,
        )
        if record_access:
            try:
                self._record_access([m["id"] for m in ranked])
            except sqlite3.Error as exc:
                # A counter is not worth failing a read for: a read-only database or a
                # write lock held by another process must still return results.
                log.warning("Could not record access counts: %s", exc)
        return ranked

    def _semantic_matches(
        self, query_vector, model, namespace, include_superseded, include_archived, pool
    ) -> dict:
        """`{memory_id: similarity}` for the closest vectors in this namespace."""
        if query_vector is None or not model or not embeddings.available():
            return {}
        sql = (
            "SELECT e.memory_id, e.vector FROM memory_embeddings e "
            "JOIN memories m ON m.id = e.memory_id "
            "WHERE e.model = ? AND m.namespace = ? "
        )
        params: list = [model, namespace or ""]
        if not include_superseded:
            sql += "AND m.superseded_by IS NULL "
        if not include_archived:
            sql += "AND m.archived = 0 "
        rows = [(r["memory_id"], r["vector"]) for r in self.db.execute(sql, params).fetchall()]
        try:
            ranked = embeddings.rank_by_similarity(query_vector, rows, pool)
        except ValueError as exc:
            # Mixed dimensions mean the model changed without a re-embed. Lexical
            # search still works, so degrade to it rather than failing the query.
            log.warning("Skipping semantic search: %s", exc)
            return {}
        return {memory_id: score for memory_id, score in ranked if score > 0.0}

    def _rank_semantic_only(
        self, semantic, limit, weights, halflife_days, semantic_weight, record_access
    ) -> list[dict]:
        """A query with no usable FTS terms can still be answered by meaning."""
        if not semantic:
            return []
        candidates = []
        for memory_id, score in semantic.items():
            memory = self.get_memory(memory_id)
            if memory is None:
                continue
            memory["lexical"] = 0.0
            memory["semantic"] = score
            candidates.append(memory)
        ranked = retrieval.rank(
            candidates,
            now=_now(),
            weights=weights,
            halflife_days=halflife_days or retrieval.DEFAULT_HALFLIFE_DAYS,
            limit=limit,
            semantic_weight=semantic_weight,
        )
        if record_access:
            with contextlib.suppress(sqlite3.Error):
                self._record_access([m["id"] for m in ranked])
        return ranked

    # ---- embeddings ----------------------------------------------------

    @_synchronized
    def set_embedding(self, memory_id: int, model: str, vector) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO memory_embeddings "
            "(memory_id, model, dimensions, vector, embedded_at) VALUES (?, ?, ?, ?, ?)",
            (int(memory_id), model, len(vector), embeddings.pack(vector), _now()),
        )
        self.db.commit()

    @_synchronized
    def get_embedding(self, memory_id: int) -> list[float] | None:
        row = self.db.execute(
            "SELECT vector FROM memory_embeddings WHERE memory_id = ?", (int(memory_id),)
        ).fetchone()
        return embeddings.unpack(row["vector"]) if row else None

    @_synchronized
    def unembedded(self, model: str, limit: int = 100, namespace: str | None = None) -> list[dict]:
        """Memories with no vector for this model — what a backfill has left to do."""
        sql = (
            "SELECT m.id, m.summary, m.raw_text FROM memories m "
            "LEFT JOIN memory_embeddings e ON e.memory_id = m.id AND e.model = ? "
            "WHERE e.memory_id IS NULL "
        )
        params: list = [model]
        if namespace is not None:
            sql += "AND m.namespace = ? "
            params.append(namespace or "")
        sql += "ORDER BY m.id LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    @_synchronized
    def embedding_coverage(self, model: str | None = None) -> dict:
        total = self.db.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
        sql = "SELECT COUNT(*) AS n FROM memory_embeddings"
        params: list = []
        if model:
            sql += " WHERE model = ?"
            params.append(model)
        embedded = self.db.execute(sql, params).fetchone()["n"]
        return {"embedded": embedded, "total": total}

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
            "version": PROTOCOL_VERSION,
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

    @_synchronized
    def expire(self, max_age_days: float, namespace: str = "") -> dict:
        """Archive memories neither created nor recalled within `max_age_days`.

        A count cap (`prune`) and an age policy answer different questions: one bounds
        how much a namespace holds, the other retires what has stopped being used. A
        namespace under its cap can still be full of memories nobody has read in a year.

        The clock is `last_accessed` falling back to `created_at`, the same reference
        `retention_score` uses — recall refreshes a memory, so use, not importance,
        is what keeps it alive. Importance is assigned once at ingest and never
        revised, so it cannot mark a memory that stopped mattering.

        Nothing is deleted: expiry is archival and `/archive` restores it.
        """
        if max_age_days <= 0:
            raise ValueError("max_age_days must be positive")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        cursor = self.db.execute(
            "UPDATE memories SET archived = 1 WHERE namespace = ? AND archived = 0 "
            "AND COALESCE(last_accessed, created_at) < ?",
            (namespace or "", cutoff),
        )
        self.db.commit()
        kept = self.db.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE namespace = ? AND archived = 0",
            (namespace or "",),
        ).fetchone()["n"]
        return {"status": "pruned", "archived": cursor.rowcount, "kept": kept}

    # ---- generational continuity ---------------------------------------
    #
    # A generation records the runtime an agent ran as; the agent_id records who
    # the agent is. Changing every field of the former must not disturb the
    # latter — that separation is the whole of v0.3.

    def _meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def _set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))

    @staticmethod
    def _as_generation_id(value) -> int:
        # bool is an int in Python; True would silently become generation 1.
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("generation id must be an integer")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError("generation id must be an integer") from None

    @staticmethod
    def _row_to_generation(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "parent_id": row["parent_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "promoted_at": row["promoted_at"],
            "label": row["label"],
            "manifest": json.loads(row["manifest"]),
        }

    @_synchronized
    def agent_id(self) -> str:
        return self._meta("agent_id")

    @_synchronized
    def agent(self) -> dict:
        current = self.current_generation()
        return {
            "agent_id": self._meta("agent_id"),
            "created_at": self._meta("agent_created_at"),
            "current_generation": current["id"] if current else None,
            "generations": self.db.execute("SELECT COUNT(*) AS c FROM generations").fetchone()["c"],
            "version": PROTOCOL_VERSION,
        }

    # ---- the memory corpus as a verifiable whole ----

    def _memory_count(self, namespace: str | None = None) -> int:
        if namespace is None:
            return self.db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
        return self.db.execute(
            "SELECT COUNT(*) AS c FROM memories WHERE namespace = ?", (namespace,)
        ).fetchone()["c"]

    def _corpus_digests(self, namespace: str | None = None) -> dict:
        sql, params = "SELECT * FROM memories", ()
        if namespace is not None:
            sql, params = sql + " WHERE namespace = ?", (namespace,)
        return lineage.corpus_digests(dict(row) for row in self.db.execute(sql, params))

    def _corpus_digest(self, namespace: str | None = None) -> str:
        return self._corpus_digests(namespace)["content"]

    @_synchronized
    def corpus_digests(self, namespace: str | None = None) -> dict:
        """Both corpus digests: what the agent knows, and how it currently reads it.

        See lineage.corpus_digests. The two are separate because they fail for
        different reasons: content says memories were lost or altered, state says
        they all arrived but are now interpreted differently.
        """
        return self._corpus_digests(namespace)

    @_synchronized
    def corpus_digest(self, namespace: str | None = None) -> str:
        """The content digest alone. See corpus_digests for the state digest too."""
        return self._corpus_digest(namespace)

    # ---- reads ----

    @_synchronized
    def get_generation(self, generation_id) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM generations WHERE id = ?", (self._as_generation_id(generation_id),)
        ).fetchone()
        return self._row_to_generation(row) if row else None

    @_synchronized
    def list_generations(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM generations ORDER BY id").fetchall()
        return [self._row_to_generation(r) for r in rows]

    @_synchronized
    def current_generation(self) -> dict | None:
        pointer = self._meta("current_generation")
        return self.get_generation(int(pointer)) if pointer else None

    @_synchronized
    def ancestry(self, generation_id) -> list[int]:
        """Every ancestor of a generation, root first, excluding the generation itself.

        Walks the parent pointer, which is also what makes branching free: two
        candidates staged from one parent are simply two rows with the same
        `parent_id`, and neither can corrupt the other's ancestry.
        """
        generation_id = self._as_generation_id(generation_id)
        row = self.db.execute(
            "SELECT parent_id FROM generations WHERE id = ?", (generation_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no generation with id {generation_id}")
        chain, seen, parent = [], {generation_id}, row["parent_id"]
        while parent is not None and parent not in seen:
            chain.append(parent)
            seen.add(parent)
            row = self.db.execute(
                "SELECT parent_id FROM generations WHERE id = ?", (parent,)
            ).fetchone()
            parent = row["parent_id"] if row else None
        chain.reverse()
        return chain

    @_synchronized
    def migrations(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM migrations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{**dict(r), "checks": json.loads(r["checks"])} for r in rows]

    @_synchronized
    def artifacts(self, generation_id) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM generation_artifacts WHERE generation_id = ? ORDER BY name",
            (self._as_generation_id(generation_id),),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- writes ----

    @_synchronized
    def create_generation(self, parent=None, label: str = "", manifest=None) -> dict:
        """Record a new generation of this agent.

        The root generation is promoted on creation: there is no prior state to
        migrate from and nothing to validate against. Every later generation is
        staged and becomes current only through `promote_generation`, so a new
        runtime never inherits the agent's identity by merely existing.

        A second parentless generation is refused. A lineage with two roots cannot
        answer "what preceded this?", which is the question the record exists for.
        """
        manifest = lineage.validate_manifest(manifest)
        if not isinstance(label, str):
            raise ValueError("'label' must be a string")
        label = label[:120]

        current = self.current_generation()
        if parent is not None:
            parent = self._as_generation_id(parent)
            if self.get_generation(parent) is None:
                raise ValueError(f"no generation with id {parent}")
        elif current is not None:
            raise ValueError(
                "this agent already has a lineage; name a parent so the new generation joins it"
            )

        agent, now, root = self._meta("agent_id"), _now(), parent is None
        try:
            cursor = self.db.execute(
                "INSERT INTO generations (agent_id, parent_id, status, created_at, promoted_at, "
                "label, manifest) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    agent,
                    parent,
                    "promoted" if root else "staged",
                    now,
                    now if root else None,
                    label,
                    json.dumps(manifest),
                ),
            )
            generation_id = cursor.lastrowid
            if root:
                self._set_meta("current_generation", generation_id)
            else:
                # The transition opens now, recording the semantic state being
                # inherited, so a later validation can tell whether it survived.
                self.db.execute(
                    "INSERT INTO migrations (agent_id, from_generation, to_generation, status, "
                    "started_at, memory_records, corpus_digest_before) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (agent, parent, generation_id, "staged", now,
                     self._memory_count(), self._corpus_digest()),
                )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return self.get_generation(generation_id)

    def _write_artifact(self, generation_id: int, artifact: dict) -> None:
        """Record one artifact digest, signed when a signing key is configured.

        Signing failures are not swallowed: a seal that silently records itself
        unsigned would look, later, exactly like a seal made before signing was
        turned on.
        """
        row = {
            "name": artifact["name"],
            "algorithm": artifact.get("algorithm") or "sha256",
            "digest": artifact["digest"],
            "bytes": artifact.get("bytes"),
            "records": artifact.get("records"),
            "recorded_at": artifact["recorded_at"],
        }
        signed = {"signature": None, "public_key": None}
        if self.signing_key:
            statement = lineage.artifact_statement(self._meta("agent_id"), generation_id, row)
            result = signing.sign(statement, self.signing_key)
            signed = {"signature": result["signature"], "public_key": result["public_key"]}
        self.db.execute(
            "INSERT OR REPLACE INTO generation_artifacts (generation_id, name, algorithm, "
            "digest, bytes, records, recorded_at, signature, public_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                generation_id, row["name"], row["algorithm"], row["digest"], row["bytes"],
                row["records"], row["recorded_at"], signed["signature"], signed["public_key"],
            ),
        )

    @_synchronized
    def record_artifacts(self, generation_id, artifacts) -> dict:
        """Record digests a caller computed. The server never reads the files itself.

        Hashing on the server would mean accepting a path from a caller and
        reading it, which is an arbitrary-file-read oracle wearing an integrity
        feature's clothes. The CLI does the reading, locally, against files the
        operator names.
        """
        generation_id = self._as_generation_id(generation_id)
        if self.get_generation(generation_id) is None:
            raise ValueError(f"no generation with id {generation_id}")
        checked = lineage.validate_artifacts(artifacts)
        now = _now()
        try:
            for artifact in checked:
                self._write_artifact(generation_id, artifact | {"recorded_at": now})
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return {"status": "recorded", "generation": generation_id, "artifacts": len(checked)}

    @_synchronized
    def seal_corpus(self, generation_id, namespace: str | None = None) -> dict:
        """Fix the memory corpus this generation inherited, as one digest."""
        generation_id = self._as_generation_id(generation_id)
        if self.get_generation(generation_id) is None:
            raise ValueError(f"no generation with id {generation_id}")
        digests, records, now = self._corpus_digests(namespace), self._memory_count(namespace), _now()
        for name, digest in (
            (lineage.CORPUS_ARTIFACT, digests["content"]),
            (lineage.STATE_ARTIFACT, digests["state"]),
        ):
            self._write_artifact(
                generation_id,
                {"name": name, "algorithm": "sha256", "digest": digest, "bytes": None,
                 "records": records, "recorded_at": now},
            )
        self.db.commit()
        return {
            "status": "sealed",
            "generation": generation_id,
            "digest": digests["content"],
            "state_digest": digests["state"],
            "records": records,
        }

    # ---- continuity validation ----

    def _check_identity(self, generation: dict) -> dict:
        expected = self._meta("agent_id")
        if generation["agent_id"] == expected:
            return {"status": "pass", "detail": f"agent_id {expected}"}
        return {
            "status": "fail",
            "detail": f"generation declares {generation['agent_id']}, this store holds {expected}",
        }

    def _check_memory(self, recorded: dict, live: dict) -> dict:
        """Did the knowledge itself arrive intact?"""
        sealed = recorded.get(lineage.CORPUS_ARTIFACT)
        if sealed is None:
            return {"status": "skipped", "detail": "no memory corpus was sealed for this generation"}
        records = self._memory_count()
        if live["content"] == sealed["digest"]:
            return {
                "status": "pass",
                "detail": f"{records} records match the sealed corpus digest",
                "records": records,
            }
        return {
            "status": "fail",
            "detail": (
                f"corpus content digest no longer matches: {sealed['records']} records were "
                f"sealed, {records} are present now — memories were lost or altered"
            ),
            "records": records,
        }

    def _check_semantic_state(self, recorded: dict, live: dict) -> dict:
        """Is that knowledge still read the same way?

        The failure this exists for: a migration carries every memory across but
        drops the correction chain, so a fact the agent had already retracted
        becomes current again. Every byte of text is present, the content digest
        matches, and the agent is wrong.
        """
        sealed = recorded.get(lineage.STATE_ARTIFACT)
        if sealed is None:
            return {
                "status": "skipped",
                "detail": "no semantic state digest was sealed for this generation",
            }
        if live["state"] == sealed["digest"]:
            return {
                "status": "pass",
                "detail": "validity, archival, corrections and links match the sealed state",
            }
        content_intact = recorded.get(lineage.CORPUS_ARTIFACT) and (
            live["content"] == recorded[lineage.CORPUS_ARTIFACT]["digest"]
        )
        return {
            "status": "fail",
            "detail": (
                "the memories are all present and unaltered, but they are interpreted "
                "differently now — a correction chain, archival flag, validity interval or "
                "consolidation link changed"
                if content_intact
                else "semantic state no longer matches the sealed state"
            ),
        }

    def _check_signature(self, generation: dict, recorded: dict, expected_key) -> dict:
        """Was this seal made by the key it claims, and by the key you expected?

        Integrity and authenticity are different properties. The digests catch a
        corpus that drifted; they cannot catch a corpus that was replaced and
        resealed, because whoever can write the store can recompute them. Only a
        signature over a statement they cannot forge distinguishes the two.

        Verifying against the public key recorded beside the signature proves
        internal consistency and nothing more — an impostor records their own key.
        Pass `expected_key` to check provenance rather than self-consistency.
        """
        signed = [a for a in recorded.values() if a.get("signature")]
        if not signed:
            return {"status": "skipped", "detail": "no artifact in this seal is signed"}
        if not signing.available():
            return {
                "status": "skipped",
                "detail": f"{len(signed)} signed artifacts; " + signing.INSTALL_HINT,
            }

        keys = {a["public_key"] for a in signed}
        if expected_key and keys != {expected_key}:
            sealed_by = ", ".join(sorted(signing.fingerprint(k) for k in keys))
            return {
                "status": "fail",
                "detail": (
                    f"sealed by {sealed_by}, expected "
                    f"{signing.fingerprint(expected_key)} — this seal is not from that key"
                ),
            }

        broken = [
            a["name"]
            for a in signed
            if not signing.verify(
                lineage.artifact_statement(generation["agent_id"], generation["id"], a),
                a["signature"],
                a["public_key"],
            )
        ]
        if broken:
            return {
                "status": "fail",
                "detail": (
                    "signature does not cover what is recorded: " + ", ".join(sorted(broken))
                    + " — the seal was altered after it was signed, or lifted from another generation"
                ),
            }
        unsigned = sorted(name for name, a in recorded.items() if not a.get("signature"))
        by = ", ".join(sorted(signing.fingerprint(k) for k in keys))
        detail = f"{len(signed)} artifacts signed by {by}"
        if unsigned:
            detail += f"; unsigned: {', '.join(unsigned)}"
        return {"status": "pass", "detail": detail}

    def _check_recall(self, probes) -> dict:
        """Can this generation still reach the knowledge it inherited?

        A digest proves the bytes arrived; a probe proves they are still findable.
        Access counts are deliberately not recorded here — validating an agent
        must not distort the retention signals that decide what it keeps.
        """
        if not probes:
            return {"status": "skipped", "detail": "no recall probes supplied"}
        if not isinstance(probes, list):
            raise ValueError("'probes' must be an array")
        missed = []
        for index, probe in enumerate(probes):
            if not isinstance(probe, dict) or not isinstance(probe.get("query"), str):
                raise ValueError(f"probes[{index}] must be an object with a 'query'")
            hits = self.search(
                probe["query"],
                limit=12,
                namespace=probe.get("namespace") or "",
                record_access=False,
            )
            expected = probe.get("expect_memory")
            found = bool(hits) if expected is None else expected in [h["id"] for h in hits]
            if not found:
                missed.append(probe["query"])
        if missed:
            return {"status": "fail", "detail": "could not recall: " + "; ".join(missed[:5])}
        return {"status": "pass", "detail": f"{len(probes)} recall probes resolved"}

    def _check_soul(self, recorded: dict, supplied) -> dict:
        if not supplied:
            return {"status": "skipped", "detail": "no artifact digests were supplied to compare"}
        checked = lineage.validate_artifacts(supplied)
        mismatched = [a["name"] for a in checked
                      if a["name"] in recorded and recorded[a["name"]]["digest"] != a["digest"]]
        missing = [a["name"] for a in checked if a["name"] not in recorded]
        if mismatched or missing:
            detail = []
            if mismatched:
                detail.append("digest mismatch: " + ", ".join(sorted(mismatched)))
            if missing:
                detail.append("never sealed: " + ", ".join(sorted(missing)))
            return {"status": "fail", "detail": "; ".join(detail)}
        return {
            "status": "pass",
            "detail": f"{len(checked)} artifacts match their recorded digests",
        }

    def _check_context(self, generation: dict) -> dict:
        """External references are recorded, never dereferenced.

        jnaapakam does not call a workflow engine, a Git host, or an artifact
        store to confirm anything, so reporting `pass` here would claim a
        verification that never happened. The status says exactly what was done.
        """
        references = generation["manifest"].get("external_state") or []
        if not references:
            return {"status": "skipped", "detail": "no external state was declared"}
        return {
            "status": "recorded",
            "detail": (
                f"{len(references)} external references recorded; verifying them is the "
                "responsibility of the systems that own them"
            ),
            "references": references,
        }

    @staticmethod
    def _check_behavioral(behavioral) -> dict:
        """Recorded, not run: what counts as behavioural drift is the operator's call."""
        if behavioral is None:
            return {"status": "skipped", "detail": "no behavioural evaluation supplied"}
        if not isinstance(behavioral, dict):
            raise ValueError("'behavioral' must be an object")
        status = behavioral.get("status")
        if status not in ("pass", "fail", "skipped"):
            raise ValueError("behavioral.status must be one of: pass, fail, skipped")
        return {"status": status, "detail": str(behavioral.get("detail") or "")[:1000]}

    @_synchronized
    def validate_continuity(
        self, generation_id, artifacts=None, probes=None, behavioral=None, public_key=None
    ) -> dict:
        """Check that a generation still holds the agent it claims to continue.

        Every check reports its own status, and one that was not requested says
        `skipped` rather than `pass` — a validation that silently passes because
        nothing was checked is exactly the failure this is meant to prevent.
        """
        generation_id = self._as_generation_id(generation_id)
        generation = self.get_generation(generation_id)
        if generation is None:
            raise ValueError(f"no generation with id {generation_id}")

        recorded = {a["name"]: a for a in self.artifacts(generation_id)}
        live = self._corpus_digests()
        checks = {
            "identity": self._check_identity(generation),
            "memory": self._check_memory(recorded, live),
            "semantic_state": self._check_semantic_state(recorded, live),
            "signature": self._check_signature(generation, recorded, public_key),
            "recall": self._check_recall(probes),
            "soul": self._check_soul(recorded, artifacts),
            "context": self._check_context(generation),
            "behavioral": self._check_behavioral(behavioral),
        }
        result = {
            "generation": generation_id,
            "agent_id": generation["agent_id"],
            "checked_at": _now(),
            "passed": all(check["status"] != "fail" for check in checks.values()),
            "checks": checks,
        }
        self._record_validation(generation_id, result)
        return result

    def _record_validation(self, generation_id: int, result: dict) -> None:
        """Attach the outcome to this generation's open transition, or open one."""
        row = self.db.execute(
            "SELECT id FROM migrations WHERE to_generation = ? ORDER BY id DESC LIMIT 1",
            (generation_id,),
        ).fetchone()
        status = "validated" if result["passed"] else "failed"
        checks, digest, records = json.dumps(result["checks"]), self._corpus_digest(), self._memory_count()
        try:
            if row is None:
                self.db.execute(
                    "INSERT INTO migrations (agent_id, from_generation, to_generation, status, "
                    "started_at, completed_at, memory_records, corpus_digest_after, checks) "
                    "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                    (self._meta("agent_id"), generation_id, status, result["checked_at"],
                     result["checked_at"], records, digest, checks),
                )
            else:
                self.db.execute(
                    "UPDATE migrations SET status = ?, completed_at = ?, corpus_digest_after = ?, "
                    "memory_records = ?, checks = ? WHERE id = ?",
                    (status, result["checked_at"], digest, records, checks, row["id"]),
                )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    # ---- promotion, rejection, rollback ----

    @_synchronized
    def promote_generation(self, generation_id, force: bool = False) -> dict:
        """Make a generation the current one.

        Refused unless its most recent validation passed, so a candidate that
        failed — or was never checked at all — cannot quietly become the agent.
        `force` exists for the operator who has decided anyway, and is written
        onto the migration record so the override is never invisible later.

        The status change, the migration record, and the current-generation
        pointer move in one transaction: a failure part-way through leaves the
        agent on the generation it already had.
        """
        generation_id = self._as_generation_id(generation_id)
        generation = self.get_generation(generation_id)
        if generation is None:
            raise ValueError(f"no generation with id {generation_id}")
        if generation["status"] == "rejected":
            raise ValueError(f"generation {generation_id} was rejected and cannot be promoted")

        latest = self.db.execute(
            "SELECT * FROM migrations WHERE to_generation = ? ORDER BY id DESC LIMIT 1",
            (generation_id,),
        ).fetchone()
        validated = latest is not None and latest["status"] == "validated"
        if not validated and not force:
            reason = (
                "its last continuity validation did not pass"
                if latest is not None and latest["status"] == "failed"
                else "its continuity has not been validated"
            )
            raise ValueError(f"refusing to promote generation {generation_id}: {reason}")

        previous = self.current_generation()
        now = _now()
        note = "" if validated else "forced promotion without a passing validation"
        try:
            self.db.execute(
                "UPDATE generations SET status = 'promoted', promoted_at = COALESCE(promoted_at, ?) "
                "WHERE id = ?",
                (now, generation_id),
            )
            if latest is None:
                self.db.execute(
                    "INSERT INTO migrations (agent_id, from_generation, to_generation, status, "
                    "started_at, completed_at, memory_records, corpus_digest_after, note) "
                    "VALUES (?, ?, ?, 'promoted', ?, ?, ?, ?, ?)",
                    (generation["agent_id"], previous["id"] if previous else None, generation_id,
                     now, now, self._memory_count(), self._corpus_digest(), note),
                )
            else:
                self.db.execute(
                    "UPDATE migrations SET status = 'promoted', completed_at = ?, note = ? "
                    "WHERE id = ?",
                    (now, note or latest["note"], latest["id"]),
                )
            self._set_meta("current_generation", generation_id)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return {
            "status": "promoted",
            "generation": generation_id,
            "previous_generation": previous["id"] if previous else None,
            "forced": not validated,
        }

    @_synchronized
    def reject_generation(self, generation_id, reason: str = "") -> dict:
        """Close a candidate off without removing it from the lineage."""
        generation_id = self._as_generation_id(generation_id)
        generation = self.get_generation(generation_id)
        if generation is None:
            raise ValueError(f"no generation with id {generation_id}")
        current = self.current_generation()
        if current and current["id"] == generation_id:
            raise ValueError(
                "refusing to reject the current generation; roll back to another one first"
            )

        now, note = _now(), str(reason)[:500]
        try:
            self.db.execute(
                "UPDATE generations SET status = 'rejected' WHERE id = ?", (generation_id,)
            )
            latest = self.db.execute(
                "SELECT id FROM migrations WHERE to_generation = ? ORDER BY id DESC LIMIT 1",
                (generation_id,),
            ).fetchone()
            if latest is None:
                self.db.execute(
                    "INSERT INTO migrations (agent_id, from_generation, to_generation, status, "
                    "started_at, completed_at, note) VALUES (?, ?, ?, 'rejected', ?, ?, ?)",
                    (generation["agent_id"], generation["parent_id"], generation_id, now, now, note),
                )
            else:
                self.db.execute(
                    "UPDATE migrations SET status = 'rejected', completed_at = ?, note = ? "
                    "WHERE id = ?",
                    (now, note, latest["id"]),
                )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return {"status": "rejected", "generation": generation_id, "reason": note}

    @_synchronized
    def rollback_generation(self, generation_id) -> dict:
        """Return to an earlier generation without erasing anything.

        The generation being left keeps its `promoted` status and every memory
        stays exactly where it is. Rolling back is a change of runtime, not a
        retraction of what the agent learned while running it — so this appends a
        `rolled_back` record rather than rewriting the log.
        """
        generation_id = self._as_generation_id(generation_id)
        target = self.get_generation(generation_id)
        if target is None:
            raise ValueError(f"no generation with id {generation_id}")
        if target["status"] != "promoted":
            raise ValueError(
                f"generation {generation_id} was never current; a rollback target must be a "
                "generation that was promoted at some point"
            )
        current = self.current_generation()
        if current and current["id"] == generation_id:
            raise ValueError(f"generation {generation_id} is already current")

        now = _now()
        try:
            self.db.execute(
                "INSERT INTO migrations (agent_id, from_generation, to_generation, status, "
                "started_at, completed_at, memory_records, corpus_digest_after, note) "
                "VALUES (?, ?, ?, 'rolled_back', ?, ?, ?, ?, 'rollback')",
                (target["agent_id"], current["id"] if current else None, generation_id, now, now,
                 self._memory_count(), self._corpus_digest()),
            )
            self._set_meta("current_generation", generation_id)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return {
            "status": "rolled_back",
            "generation": generation_id,
            "from_generation": current["id"] if current else None,
        }

    # ---- comparison ----

    @_synchronized
    def diff_generations(self, before_id, after_id) -> dict:
        """What changed between two generations, and what did not."""
        before_id, after_id = self._as_generation_id(before_id), self._as_generation_id(after_id)
        before, after = self.get_generation(before_id), self.get_generation(after_id)
        for identifier, generation in ((before_id, before), (after_id, after)):
            if generation is None:
                raise ValueError(f"no generation with id {identifier}")

        left = {a["name"]: a for a in self.artifacts(before_id)}
        right = {a["name"]: a for a in self.artifacts(after_id)}
        artifacts = {}
        for name in sorted(
            (left.keys() | right.keys()) - {lineage.CORPUS_ARTIFACT, lineage.STATE_ARTIFACT}
        ):
            if name not in left:
                artifacts[name] = "added"
            elif name not in right:
                artifacts[name] = "removed"
            else:
                artifacts[name] = (
                    "unchanged" if left[name]["digest"] == right[name]["digest"] else "changed"
                )

        def compare(artifact):
            first, second = left.get(artifact), right.get(artifact)
            if not (first and second):
                return "unknown"
            return "unchanged" if first["digest"] == second["digest"] else "changed"

        sealed_before = left.get(lineage.CORPUS_ARTIFACT)
        sealed_after = right.get(lineage.CORPUS_ARTIFACT)
        corpus, state = compare(lineage.CORPUS_ARTIFACT), compare(lineage.STATE_ARTIFACT)

        difference = {
            "from": before_id,
            "to": after_id,
            "agent_id": {
                "stable": before["agent_id"] == after["agent_id"],
                "value": after["agent_id"],
            },
            "sections": lineage.diff_manifests(before["manifest"], after["manifest"]),
            "artifacts": artifacts,
            "memory": {
                "records": [
                    sealed_before["records"] if sealed_before else None,
                    sealed_after["records"] if sealed_after else None,
                ],
                "corpus": corpus,
                "state": state,
            },
        }
        external = lineage.diff_external_state(before["manifest"], after["manifest"])
        if external:
            difference["external_state"] = external
        return difference

    # ---- backup --------------------------------------------------------

    @_synchronized
    def export_all(self) -> dict:
        """Export memories and the continuity record that describes them.

        Soul files stay out, exactly as in v0.2: they are plain files the user
        versions themselves. Only their *digests* travel, inside `artifacts`, so a
        restore can prove the soul that arrived is the soul that left without the
        backup ever becoming a place secrets could hide.
        """
        memories = [dict(r) for r in self.db.execute("SELECT * FROM memories ORDER BY id")]
        consolidations = [dict(r) for r in self.db.execute("SELECT * FROM consolidations ORDER BY id")]
        current = self._meta("current_generation")
        digests = self._corpus_digests()
        return {
            "version": PROTOCOL_VERSION,
            "exported_at": _now(),
            "agent_id": self._meta("agent_id"),
            "agent_created_at": self._meta("agent_created_at"),
            "current_generation": int(current) if current else None,
            "corpus_digest": digests["content"],
            "corpus_state_digest": digests["state"],
            "memories": memories,
            "consolidations": consolidations,
            "generations": [
                self._row_to_generation(r)
                for r in self.db.execute("SELECT * FROM generations ORDER BY id")
            ],
            "migrations": [
                {**dict(r), "checks": json.loads(r["checks"])}
                for r in self.db.execute("SELECT * FROM migrations ORDER BY id")
            ],
            "artifacts": [
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM generation_artifacts ORDER BY generation_id, name"
                )
            ],
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

        v0.3 adds the continuity record — identity, generations, migrations and
        artifact digests — through the same transaction and the same remapping. A
        v0.2 payload simply carries none of those keys and restores as it always did.
        """
        if not isinstance(data, dict):
            raise ValueError("backup must be a JSON object")
        memories = data.get("memories", [])
        consolidations = data.get("consolidations", [])
        if not isinstance(memories, list) or not isinstance(consolidations, list):
            raise ValueError("'memories' and 'consolidations' must be arrays")

        adopt, prepared_lineage = self._prepare_lineage(data)

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
            generations_imported = self._import_lineage(data, adopt, prepared_lineage)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

        return {
            "status": "restored",
            "memories_imported": len(prepared),
            "consolidations_imported": len(prepared_consolidations),
            "generations_imported": generations_imported,
            "agent_id": self._meta("agent_id"),
        }

    def _prepare_lineage(self, data: dict) -> tuple[str | None, dict]:
        """Validate the continuity half of a backup before a single row is written.

        Returns the agent identity to adopt, if any, and the checked payload.

        The identity rule: a store with no lineage of its own adopts the backup's
        agent_id — that is the migration case, a fresh machine inheriting an
        existing agent, and it has to be frictionless. A store that already has a
        lineage refuses a different agent outright, because merging two agents'
        continuity records produces one that describes neither.
        """
        generations = data.get("generations", [])
        migrations = data.get("migrations", [])
        artifacts = data.get("artifacts", [])
        for name, value in (
            ("generations", generations), ("migrations", migrations), ("artifacts", artifacts)
        ):
            if not isinstance(value, list):
                raise ValueError(f"'{name}' must be an array")

        adopt = None
        incoming = data.get("agent_id")
        if incoming is not None:
            if not lineage.is_agent_id(incoming):
                raise ValueError("'agent_id' is not a jnaapakam agent identifier")
            if incoming != self._meta("agent_id"):
                if self.db.execute("SELECT COUNT(*) AS c FROM generations").fetchone()["c"]:
                    raise ValueError(
                        "refusing to restore another agent's continuity state into a store that "
                        "already has a lineage of its own"
                    )
                adopt = incoming

        prepared_generations = []
        for index, generation in enumerate(generations):
            if not isinstance(generation, dict):
                raise ValueError(f"generations[{index}] must be an object")
            status = generation.get("status") or "staged"
            if status not in GENERATION_STATUSES:
                raise ValueError(f"generations[{index}].status is not a known generation status")
            if not generation.get("created_at"):
                raise ValueError(f"generations[{index}] is missing 'created_at'")
            manifest = generation.get("manifest")
            if isinstance(manifest, str):
                try:
                    manifest = json.loads(manifest)
                except (TypeError, ValueError):
                    raise ValueError(f"generations[{index}].manifest is not valid JSON") from None
            # Restored manifests are as untrusted as freshly submitted ones.
            prepared_generations.append(
                {"row": generation, "status": status,
                 "manifest": lineage.validate_manifest(manifest or {})}
            )

        for index, migration in enumerate(migrations):
            if not isinstance(migration, dict):
                raise ValueError(f"migrations[{index}] must be an object")
            if not migration.get("started_at"):
                raise ValueError(f"migrations[{index}] is missing 'started_at'")

        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict):
                raise ValueError(f"artifacts[{index}] must be an object")
            if not lineage.is_artifact_name(artifact.get("name")):
                raise ValueError(f"artifacts[{index}].name must be a plain label, not a path")
            if not lineage.is_digest(artifact.get("digest")):
                raise ValueError(f"artifacts[{index}].digest must be a lowercase hex sha256")

        return adopt, {
            "generations": prepared_generations,
            "migrations": migrations,
            "artifacts": artifacts,
        }

    def _import_lineage(self, data: dict, adopt: str | None, prepared: dict) -> int:
        """Write the continuity record, inside the caller's open transaction."""
        if adopt:
            self._set_meta("agent_id", adopt)
            if data.get("agent_created_at"):
                self._set_meta("agent_created_at", data["agent_created_at"])
        agent = self._meta("agent_id")

        # A generation already present with the same id and creation time is the
        # same generation: re-importing this agent's own backup must not fork its
        # lineage into two copies of itself.
        existing = {
            row["id"]: row["created_at"]
            for row in self.db.execute("SELECT id, created_at FROM generations")
        }
        generation_map: dict[int, int] = {}
        imported = 0
        for item in prepared["generations"]:
            generation = item["row"]
            old_id = generation.get("id")
            if isinstance(old_id, int) and existing.get(old_id) == generation.get("created_at"):
                generation_map[old_id] = old_id
                continue
            keep = isinstance(old_id, int) and old_id > 0 and old_id not in existing
            cursor = self.db.execute(
                "INSERT INTO generations (id, agent_id, parent_id, status, created_at, "
                "promoted_at, label, manifest) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
                (old_id if keep else None, agent, item["status"], generation["created_at"],
                 generation.get("promoted_at"), str(generation.get("label") or "")[:120],
                 json.dumps(item["manifest"])),
            )
            new_id = cursor.lastrowid
            existing[new_id] = generation["created_at"]
            imported += 1
            if isinstance(old_id, int):
                generation_map[old_id] = new_id

        # Second pass, as for memories: parents may have been renumbered, and a
        # parent that no longer resolves becomes a root rather than a dangling id.
        for item in prepared["generations"]:
            old_id = item["row"].get("id")
            new_id = generation_map.get(old_id) if isinstance(old_id, int) else None
            if new_id is None:
                continue
            parent = item["row"].get("parent_id")
            self.db.execute(
                "UPDATE generations SET parent_id = ? WHERE id = ?",
                (generation_map.get(parent) if isinstance(parent, int) else None, new_id),
            )

        for artifact in prepared["artifacts"]:
            target = generation_map.get(artifact.get("generation_id"))
            if target is None:
                continue
            self.db.execute(
                "INSERT OR REPLACE INTO generation_artifacts (generation_id, name, algorithm, "
                "digest, bytes, records, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (target, artifact["name"], artifact.get("algorithm") or "sha256",
                 artifact["digest"], artifact.get("bytes"), artifact.get("records"),
                 artifact.get("recorded_at") or _now()),
            )

        seen_migrations = {
            (row["id"], row["started_at"])
            for row in self.db.execute("SELECT id, started_at FROM migrations")
        }
        for migration in prepared["migrations"]:
            if (migration.get("id"), migration.get("started_at")) in seen_migrations:
                continue
            target = generation_map.get(migration.get("to_generation"))
            if target is None:
                continue
            checks = migration.get("checks")
            self.db.execute(
                "INSERT INTO migrations (agent_id, from_generation, to_generation, status, "
                "started_at, completed_at, memory_records, corpus_digest_before, "
                "corpus_digest_after, checks, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (agent, generation_map.get(migration.get("from_generation")), target,
                 str(migration.get("status") or "staged"), migration["started_at"],
                 migration.get("completed_at"), migration.get("memory_records"),
                 migration.get("corpus_digest_before"), migration.get("corpus_digest_after"),
                 checks if isinstance(checks, str) else json.dumps(checks or {}),
                 str(migration.get("note") or "")[:500]),
            )

        # Only adopted when this store has no current generation of its own:
        # restoring a backup must not silently move a running agent's pointer.
        incoming_current = data.get("current_generation")
        if self._meta("current_generation") is None and isinstance(incoming_current, int):
            mapped = generation_map.get(incoming_current)
            if mapped is not None:
                self._set_meta("current_generation", mapped)
        return imported
