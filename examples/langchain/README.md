# jñāpakaṁ + LangChain

## Setup

```python
import os
import requests
from langchain.tools import Tool

MEMORY_URL = "http://localhost:8889"
# Only needed if the server was started with MEMORY_AUTH_TOKEN set.
HEADERS = (
    {"Authorization": f"Bearer {os.environ['MEMORY_AUTH_TOKEN']}"}
    if os.getenv("MEMORY_AUTH_TOKEN")
    else {}
)


def search_memory(query: str) -> str:
    """Return ranked memory records, so the agent reasons over them itself."""
    resp = requests.get(f"{MEMORY_URL}/search", params={"q": query}, headers=HEADERS)
    resp.raise_for_status()
    memories = resp.json()["memories"]
    if not memories:
        return "No relevant memories found."
    return "\n".join(f"[#{m['id']} {m['created_at'][:10]}] {m['summary']}" for m in memories)


def remember(text: str, source: str = "langchain") -> str:
    resp = requests.post(
        f"{MEMORY_URL}/ingest", json={"text": text, "source": source}, headers=HEADERS
    )
    resp.raise_for_status()
    return resp.json().get("summary", "stored")


memory_search = Tool(
    name="search_memory",
    description="Search the agent's persistent memory for past context and knowledge",
    func=search_memory,
)

memory_ingest = Tool(
    name="remember",
    description="Store important information in persistent memory",
    func=remember,
)

# Add to your agent
from langchain.agents import initialize_agent

agent = initialize_agent(
    tools=[memory_search, memory_ingest, ...],
    llm=your_llm,
)
```

## Notes

- **Prefer `/search` over `/query`.** It returns the memory records with their IDs and
  sources, so your agent can cite them and decide what matters. `/query` runs a second
  LLM call server-side and hands back prose.
- `raise_for_status()` matters: a failed ingest is a real error, and the server
  reports it as one rather than silently storing a degraded memory.
