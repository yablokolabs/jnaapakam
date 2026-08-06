"""Detecting contradictions between memories, and resolving them by supersession.

This is the most failure-prone component in the system, and its design is mostly
guardrails:

1. **Off by default.** LLM contradiction judging degrades sharply on small
   non-reasoning models — the class this project defaults to — so it runs only when
   a judge model is configured explicitly.
2. **Deterministic prefilter.** Only same-namespace pairs with real lexical overlap
   reach the model. This bounds cost and removes the obvious false positives before
   any judgement is asked for.
3. **Reasoning before verdict.** A response whose verdict precedes its reasoning is
   rejected as malformed; ordering the schema this way is a measured mitigation for
   weak-model collapse, not a stylistic preference.
4. **Never on the ingest path.** Reconciliation is a background or explicitly
   triggered operation, so a slow or failing judge cannot block a write.
5. **Abstain on doubt.** Below the confidence threshold, nothing happens. Deleting
   a true memory is worse than keeping a stale one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .llm import ExtractionError, extract_json

log = logging.getLogger("jnaapakam.reconcile")

VERDICTS = {"contradicts", "compatible", "duplicate"}

JUDGE_SYSTEM = """You compare two memories about the same subject and decide whether they conflict.

Think first, then decide. Respond with a JSON object whose keys appear in this order:

{
  "reasoning": "what each memory claims, and whether both can be true at once",
  "verdict": "contradicts" | "compatible" | "duplicate",
  "confidence": 0.0-1.0
}

- "contradicts" — both concern the same subject and cannot both be true now.
- "duplicate"   — they state the same thing; the newer should replace the older.
- "compatible"  — they can both be true. This includes facts that differ because
                  they describe different scopes, times, or subjects.

Be conservative. If you are unsure, answer "compatible" with low confidence: wrongly
marking a contradiction destroys a memory that was true. Only output valid JSON."""

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "was", "were", "are", "has",
    "have", "had", "not", "but", "you", "your", "our", "their", "its", "his", "her",
    "they", "them", "there", "here", "then", "than", "into", "onto", "about", "over",
    "user", "agent", "memory", "said", "says", "will", "would", "can", "could",
}


@dataclass
class Verdict:
    reasoning: str
    verdict: str
    confidence: float


def parse_verdict(raw: str) -> Verdict:
    """Parse a judge response, rejecting anything that was not actually reasoned.

    The key-order check is the load-bearing part: a model that emits `verdict`
    before `reasoning` decided first and rationalised afterwards, which is exactly
    the failure mode the reasoning-first schema exists to prevent.
    """
    try:
        data = extract_json(raw)
    except ExtractionError as exc:
        raise ValueError(f"judge response was not JSON: {exc}") from exc

    keys = list(data.keys())
    if "reasoning" not in keys:
        raise ValueError("judge response has no reasoning; refusing to act on a bare verdict")
    if "verdict" not in keys:
        raise ValueError("judge response has no verdict")
    if keys.index("reasoning") > keys.index("verdict"):
        raise ValueError("judge emitted its verdict before its reasoning; treating as unreasoned")

    reasoning = str(data.get("reasoning") or "").strip()
    if not reasoning:
        raise ValueError("judge reasoning was empty")

    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"unrecognised verdict: {verdict!r}")

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        raise ValueError("confidence was not a number") from None

    return Verdict(reasoning=reasoning, verdict=verdict, confidence=max(0.0, min(1.0, confidence)))


def significant_tokens(text: str) -> set[str]:
    """Content words only — the shared vocabulary that makes two memories comparable."""
    return {
        token
        for token in re.findall(r"\w+", (text or "").lower())
        if len(token) >= 3 and token not in STOPWORDS and not token.isdigit()
    }


def overlap(a: str, b: str) -> float:
    """Jaccard similarity over content words, in [0, 1]."""
    ta, tb = significant_tokens(a), significant_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _text_of(memory: dict) -> str:
    return f"{memory.get('summary', '')} {memory.get('raw_text', '')}"


class Reconciler:
    def __init__(self, store, config, chat):
        self.store = store
        self.config = config
        self.chat = chat

    def _pairs(self, memories: list[dict]) -> list[tuple[dict, dict]]:
        """Older/newer pairs that share enough vocabulary to be worth judging."""
        ordered = sorted(memories, key=lambda m: (m["created_at"], m["id"]))
        pairs = []
        for index, newer in enumerate(ordered):
            for older in ordered[:index]:
                if older["namespace"] != newer["namespace"]:
                    continue
                if overlap(_text_of(older), _text_of(newer)) < self.config.reconcile_min_overlap:
                    continue
                pairs.append((older, newer))
        # Judge the most recent candidates first, then cap: a dense cluster of
        # similar memories is quadratic, and an uncapped cycle would be unaffordable.
        pairs.sort(key=lambda p: (p[1]["created_at"], p[1]["id"]), reverse=True)
        return pairs[: self.config.reconcile_max_comparisons]

    async def _judge(self, older: dict, newer: dict) -> Verdict:
        message = (
            f"Memory A (older, #{older['id']}, recorded {older['created_at'][:10]}):\n"
            f"{older['summary']}\n\n"
            f"Memory B (newer, #{newer['id']}, recorded {newer['created_at'][:10]}):\n"
            f"{newer['summary']}"
        )
        return parse_verdict(await self.chat(self.config.judge_model, JUDGE_SYSTEM, message))

    async def run(self, namespace: str = "", limit: int = 50) -> dict:
        if not self.config.judge_model:
            return {
                "status": "disabled",
                "reason": (
                    "No judge model configured. Contradiction detection is unreliable on small "
                    "models, so it stays off until MEMORY_JUDGE_MODEL names one explicitly."
                ),
            }

        memories = self.store.active_memories(namespace=namespace, limit=limit)
        pairs = self._pairs(memories)

        superseded, compared, errors = [], 0, 0
        # Once a memory is superseded it drops out of consideration, so a chain of
        # corrections resolves to the newest without re-judging retired links.
        retired: set[int] = set()

        for older, newer in pairs:
            if older["id"] in retired or newer["id"] in retired:
                continue
            compared += 1
            try:
                verdict = await self._judge(older, newer)
            except ValueError as exc:
                log.warning("Unusable judge response for #%s vs #%s: %s", older["id"], newer["id"], exc)
                errors += 1
                continue
            except Exception as exc:
                log.error("Judge call failed for #%s vs #%s: %s", older["id"], newer["id"], exc)
                errors += 1
                continue

            if verdict.verdict not in ("contradicts", "duplicate"):
                continue
            if verdict.confidence < self.config.reconcile_min_confidence:
                log.info(
                    "Abstaining on #%s vs #%s: %s at confidence %.2f",
                    older["id"], newer["id"], verdict.verdict, verdict.confidence,
                )
                continue

            if self.store.supersede(older["id"], newer["id"]):
                retired.add(older["id"])
                superseded.append(
                    {
                        "old_id": older["id"],
                        "new_id": newer["id"],
                        "verdict": verdict.verdict,
                        "confidence": verdict.confidence,
                        "reasoning": verdict.reasoning,
                    }
                )

        return {
            "status": "reconciled",
            "namespace": namespace,
            "compared": compared,
            "superseded": superseded,
            "errors": errors,
        }
