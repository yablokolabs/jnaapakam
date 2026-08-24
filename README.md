# jñāpakaṁ

[![MCPize](https://mcpize.com/badge/@yablokolabs/jnaapakam)](https://mcpize.com/mcp/jnaapakam)

**An open protocol for AI agent memory persistence.**

*Your AI has a soul now. Don't lose it.*

---

jñāpakaṁ (Sanskrit: *memory, reminder*) is an open standard for persisting AI agent identity, personality, and memory across sessions, restarts, and platforms. It provides:

- 📋 **Soul Schema** — A standard format for defining who your agent *is*
- 🧠 **Memory Protocol** — Ingest, retrieve, consolidate, and query agent memories over HTTP or MCP
- 🔍 **Real retrieval** — Full-text search with BM25 ranking, using nothing but stock SQLite
- 🔄 **Backup & Restore** — Never lose your agent's accumulated knowledge
- 🔌 **Framework Agnostic** — Works with any agent framework, any LLM provider

## The Problem

AI agents today have amnesia. Every session starts fresh. The personality you spent weeks refining, the preferences it learned, the context it accumulated — gone on restart.

Some frameworks bolt on vector databases or conversation logs, but there's no standard way to:

- Define an agent's identity and personality portably
- Persist structured memories across sessions
- Migrate an agent's "soul" between platforms
- Back up and restore agent knowledge

**jñāpakaṁ fixes this.**

## Quick Start

### 1. Install

From source (works today):

```bash
git clone https://github.com/yablokolabs/jnaapakam.git
cd jnaapakam
pip install .
```

Once v0.2 is published to PyPI:

```bash
pip install jnaapakam
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
| `/prune` | POST | Archive all but the `?keep=N` highest-retention memories |
| `/archive` | POST | Archive or restore one memory `{"memory_id": N, "restore": bool}` |
| `/prune` requires `?keep=N` | | omitting it is a `400`, not a silent default |
| `/namespaces` | GET | List namespaces with memory counts |
| `/backup` | GET | Export memories and consolidations as JSON |
| `/restore` | POST | Import from backup JSON |
| `/mcp` | POST | MCP JSON-RPC endpoint (also served on `/`) |

Prefer `/search` over `/query` when your agent should reason over the memories itself — it keeps provenance intact and skips an LLM call.

### MCP tools

`search_memory`, `ingest_memory`, `query_memory`, `list_memories`, `get_memory_status`, `consolidate_memories`.

Every tool takes an optional `namespace` argument. The surface is deliberately
small: tool definitions consume context on every request, so memory types arrive as
parameters rather than as new tools.

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

### CLI

```bash
jnaapakam serve [options]

  --host HOST              Bind address (default: 127.0.0.1)
  --port PORT              HTTP port (default: 8889)
  --db PATH                SQLite database path
  --model MODEL            LLM model alias or full name
  --watch DIR              Folder to watch for file ingestion
  --consolidate-every MIN  Consolidation interval (default: 30)

jnaapakam init [directory]   Create SOUL.md / IDENTITY.md / MEMORY.md
```

## LLM Provider Support

| Provider | Setup |
|----------|-------|
| **Anthropic** | `export ANTHROPIC_API_KEY=sk-ant-...` |
| **OpenAI** | `export OPENAI_API_KEY=sk-...` |
| **Local (Ollama)** | `export LLM_BASE_URL=http://localhost:11434/v1` |
| **Any OpenAI-compatible** | Set `LLM_BASE_URL` and `LLM_API_KEY` |

Model aliases: `haiku`, `sonnet`, `opus`, `gpt4mini`, `gpt4`. Any other value is passed through unchanged, so custom and locally-hosted model names work as-is.

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

## Forgetting

Forgetting is soft. Memories are **archived, never deleted**:

```bash
curl -X POST "http://localhost:8889/prune?keep=500&namespace=project-a"
curl "http://localhost:8889/search?q=...&include_archived=true"   # still reachable
curl -X POST http://localhost:8889/archive -d '{"memory_id": 7, "restore": true}'
```

Retention combines importance, **how often the memory was actually recalled**, and
temporal decay. The frequency term matters most: `importance` is assigned once by an
LLM at ingest and never revised, so it cannot distinguish a memory that proved
useful from one that merely sounded important. Superseded memories are evicted
before live ones.

> [!NOTE]
> The literature publishes no agreed decay formula, so these weights are defaults to
> tune, not findings. Nothing is ever destroyed automatically.

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
- [ ] Optional embeddings as a runtime-detected capability
- [ ] Pluggable storage backends (Postgres/pgvector, embedded graph)
- [ ] Encryption at rest
- [ ] Memory expiry and retention policies
- [ ] Dashboard UI

## Philosophy

> The best AI memory system is the one that feels invisible.

jñāpakaṁ is designed to be:

- **Simple** — SQLite + HTTP + LLM. One dependency. No infrastructure sprawl.
- **Portable** — Standard files and APIs. Move between frameworks freely.
- **Respectful** — Your agent's memories belong to you. Always local-first.
- **Honest** — If an extraction fails you get an error, not a quietly degraded memory.

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
