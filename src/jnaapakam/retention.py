"""How worth keeping a memory is.

A composite of importance, access frequency, and temporal decay, after MemoryBank
(Zhong et al., 2024). Attribution matters here: the agent-memory literature
describes this *family* of scores but publishes no agreed weights or half-life, so
the constants below are defaults to tune, not findings.

The frequency term is the point. `importance` is assigned once by an LLM at ingest
and never revised, so it cannot tell a memory that proved useful from one that
merely sounded important; `access_count` is grounded in outcomes.
"""

from __future__ import annotations

from .retrieval import recency_score

DEFAULT_WEIGHTS = {"importance": 0.35, "frequency": 0.40, "recency": 0.25}
DEFAULT_HALFLIFE_DAYS = 60.0

# Access counts are unbounded and long-tailed, so they are squashed rather than
# normalised against the maximum in the set — otherwise one heavily-used memory
# flattens every other memory's frequency term to nearly zero.
FREQUENCY_SATURATION = 5.0


def frequency_score(access_count: int, saturation: float = FREQUENCY_SATURATION) -> float:
    """Diminishing returns on recall count, mapped to [0, 1)."""
    count = max(0, int(access_count or 0))
    return count / (count + saturation)


def retention_score(
    memory: dict,
    now,
    weights: dict | None = None,
    halflife_days: float = DEFAULT_HALFLIFE_DAYS,
) -> float:
    """Score a memory for retention. Higher survives longer."""
    w = weights or DEFAULT_WEIGHTS
    # A memory that has been recalled is measured from that recall, not from when
    # it happened to be written — otherwise steady use never refreshes it.
    reference = memory.get("last_accessed") or memory.get("created_at")
    return (
        w["importance"] * float(memory.get("importance") or 0.0)
        + w["frequency"] * frequency_score(memory.get("access_count"))
        + w["recency"] * recency_score(reference, now, halflife_days)
    )
