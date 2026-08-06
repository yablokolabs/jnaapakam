"""Guard against shipping a model ID that the provider has withdrawn.

v0.1 shipped `claude-3-haiku-20240307` as both the default alias and the
Anthropic fallback. It retired on 2026-04-19, so every default install had been
404ing on ingest — and the bare `except` reported those failures as success.
"""

import pytest

from jnaapakam.llm import MODEL_ALIASES, resolve_model

# Withdrawn by Anthropic; a request naming any of these returns 404.
RETIRED = {
    "claude-3-haiku-20240307",
    "claude-3-opus-20240229",
    "claude-3-sonnet-20240229",
    "claude-3-5-sonnet-20240620",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-7-sonnet-20250219",
    "claude-2.1",
    "claude-2.0",
}


@pytest.mark.parametrize("alias", sorted(MODEL_ALIASES))
def test_no_shipped_alias_points_at_a_withdrawn_model(alias):
    _, model_name = MODEL_ALIASES[alias]

    assert model_name not in RETIRED


def test_the_out_of_the_box_default_resolves_to_a_live_model():
    _, model_name, _, _ = resolve_model("default")

    assert model_name not in RETIRED


def test_an_unknown_alias_is_passed_through_so_custom_endpoints_still_work():
    """Ollama and other OpenAI-compatible servers use their own model names."""
    _, model_name, _, _ = resolve_model("llama3.3:70b")

    assert model_name == "llama3.3:70b"


def test_an_explicit_anthropic_model_name_routes_to_anthropic():
    provider, model_name, _, _ = resolve_model("claude-sonnet-5")

    assert provider == "anthropic"
    assert model_name == "claude-sonnet-5"
