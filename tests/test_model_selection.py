"""Guard against shipping a model ID that the provider has withdrawn.

v0.1 shipped `claude-3-haiku-20240307` as both the default alias and the
Anthropic fallback. It retired on 2026-04-19, so every default install had been
404ing on ingest — and the bare `except` reported those failures as success.
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from jnaapakam import llm
from jnaapakam.llm import MODEL_ALIASES, LLMError, resolve_model

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


def test_a_custom_endpoint_gets_the_model_name_as_written(monkeypatch):
    """Aliases name cloud models; a proxy or local server has its own names."""
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")

    provider, model_name, base_url, _ = resolve_model("haiku")

    assert (provider, model_name, base_url) == ("openai", "haiku", "http://localhost:11434/v1")


def test_a_custom_endpoint_refuses_the_unconfigured_default(monkeypatch):
    """`default` is a cloud alias: sending claude-haiku-4-5 to Ollama only 404s."""
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")

    with pytest.raises(LLMError, match="MEMORY_MODEL"):
        resolve_model("default")


async def test_a_custom_endpoint_failure_never_falls_back_to_the_cloud(monkeypatch):
    """A self-hosted endpoint is chosen for privacy; a silent cloud retry leaks the memory."""
    calls = []

    async def handler(request):
        calls.append(request.path)
        return web.Response(status=500, text="model unavailable")

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    app.router.add_post("/v1/messages", handler)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("/v1"))

    monkeypatch.setenv("LLM_BASE_URL", base)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-be-used")
    # Point the cloud fallback at the same server, so a leak shows up as a request
    # here instead of leaving the machine.
    monkeypatch.setattr(llm, "ANTHROPIC_BASE", base)

    try:
        with pytest.raises(LLMError):
            await llm.chat("llama3.1", "system", "message")
    finally:
        await server.close()

    assert calls == ["/v1/chat/completions"]
