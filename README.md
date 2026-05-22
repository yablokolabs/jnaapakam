# jñāpakaṁ

[![MCPize](https://mcpize.com/badge/@yablokolabs/jnaapakam)](https://mcpize.com/mcp/jnaapakam)

**An open protocol for AI agent memory persistence.**

*Your AI has a soul now. Don't lose it.*

---

jñāpakaṁ (Sanskrit: *memory, reminder*) is an open standard for persisting AI agent identity, personality, and memory across sessions, restarts, and platforms. It provides:

- 📋 **Soul Schema** — A standard format for defining who your agent *is*
- 🧠 **Memory Protocol** — Ingest, consolidate, and query agent memories via a simple HTTP API
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

```bash
pip install jnaapakam
```

Or run from source:

```bash
git clone https://github.com/yablokolabs/jnaapakam.git
cd jnaapakam
pip install -r requirements.txt
```

### 2. Define Your Agent's Soul

Create the soul files in your agent's workspace:

```bash
jnaapakam init
```

This creates:

```
your-agent/
├── SOUL.md       # Personality, tone, boundaries
├── IDENTITY.md   # Name, emoji, creature type
└── MEMORY.md     # Long-term curated memory
```

Edit them to match your agent's personality. See [schema/](schema/) for examples.

### 3. Start the Memory Server

```bash
jnaapakam serve --port 8889
```

Your agent now has persistent memory via a simple HTTP API:

```bash
# Store a memory
curl -X POST http://localhost:8889/ingest \
  -H 'Content-Type: application/json' \
  -d '{"text": "User prefers dark mode and vim keybindings", "source": "conversation"}'

# Query memories
curl "http://localhost:8889/query?q=what+are+the+user+preferences"

# Check status
curl http://localhost:8889/status
```

### 4. Connect Your Agent

Add to your agent's startup routine:

```python
import requests

# Retrieve context on startup
resp = requests.get("http://localhost:8889/query", params={"q": "recent context and active tasks"})
context = resp.json()["answer"]

# Ingest important takeaways during conversation
requests.post("http://localhost:8889/ingest", json={
    "text": "User decided to switch from React to Svelte for the new project",
    "source": "conversation:2026-03-08"
})
```

## Connect via MCPize

Use this MCP server instantly with no local installation:

```bash
npx -y mcpize connect @yablokolabs/jnaapakam --client claude
```

Or connect at: **https://mcpize.com/mcp/jnaapakam**

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Your AI Agent                      │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │ SOUL.md  │  │IDENTITY.md│  │    MEMORY.md      │  │
│  │personality│  │name, emoji│  │curated long-term  │  │
│  └──────────┘  └──────────┘  └───────────────────┘  │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP API
              ┌────────▼────────┐
              │  jñāpakaṁ Server │
              │                  │
              │  ┌────────────┐  │
              │  │  Ingest    │  │  ← New information arrives
              │  │  (LLM)     │  │  ← Extract entities, topics, importance
              │  └─────┬──────┘  │
              │        ▼         │
              │  ┌────────────┐  │
              │  │  SQLite    │  │  ← Structured memory store
              │  │  memory.db │  │
              │  └─────┬──────┘  │
              │        ▼         │
              │  ┌────────────┐  │
              │  │Consolidate │  │  ← Periodic: find patterns, connections
              │  │  (LLM)     │  │  ← Like sleep cycles for AI
              │  └─────┬──────┘  │
              │        ▼         │
              │  ┌────────────┐  │
              │  │   Query    │  │  ← Answer questions from memory
              │  │  (LLM)     │  │
              │  └────────────┘  │
              └──────────────────┘
```

### How It Works

1. **Ingest** — Feed text to the server. An LLM extracts a summary, entities, topics, and importance score. Stored in SQLite.

2. **Consolidate** — Every 30 minutes (configurable), the server reviews unconsolidated memories, finds cross-cutting patterns, and generates insights. Like how the human brain consolidates during sleep.

3. **Query** — Ask any question. The server reads all memories and consolidation insights, then synthesizes an answer with source citations.

### Why Not Vector Databases?

Traditional RAG embeds once and retrieves later. No active processing. No consolidation. No cross-referencing.

jñāpakaṁ uses an LLM to actively think about memories — finding connections, generating insights, compressing related information. The tradeoff: it uses more LLM calls, but produces richer, more connected memory.

For most agent workloads (hundreds to low thousands of memories), this approach is simpler, cheaper, and more effective than maintaining a vector database pipeline.

## Soul Schema

The soul schema defines three standard files that any agent framework can read:

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
- **Personality:** Helpful navigator, slightly nerdy
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
| `/memories` | GET | List stored memories (accepts `?limit=N`) |
| `/ingest` | POST | Ingest new text `{"text": "...", "source": "..."}` |
| `/query?q=...` | GET | Query memory with natural language |
| `/consolidate` | POST | Trigger manual consolidation |
| `/delete` | POST | Delete a memory `{"memory_id": N}` |
| `/clear` | POST | Delete all memories (full reset) |
| `/backup` | GET | Export all data as JSON |
| `/restore` | POST | Import from backup JSON |

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_MODEL` | `default` | LLM model for memory operations |
| `MEMORY_DB` | `./memory.db` | SQLite database path |
| `MEMORY_PORT` | `8889` | HTTP server port |
| `CONSOLIDATE_INTERVAL` | `30` | Minutes between consolidation cycles |

### CLI Options

```bash
jnaapakam serve [options]

  --port PORT              HTTP API port (default: 8889)
  --watch DIR              Folder to watch for file ingestion (default: ./inbox)
  --consolidate-every MIN  Consolidation interval (default: 30)
  --model MODEL            LLM model alias or full name
  --db PATH                SQLite database path
```

## LLM Provider Support

jñāpakaṁ is model-agnostic. Configure any provider:

| Provider | Setup |
|----------|-------|
| **Anthropic** | `export ANTHROPIC_API_KEY=sk-ant-...` |
| **OpenAI** | `export OPENAI_API_KEY=sk-...` |
| **Local (Ollama)** | `export LLM_BASE_URL=http://localhost:11434/v1` |
| **Any OpenAI-compatible** | Set `LLM_BASE_URL` and `LLM_API_KEY` |

The reference implementation uses Anthropic Haiku by default (fast and cheap for background memory work), with automatic fallback support.

## Framework Integration

### OpenClaw

Add to your agent's `AGENTS.md`:

```markdown
## Shared Memory

Query memory on session start:
\`\`\`bash
curl -s "http://localhost:8889/query?q=recent+context"
\`\`\`

Ingest takeaways during conversations:
\`\`\`bash
curl -s -X POST http://localhost:8889/ingest \
  -H 'Content-Type: application/json' \
  -d '{"text": "...", "source": "conversation"}'
\`\`\`
```

### LangChain

```python
from langchain.tools import Tool

memory_tool = Tool(
    name="agent_memory",
    description="Query the agent's persistent memory",
    func=lambda q: requests.get(f"http://localhost:8889/query?q={q}").json()["answer"]
)
```

### CrewAI

```python
from crewai import Tool

@Tool
def remember(query: str) -> str:
    """Query persistent agent memory."""
    return requests.get(f"http://localhost:8889/query?q={query}").json()["answer"]
```

### Raw Python Agent

```python
class PersistentAgent:
    def __init__(self, memory_url="http://localhost:8889"):
        self.memory = memory_url
        # Load context on startup
        self.context = requests.get(f"{self.memory}/query?q=who+am+i").json()["answer"]

    def on_conversation_end(self, summary: str):
        requests.post(f"{self.memory}/ingest", json={"text": summary, "source": "conversation"})
```

## Multi-Agent Support

Multiple agents can share the same memory server, creating a collective knowledge base:

```
Agent A (Coder) ──┐
                  ├──► jñāpakaṁ Server ◄──► memory.db
Agent B (Analyst)─┘
```

Each agent ingests from its own domain; all agents can query the shared pool.

## Deployment

### systemd (Linux)

```ini
[Unit]
Description=jñāpakaṁ Memory Server

[Service]
ExecStart=/usr/bin/python3 /path/to/jnaapakam/server.py --port 8889
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

### Docker

```bash
docker run -d -p 8889:8889 -v ./data:/app/data yablokolabs/jnaapakam
```

### macOS (launchd)

```bash
jnaapakam install-service  # Creates and loads a LaunchAgent
```

## Roadmap

- [ ] Backup/restore endpoints
- [ ] Encryption at rest
- [ ] Multi-agent namespacing
- [ ] WebSocket streaming for real-time memory updates
- [ ] Memory expiry and retention policies
- [ ] Dashboard UI
- [ ] Plugin system for custom memory processors
- [ ] Cloud sync (jñāpakaṁ.ai)

## Philosophy

> The best AI memory system is the one that feels invisible.

jñāpakaṁ is designed to be:

- **Simple** — SQLite + HTTP + LLM. No infrastructure sprawl.
- **Portable** — Standard files and APIs. Move between frameworks freely.
- **Respectful** — Your agent's memories belong to you. Always local-first.
- **Alive** — Active consolidation, not passive storage. Memories evolve.

## Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

Areas we'd love help with:
- Integration examples for more frameworks
- Alternative LLM provider backends
- Memory visualization tools
- Performance benchmarks

## License

MIT — Use it however you want. Your agent's soul is yours.

---

<p align="center">
  <b>jñāpakaṁ</b> — <i>memory that persists</i>
  <br>
  Built by <a href="https://yablokolabs.com">Yabloko Labs</a>
</p>