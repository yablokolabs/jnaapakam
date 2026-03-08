# jñāpakaṁ + CrewAI

## Setup

```python
import requests
from crewai import Agent, Task, Crew
from crewai.tools import tool

MEMORY_URL = "http://localhost:8889"

@tool("Query Memory")
def query_memory(question: str) -> str:
    """Query persistent agent memory for past context and knowledge."""
    return requests.get(f"{MEMORY_URL}/query", params={"q": question}).json()["answer"]

@tool("Remember")
def remember(text: str) -> str:
    """Store important information in persistent memory."""
    resp = requests.post(f"{MEMORY_URL}/ingest", json={"text": text, "source": "crewai"})
    return resp.json().get("summary", "stored")

# Create an agent with persistent memory
researcher = Agent(
    role="Research Analyst",
    goal="Analyze data and remember findings",
    tools=[query_memory, remember],
    backstory="You are a researcher with persistent memory across sessions.",
)

# The agent can now remember across crew runs
task = Task(
    description="Research the topic and store key findings in memory",
    agent=researcher,
)

crew = Crew(agents=[researcher], tasks=[task])
crew.kickoff()
```
