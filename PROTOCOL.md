# jñāpakaṁ Protocol Specification v0.2

## Overview

The jñāpakaṁ protocol defines a standard for AI agent memory persistence. It consists of two parts:

1. **Soul Schema** — Static identity files that define who an agent is
2. **Memory API** — HTTP endpoints for dynamic memory operations

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
- **Base Path:** `/` (no versioned prefix in v0.2)

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
  "version": "0.2"
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

Archive the lowest-retention memories in a namespace.

**Parameters:** `keep` (required), `namespace` (optional)

**Response:** `{"status": "pruned", "archived": N, "kept": N}`

#### POST /archive

Archive or restore a single memory.

**Request:** `{"memory_id": N, "restore": false}`

**Rules:**
- Forgetting MUST be reversible. An implementation MUST NOT delete memories to satisfy a retention policy; `/delete` remains the only destructive operation.
- Archived memories MUST be excluded from retrieval by default and reachable when explicitly requested.
- Retention scoring SHOULD combine importance, access frequency, and temporal decay. Frequency is the outcome-grounded term; `importance` is assigned once and never revised.
- Superseded memories SHOULD be evicted before live ones.

#### POST /clear

Delete all memories, consolidations, and processed file records. Destructive; MUST be protected by authentication when configured.

#### GET /backup

Export memories and consolidations as JSON.

```json
{
  "version": "0.2",
  "exported_at": "2026-03-08T12:00:00Z",
  "memories": [ ... ],
  "consolidations": [ ... ]
}
```

Soul files are plain files on disk and are versioned by the user; they are
deliberately not part of the backup payload.

#### POST /restore

Import from a backup. Same format as the `/backup` response.

The payload MUST be validated before any row is written; a malformed backup MUST
return `400` rather than partially importing.

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

## 4. Retrieval

Retrieval is a first-class protocol operation, not an implementation detail.

### Requirements

- Retrieval MUST be content-addressed. Recency-only selection does not satisfy this protocol.
- Ranking SHOULD combine lexical relevance, recency, and importance.
- Recency SHOULD decay rather than act as a hard cutoff, and a timestamp in the future MUST NOT rank above the present — clock skew between agents would otherwise let one memory dominate every result set.
- Ranking MUST be deterministic for a fixed input set.

### Reference Ranking

```
score = 0.6 · relevance + 0.25 · recency + 0.15 · importance
recency = 0.5 ^ (age_days / halflife_days)      # halflife default 30 days
```

Weights and half-life are implementation-configurable.

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
- Ingested content is untrusted input that later reaches an LLM prompt; implementations SHOULD treat retrieved memories as data rather than instructions

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 0.1 | 2026-03-08 | Initial protocol specification |
| 0.2 | 2026-08-05 | Retrieval as a protocol operation (`/search`, ranking requirements); enforceable namespaces (§6); memory correction by supersession (§7); gated contradiction detection and soft forgetting (§8); bearer authentication and safe bind defaults; error semantics; corrected default port and `/backup` payload |
