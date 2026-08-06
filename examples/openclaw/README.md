# jñāpakaṁ + OpenClaw

## Setup

1. Start the jñāpakaṁ server:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
jnaapakam serve
```

It binds `127.0.0.1:8889` by default. To reach it from another machine you must set
`MEMORY_AUTH_TOKEN` — the server refuses to bind a public interface without one.

2. Add to your agent's `AGENTS.md`:

```markdown
## Shared Memory (jñāpakaṁ)

On session start, pull relevant context:
\`\`\`bash
curl -s "http://localhost:8889/search?q=recent+context+and+active+tasks" \
  | jq -r '.memories[] | "- [#\(.id)] \(.summary)"'
\`\`\`

During conversations, ingest important takeaways:
\`\`\`bash
curl -sf -X POST http://localhost:8889/ingest \
  -H 'Content-Type: application/json' \
  -d '{"text": "...", "source": "conversation"}'
\`\`\`
```

Use `curl -sf` so a failed ingest is visible. The server returns an error when the
model is unreachable rather than storing an empty memory and reporting success.

## Why `/search` rather than `/query`

`/search` returns the memory records themselves — ids, summaries, sources, scores —
so the agent can cite them and judge relevance. `/query` spends a second LLM call
turning those same records into prose and drops the provenance on the floor. For an
agent that is already reasoning, `/search` is the cheaper and more useful endpoint.

## Multi-Agent

Multiple OpenClaw agents can share one jñāpakaṁ server:

```
Agent A ──┐
          ├──► jñāpakaṁ :8889 ◄──► memory.db
Agent B ──┘
```

Each agent ingests from its sessions; all agents query the shared pool.

Pass a `namespace` to keep unrelated projects apart — one server can host many:

```bash
curl -s "http://localhost:8889/search?q=recent+context&namespace=project-a"
```

Omitting it uses the shared namespace. Note that omitting it means the *shared*
namespace, not every namespace — see PROTOCOL.md §6.
