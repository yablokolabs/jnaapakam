"""Ranking policy.

A pure function over candidate dicts: no database, no LLM, no clock. Everything
it needs is passed in, so the policy is testable on its own and reusable by any
storage backend.
"""

from __future__ import annotations

from datetime import datetime, timezone

DEFAULT_WEIGHTS = {"relevance": 0.6, "recency": 0.25, "importance": 0.15}
DEFAULT_HALFLIFE_DAYS = 30.0


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


def score(
    candidate: dict,
    now,
    weights: dict | None = None,
    halflife_days: float = DEFAULT_HALFLIFE_DAYS,
) -> float:
    w = weights or DEFAULT_WEIGHTS
    return (
        w["relevance"] * float(candidate.get("lexical") or 0.0)
        + w["recency"] * recency_score(candidate.get("created_at"), now, halflife_days)
        + w["importance"] * float(candidate.get("importance") or 0.0)
    )


def rank(
    candidates: list[dict],
    now,
    weights: dict | None = None,
    halflife_days: float = DEFAULT_HALFLIFE_DAYS,
    limit: int | None = None,
) -> list[dict]:
    """Order candidates best-first, attaching the score that produced the order.

    Ties preserve input order, so callers can make ordering deterministic by
    controlling what they pass in.
    """
    scored = []
    for candidate in candidates:
        enriched = dict(candidate)
        enriched["score"] = score(candidate, now, weights, halflife_days)
        scored.append(enriched)
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:limit] if limit is not None else scored
