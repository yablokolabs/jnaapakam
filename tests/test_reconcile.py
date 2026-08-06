"""Contradiction detection driving supersession.

The research behind this phase was blunt about the failure modes, and the tests
below exist to pin each one:

- LLM contradiction judging collapses on small non-reasoning models — the class
  this project defaults to. So reconciliation is *off* unless a judge model is
  explicitly configured.
- A reasoning-before-verdict response schema measurably rescues weak models, so a
  verdict arriving without reasoning is treated as malformed rather than obeyed.
- The hard part is not detection but discrimination: a scope difference is not a
  contradiction.
- It must never run on the ingest hot path.
"""

import pytest

from jnaapakam.config import Config
from jnaapakam.reconcile import Reconciler, parse_verdict


def _ingest(store, text, namespace="", **kw):
    return store.add_memory(
        raw_text=text,
        summary=kw.get("summary", text[:120]),
        entities=kw.get("entities", []),
        topics=[],
        importance=kw.get("importance", 0.5),
        source="test",
        namespace=namespace,
    )


def judge(verdict="compatible", confidence=0.9, reasoning="the two statements concern the same subject"):
    """A judge that returns a fixed, well-formed verdict."""

    async def _chat(model, system, message):
        import json

        return json.dumps(
            {"reasoning": reasoning, "verdict": verdict, "confidence": confidence}
        )

    return _chat


def judging_config(config, **kw):
    return Config(
        db_path=config.db_path,
        auth_token=None,
        host="127.0.0.1",
        port=0,
        judge_model=kw.pop("judge_model", "sonnet"),
        **kw,
    )


# ---- verdict parsing ---------------------------------------------------


def test_a_well_formed_verdict_is_accepted():
    result = parse_verdict(
        '{"reasoning": "same subject, opposite claims", "verdict": "contradicts", "confidence": 0.9}'
    )

    assert result.verdict == "contradicts"
    assert result.confidence == 0.9


def test_a_verdict_without_reasoning_is_rejected():
    """Weak models skip straight to a verdict; that output is not trustworthy."""
    with pytest.raises(ValueError):
        parse_verdict('{"verdict": "contradicts", "confidence": 0.95}')


def test_a_verdict_with_empty_reasoning_is_rejected():
    with pytest.raises(ValueError):
        parse_verdict('{"reasoning": "   ", "verdict": "contradicts", "confidence": 0.9}')


def test_an_unrecognised_verdict_value_is_rejected():
    with pytest.raises(ValueError):
        parse_verdict('{"reasoning": "unsure", "verdict": "maybe?", "confidence": 0.5}')


def test_reasoning_must_precede_the_verdict_in_the_response():
    """Ordering is the mechanism, not decoration — a verdict written first was not reasoned to."""
    with pytest.raises(ValueError):
        parse_verdict(
            '{"verdict": "contradicts", "reasoning": "decided after", "confidence": 0.9}'
        )


# ---- gating ------------------------------------------------------------


async def test_reconciliation_is_disabled_unless_a_judge_model_is_configured(store, config):
    _ingest(store, "the deadline is March 15")
    _ingest(store, "the deadline is April 2")

    result = await Reconciler(store, config, chat=judge("contradicts")).run()

    assert result["status"] == "disabled"
    assert store.search("deadline")[0]["superseded_by"] is None


async def test_configuring_a_judge_model_enables_reconciliation(store, config):
    _ingest(store, "the deadline is March 15")
    _ingest(store, "the deadline is April 2")

    result = await Reconciler(store, judging_config(config), chat=judge("contradicts")).run()

    assert result["status"] != "disabled"


# ---- the prefilter -----------------------------------------------------


async def test_unrelated_memories_never_reach_the_judge(store, config):
    """The deterministic prefilter is what keeps this affordable and precise."""
    calls = []

    async def counting_judge(model, system, message):
        calls.append(message)
        return await judge("compatible")(model, system, message)

    _ingest(store, "the deadline for the rust CLI is March 15")
    _ingest(store, "the office coffee machine is broken again")

    await Reconciler(store, judging_config(config), chat=counting_judge).run()

    assert calls == [], "memories with no lexical overlap should not cost an LLM call"


async def test_memories_in_different_namespaces_never_reach_the_judge(store, config):
    """Conflict precision: the same claim in two projects is not a conflict."""
    calls = []

    async def counting_judge(model, system, message):
        calls.append(message)
        return await judge("contradicts")(model, system, message)

    a = _ingest(store, "the preferred editor is vim", namespace="project-a")
    b = _ingest(store, "the preferred editor is VS Code", namespace="project-b")

    await Reconciler(store, judging_config(config), chat=counting_judge).run(namespace="project-a")

    assert calls == []
    assert store.get_memory(a)["superseded_by"] is None
    assert store.get_memory(b)["superseded_by"] is None


# ---- resolution --------------------------------------------------------


async def test_a_confirmed_contradiction_supersedes_the_older_memory(store, config):
    old = _ingest(store, "the deadline for the rust CLI is March 15")
    new = _ingest(store, "the deadline for the rust CLI moved to April 2")

    await Reconciler(store, judging_config(config), chat=judge("contradicts", 0.9)).run()

    assert store.get_memory(old)["superseded_by"] == new
    assert store.get_memory(new)["superseded_by"] is None


async def test_a_compatible_verdict_leaves_both_memories_active(store, config):
    a = _ingest(store, "the rust CLI deadline is March 15")
    b = _ingest(store, "the rust CLI has a new logging subsystem")

    await Reconciler(store, judging_config(config), chat=judge("compatible")).run()

    assert store.get_memory(a)["superseded_by"] is None
    assert store.get_memory(b)["superseded_by"] is None


async def test_a_low_confidence_contradiction_is_left_alone(store, config):
    """Destroying a true memory is worse than keeping a stale one."""
    old = _ingest(store, "the deadline for the rust CLI is March 15")
    _ingest(store, "the deadline for the rust CLI moved to April 2")

    await Reconciler(store, judging_config(config), chat=judge("contradicts", 0.3)).run()

    assert store.get_memory(old)["superseded_by"] is None


async def test_a_malformed_judge_response_changes_nothing(store, config):
    old = _ingest(store, "the deadline for the rust CLI is March 15")
    _ingest(store, "the deadline for the rust CLI moved to April 2")

    async def broken_judge(model, system, message):
        return "I think they might conflict, but I'm not sure."

    result = await Reconciler(store, judging_config(config), chat=broken_judge).run()

    assert store.get_memory(old)["superseded_by"] is None
    assert result["errors"] >= 1, "an unusable judge response must be reported, not swallowed"


async def test_the_newer_memory_always_wins_regardless_of_comparison_order(store, config):
    old = _ingest(store, "the deadline for the rust CLI is March 15")
    new = _ingest(store, "the deadline for the rust CLI moved to April 2")

    await Reconciler(store, judging_config(config), chat=judge("contradicts", 0.9)).run()

    assert store.get_memory(old)["valid_to"] is not None
    assert store.get_memory(new)["valid_to"] is None


async def test_an_already_superseded_memory_is_not_judged_again(store, config):
    calls = []

    async def counting_judge(model, system, message):
        calls.append(message)
        return await judge("contradicts", 0.9)(model, system, message)

    old = _ingest(store, "the deadline for the rust CLI is March 15")
    new = _ingest(store, "the deadline for the rust CLI moved to April 2")
    store.supersede(old, new)

    await Reconciler(store, judging_config(config), chat=counting_judge).run()

    assert calls == [], "resolved contradictions should not be re-judged on every cycle"


# ---- cost ---------------------------------------------------------------


async def test_the_number_of_judge_calls_is_bounded(store, config):
    """Without a cap, a dense cluster of similar memories is quadratic."""
    calls = []

    async def counting_judge(model, system, message):
        calls.append(message)
        return await judge("compatible")(model, system, message)

    for i in range(12):
        _ingest(store, f"the rust CLI deadline discussion note number {i}")

    cfg = judging_config(config)
    await Reconciler(store, cfg, chat=counting_judge).run()

    assert len(calls) <= cfg.reconcile_max_comparisons
