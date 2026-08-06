"""Shared fixtures.

The LLM stand-in here is a *real* implementation with deterministic rules, not a
mock. Tests assert on stored and retrieved state, never on whether a prompt was
constructed or a function was called.
"""

import itertools
import json
import re

import pytest

from jnaapakam.config import Config
from jnaapakam.store import Store

STOPWORDS = {"the", "a", "an", "and", "or", "is", "are", "to", "for", "of", "in", "on", "user"}


def rule_based_llm(system: str, message: str) -> str:
    """Extract structure using deterministic rules.

    Behaves like the real ingest/consolidate/query agents — same output contract,
    no network. Real logic, so tests exercise real parsing and storage paths.
    """
    if "Memory Ingest Agent" in system:
        body = message.split("\n\n", 1)[-1]
        words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9+.-]{2,}", body)]
        entities = sorted({w for w in words if w not in STOPWORDS})[:4]
        return json.dumps(
            {
                "summary": body.strip()[:120],
                "entities": entities,
                "topics": entities[:2],
                "importance": min(1.0, 0.3 + 0.1 * len(entities)),
            }
        )
    if "Memory Consolidation Agent" in system:
        ids = [int(n) for n in re.findall(r"Memory #(\d+)", message)]
        return json.dumps(
            {
                "summary": f"consolidated {len(ids)} memories",
                "insight": "recurring themes across the set",
                "connections": [
                    {"from_id": a, "to_id": b, "relationship": "co-occurs"}
                    for a, b in itertools.pairwise(ids)
                ],
            }
        )
    return "Based on the provided memories: " + message.strip()[-200:]


@pytest.fixture
def llm():
    async def _chat(model, system, message):
        return rule_based_llm(system, message)

    return _chat


@pytest.fixture
def config(tmp_path):
    return Config(db_path=str(tmp_path / "memory.db"), auth_token=None, host="127.0.0.1", port=0)


@pytest.fixture
def store(config):
    s = Store(config.db_path)
    s.initialize()
    yield s
    s.close()
