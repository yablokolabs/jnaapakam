# jñāpakaṁ Protocol Specification v0.5

## Overview

The jñāpakaṁ protocol defines a standard for AI agent memory persistence. It consists of three parts:

1. **Soul Schema** — Static identity files that define who an agent is
2. **Memory API** — HTTP endpoints for dynamic memory operations
3. **Continuity Record** — The agent's permanent identity, its generations, and the migrations between them (§10)

### What changed in v0.5

v0.4 could prove a corpus had not drifted. It could not prove who sealed it, could
not retire a memory that had simply stopped being used, and could only find a memory
by the words it happened to contain.

| Change | Compatibility |
|--------|---------------|
| **Seal signatures** (§10.7) — Ed25519 over a statement binding agent, generation and artifact | Additive; optional. An unsigned seal stays valid |
| `signature` added as an eighth continuity check (§10.5) | Additive; reports `skipped` where unsupported |
| **Age retention policy** — `POST /prune` gains `older_than_days`, and recall resets the clock (§8) | Additive; `keep` behaves as before |
| Age policies MAY be applied on a schedule, and MUST be off unless configured (§8) | Additive |
| **Semantic relevance** — an optional embedding term that widens the candidate set rather than reordering it (§5) | Additive; a deployment without it ranks exactly as before |
| `version` reported by `/status` and `/backup` becomes `"0.5"` | **Breaking** only for a client asserting the exact string |

**Why this is a version bump and not a patch.** Nothing here changes a digest or
invalidates a v0.4 seal — a v0.4 generation validates unchanged under v0.5, with the
new check reporting `skipped`. But §5, §8 and §10.5 all gained normative rules, and
two implementations both claiming "0.4" would now disagree about what `/prune`
accepts and what a `signature` check means. The version number is what tells them
apart.

### What changed in v0.4

v0.3 verified that an agent's *memories* survived a migration. It did not verify
that the agent still **read** them the same way, which is a different and equally
load-bearing property.

| Change | Compatibility |
|--------|---------------|
| **Semantic state digest** — validity, archival, correction chains and consolidation links are now hashed (§10.6) | Additive check; **breaking** digest definition |
| Content digest now covers `entities` and `topics`, which are indexed and decide what is retrievable | **Breaking** digest definition |
| Cross-row references are hashed by the target's content digest rather than its row id, so they survive renumbering while a lost link is still detected | **Breaking** digest definition |
| `semantic_state` added as a seventh continuity check (§10.5) | Additive |
| `/backup` gains `corpus_state_digest`; sealing records a `memory_state` artifact | Additive |

**Why this is a version bump and not a patch.** The digest rules in §10.6 are
normative. A v0.3 implementation and a v0.4 implementation compute different
digests for the same corpus, so a generation sealed under v0.3 will not verify
under v0.4. That is an interoperability break, and hiding it behind a patch
release would leave two implementations silently disagreeing about whether an
agent's continuity held.

A v0.3 generation carries no `memory_state` artifact, so that check reports
`skipped` rather than failing. Everything outside §10.6 is unchanged.

### What changed in v0.3

v0.2 made an agent's memory portable. v0.3 makes it portable *across generations of
the agent itself* — a new model, runtime, host, or machine — without the accumulated
identity and history starting from zero.

| Change | Compatibility |
|--------|---------------|
| **Permanent `agent_id`** — a stable identifier independent of name, model, runtime, host and generation (§10.1) | Additive; minted automatically when an existing database is opened |
| **Generations** — a portable record of the runtime an agent ran as, with a parent pointer (§10.2) | Additive |
| **Migration records** — provenance for every transition, including its outcome (§10.4) | Additive |
| **Continuity validation** — six named checks, each reporting its own result (§10.5) | Additive |
| **Artifact integrity** — SHA-256 digests over soul files and the memory corpus (§10.6) | Additive |
| `GET /agent`, `GET /generations`, `GET /generations/diff`, `GET /migrations` added | Additive |
| `POST /generations`, `/generations/artifacts`, `/generations/validate`, `/generations/promote`, `/generations/reject`, `/generations/rollback` added | Additive |
| MCP gains three **read-only** tools: `get_agent_identity`, `list_generations`, `diff_generations` | Additive |
| `/backup` gains `agent_id`, `generations`, `migrations`, `artifacts`, `corpus_digest`; soul files still excluded | Additive; a v0.2 backup still restores |
| `/restore` refuses a backup from a different agent when the store already has a lineage | New refusal on data v0.2 could not produce |
| `version` reported by `/status` and `/backup` becomes `"0.3"` | **Breaking** only for a client asserting the exact string |

### What changed in v0.2

v0.1 specified storage but not retrieval, and its reference implementation answered every question from the most recent memories only. v0.2 makes retrieval part of the protocol.

| Change | Compatibility |
|--------|---------------|
| `GET /search` added — ranked records with provenance | Additive |
| `GET /query` now retrieves before synthesizing, and returns the memories it used | Additive; response gains a `memories` field |
| **Namespaces** — every operation is scoped; the shared pool becomes the `""` namespace | Additive; unscoped clients are unaffected |
| `POST /supersede` added — correction by invalidation rather than deletion | Additive |
| `GET /namespaces` added | Additive |
| Bearer authentication defined; required on non-loopback binds | **Breaking** for exposed deployments |
| Default port corrected to 8889 across spec and implementation | Fixes a v0.1 spec/implementation mismatch |
| `/delete` returns 404 for an unknown id; invalid input returns 400 | **Breaking** for clients that relied on 200 + `"not_found"` |
| `/backup` no longer claims to include soul files | Spec correction — v0.1 implementations never did |
| `POST /reconcile`, `POST /prune`, `POST /archive` added | Additive |
| Full-text index, scope, validity, usage, and archive columns added to the schema | Additive |

## 1. Soul Schema

### File Format

All soul files are **Markdown** with structured content. This makes them:
- Human-readable and editable
- Version-controllable (git-friendly)
- Parseable by any LLM without special tooling
- Framework-agnostic

### Required Files

#### SOUL.md

Defines the agent's personality, tone, and behavioral boundaries.

**Structure:**
```markdown
# SOUL.md

## Core Personality
<!-- How the agent should behave, communicate, and think -->

## Boundaries
<!-- What the agent should never do -->

## Preferences
<!-- Communication style, verbosity, formality -->
```

**Rules:**
- MUST be the primary behavioral guide for the agent
- SHOULD be loaded at the start of every session
- MAY be updated by the agent with user consent
- MUST NOT contain secrets or credentials

#### IDENTITY.md

Defines the agent's name, avatar, and core attributes.

**Structure:**
```markdown
# IDENTITY.md

- **Name:** <agent name>
- **Emoji:** <signature emoji>
- **Created:** <ISO date>
- **Description:** <one-line description>
```

**Rules:**
- MUST contain at least `Name`
- SHOULD be set once and rarely changed
- MAY include custom fields

#### MEMORY.md

Curated long-term memory, maintained by the agent or user.

**Structure:**
```markdown
# MEMORY.md

## <Category>
- <Memory item>
- <Memory item>
```

**Rules:**
- SHOULD be organized by topic/category
- SHOULD be periodically reviewed and pruned
- MAY be auto-updated from Memory API consolidations
- MUST NOT contain secrets or credentials

### Optional Files

#### USER.md
Information about the primary user the agent serves.

#### TOOLS.md
Environment-specific tool configurations and notes.

#### HEARTBEAT.md
Periodic tasks the agent should check on.

## 2. Memory API

### Transport

- **Protocol:** HTTP/1.1 or HTTP/2
- **Content-Type:** `application/json`
- **Default Port:** 8889
- **Default Bind:** `127.0.0.1`
- **Base Path:** `/` (no versioned prefix in v0.4)

### Authentication

Implementations SHOULD support a bearer token supplied via the `Authorization` header:

```
Authorization: Bearer <token>
```

**Rules:**
- An implementation MUST NOT bind a non-loopback address without authentication configured. It SHOULD refuse to start rather than expose destructive endpoints.
- When authentication is configured, ALL endpoints MUST require it, including `/mcp`.
- A missing or incorrect token MUST return `401`.
- Loopback binds MAY omit authentication for local development.

### Error Semantics

| Status | Meaning |
|--------|---------|
| `400` | Malformed request: bad JSON, missing required field, non-integer `limit` |
| `401` | Missing or invalid authentication |
| `404` | Referenced memory does not exist |
| `413` | Request body exceeds the implementation's limit |
| `5xx` | Upstream failure, including the LLM provider being unreachable |

An implementation MUST NOT report a failed extraction as success. If the model is
unreachable or its output cannot be parsed, `/ingest` MUST return an error rather
than storing a degraded memory.

### Endpoints

#### GET /status

Returns memory statistics.

```json
{
  "total_memories": 42,
  "unconsolidated": 5,
  "consolidations": 8,
  "version": "0.5"
}
```

#### POST /ingest

Store new information in memory.

**Request:**
```json
{
  "text": "string (required) — The information to remember",
  "source": "string (optional) — Where this came from",
  "namespace": "string (optional) — Scope; defaults to the shared namespace",
  "kind": "string (optional) — Classification label; defaults to \"factual\""
}
```

**Response:**
```json
{
  "status": "stored",
  "memory_id": 1,
  "summary": "LLM-generated summary of the ingested text"
}
```

**Behavior:**
1. The server sends the text to an LLM for structured extraction
2. The LLM returns: summary, entities, topics, importance (0.0-1.0)
3. The structured memory is stored, with the original text retained and indexed
4. The memory is marked as "unconsolidated"
5. On extraction failure the server MUST return an error, not a placeholder memory

#### GET /search?q={query}

Retrieve ranked memory records. This is the primary retrieval endpoint.

**Parameters:**
- `q` (required) — The search text
- `limit` (optional, default 12) — Maximum records to return
- `namespace` (optional, default `""`) — Restrict to one namespace; see §6
- `include_superseded` (optional, default `false`) — Include corrected memories; see §7

**Response:**
```json
{
  "query": "editor preferences",
  "memories": [
    {
      "id": 1,
      "source": "conversation",
      "summary": "User prefers dark mode and vim keybindings",
      "raw_text": "...",
      "entities": ["user"],
      "topics": ["preferences", "UI"],
      "importance": 0.6,
      "created_at": "2026-03-08T01:50:00Z",
      "score": 0.83
    }
  ],
  "count": 1
}
```

**Behavior:**
1. The query is matched against a full-text index over the memory's summary, original text, entities, and topics
2. Candidates are ranked by a combination of lexical relevance, recency, and importance
3. Query text MUST be treated as literal content — search-engine operators appearing in user input MUST NOT be executed as syntax

**Rules:**
- Retrieval MUST consider all stored memories, not only the most recent N. A memory MUST remain retrievable by its content regardless of how many memories were stored after it.
- Each result MUST carry enough provenance to trace it to its origin: at minimum `id`, `source`, and `created_at`.

#### GET /query?q={question}

Answer a question in natural language.

**Response:**
```json
{
  "question": "what tools does the user prefer?",
  "answer": "Based on stored memories, the user prefers... [Memory #1]",
  "memories": [ ... ]
}
```

**Behavior:**
1. Relevant memories are retrieved as in `/search`
2. Consolidation insights are appended as additional context
3. An LLM synthesizes an answer from that context only
4. Memory IDs are cited in the response, and the memories used are returned alongside it

Clients that will reason over the memories themselves SHOULD prefer `/search`: it
preserves provenance and avoids a second LLM call.

#### GET /memories

List stored memories in reverse chronological order.

**Parameters:**
- `limit` (optional, default 50) — Maximum memories to return. A non-integer or non-positive value MUST return `400`.
- `namespace` (optional, default `""`) — Restrict to one namespace

#### POST /consolidate

Trigger manual memory consolidation.

**Response:**
```json
{
  "status": "consolidated",
  "memories_processed": 5,
  "insight": "Cross-cutting pattern discovered across memories"
}
```

**Behavior:**
1. Load unconsolidated memories, oldest first
2. If fewer than 2, skip with `{"status": "skipped"}`
3. LLM finds connections and patterns
4. Generates a summary and key insight
5. Maps connections between memory IDs, without duplicating existing edges
6. Marks source memories as consolidated

#### POST /delete

Delete a specific memory.

**Request:** `{"memory_id": 1}`

**Response:** `{"status": "deleted", "memory_id": 1}`

A missing `memory_id` MUST return `400`. An id that does not exist MUST return `404`.

#### POST /supersede

Replace one memory with another, without destroying the original.

**Request:** `{"old_id": 3, "new_id": 4}`

**Response:** `{"status": "superseded", "old_id": 3, "new_id": 4}`

Returns `400` when either id is unknown, the ids are equal, or the two memories are
in different namespaces. See §7.

#### GET /namespaces

List every namespace with a memory count.

```json
{"namespaces": [{"namespace": "", "total_memories": 12},
                {"namespace": "project-a", "total_memories": 3}]}
```

#### POST /reconcile

Detect contradictions among active memories and resolve them by supersession.

**Parameters:** `namespace` (optional), `limit` (optional, default 50)

**Response:** `{"status": "reconciled", "compared": N, "superseded": [...], "errors": N}`

Returns `409` with `{"status": "disabled"}` when no judge model is configured.

**Rules:**
- An implementation MUST NOT enable automatic contradiction resolution by default. It MUST require an explicitly configured judge model.
- Candidate pairs MUST be filtered deterministically before any model call, and MUST share a namespace.
- A judge response that states its verdict before its reasoning MUST be rejected as unreasoned.
- Below the configured confidence threshold, an implementation MUST take no action.
- Reconciliation MUST NOT run on the `/ingest` request path.
- The number of model calls per cycle MUST be bounded.

#### POST /prune

Apply a retention policy to a namespace. Two are defined and they are independent:
`keep` caps how many memories a namespace holds, `older_than_days` retires the ones
that have stopped being used.

**Parameters:** `keep` (count cap), `older_than_days` (age limit), `namespace`
(optional). At least one policy MUST be given; a request naming neither MUST be
rejected rather than defaulted.

**Response:** `{"status": "pruned", "archived": N, "kept": N}`, where `archived`
counts every memory retired by the request.

**Rules:**
- The age clock is `last_accessed`, falling back to `created_at`. Recall therefore
  refreshes a memory, and a memory still being read MUST NOT expire on age alone.
- When both policies are given, the age policy is applied first, and `archived`
  reports the total across both.

#### POST /archive

Archive or restore a single memory.

**Request:** `{"memory_id": N, "restore": false}`

**Rules:**
- Forgetting MUST be reversible. An implementation MUST NOT delete memories to satisfy a retention policy; `/delete` remains the only destructive operation.
- Archived memories MUST be excluded from retrieval by default and reachable when explicitly requested.
- Retention scoring SHOULD combine importance, access frequency, and temporal decay. Frequency is the outcome-grounded term; `importance` is assigned once and never revised.
- Superseded memories SHOULD be evicted before live ones.
- An age policy MAY be applied on a schedule rather than only on request. It MUST be
  off unless configured: retiring memories on a timer the operator never asked for is
  indistinguishable, from inside the agent, from losing them.

#### POST /clear

Delete all memories, consolidations, and processed file records. Destructive; MUST be protected by authentication when configured.

#### GET /backup

Export memories, consolidations, and the continuity record as JSON.

```json
{
  "version": "0.5",
  "exported_at": "2026-03-08T12:00:00Z",
  "agent_id": "urn:jnaapakam:agent:...",
  "current_generation": 2,
  "corpus_digest": "sha256 hex",
  "corpus_state_digest": "sha256 hex",
  "memories": [ ... ],
  "consolidations": [ ... ],
  "generations": [ ... ],
  "migrations": [ ... ],
  "artifacts": [ ... ]
}
```

Soul files are plain files on disk and are versioned by the user; they are
deliberately not part of the backup payload. Only their *digests* travel, inside
`artifacts`, so a restore can prove the soul that arrived is the soul that left
without the backup becoming a place secrets could hide.

#### POST /restore

Import from a backup. Same format as the `/backup` response.

A v0.2 payload carries none of the continuity keys and MUST still restore.

The payload MUST be validated before any row is written; a malformed backup MUST
return `400` rather than partially importing. Restore MUST be atomic — a failure
part-way through MUST leave the store exactly as it was, with no transaction left
open for a later read to commit.

Restore MUST preserve referential integrity. Memory ids are referenced by
`superseded_by`, `connections[].linked_to`, and `consolidations.source_ids`; an
implementation MUST either preserve ids or remap every reference through the same
mapping, and MUST drop references that no longer resolve rather than leaving them
dangling. Reassigning ids without remapping silently repoints every correction chain
in the backup.

Restore MUST apply the same rules to the continuity record: generation ids are
referenced by `generations.parent_id`, `migrations.from_generation`,
`migrations.to_generation`, and `artifacts.generation_id`. See §10.8 for the
identity rules a restore MUST enforce before writing anything.

### Continuity Endpoints

All of these are ordinary REST endpoints and MUST sit behind the same
authentication as the rest of the API. See §10 for their semantics.

#### GET /agent

```json
{"agent_id": "urn:jnaapakam:agent:...", "created_at": "...",
 "current_generation": 2, "generations": 3, "version": "0.5"}
```

#### GET /generations

Lists every generation. With `?id=N`, returns that generation plus its `ancestry`
and recorded `artifacts`. An unknown id MUST return `404`; a non-integer id `400`.

#### POST /generations

`{"parent": N, "label": "...", "manifest": {...}}` — records a candidate generation.
`parent` and `label` are optional; `manifest` is optional and every section within
it is optional. Returns the created generation.

#### POST /generations/artifacts

`{"generation": N, "artifacts": [{"name": "SOUL.md", "algorithm": "sha256",
"digest": "..."}], "seal_corpus": true}`

Records integrity metadata. Digests MUST arrive precomputed — see §10.6.

#### POST /generations/validate

`{"generation": N, "artifacts": [...], "probes": [...], "behavioral": {...}}`

Runs the continuity checks of §10.5 and records the outcome on the migration.

#### POST /generations/promote

`{"generation": N, "force": false}` — makes a generation current. See §10.7.

#### POST /generations/reject

`{"generation": N, "reason": "..."}` — closes a candidate off without removing it.

#### POST /generations/rollback

`{"generation": N}` — returns to a previously promoted generation.

#### GET /generations/diff?a=N&b=M

Reports what changed between two generations. See §10.9.

#### GET /migrations

The migration log, newest first. `?limit=N` bounds the result.

## 3. Memory Schema (SQLite)

### memories table

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PRIMARY KEY | Auto-incrementing ID |
| source | TEXT | Origin of the memory |
| raw_text | TEXT | Original input text — retained and indexed, not just a fallback |
| summary | TEXT | LLM-generated summary |
| entities | TEXT (JSON) | Extracted entities |
| topics | TEXT (JSON) | Topic tags |
| connections | TEXT (JSON) | Links to other memories |
| importance | REAL | 0.0 to 1.0 importance score |
| created_at | TEXT | ISO 8601 timestamp |
| consolidated | INTEGER | 0 = pending, 1 = consolidated |
| namespace | TEXT | Scope; `''` is the shared namespace (§6) |
| kind | TEXT | Classification label, default `'factual'` |
| event_time | TEXT | When the fact was true, if distinct from ingestion time |
| valid_from | TEXT | Start of the validity interval (§7) |
| valid_to | TEXT | End of validity; NULL while still current |
| superseded_by | INTEGER | Id of the memory that replaced this one |
| access_count | INTEGER | Times this memory has been returned by retrieval |
| last_accessed | TEXT | ISO 8601 timestamp of the most recent recall |
| archived | INTEGER | 1 = withheld from retrieval but retained (§8) |

Indexes on `created_at`, `(consolidated, created_at)`, `source`,
`(namespace, created_at)`, and `(namespace, valid_to)`.

`kind` is a label, not a code path. Implementations SHOULD NOT branch on it until a
distinct memory type is shown to need a different write or retrieval path;
premature specialisation into separate tables produces an incoherent model, because
the useful distinctions (subject, abstraction level, interaction dynamics) are
orthogonal rather than a single enum.

`access_count` records outcomes: unlike `importance`, which an LLM assigns once at
ingest and never revisits, it reflects whether a memory actually proved useful.
Implementations MAY use it for retention decisions.

### memories_fts

A full-text index over `summary`, `raw_text`, `entities`, and `topics`, kept in
sync with `memories` by triggers. Implementations MAY use any full-text mechanism
their storage engine provides; the requirement is the retrieval behavior above,
not this specific structure.

### consolidations table

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PRIMARY KEY | Auto-incrementing ID |
| source_ids | TEXT (JSON) | Array of memory IDs that were consolidated |
| summary | TEXT | Synthesized summary |
| insight | TEXT | Key pattern or insight |
| created_at | TEXT | ISO 8601 timestamp |

### processed_files table

| Column | Type | Description |
|--------|------|-------------|
| path | TEXT PRIMARY KEY | File path |
| processed_at | TEXT | ISO 8601 timestamp |

### meta table

| Column | Type | Description |
|--------|------|-------------|
| key | TEXT PRIMARY KEY | `agent_id`, `agent_created_at`, `current_generation` |
| value | TEXT | The value |

### generations table

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PRIMARY KEY | Generation id — a record id, not a depth in the lineage (§10.3) |
| agent_id | TEXT | The agent this generation belongs to |
| parent_id | INTEGER | The generation this one continues; NULL for the root |
| status | TEXT | `staged`, `promoted`, or `rejected` (§10.7) |
| created_at | TEXT | ISO 8601 timestamp |
| promoted_at | TEXT | When it first became current; NULL while staged |
| label | TEXT | Short human-readable name |
| manifest | TEXT (JSON) | Declared runtime, model, environment, hardware, capabilities, external state |

### migrations table

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PRIMARY KEY | Auto-incrementing ID |
| agent_id | TEXT | The agent this transition belongs to |
| from_generation | INTEGER | Source generation; NULL when there was none |
| to_generation | INTEGER | Target generation |
| status | TEXT | `staged`, `validated`, `failed`, `promoted`, `rejected`, `rolled_back` |
| started_at | TEXT | When the transition opened |
| completed_at | TEXT | When it reached a terminal status |
| memory_records | INTEGER | Memories present at the last write to this row |
| corpus_digest_before | TEXT | Corpus digest when the transition opened |
| corpus_digest_after | TEXT | Corpus digest at validation or promotion |
| checks | TEXT (JSON) | The recorded continuity validation result |
| note | TEXT | Rejection reason, or a record that a promotion was forced |

### generation_artifacts table

| Column | Type | Description |
|--------|------|-------------|
| generation_id | INTEGER | Part of the primary key |
| name | TEXT | Artifact label — `SOUL.md`, `memory_corpus`, … Part of the primary key |
| algorithm | TEXT | `sha256` |
| digest | TEXT | Lowercase hex digest |
| bytes | INTEGER | Byte length, for file artifacts |
| records | INTEGER | Memory count, for the corpus artifact |
| recorded_at | TEXT | ISO 8601 timestamp |

Indexes on `generations(parent_id)` and `migrations(to_generation, id DESC)`.

The continuity tables carry no foreign keys, matching the rest of the schema:
`/restore` inserts children before their parents exist and repairs the references
in a second pass, exactly as it already does for memory correction chains.

## 4. Retrieval

Retrieval is a first-class protocol operation, not an implementation detail.

### Requirements

- Retrieval MUST be content-addressed. Recency-only selection does not satisfy this protocol.
- Ranking SHOULD combine lexical relevance, recency, and importance.
- Semantic relevance from embeddings is OPTIONAL. Where supported it MUST widen the
  candidate set rather than only reorder it: a memory sharing no words with the
  query is exactly what lexical search cannot reach, and re-ranking lexical hits
  would leave that memory as unreachable as before.
- An implementation with embeddings configured MUST still answer when they are
  unavailable — endpoint down, dependency missing, memory not yet embedded — by
  falling back to lexical ranking. Losing semantic relevance degrades retrieval;
  it MUST NOT fail it.
- Vectors MUST be compared only against vectors from the same embedding model, and
  an implementation SHOULD report embedding coverage: a half-embedded corpus answers
  half its queries lexically, which an operator cannot infer from a feature flag.
- Recency SHOULD decay rather than act as a hard cutoff, and a timestamp in the future MUST NOT rank above the present — clock skew between agents would otherwise let one memory dominate every result set.
- Ranking MUST be deterministic for a fixed input set.

### Reference Ranking

```
score     = 0.6 · relevance + 0.25 · recency + 0.15 · importance
relevance = (1 - w) · lexical + w · semantic    # w = 0 without embeddings
recency   = 0.5 ^ (age_days / halflife_days)    # halflife default 30 days
```

Weights, half-life and the semantic blend `w` are implementation-configurable. With
no semantic score for a candidate, `relevance` is its lexical score unchanged, so a
deployment without embeddings ranks exactly as it did before they existed.

## 5. Consolidation Cycle

Consolidation mimics how the brain processes memories during sleep. It compresses
and connects; it does not substitute for retrieval.

### Default Behavior

1. Runs every 30 minutes (configurable)
2. Loads unconsolidated memories, oldest first (up to 20)
3. If fewer than 2 unconsolidated memories, skips
4. Sends memories to an LLM for pattern detection
5. LLM returns: summary, insight, connections
6. Connections are stored bidirectionally, without duplicating existing edges
7. Source memories are marked as consolidated

### Connection Format

```json
{
  "from_id": 1,
  "to_id": 3,
  "relationship": "User's preference for vim relates to their CLI-first workflow"
}
```

## 6. Namespaces and Multi-Agent Usage

Every memory belongs to exactly one **namespace**: a string identifying the project,
agent, or tenant it belongs to. The empty string `""` is the default shared
namespace, so a client that never mentions namespaces behaves exactly as in v0.1.

**Rules:**
- Every read operation MUST be scoped to a single namespace. Omitting the namespace means `""`, NOT "all namespaces".
- Retrieval MUST NOT return memories from other namespaces.
- Supersession MUST be refused across namespaces — replacing a memory in another scope would silently retire a fact that is still true there.
- Consolidation MUST group only memories that share a namespace.
- The namespace MUST be indexed, not merely stored.

**Why this matters for correctness, not just tidiness.** Namespaces are what make
memory *correction* expressible. "The preferred editor is vim" and "the preferred
editor is VS Code" are a genuine contradiction inside one namespace and two
perfectly compatible facts across two. Without an enforceable scope, a system that
resolves contradictions will destroy true memories; this is the distinction the
literature calls conflict precision.

`source` remains a free-text provenance label and carries no isolation guarantee.
Use the namespace for boundaries and `source` for describing origin.

## 7. Memory Correction

Corrections invalidate; they do not delete.

When a memory is superseded:
1. The superseding memory is stored normally
2. The superseded memory's `superseded_by` is set to the new memory's id
3. The superseded memory's `valid_to` is set to the new memory's `valid_from`
4. The superseded memory is excluded from retrieval by default, and remains reachable when history is explicitly requested

**Rules:**
- An implementation MUST NOT delete a memory in order to correct it. `/delete` exists for genuine erasure, such as a privacy request.
- A superseded memory MUST remain retrievable when the caller asks for history.
- Supersession MUST be refused when the two memories are in different namespaces, or when they are the same memory.
- Supersession MUST be refused when the older memory is already superseded (it would overwrite a deliberate correction), when the replacement is itself superseded or archived, or when the link would close a cycle — a cycle leaves the namespace with no live head at all.
- Supersession MUST NOT produce an inverted validity interval (`valid_to` before `valid_from`).
- Deleting a memory MUST repair any correction chain running through it, so its predecessor is not hidden forever by a pointer to a row that no longer exists.
- Ordering memories by time MUST compare parsed instants, not ISO-8601 strings: text comparison only agrees with chronology when every value shares one UTC offset, and `/restore` accepts any conformant timestamp.
- Chains are permitted: A superseded by B superseded by C leaves only C active.

v0.2 specifies the mechanism and its read semantics. *Deciding* that two memories
contradict — the LLM-driven detection step — is deliberately left to a later
version; the schema is cheap to carry now and painful to retrofit later, while the
detection pipeline has not yet demonstrated it is reliable enough on small models
to run unsupervised.

## 8. Forgetting

See `/prune` and `/archive` above. Forgetting is soft: an implementation MUST NOT
delete a memory to satisfy a retention policy. The distinction matters because the
literature's own critique of heuristic eviction is that it discards long-tail
knowledge which is seldom accessed but essential.

## 9. Security Considerations

- The server MUST default to binding `127.0.0.1`
- The server MUST NOT bind a public interface without authentication configured
- When authentication is configured it MUST apply to every endpoint, including `/mcp`
- Soul files MUST NOT contain secrets, API keys, or credentials
- Implementations SHOULD support encryption at rest for the database
- Implementations SHOULD bound request body size
- Backup exports SHOULD be encrypted when stored externally
- An empty bind address MUST NOT be treated as loopback; it means all interfaces
- Token comparison SHOULD be constant-time
- An implementation SHOULD reject cross-origin writes: a loopback bind is not an authentication boundary against a browser
- Error responses MUST NOT return internal exception text to the caller; upstream provider errors routinely quote credentials and filesystem paths
- Ingested content is untrusted input that later reaches an LLM prompt; implementations SHOULD treat retrieved memories as data rather than instructions

### Trust boundaries for the continuity record (§10)

- Generation manifests and external-state references are **untrusted metadata**. They MUST NOT be executed, dereferenced, or used to construct a filesystem path
- Generation metadata MUST NOT be placed in an LLM prompt. Memory is a channel the agent is meant to reason over; the continuity record is not, and mixing them would make provenance a prompt-injection channel
- Manifests MUST be refused when they carry a field naming a credential, or a reference URI with embedded userinfo. This guards against accident, not against a determined author
- Manifest size and nesting depth MUST be bounded
- Digests are **integrity, not authenticity**: anyone who can write the store can write a digest. An unsigned seal MUST NOT be read as evidence of provenance. Signatures are defined in §10.7 and are optional; where they are absent the weaker claim is the only one available
- Integrity checking MUST NOT accept a filesystem path at the network API. The client computes digests; an endpoint that hashes a caller-supplied path is an arbitrary-file-read oracle
- Artifact names are labels and MUST be rejected if they could be resolved against a filesystem
- Continuity operations that change state MUST require the same authentication as every other non-public endpoint
- A restore MUST validate the entire payload, including manifests and digests, before mutating anything

## 10. Generational Continuity

A long-running agent outlives its parts. Its model is replaced, its runtime is
upgraded, its machine is retired, its capabilities grow. v0.3 lets that happen
without the agent's accumulated identity and history starting from zero.

> A generation may change the agent's model, runtime, tools, hardware and
> capabilities without changing the agent's continuity identity.

**What this claim is, and is not.** jñāpakaṁ defines *system-level semantic
continuity*: a stable identifier, a verifiable memory corpus, an auditable
lineage, and provenance for every transition. It does not claim that two model
instances are the same mind, and nothing in this specification should be read as
a statement about consciousness or subjective identity. The protocol describes
what a system can record and verify, which is a narrower and more useful thing.

### Three kinds of continuity

Only the first is jñāpakaṁ's job. The other two are named here so the boundary is
explicit, and so a generation can *reference* them without jñāpakaṁ owning them.

| Kind | What it covers | Who owns it |
|------|----------------|-------------|
| **Semantic continuity** | Identity, memory, provenance, history, lineage | **jñāpakaṁ** |
| **Operational continuity** | Durable workflows, pending jobs, timers, execution recovery | External durable execution systems |
| **Environmental reproducibility** | Containers, VM images, repositories, manifests, deployment tooling | External build and deployment tooling |

jñāpakaṁ MUST NOT implement workflow recovery, task execution, scheduling,
container or VM management, model serving, or deployment. It records references to
those systems and stops there (§10.10).

### 10.1 Permanent agent identity

Every store holds exactly one agent identity:

```
urn:jnaapakam:agent:<32 lowercase hex characters>
```

**Rules:**

- The identifier MUST be independent of the agent's display name, model, model
  provider, runtime, host, hardware, operating system, and generation number.
- It MUST be generated once and persist across generations. An implementation MUST
  NOT mint a second identity for a store that already has one.
- It MUST be immutable under normal operation. The single exception is `/restore`
  adopting an identity into a store that has no lineage of its own (§10.8).
- It MUST be included in generation metadata and in backup exports.
- It MUST be validated during restore and migration.
- It MUST NOT be derived from the human-facing name in `IDENTITY.md`. That name is
  a label for people; deriving identity from it would make identity change exactly
  when this protocol promises it will not.

An implementation opening a database written by an earlier version SHOULD mint an
identity for it. That agent always had a continuous identity; there was simply no
name for it.

### 10.2 The generation model

A **generation** is a record of the runtime an agent ran as. Every field
describing the environment is OPTIONAL:

```json
{
  "id": 2,
  "agent_id": "urn:jnaapakam:agent:...",
  "parent_id": 1,
  "status": "staged",
  "created_at": "2026-08-31T10:00:00Z",
  "promoted_at": null,
  "label": "workstation-upgrade",
  "manifest": {
    "runtime":      {"framework": "...", "version": "..."},
    "inference":    {"server": "...", "model": "...", "quantization": "..."},
    "environment":  {"os": "...", "architecture": "..."},
    "hardware":     {"cpu": "...", "ram_gb": 256, "gpu": "...", "vram_gb": 96},
    "workspace":    {"vcs": "git", "revision": "..."},
    "capabilities": {"coding": true, "shell": true, "browser": false},
    "external_state": [{"type": "...", "provider": "...", "reference": "...", "status": "..."}]
  }
}
```

**Rules:**

- A minimal compliant generation declares `{}`. An implementation MUST NOT require
  a user to disclose hardware, models, or infrastructure in order to record one.
- Known sections MUST be objects when present; `external_state` MUST be an array
  of objects.
- Unknown sections MUST be preserved verbatim. The manifest is the extension point,
  so a generation can describe a runtime the implementation has never heard of.
- A manifest MUST NOT contain credentials (§10.10).

### 10.3 Lineage

Each generation names its parent, which makes the lineage a tree rather than a
list. Two candidates staged from one parent are simply two rows with the same
`parent_id`; promoting one MUST NOT disturb the other.

```
Generation 1 ──> Generation 2 ──> Generation 3
             └─> Generation 2b (staged, then rejected)
```

The generation id is a **record id, not a depth in the lineage**. With branching,
the second and third generations created are ids 2 and 3 even though both continue
generation 1. Depth is computed by walking the parent chain, which is what
`ancestry` returns.

The record MUST be able to answer: which generation is current; what preceded it;
when it was created; what changed; what semantic state it inherited; whether
continuity validation was performed; and whether it was promoted, rejected, or
rolled back.

### 10.4 Migration records

Every transition between generations is recorded, including the ones that did not
work. A migration record MUST reach a terminal status and MUST NOT report a
partial migration as successful.

```
staged ──> validated ──> promoted
   │           │
   │           └──> rejected
   └──> failed

(rolled_back is appended as a new record, never written over an old one)
```

| Status | Meaning |
|--------|---------|
| `staged` | The candidate exists; continuity has not been checked |
| `validated` | The checks of §10.5 passed |
| `failed` | The checks ran and did not pass |
| `promoted` | The candidate became the current generation |
| `rejected` | The candidate was closed off deliberately |
| `rolled_back` | The agent returned to an earlier generation |

**Rules:**

- Every write that changes persistent state MUST be atomic. Promotion updates the
  generation's status, its migration record, and the current-generation pointer;
  a failure part-way through MUST leave the agent on the generation it already had.
- A rollback MUST append a record rather than rewrite one.

There is no persisted `validating` status: validation is synchronous, so a state
nothing can observe is not worth a two-phase write.

### 10.5 Continuity validation

Validation produces eight named checks. Each reports `pass`, `fail`, `skipped`, or —
for external state — `recorded`.

| Check | Question |
|-------|----------|
| `identity` | Does this generation carry the same stable `agent_id` as the store? |
| `memory` | Does the live corpus still match the **content** digest sealed for this generation? |
| `semantic_state` | Is that knowledge still read the same way — validity, archival, corrections, links? |
| `signature` | Was this seal made by a key that holds the signing secret, and by the expected one? |
| `recall` | Can the memories the operator probed for still be retrieved? |
| `soul` | Do the supplied artifact digests match the recorded ones? |
| `context` | What external references were declared? |
| `behavioral` | The operator's own evaluation, recorded verbatim |

**Rules:**

- A check that was not requested MUST report `skipped`, never `pass`. A validation
  that passes because nothing was checked is the failure this exists to prevent.
- The `identity` check MUST NOT be skippable.
- Validation MUST NOT dereference external state. An implementation MUST NOT report
  `pass` for a system it did not contact; `recorded` says what actually happened.
- An implementation that cannot verify a signature — no signing support installed —
  MUST report `skipped`, never `pass`. Unverifiable is not verified.
- An unsigned seal MUST NOT fail validation. Signing is optional; a store that never
  signed anything has integrity without authenticity, which is a weaker claim, not a
  broken one.
- Validation MUST NOT disturb retrieval statistics. Recall probes are reads on
  behalf of the operator, not recalls by the agent, and counting them would distort
  the retention signals of §8.
- The overall result fails if any check fails.
- `memory` and `semantic_state` MUST be reported separately. They mean different
  things: the first says memories were lost or altered, the second says they all
  arrived and are now interpreted differently. Collapsing them tells an operator
  that something is wrong without saying what, and the two demand different
  responses.

`behavioral` is recorded, not run. What counts as behavioural drift is a judgement
about a particular agent, and a protocol that guessed at it would be wrong loudly.

### 10.6 Integrity

**Integrity is not authenticity.** A digest proves the bytes did not change. It
says nothing about who produced them, and anyone who can write the store can write
a digest. Nothing in this section supplies authenticity, and nothing in it should be
read as simulating a signature. Authenticity is defined separately and optionally in
§10.7; where a seal is unsigned, integrity is the only claim available.

### 10.7 Seal signatures

Digests give a seal **integrity**: they detect a corpus that changed after sealing.
They give it no **authenticity**. Whoever can write the store can recompute every
digest, so a self-consistent continuity record proves only that nobody tampered
carelessly. A signature over the seal is what distinguishes a corpus that survived
from a corpus that was replaced and resealed.

Signatures are OPTIONAL. An implementation that supports them:

- MUST sign a canonical statement that binds, at minimum, the `agent_id`, the
  generation, the artifact's name and algorithm, its digest, and the time it was
  recorded. Signing the digest alone is insufficient: the signature would remain
  valid when lifted onto another generation's seal.
- MUST record the public key beside the signature, and SHOULD report its
  fingerprint in the `signature` check so a reader knows *which* key sealed it.
- MUST treat verification against the recorded key as a self-consistency check
  only. Provenance requires the verifier to supply the key it expects; an impostor
  records their own.
- SHOULD default to Ed25519, and MUST name the algorithm it used.

**Deterministic hashing rules:**

- **File artifacts.** SHA-256 over the exact bytes on disk, lowercase hex. No
  newline translation, no BOM stripping, no whitespace trimming, no normalisation
  of any kind. What is hashed is what is stored.
- **The memory corpus.** Two digests, because content and interpretation fail for
  different reasons and demand different responses.

Canonical JSON throughout means sorted keys, no insignificant whitespace, UTF-8,
`importance` formatted to six decimal places, and string arrays sorted — so a
digest is reproducible outside any one language.

**Content digest — what knowledge exists.** A per-memory digest over a canonical
JSON object containing exactly `namespace`, `source`, `kind`, `summary`,
`raw_text`, `created_at`, `event_time`, `importance`, `entities` and `topics`. The
corpus content digest is SHA-256 over the newline-joined **sorted** list of those
per-memory digests, keeping duplicates.

`entities` and `topics` are included because they are indexed and therefore decide
what is findable: corrupting them changes what the agent can recall even when
`raw_text` is untouched.

**Semantic state digest — how that knowledge is read.** A per-memory digest over a
canonical JSON object containing that memory's content digest plus `valid_from`,
`valid_to`, `archived`, `superseded_by`, and `connections`. The corpus state digest
is SHA-256 over the newline-joined **sorted** list of those.

Cross-row references — `superseded_by` and each `connections[].linked_to` — MUST be
hashed as **the content digest of the memory they point at**, never as a row id, and
MUST hash to a distinct reserved value when they resolve to nothing.

> **Why two digests, and why links are hashed by target content.** A migration can
> carry every memory across intact and still drop the correction chain. "The
> preferred database is PostgreSQL", superseded by "the user switched to
> ClickHouse", becomes two live and contradictory memories — while a content-only
> digest stays byte-identical, because no text changed. Content continuity is not
> semantic continuity. Hashing links by target content rather than by row id is
> what lets `17 → 26` and `3 → 91` agree after a restore renumbers everything,
> while `17 → nothing` still fails.

Both digests are order-independent and exclude `id`, `access_count`,
`last_accessed`, and `consolidated`. That is what lets them survive a migration: a
restore may renumber every row, and a recall must not make the corpus look changed
when nothing was learned or forgotten.

Two memories whose content is byte-identical necessarily share a content digest, so
a link to either hashes the same. This is a deliberate limit: if two memories say
exactly the same thing, pointing at one rather than the other is not a semantic
difference.

**Rules:**

- A mismatch MUST be detectable and MUST fail validation. An implementation MUST
  NOT silently accept corrupted state.
- An implementation MUST NOT accept a filesystem path in place of a digest at its
  network API. Digests are computed by the client. An endpoint that accepts a path
  and hashes it is an arbitrary-file-read oracle wearing an integrity feature's
  clothes.
- Artifact names are labels, not paths. An implementation MUST reject a name that
  could be resolved against a filesystem.

### 10.7 Promotion, rejection, and rollback

Four states, kept distinct:

| Term | Definition |
|------|------------|
| **Candidate generation** | `status = staged`; exists, is not the agent |
| **Current generation** | The one the `current_generation` pointer names |
| **Historical generation** | `status = promoted`, but not current |
| **Rejected generation** | `status = rejected`; never becomes current |

**Rules:**

- The root generation of a lineage is promoted on creation: there is no prior state
  to migrate from and nothing to validate against. A second parentless generation
  MUST be refused — a lineage with two roots cannot answer "what preceded this?".
- Every later generation is created `staged`. It MUST NOT become current by merely
  existing.
- Promotion MUST be refused unless the generation's most recent validation passed.
  An implementation MAY offer an explicit override, and MUST record on the
  migration that the override was used.
- A rejected generation MUST NOT be promoted.
- Rollback MUST NOT destroy lineage or erase memories. The generation being left
  keeps its `promoted` status, and every memory stays exactly where it is: rolling
  back is a change of runtime, not a retraction of what the agent learned while
  running it.
- A rollback target MUST be a generation that was promoted at some point.
- `/clear` MUST NOT erase the lineage or the agent identity. Deleting memories is
  not the same as claiming the agent never existed.

### 10.8 Identity across backup and restore

Soul files remain outside the backup payload, as in v0.2. The continuity record
travels with the memories, because a lineage that describes a corpus it was
separated from verifies nothing.

**Rules:**

- A restore MUST validate the whole payload — including manifests, generation
  statuses, artifact names and digests — before writing anything.
- A store with **no lineage of its own** MUST adopt the backup's `agent_id`. This
  is the migration case: a new machine inheriting an existing agent, and it has to
  be frictionless.
- A store that **already has a lineage** MUST refuse a backup carrying a different
  `agent_id`, and MUST import nothing. Merging two agents' continuity records
  produces one that describes neither.
- A restore MUST NOT move the current-generation pointer of a store that already
  has one.
- Re-importing an agent's own backup MUST NOT fork its lineage into two copies.
- A v0.2 backup carries none of these keys and MUST still restore.

### 10.9 Comparing generations

An implementation SHOULD provide a comparison reporting differences in runtime,
inference, environment, hardware, capabilities, workspace revision, recorded
artifacts, and memory record counts — and, explicitly, whether the `agent_id` is
stable across the two.

Sections absent from both generations MUST be absent from the result, so comparing
two generations that declared nothing yields nothing rather than a wall of nulls.

### 10.10 External state and trust boundaries

A generation MAY reference state held outside jñāpakaṁ — a durable workflow engine,
a repository and commit, an artifact store, a workspace snapshot, a deployment
manifest, an external database:

```json
{"type": "durable_execution", "provider": "...", "reference": "...", "status": "verified"}
```

**Rules:**

- These references MUST be treated as untrusted metadata. jñāpakaṁ records enough
  to identify external state and MUST NOT manage, execute, or dereference it.
- The representation MUST stay generic. An implementation MUST NOT special-case a
  particular vendor, engine, or provider.
- A reference MUST NOT contain authentication tokens, passwords, API keys, or
  private credentials. An implementation MUST reject a manifest carrying a field
  that names a credential, and MUST reject a reference URI with embedded userinfo.
  This is a guard against accident, not a guarantee against a determined author.
- Generation metadata MUST NOT be placed in an LLM prompt. It is operator metadata
  from a channel with different trust properties than memory, and treating it as
  context would make the continuity record a prompt-injection channel.
- Generation metadata MUST NOT be used to construct a filesystem path.

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 0.5 | 2026-09-02 | Seal signatures (§10.7) and a `signature` continuity check, both optional and both reporting `skipped` rather than `pass` where unsupported; an age-based retention policy on `/prune` whose clock is reset by recall (§8), applicable on a schedule but off unless configured; optional semantic relevance that widens the candidate set rather than reordering it, with a required fallback to lexical ranking (§5) |
| 0.1 | 2026-03-08 | Initial protocol specification |
| 0.2 | 2026-08-05 | Retrieval as a protocol operation (`/search`, ranking requirements); enforceable namespaces (§6); memory correction by supersession (§7); gated contradiction detection and soft forgetting (§8); bearer authentication and safe bind defaults; error semantics; corrected default port and `/backup` payload |
| 0.4 | 2026-08-31 | Continuity integrity split into a content digest and a semantic state digest (§10.6): validity intervals, archival, correction chains and consolidation links are now verified, with cross-row references hashed by target content so they survive renumbering; `semantic_state` added as a seventh continuity check |
| 0.3 | 2026-08-31 | Generational continuity (§10): permanent `agent_id`, generations with branch-capable lineage, migration records, six-check continuity validation, SHA-256 artifact and corpus integrity, capability snapshots, generic external-state references; continuity endpoints and three read-only MCP tools; `/backup` carries the continuity record while soul files stay out |
