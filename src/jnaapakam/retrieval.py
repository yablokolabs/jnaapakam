"""Ranking policy.

A pure function over candidate dicts: no database, no LLM, no clock. Everything
it needs is passed in, so the policy is testable on its own and reusable by any
storage backend.
"""

from __future__ import annotations

from datetime import datetime, timezone

DEFAULT_WEIGHTS = {"relevance": 0.6, "recency": 0.25, "importance": 0.15}
DEFAULT_HALFLIFE_DAYS = 30.0

# How much of the relevance term comes from meaning rather than words, when a
# semantic score is present at all. Half and half: BM25 is precise about the words
# an operator actually typed, embeddings are right about what they meant, and
# neither is trustworthy enough alone to be worth silencing the other.
DEFAULT_SEMANTIC_WEIGHT = 0.5


def _parse_time(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def recency_score(created_at, now, halflife_days: float = DEFAULT_HALFLIFE_DAYS) -> float:
    """Exponential decay on age, clamped so a future timestamp cannot outrank the present.

    Without the clamp, an agent whose clock is ahead could park a memory at the
    top of every result set indefinitely.
    """
    created = _parse_time(created_at)
    reference = _parse_time(now)
    if created is None or reference is None:
        return 0.0
    age_days = max(0.0, (reference - created).total_seconds() / 86400.0)
    return 0.5 ** (age_days / halflife_days)


def relevance_score(candidate: dict, semantic_weight: float = DEFAULT_SEMANTIC_WEIGHT) -> float:
    """Blend lexical and semantic relevance, using whichever are present.

    A candidate with no `semantic` key scores exactly as it did before embeddings
    existed — the blend only applies where there is something to blend.
    """
    lexical = float(candidate.get("lexical") or 0.0)
    if candidate.get("semantic") is None:
        return lexical
    semantic = max(0.0, min(1.0, float(candidate["semantic"])))
    return (1.0 - semantic_weight) * lexical + semantic_weight * semantic


def score(
    candidate: dict,
    now,
    weights: dict | None = None,
    halflife_days: float = DEFAULT_HALFLIFE_DAYS,
    semantic_weight: float = DEFAULT_SEMANTIC_WEIGHT,
) -> float:
    w = weights or DEFAULT_WEIGHTS
    return (
        w["relevance"] * relevance_score(candidate, semantic_weight)
        + w["recency"] * recency_score(candidate.get("created_at"), now, halflife_days)
        + w["importance"] * float(candidate.get("importance") or 0.0)
    )


def rank(
    candidates: list[dict],
    now,
    weights: dict | None = None,
    halflife_days: float = DEFAULT_HALFLIFE_DAYS,
    limit: int | None = None,
    semantic_weight: float = DEFAULT_SEMANTIC_WEIGHT,
) -> list[dict]:
    """Order candidates best-first, attaching the score that produced the order.

    Ties preserve input order, so callers can make ordering deterministic by
    controlling what they pass in.
    """
    scored = []
    for candidate in candidates:
        enriched = dict(candidate)
        enriched["score"] = score(candidate, now, weights, halflife_days, semantic_weight)
        scored.append(enriched)
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:limit] if limit is not None else scored
