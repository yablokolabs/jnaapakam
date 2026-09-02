# jñāpakaṁ

[![PyPI](https://img.shields.io/pypi/v/jnaapakam)](https://pypi.org/project/jnaapakam/)
[![Python versions](https://img.shields.io/pypi/pyversions/jnaapakam)](https://pypi.org/project/jnaapakam/)
[![CI](https://github.com/yablokolabs/jnaapakam/actions/workflows/ci.yml/badge.svg)](https://github.com/yablokolabs/jnaapakam/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/pypi/l/jnaapakam)](LICENSE)
[![MCPize](https://mcpize.com/badge/@yablokolabs/jnaapakam)](https://mcpize.com/mcp/jnaapakam)

**An open protocol for AI agent memory persistence.**

*Your AI has a soul now. Don't lose it.*

---

jñāpakaṁ (Sanskrit: *memory, reminder*) is an open standard for persisting AI agent identity, personality, and memory across sessions, restarts, and platforms. It provides:

- 📋 **Soul Schema** — A standard format for defining who your agent *is*
- 🧠 **Memory Protocol** — Ingest, retrieve, consolidate, and query agent memories over HTTP or MCP
- 🔍 **Real retrieval** — Full-text search with BM25 ranking on stock SQLite, plus optional semantic search that finds the memory sharing no words with your query
- 🔄 **Backup & Restore** — Never lose your agent's accumulated knowledge
- 🧬 **Generational Continuity** — Change the model, runtime, machine and GPU without changing who the agent is
- 🔏 **Signed continuity** — Optional Ed25519 seals, so a continuity record proves *who* sealed it and not merely that nobody tampered carelessly
- 🔌 **Framework Agnostic** — Works with any agent framework, any LLM provider

## The Problem

AI agents today have amnesia. Every session starts fresh. The personality you spent weeks refining, the preferences it learned, the context it accumulated — gone on restart.

Some frameworks bolt on vector databases or conversation logs, but there's no standard way to:

- Define an agent's identity and personality portably
- Persist structured memories across sessions
- Migrate an agent's "soul" between platforms
- Back up and restore agent knowledge
- Carry an agent's identity and history forward when its model, runtime or hardware is replaced

**jñāpakaṁ fixes this.**

## Quick Start

### 1. Install

```bash
pip install jnaapakam                 # one dependency: aiohttp
pip install "jnaapakam[signing]"      # + cryptography, to sign continuity seals
pip install "jnaapakam[embeddings]"   # + numpy, for semantic retrieval
```

Or from source:

```bash
git clone https://github.com/yablokolabs/jnaapakam.git
cd jnaapakam
pip install .
```

The only runtime dependency is `aiohttp`. Full-text search uses SQLite's built-in FTS5, so there is no extension to compile, no daemon to run, and no network call on first start.

### 2. Define Your Agent's Soul

```bash
jnaapakam init
```

This creates:

```
your-agent/
├── SOUL.md       # Personality, tone, boundaries
├── IDENTITY.md   # Name, emoji, description
└── MEMORY.md     # Long-term curated memory
```

Edit them to match your agent's personality. See [schema/](schema/) for the templates.

### 3. Start the Memory Server

```bash
export ANTHROPIC_API_KEY=sk-ant-...     # or OPENAI_API_KEY, or LLM_BASE_URL
jnaapakam serve
```

Binds `127.0.0.1:8889` by default. Your agent now has persistent memory:

```bash
# Store a memory
curl -X POST http://localhost:8889/ingest \
  -H 'Content-Type: application/json' \
  -d '{"text": "User prefers dark mode and vim keybindings", "source": "conversation"}'

# Search memories — returns ranked records with provenance
curl "http://localhost:8889/search?q=editor+preferences"

# Ask a question — returns a synthesized answer with citations
curl "http://localhost:8889/query?q=what+are+the+user+preferences"

# Check status
curl http://localhost:8889/status
```

### 4. Connect Your Agent

```python
import requests

MEMORY = "http://localhost:8889"

# Retrieve relevant context on startup
hits = requests.get(f"{MEMORY}/search", params={"q": "recent context and active tasks"}).json()
context = "\n".join(f"- {m['summary']}" for m in hits["memories"])

# Ingest important takeaways during the conversation
requests.post(f"{MEMORY}/ingest", json={
    "text": "User decided to switch from React to Svelte for the new project",
    "source": "conversation:2026-03-08",
})
```

## Connect via MCPize

Use this MCP server with no local installation:

```bash
npx -y mcpize connect @yablokolabs/jnaapakam --client claude
```

Or connect at: **https://mcpize.com/mcp/jnaapakam**

Deploying this repo yourself? `mcpize deploy` requires the publisher secret
`MEMORY_AUTH_TOKEN` (generate with `openssl rand -hex 32`) — the server refuses
to bind a public interface without it. The server listens on `0.0.0.0:$PORT` and
exposes a public `GET /health` for platform startup probes.

## Security

> [!IMPORTANT]
> `/clear` and `/restore` are destructive. Since v0.2 the server **refuses to start** if it is told to bind a non-loopback address without an auth token.

```bash
# Local development — loopback, no token needed
jnaapakam serve

# Exposed deployment — a token is mandatory
export MEMORY_AUTH_TOKEN=$(openssl rand -hex 32)
jnaapakam serve --host 0.0.0.0
```

Authenticated requests send the token as a bearer credential:

```bash
curl -H "Authorization: Bearer $MEMORY_AUTH_TOKEN" http://your-host:8889/status
```

The MCP JSON-RPC endpoints (`POST /mcp` and `POST /`) are exempt from the bearer
check: the MCPize in-container bridge cannot attach credentials, and the MCP
surface (search/ingest/query/list/status/consolidate) contains no destructive
operations. Every REST endpoint — including `/clear`, `/restore`, `/delete`, and
`/backup` — still requires the token. On MCPize, Cloud Run's ingress guard also
blocks anonymous access to the container URL.

Soul files and memories should never contain secrets or credentials.

Two further protections on the default loopback deployment:

- **Cross-origin writes are refused.** Loopback is not an authentication boundary
  against a browser — any page you visit can POST to `127.0.0.1`. Requests carrying a
  foreign `Origin` get a `403`. Non-browser clients send no `Origin` and are unaffected.
- **An empty `MEMORY_HOST` falls back to `127.0.0.1`.** `MEMORY_HOST=` in a compose
  file means "all interfaces" to the OS, so it is treated as a public bind and
  requires a token.

> [!WARNING]
> Memory text reaches LLM prompts. Anything that can write a memory — `/ingest`, a
> watched folder, `/restore` — can attempt prompt injection. The contradiction judge
> fences memory text in per-call random delimiters and is told to treat it as data,
> but treat ingestion from untrusted sources as a trust decision.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Your AI Agent                     │
│  ┌──────────┐  ┌───────────┐  ┌───────────────────┐ │
│  │ SOUL.md  │  │IDENTITY.md│  │    MEMORY.md      │ │
│  │personality│ │name, emoji│  │curated long-term  │ │
│  └──────────┘  └───────────┘  └───────────────────┘ │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP  /  MCP
              ┌────────▼─────────┐
              │  jñāpakaṁ Server │
              │                  │
              │  ┌────────────┐  │
              │  │  Ingest    │  │  ← New information arrives
              │  │  (LLM)     │  │  ← Extract entities, topics, importance
              │  └─────┬──────┘  │
              │        ▼         │
              │  ┌────────────┐  │
              │  │  SQLite    │  │  ← Structured store
              │  │  + FTS5    │  │  ← Full-text index, BM25 ranked
              │  └─────┬──────┘  │
              │        ▼         │
              │  ┌────────────┐  │
              │  │  Retrieve  │  │  ← relevance × recency × importance
              │  │  (ranking) │  │  ← no LLM call, no network
              │  └─────┬──────┘  │
              │        ▼         │
              │  ┌────────────┐  │
              │  │Consolidate │  │  ← Periodic: find patterns, connections
              │  │  (LLM)     │  │
              │  └────────────┘  │
              └──────────────────┘
```

### How It Works

1. **Ingest** — Feed text to the server. An LLM extracts a summary, entities, topics, and an importance score. The original text is kept alongside the summary, and both are indexed.

2. **Retrieve** — Every query runs against the full-text index, not just the newest rows. Candidates are ranked by a weighted combination of lexical relevance (BM25), recency (exponential decay), and importance.

3. **Consolidate** — Periodically the server reviews unconsolidated memories, finds cross-cutting patterns, and records insights and connections between them.

4. **Query** — `/search` returns the ranked records themselves, with their sources and scores. `/query` additionally asks an LLM to synthesize an answer from them, with citations.

### On vector databases

An earlier version of this README argued that active LLM consolidation was a *replacement* for retrieval. That was wrong, and the ordering it implied cost the project its most important feature: before v0.2, `/query` read the fifty most recent memories and ignored everything older, so a memory could become permanently unreachable purely by age.

The current position is narrower and, we think, defensible:

- **Retrieval is not optional.** A memory system that cannot find an old memory by its content is not a memory system.
- **Consolidation is compression, not retrieval.** It earns its keep by summarizing and connecting memories, not by substituting for search.
- **Lexical search goes further than expected.** FTS5 with BM25 ranking is built into stock SQLite. It needs no embeddings, no extension, and no network, and it covers the scale most agents actually operate at.
- **Embeddings are a future increment, not a foundation.** When they land they will be a runtime-detected capability, so the zero-dependency path keeps working when they are unavailable.

## Soul Schema

Three standard files that any agent framework can read.

### SOUL.md — Personality & Boundaries

```markdown
# SOUL.md

## Core Personality
- Tone: Casual, direct, no filler
- Style: Concise when simple, thorough when complex

## Boundaries
- Never share private user data
- Ask before taking external actions
- Be honest about uncertainty
```

### IDENTITY.md — Who Am I?

```markdown
# IDENTITY.md

- **Name:** Atlas
- **Emoji:** 🗺️
- **Description:** Helpful navigator, slightly nerdy
- **Created:** 2026-03-01
```

### MEMORY.md — Curated Long-Term Memory

```markdown
# MEMORY.md

## User Preferences
- Prefers dark mode
- Uses vim keybindings
- Timezone: UTC+5:30

## Project Context
- Currently building a Rust CLI tool
- Deadline: March 15
```

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Memory statistics (counts) |
| `/search?q=...` | GET | **Ranked memory records** with sources and scores (`&limit=N&namespace=...`) |
| `/query?q=...` | GET | LLM-synthesized answer with citations, plus the memories used |
| `/memories` | GET | List recent memories (`?limit=N`) |
| `/ingest` | POST | Ingest new text `{"text": "...", "source": "..."}` |
| `/consolidate` | POST | Trigger manual consolidation |
| `/delete` | POST | Delete a memory `{"memory_id": N}` |
| `/clear` | POST | Delete all memories (`?namespace=...` to scope the reset) |
| `/supersede` | POST | Correct a memory `{"old_id": N, "new_id": M}` — invalidates, never deletes |
| `/reconcile` | POST | Detect and resolve contradictions (requires a judge model) |
| `/prune` | POST | Apply a retention policy: `?keep=N` (count cap), `?older_than_days=N` (age), or both |
| `/archive` | POST | Archive or restore one memory `{"memory_id": N, "restore": bool}` |
| `/prune` requires one policy | | omitting both `keep` and `older_than_days` is a `400`, not a silent default |
| `/namespaces` | GET | List namespaces with memory counts |
| `/dashboard` | GET | Read-only operator view in a browser. Local binds only |
| `/embed` | POST | Backfill embeddings for memories stored before the model was configured |
| `/backup` | GET | Export memories and consolidations as JSON |
| `/restore` | POST | Import from backup JSON |
| `/mcp` | POST | MCP JSON-RPC endpoint (also served on `/`) |
| `/agent` | GET | This agent's permanent identity and current generation |
| `/generations` | GET | List generations; `?id=N` adds ancestry and artifacts |
| `/generations` | POST | Record a candidate generation `{"parent": N, "label": "...", "manifest": {...}}` |
| `/generations/artifacts` | POST | Record digests `{"generation": N, "artifacts": [...], "seal_corpus": true}` |
| `/generations/validate` | POST | Run the continuity checks and record the outcome; `public_key` demands provenance |
| `/generations/promote` | POST | Make a generation current `{"generation": N, "force": false}` |
| `/generations/reject` | POST | Close a candidate off `{"generation": N, "reason": "..."}` |
| `/generations/rollback` | POST | Return to an earlier generation `{"generation": N}` |
| `/generations/diff` | GET | Compare two generations (`?a=1&b=2`) |
| `/migrations` | GET | The migration log, newest first |

Prefer `/search` over `/query` when your agent should reason over the memories itself — it keeps provenance intact and skips an LLM call.

### MCP tools

`search_memory`, `ingest_memory`, `query_memory`, `list_memories`, `get_memory_status`,
`consolidate_memories`, `get_agent_identity`, `list_generations`, `diff_generations`.

Every tool takes an optional `namespace` argument. The surface is deliberately
small: tool definitions consume context on every request, so memory types arrive as
parameters rather than as new tools.

The three generational tools are **read-only**. Creating, promoting and rolling back
a generation decide which runtime *is* the agent, and that is an operator decision —
the same reason `/clear` and `/restore` are not MCP tools. A model can inspect its
own lineage; it cannot rewrite it.

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_AUTH_TOKEN` | *(unset)* | Bearer token. **Required** to bind a non-loopback address |
| `MEMORY_HOST` | `127.0.0.1` | Bind address |
| `MEMORY_PORT` / `PORT` | `8889` | HTTP port |
| `MEMORY_MODEL` | `default` | LLM model alias or full name |
| `MEMORY_JUDGE_MODEL` | *(unset)* | Model for contradiction judging. Unset disables `/reconcile` |
| `MEMORY_DB` | *(package dir)* `memory.db` | SQLite database path |
| `MEMORY_WATCH` | *(unset)* | Folder to auto-ingest text files from |
| `CONSOLIDATE_INTERVAL` | `30` | Minutes between consolidation cycles |
| `MEMORY_EXPIRE_AFTER_DAYS` | *(unset)* | Archive memories untouched for this many days. Unset means nothing expires |
| `MEMORY_SIGNING_KEY` | *(unset)* | Path to an Ed25519 private key. Unset means seals carry integrity but not authenticity |
| `MEMORY_EMBEDDING_MODEL` | *(unset)* | Embedding model for semantic retrieval. Unset means lexical search only |
| `MEMORY_SEMANTIC_WEIGHT` | `0.5` | How much of relevance comes from meaning rather than matching words |

### CLI

```bash
jnaapakam serve [options]

  --host HOST              Bind address (default: 127.0.0.1)
  --port PORT              HTTP port (default: 8889)
  --db PATH                SQLite database path
  --model MODEL            LLM model alias or full name
  --watch DIR              Folder to watch for file ingestion
  --consolidate-every MIN  Consolidation interval (default: 30)
  --expire-after DAYS      Archive memories untouched for this long (default: off)

jnaapakam init [directory]   Create SOUL.md / IDENTITY.md / MEMORY.md

jnaapakam agent [--db PATH]  Show this agent's permanent identity

jnaapakam generation list                       List every generation
jnaapakam generation show ID                    Show one generation in full
jnaapakam generation create [--parent N] [--label L] [--manifest FILE]
jnaapakam generation seal ID [--soul-dir DIR]   Hash the soul files and memory corpus
jnaapakam generation validate ID [--soul-dir DIR]
jnaapakam generation promote ID [--force]       Make a generation current
jnaapakam generation reject ID [--reason R]
jnaapakam generation rollback ID
jnaapakam generation diff A B                   Compare two generations
```

Every `generation` command takes `--db PATH` and opens the database directly, so
continuity works offline with no server, no token, and no network. `validate`
exits non-zero when continuity fails, which makes it usable as a migration gate
in a script.

## LLM Provider Support

| Provider | Setup |
|----------|-------|
| **Anthropic** | `export ANTHROPIC_API_KEY=sk-ant-...` |
| **OpenAI** | `export OPENAI_API_KEY=sk-...` |
| **Local (Ollama)** | `export LLM_BASE_URL=http://localhost:11434/v1` and `export MEMORY_MODEL=llama3.1` |
| **Any OpenAI-compatible** | Set `LLM_BASE_URL`, `MEMORY_MODEL`, and `LLM_API_KEY` if the endpoint needs one |

Model aliases: `haiku`, `sonnet`, `opus`, `gpt4mini`, `gpt4`. Any other value is passed through unchanged, so custom and locally-hosted model names work as-is.

`LLM_BASE_URL` takes over completely: every call goes to that endpoint, the aliases above are *not* expanded (your endpoint's own names are sent verbatim, so name the model with `MEMORY_MODEL` or `--model`), and the cloud fallback is disabled — a failure there is reported, never retried against Anthropic or OpenAI. Self-hosting is usually a decision about where the memory content is allowed to go, so a stray `ANTHROPIC_API_KEY` in the environment must not quietly send it off-box.

> [!NOTE]
> Provider model IDs get retired on a schedule, and a retired ID returns a 404. v0.1 shipped a default that was withdrawn while still in the code, which — combined with an over-broad `except` — meant failed extractions were stored as degraded memories and reported as successes. Both are fixed, and a test now fails the build if any shipped alias points at a known-retired model.

## Framework Integration

### LangChain

```python
from langchain.tools import Tool
import requests

MEMORY = "http://localhost:8889"

recall = Tool(
    name="search_memory",
    description="Search the agent's persistent memory for relevant past context",
    func=lambda q: requests.get(f"{MEMORY}/search", params={"q": q}).json()["memories"],
)
```

### CrewAI

```python
from crewai.tools import tool
import requests

@tool("Search Memory")
def search_memory(query: str) -> str:
    """Search persistent agent memory for relevant past context."""
    hits = requests.get("http://localhost:8889/search", params={"q": query}).json()
    return "\n".join(f"[#{m['id']}] {m['summary']}" for m in hits["memories"])
```

See [examples/](examples/) for OpenClaw, LangChain, and CrewAI setups.

## Namespaces & Multi-Agent Support

Every memory belongs to a **namespace** — a project, agent, or tenant. Omitting it
uses the shared namespace `""`, so single-tenant setups need no changes.

```bash
curl -X POST http://localhost:8889/ingest \
  -d '{"text": "the deploy target is staging", "namespace": "project-a"}'

curl "http://localhost:8889/search?q=deploy+target&namespace=project-a"   # finds it
curl "http://localhost:8889/search?q=deploy+target&namespace=project-b"   # finds nothing
curl "http://localhost:8889/namespaces"
```

Retrieval, listing, consolidation, and stats are all scoped. Omitting `namespace`
means the shared namespace — **not** "every namespace".

### Why this is a correctness feature

Namespaces are what make memory *correction* possible. "The preferred editor is
vim" and "the preferred editor is VS Code" are a genuine contradiction within one
project and two compatible facts across two. Without an enforceable boundary, any
system that resolves contradictions will delete memories that were true.

## Correcting a Memory

Corrections invalidate; they never delete.

```bash
curl -X POST http://localhost:8889/supersede -d '{"old_id": 3, "new_id": 4}'
```

The superseded memory keeps its content, gains an end to its validity interval, and
drops out of normal retrieval — but stays reachable when you ask for history:

```bash
curl "http://localhost:8889/search?q=deadline&include_superseded=true"
```

Supersession across namespaces is refused: retiring a fact in someone else's scope
would delete something still true there. `/delete` remains for genuine erasure.

### Detecting contradictions automatically

`POST /reconcile` finds conflicting memories and supersedes the stale one. It is
**off unless you name a judge model**, because contradiction judging degrades
sharply on small models — and this is a feature that *deletes things* when it is
wrong:

```bash
export MEMORY_JUDGE_MODEL=sonnet
curl -X POST "http://localhost:8889/reconcile?namespace=project-a"
```

Without it you get a `409` explaining why rather than silent inaction.

Four guardrails, each because the failure mode is real:

| Guardrail | Why |
|---|---|
| **Off by default** | Small models collapse at this task; the default model is a small one |
| **Deterministic prefilter** | Only same-namespace pairs with real word overlap cost an LLM call — bounds spend and removes obvious false positives |
| **Reasoning before verdict** | A response whose verdict precedes its reasoning is rejected as unreasoned. Ordering the schema this way measurably rescues weaker models |
| **Abstain when unsure** | Below the confidence threshold nothing happens. Keeping a stale memory beats destroying a true one |

Reconciliation never runs on the ingest path, so a slow or failing judge cannot
block a write. Tune with `reconcile_min_confidence`, `reconcile_min_overlap`, and
`reconcile_max_comparisons`.

## Semantic retrieval

BM25 ranks memories that share words with the query. It cannot reach the memory
that says the same thing in other words — and that is most of what an agent is
asked to remember. Embeddings close that gap, and they are off by default:

```bash
pip install "jnaapakam[embeddings]"
export MEMORY_EMBEDDING_MODEL=text-embedding-3-small   # or a local model
export OPENAI_API_KEY=sk-...                           # or LLM_BASE_URL for a local endpoint
```

```bash
curl "http://localhost:8889/search?q=which+data+warehouse"
# finds "the team standardised on ClickHouse for analytics" — no shared words
```

Semantic hits are **added to** the candidate pool, not used to reorder it. Re-ranking
what BM25 already found could never surface the memory that shares no words with the
query, which is the only reason to run an embedding model at all. Relevance becomes
`(1 - w)·lexical + w·semantic`, with `w` from `MEMORY_SEMANTIC_WEIGHT`.

The capability is detected at runtime, not just configured: a model name without
numpy installed logs a warning and leaves retrieval lexical, rather than presenting a
feature that silently never runs. `/status` reports coverage, because a half-embedded
corpus answers half its queries lexically and a flag cannot tell you that:

```bash
curl localhost:8889/status
# "embeddings": {"model": "text-embedding-3-small", "enabled": true, "embedded": 1841, "total": 1842}

curl -X POST "localhost:8889/embed?limit=500"     # backfill what predates the model
```

New memories are embedded at ingest. If the embedding endpoint is down the memory is
still stored and still findable lexically — losing semantic relevance degrades
retrieval, it does not fail it. Anthropic publishes no embeddings API, so this routes
to OpenAI or to whatever `LLM_BASE_URL` serves.

## Dashboard

```bash
jnaapakam serve
open http://localhost:8889/dashboard
```

A read-only view of what the agent remembers: counts, namespaces, recent memories,
and search. One HTML file with no build step and no dependencies, served by the same
process — archived memories are dimmed, superseded ones marked, and nothing on the
page can write.

It is served on **local binds only**. On a public bind it is a 404 even with a valid
token: exposing the API to a network should not also publish a browsable window onto
an agent's memory. When a token is configured, the dashboard needs it like every
other route.

## Forgetting

Forgetting is soft. Memories are **archived, never deleted**:

```bash
curl -X POST "http://localhost:8889/prune?keep=500&namespace=project-a"
curl -X POST "http://localhost:8889/prune?older_than_days=180&namespace=project-a"
curl "http://localhost:8889/search?q=...&include_archived=true"   # still reachable
curl -X POST http://localhost:8889/archive -d '{"memory_id": 7, "restore": true}'
```

Two policies, because they answer different questions. `keep` bounds how much a
namespace holds; `older_than_days` retires what has stopped being used. A namespace
comfortably under its cap can still be full of memories nobody has read in a year.
Give both and the age policy runs first.

The age clock is `last_accessed`, falling back to `created_at` — so **recall resets
it**, and a memory still being read never expires no matter how old it is. Use is the
signal, not importance: `importance` is assigned once at ingest and never revised, so
it cannot notice that a memory stopped mattering.

Set `MEMORY_EXPIRE_AFTER_DAYS` (or `--expire-after`) and the policy applies on its own,
swept hourly, starting at boot. Unset, nothing expires — archiving an operator's
memories on a timer they never asked for is not a sensible default.

Retention combines importance, **how often the memory was actually recalled**, and
temporal decay. The frequency term matters most: `importance` is assigned once by an
LLM at ingest and never revised, so it cannot distinguish a memory that proved
useful from one that merely sounded important. Superseded memories are evicted
before live ones.

> [!NOTE]
> The literature publishes no agreed decay formula, so these weights are defaults to
> tune, not findings. Nothing is ever destroyed automatically.

## Generational Continuity

A resident agent outlives its parts. It may start on a modest workstation with a
small local model and later move to far stronger hardware. v0.3 lets that happen
without its accumulated identity and history starting from zero.

> A generation may change the agent's model, runtime, tools, hardware and
> capabilities without changing the agent's continuity identity.

This is a claim about **systems**, not about minds. jñāpakaṁ defines a stable
identifier, a verifiable memory corpus, an auditable lineage, and provenance for
every transition. It does not claim that two model instances are the same
consciousness, and nothing here should be read that way.

### Three kinds of continuity — only one is ours

| Kind | What it covers | Who owns it |
|------|----------------|-------------|
| **Semantic continuity** | Identity, memory, provenance, history, lineage | **jñāpakaṁ** |
| **Operational continuity** | Durable workflows, pending jobs, timers, execution recovery | Your durable execution system |
| **Environmental reproducibility** | Containers, VM images, repositories, deployment manifests | Your build and deploy tooling |

jñāpakaṁ **references** the other two and never owns them. It is not a workflow
engine, a task queue, a container orchestrator, an inference server, or a
deployment platform, and it takes no dependency on any particular one.

```
Agent runtime
   ├── jñāpakaṁ ................ identity, memory, lineage, migration provenance
   ├── durable execution ....... referenced, never managed
   ├── inference server ........ referenced, never managed
   └── workspace / VCS ......... referenced, never managed
```

### Permanent identity

Every store holds one identity, minted once and stable forever after:

```bash
$ jnaapakam agent
agent:       urn:jnaapakam:agent:9f2c1e7a4b8d40f1a3c6e9b2d5f80147
created:     2026-08-31T09:12:44+00:00
current:     generation 2
generations: 2
```

It is independent of the agent's display name, model, provider, runtime, host,
hardware, OS and generation number. The name in `IDENTITY.md` is a label for
people; identity is not derived from it, because then identity would change
exactly when this protocol promises it will not. Opening a v0.2 database mints
one automatically — that agent always had a continuous identity, there was just
never a name for it.

### Migrating a generation

Everything the environment declares is optional. A minimal generation is `{}`;
disclosing your hardware, model, or infrastructure is never required.

```bash
# On the new machine, restore the agent's semantic state
curl -X POST http://localhost:8889/restore -d @backup.json

# Record what changed about the runtime
jnaapakam generation create --parent 1 --label upgrade --manifest gen2.json

# Seal the soul files and the memory corpus, then check continuity
jnaapakam generation seal 2 --soul-dir ./soul
jnaapakam generation validate 2 --soul-dir ./soul

# Only a generation that validated can become the agent
jnaapakam generation promote 2
```

`validate` runs eight checks, and one that was not requested reports `skipped`
rather than `pass` — a validation that passes because nothing was checked is
exactly the failure this exists to prevent:

```
  identity       pass     agent_id urn:jnaapakam:agent:9f2c1e7a…
  memory         pass     1842 records match the sealed corpus digest
  semantic_state pass     validity, archival, corrections and links match the sealed state
  signature      pass     2 artifacts signed by 46ce01f0611df872
  recall         pass     3 recall probes resolved
  soul           pass     3 artifacts match their recorded digests
  context        recorded 1 external references recorded; verifying them is the
                          responsibility of the systems that own them
  behavioral     skipped  no behavioural evaluation supplied
generation 2: continuity verified
```

Promotion is refused unless the last validation passed. `--force` exists for the
operator who has decided anyway, and writes onto the migration record that the
override was used — so it is never invisible afterwards.

### Signing a seal

The digests above give a seal **integrity**: they catch a corpus that drifted.
They cannot give it **authenticity**. Anyone who can write the database can
recompute every digest, so an intact continuity record proves only that nobody
tampered carelessly. Signing is what separates a corpus that survived from one
that was replaced and resealed.

```bash
pip install "jnaapakam[signing]"                          # Ed25519 via cryptography
openssl genpkey -algorithm ed25519 -out sealing.key       # no extra tooling needed
export MEMORY_SIGNING_KEY=$PWD/sealing.key
```

Every seal from then on is signed, and `validate` gains a `signature` check:

```
  signature      pass     2 artifacts signed by 46ce01f0611df872
```

The signature covers a canonical statement binding the `agent_id`, the generation,
the artifact name, its digest and the time it was recorded — not the digest alone,
so a valid signature cannot be lifted onto another generation's seal.

Verifying against the key recorded beside the signature proves self-consistency and
nothing more: an impostor records their own key. To check *provenance*, name the key
you expect:

```bash
jnaapakam generation validate 2 --public-key 46ce01f0…      # hex public key
```

```
  signature      fail     sealed by 9a2db2e23f1504cd, expected 46ce01f0611df872
                          — this seal is not from that key
```

Signing is optional in both directions. Without a key, seals record digests as
before. Without the dependency installed, an existing signature reports `skipped`,
never `pass` — unverifiable is not verified. An unsigned seal is a weaker claim, not
a failed one, so it does not fail validation.

### Comparing generations

```bash
$ jnaapakam generation diff 1 2
Generation 1 -> Generation 2

agent_id:  unchanged  urn:jnaapakam:agent:9f2c1e7a4b8d40f1a3c6e9b2d5f80147

inference:
  model: small-9b -> large-70b
hardware:
  vram_gb: 12 -> 96
  ram_gb: 32 -> 256
capabilities:
  + long_context: True

memory:    1842 -> 1842 records (unchanged)
SOUL.md:   unchanged
```

### Rollback keeps history

If a promoted generation turns out badly, roll back. The generation you leave
keeps its `promoted` status, the migration log gains a `rolled_back` entry rather
than losing the old one, and **no memory is touched**. Rolling back is a change of
runtime, not a retraction of what the agent learned while running it.

```bash
jnaapakam generation rollback 1
curl http://localhost:8889/migrations      # the whole history, still intact
```

`/clear` does not erase the lineage or the identity either. Deleting memories is
not the same as claiming the agent never existed.

### Content is not continuity

A migration can carry every memory across intact and still lose the agent. Say
generation 1 learned *"the preferred database is PostgreSQL"*, then later
*"the user switched to ClickHouse"*, and superseded the first with the second. If
the correction chain is dropped on the way over, both memories arrive, both are
live, and the agent now believes two contradictory things — while a digest over
memory *text* stays byte-identical, because no text changed.

So the corpus gets two digests:

| Digest | Question | Covers |
|---|---|---|
| `content` | What knowledge exists? | text, summary, source, namespace, kind, importance, event time, entities, topics |
| `semantic_state` | How is it read? | content digest + validity interval, archival, correction chain, consolidation links |

They fail separately, because they mean different things and demand different
responses: a content failure means memories were lost or altered; a state failure
means they all arrived and are now interpreted differently.

Cross-row references — supersession and consolidation links — are hashed as **the
content digest of what they point at**, never as a row id. So `17 → 26` becoming
`3 → 91` after a restore renumbers everything still matches, while `17 → nothing`
fails.

### Integrity, not authenticity

Digests are SHA-256 over the exact bytes on disk — no newline translation, no BOM
stripping, no normalisation. Both corpus digests are order-independent so they
survive a restore that renumbers every row, while a recall (which bumps access
counters) leaves them unchanged.

> [!IMPORTANT]
> A digest proves the bytes did not change. It says nothing about *who* produced
> them: anyone who can write the store can write a digest. Integrity is not
> authenticity. Since v0.5 a seal can be **signed** — see
> [Signing a seal](#signing-a-seal) — but signing is optional, and an unsigned seal
> carries integrity alone.

The server never opens a file to hash it. Digests arrive precomputed, and the CLI
does the reading — locally, from a directory you name, restricted to soul
filenames. An endpoint that accepts a path and hashes it is an arbitrary-file-read
oracle wearing an integrity feature's clothes.

Generation manifests are untrusted metadata: never executed, never dereferenced,
never used to build a path, and never placed in an LLM prompt. Manifests carrying a
credential field, or a reference URI with embedded userinfo, are refused outright.

See [PROTOCOL.md §10](PROTOCOL.md) for the normative rules and
[examples/generational-continuity](examples/generational-continuity/) for a full
Generation 1 → Generation 2 walkthrough.

## Deployment

### systemd (Linux)

```ini
[Unit]
Description=jñāpakaṁ Memory Server

[Service]
Environment="MEMORY_AUTH_TOKEN=<your-token>"
Environment="ANTHROPIC_API_KEY=<your-key>"
ExecStart=/usr/bin/jnaapakam serve --host 0.0.0.0
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

### Docker

No image is published yet. To build one:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install .
ENV MEMORY_DB=/data/memory.db MEMORY_HOST=0.0.0.0
VOLUME /data
EXPOSE 8889
CMD ["jnaapakam", "serve"]
```

## Roadmap

- [x] Backup/restore endpoints
- [x] Real retrieval (FTS5 + BM25 ranking)
- [x] Authentication and safe bind defaults
- [x] Multi-agent namespacing and scoped retrieval
- [x] Memory correction: supersede and invalidate rather than delete
- [x] Validity intervals (`valid_from` / `valid_to`) and usage tracking
- [x] Automatic contradiction detection driving supersession
- [x] Soft forgetting: retention scoring, archive and prune
- [x] Generational continuity: stable `agent_id`, lineage, migration provenance, integrity
- [x] Memory expiry and retention policies: age-based, scheduled, still reversible
- [x] Signed continuity records: Ed25519 seals, optional dependency, provenance on request
- [x] Dashboard UI: read-only, local binds only, no build step
- [x] Optional embeddings as a runtime-detected capability
- [ ] Pluggable storage backends (Postgres/pgvector, embedded graph)
- [ ] Encryption at rest

## Philosophy

> The best AI memory system is the one that feels invisible.

jñāpakaṁ is designed to be:

- **Simple** — SQLite + HTTP + LLM. One dependency. No infrastructure sprawl.
- **Portable** — Standard files and APIs. Move between frameworks freely.
- **Respectful** — Your agent's memories belong to you. Always local-first.
- **Honest** — If an extraction fails you get an error, not a quietly degraded memory.
- **Continuous** — An agent's identity survives its parts. Replacing the model is not a new agent.

## Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md).

Areas we'd love help with:
- Multi-agent namespacing design
- Integration examples for more frameworks
- Memory visualization tools
- Benchmarks at realistic memory scales

## License

MIT — Use it however you want. Your agent's soul is yours.

---

<p align="center">
  <b>jñāpakaṁ</b> — <i>memory that persists</i>
  <br>
  Built by <a href="https://yablokolabs.com">Yabloko Labs</a>
</p>
