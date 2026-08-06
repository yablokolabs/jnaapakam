"""Extracting structured output from real-world LLM replies.

v0.1 used `response.strip().strip("```json").strip("```")`, which strips a
*character set* rather than a suffix. It happened to survive a clean fence and
failed on every imperfect one — and the bare `except` around it then stored a
degraded memory while reporting success.
"""

import pytest

from jnaapakam.llm import ExtractionError, extract_json

WELL_FORMED = '{"summary": "ok", "entities": ["vim"], "topics": ["tools"], "importance": 0.6}'


@pytest.mark.parametrize(
    "reply",
    [
        WELL_FORMED,
        f"```json\n{WELL_FORMED}\n```",
        f"```JSON\n{WELL_FORMED}\n```",
        f"```\n{WELL_FORMED}\n```",
        f"Here is the JSON you asked for:\n{WELL_FORMED}",
        f"```json\n{WELL_FORMED}\n```\nHope that helps!",
        f"  \n\t{WELL_FORMED}\n\n  ",
    ],
    ids=["bare", "fenced", "upper-fence", "unlabelled-fence", "preamble", "trailing-prose", "whitespace"],
)
def test_structured_payload_is_recovered_from_realistic_replies(reply):
    assert extract_json(reply)["summary"] == "ok"


def test_content_that_merely_looks_like_fencing_is_preserved():
    """The old character-set strip would eat leading/trailing j, s, o, n and backticks."""
    payload = '{"summary": "json", "entities": ["nodejs"], "topics": ["json"], "importance": 0.4}'

    assert extract_json(payload)["summary"] == "json"
    assert extract_json(payload)["entities"] == ["nodejs"]


def test_a_reply_with_no_json_raises_rather_than_returning_a_default():
    """Silent degradation is the real defect — a caller must be able to see the failure."""
    with pytest.raises(ExtractionError):
        extract_json("I'm sorry, I can't help with that request.")


def test_truncated_json_raises_rather_than_returning_partial_data():
    with pytest.raises(ExtractionError):
        extract_json('{"summary": "cut off mid')
