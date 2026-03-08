# jñāpakaṁ Protocol Specification v0.1

## Overview

The jñāpakaṁ protocol defines a standard for AI agent memory persistence. It consists of two parts:

1. **Soul Schema** — Static identity files that define who an agent is
2. **Memory API** — HTTP endpoints for dynamic memory operations

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

## <Category>
- <Memory item>
```

**Rules:**
- SHOULD be organized by topic/category
- SHOULD be periodically reviewed and pruned
- MAY be auto-updated from the Memory API consolidations
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
- **Base Path:** `/` (no versioned prefix in v0.1)

### Endpoints

#### GET /status

Returns memory statistics.

**Response:**
```json
{
  "total_memories": 42,
  "unconsolidated": 5,
  "consolidations": 8,
  "version": "0.1"
}
```

#### POST /ingest

Store new information in memory.

**Request:**
```json
{
  "text": "string (required) — The information to remember",
  "source": "string (optional) — Where this came from"
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
3. The structured memory is stored in the database
4. The memory is marked as "unconsolidated"

#### GET /query?q={question}

Query memories using natural language.

**Parameters:**
- `q` (required) — The question to answer

**Response:**
```json
{
  "question": "what tools does the user prefer?",
  "answer": "Based on stored memories, the user prefers... [Memory #1] [Memory #3]"
}
```

**Behavior:**
1. All memories and consolidation insights are loaded
2. The LLM synthesizes an answer based ONLY on stored memories
3. Memory IDs are cited in the response

#### GET /memories

List stored memories.

**Parameters:**
- `limit` (optional, default: 50) — Maximum memories to return

**Response:**
```json
{
  "memories": [
    {
      "id": 1,
      "source": "conversation",
      "summary": "User prefers dark mode",
      "entities": ["user"],
      "topics": ["preferences", "UI"],
      "importance": 0.6,
      "connections": [],
      "created_at": "2026-03-08T01:50:00Z",
      "consolidated": true
    }
  ],
  "count": 1
}
```

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
1. Load all unconsolidated memories
2. If fewer than 2, skip with `{"status": "skipped"}`
3. LLM finds connections and patterns
4. Generates a summary and key insight
5. Maps connections between memory IDs
6. Marks source memories as consolidated

#### POST /delete

Delete a specific memory.

**Request:**
```json
{
  "memory_id": 1
}
```

**Response:**
```json
{
  "status": "deleted",
  "memory_id": 1
}
```

#### POST /clear

Delete all memories, consolidations, and processed file records.

**Response:**
```json
{
  "status": "cleared",
  "memories_deleted": 42
}
```

#### GET /backup

Export all data.

**Response:**
```json
{
  "version": "0.1",
  "exported_at": "2026-03-08T12:00:00Z",
  "memories": [...],
  "consolidations": [...],
  "soul_files": {
    "SOUL.md": "...",
    "IDENTITY.md": "...",
    "MEMORY.md": "..."
  }
}
```

#### POST /restore

Import from a backup.

**Request:** Same format as `/backup` response.

**Response:**
```json
{
  "status": "restored",
  "memories_imported": 42,
  "consolidations_imported": 8
}
```

## 3. Memory Schema (SQLite)

### memories table

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PRIMARY KEY | Auto-incrementing ID |
| source | TEXT | Origin of the memory |
| raw_text | TEXT | Original input text |
| summary | TEXT | LLM-generated summary |
| entities | TEXT (JSON) | Extracted entities |
| topics | TEXT (JSON) | Topic tags |
| connections | TEXT (JSON) | Links to other memories |
| importance | REAL | 0.0 to 1.0 importance score |
| created_at | TEXT | ISO 8601 timestamp |
| consolidated | INTEGER | 0 = pending, 1 = consolidated |

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

## 4. Consolidation Cycle

Consolidation is the core differentiator of jñāpakaṁ. It mimics how the human brain processes memories during sleep.

### Default Behavior

1. Runs every 30 minutes (configurable)
2. Loads unconsolidated memories (up to 20)
3. If fewer than 2 unconsolidated memories, skips
4. Sends memories to LLM for pattern detection
5. LLM returns: summary, insight, connections
6. Connections are bidirectionally stored on memory records
7. Source memories are marked as consolidated

### Connection Format

```json
{
  "from_id": 1,
  "to_id": 3,
  "relationship": "User's preference for vim relates to their CLI-first workflow"
}
```

## 5. Multi-Agent Usage

Multiple agents MAY share a single jñāpakaṁ server. The protocol does not enforce namespacing in v0.1 — all memories are in a shared pool.

**Recommended pattern:**
- Use the `source` field to identify which agent ingested a memory
- Example: `"source": "agent:coder:conversation:2026-03-08"`

Future versions will add optional namespace support.

## 6. Security Considerations

- The memory server SHOULD only bind to `localhost` unless explicitly configured otherwise
- Soul files MUST NOT contain secrets, API keys, or credentials
- Implementations SHOULD support encryption at rest for the SQLite database
- The `/clear` endpoint SHOULD require confirmation in production deployments
- Backup exports SHOULD be encrypted when stored externally

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 0.1 | 2026-03-08 | Initial protocol specification |
