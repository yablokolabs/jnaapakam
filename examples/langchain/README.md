# jñāpakaṁ + LangChain

## Setup

```python
import requests
from langchain.tools import Tool

MEMORY_URL = "http://localhost:8889"

# Query tool
memory_query = Tool(
    name="query_memory",
    description="Query the agent's persistent memory for past context and knowledge",
    func=lambda q: requests.get(f"{MEMORY_URL}/query", params={"q": q}).json()["answer"]
)

# Ingest tool
def remember(text: str, source: str = "langchain") -> str:
    resp = requests.post(f"{MEMORY_URL}/ingest", json={"text": text, "source": source})
    return resp.json().get("summary", "stored")

memory_ingest = Tool(
    name="remember",
    description="Store important information in persistent memory",
    func=remember
)

# Add to your agent
from langchain.agents import initialize_agent

agent = initialize_agent(
    tools=[memory_query, memory_ingest, ...],
    llm=your_llm,
)
```
