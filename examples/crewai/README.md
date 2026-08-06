# jñāpakaṁ + CrewAI

## Setup

```python
import os
import requests
from crewai import Agent, Task, Crew
from crewai.tools import tool

MEMORY_URL = "http://localhost:8889"
# Only needed if the server was started with MEMORY_AUTH_TOKEN set.
HEADERS = (
    {"Authorization": f"Bearer {os.environ['MEMORY_AUTH_TOKEN']}"}
    if os.getenv("MEMORY_AUTH_TOKEN")
    else {}
)


@tool("Search Memory")
def search_memory(query: str) -> str:
    """Search persistent agent memory and return the matching records."""
    resp = requests.get(f"{MEMORY_URL}/search", params={"q": query}, headers=HEADERS)
    resp.raise_for_status()
    memories = resp.json()["memories"]
    if not memories:
        return "No relevant memories found."
    return "\n".join(f"[#{m['id']}] {m['summary']} (source: {m['source']})" for m in memories)


@tool("Remember")
def remember(text: str) -> str:
    """Store important information in persistent memory."""
    resp = requests.post(
        f"{MEMORY_URL}/ingest", json={"text": text, "source": "crewai"}, headers=HEADERS
    )
    resp.raise_for_status()
    return resp.json().get("summary", "stored")


researcher = Agent(
    role="Research Analyst",
    goal="Analyze data and remember findings",
    tools=[search_memory, remember],
    backstory="You are a researcher with persistent memory across sessions.",
)

task = Task(
    description="Research the topic and store key findings in memory",
    agent=researcher,
)

crew = Crew(agents=[researcher], tasks=[task])
crew.kickoff()
```

## Isolating Crews

Pass a `namespace` so unrelated crews don't retrieve each other's memories:

```python
NAMESPACE = "research-crew"

requests.get(f"{MEMORY_URL}/search", params={"q": query, "namespace": NAMESPACE}, headers=HEADERS)
requests.post(
    f"{MEMORY_URL}/ingest",
    json={"text": text, "source": "crewai", "namespace": NAMESPACE},
    headers=HEADERS,
)
```

Omitting `namespace` uses the shared pool, which is fine for a single crew. One
server can host many isolated crews.
