"""Ranking behaviour, as a pure function over candidate dicts.

No database and no LLM, so these describe the ranking policy itself.
"""

from jnaapakam.retrieval import rank

NOW = "2026-08-05T12:00:00+00:00"


def candidate(cid, *, lexical=0.0, importance=0.5, created_at="2026-08-05T11:00:00+00:00"):
    return {"id": cid, "lexical": lexical, "importance": importance, "created_at": created_at}


def test_a_strong_content_match_beats_a_recent_unrelated_memory():
    old_but_relevant = candidate(1, lexical=0.9, created_at="2025-01-01T00:00:00+00:00")
    fresh_but_irrelevant = candidate(2, lexical=0.0, created_at=NOW)

    ordered = rank([fresh_but_irrelevant, old_but_relevant], now=NOW)

    assert [c["id"] for c in ordered] == [1, 2]


def test_recency_decides_between_equally_relevant_memories():
    older = candidate(1, lexical=0.5, created_at="2026-01-01T00:00:00+00:00")
    newer = candidate(2, lexical=0.5, created_at="2026-08-05T11:59:00+00:00")

    ordered = rank([older, newer], now=NOW)

    assert [c["id"] for c in ordered] == [2, 1]


def test_importance_decides_between_memories_alike_in_content_and_age():
    dull = candidate(1, lexical=0.5, importance=0.1)
    vital = candidate(2, lexical=0.5, importance=0.9)

    ordered = rank([dull, vital], now=NOW)

    assert [c["id"] for c in ordered] == [2, 1]


def test_ranking_is_stable_when_every_signal_ties():
    items = [candidate(i, lexical=0.4) for i in (3, 1, 2)]

    assert [c["id"] for c in rank(items, now=NOW)] == [3, 1, 2]


def test_a_memory_dated_in_the_future_is_not_scored_above_everything_else():
    """Clock skew between agents must not let one memory dominate retrieval."""
    skewed = candidate(1, lexical=0.1, created_at="2027-01-01T00:00:00+00:00")
    solid = candidate(2, lexical=0.9, created_at=NOW)

    ordered = rank([skewed, solid], now=NOW)

    assert ordered[0]["id"] == 2


def test_ranking_an_empty_candidate_set_yields_nothing():
    assert rank([], now=NOW) == []
