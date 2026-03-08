# jñāpakaṁ + OpenClaw

## Setup

1. Start the jñāpakaṁ server:
```bash
cd /path/to/jnaapakam/reference
python server.py --port 8889
```

2. Add to your agent's `AGENTS.md`:

```markdown
## Shared Memory (jñāpakaṁ)

On session start, query memory for context:
\`\`\`bash
curl -s "http://localhost:8889/query?q=recent+context+and+active+tasks"
\`\`\`

During conversations, ingest important takeaways:
\`\`\`bash
curl -s -X POST http://localhost:8889/ingest \
  -H 'Content-Type: application/json' \
  -d '{"text": "...", "source": "conversation"}'
\`\`\`
```

## Multi-Agent

Multiple OpenClaw agents can share the same jñāpakaṁ server.
Each agent ingests from its sessions; all agents query the shared pool.

```
Agent A ──┐
          ├──► jñāpakaṁ :8889 ◄──► memory.db
Agent B ──┘
```
