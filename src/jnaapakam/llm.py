"""Multi-provider LLM client and structured-output extraction.

Deliberately raw HTTP rather than a vendor SDK: the same code path serves
Anthropic, OpenAI, and any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio),
which is what keeps the dependency list at one entry.
"""

from __future__ import annotations

import json
import logging
import os
import re

import aiohttp

log = logging.getLogger("jnaapakam.llm")

ANTHROPIC_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_BASE = "https://api.openai.com/v1"

# Anthropic retires model IDs on a published schedule and a retired ID returns 404.
# v0.1 shipped claude-3-haiku-20240307 here; it retired 2026-04-19, which meant every
# default install silently stored degraded memories. tests/test_model_selection.py
# guards against a repeat.
MODEL_ALIASES = {
    "haiku": ("anthropic", "claude-haiku-4-5"),
    "sonnet": ("anthropic", "claude-sonnet-5"),
    "opus": ("anthropic", "claude-opus-5"),
    "gpt4mini": ("openai", "gpt-4.1-mini"),
    "gpt4": ("openai", "gpt-4.1"),
    "default": ("anthropic", "claude-haiku-4-5"),
}


class ExtractionError(ValueError):
    """The model's reply did not contain usable structured output."""


class LLMError(RuntimeError):
    """The provider could not be reached, or refused the request."""


def extract_json(text: str) -> dict:
    """Recover a JSON object from a model reply.

    Handles the shapes real models actually emit: bare JSON, fenced blocks
    (labelled or not, any case), and payloads wrapped in prose. Raises rather
    than returning a default — a caller that cannot see the failure will store a
    degraded memory and report success.
    """
    if not text or not text.strip():
        raise ExtractionError("empty response")

    candidates = [text.strip()]

    fenced = re.findall(r"```[a-zA-Z]*\s*\n?(.*?)```", text, re.DOTALL)
    candidates = [block.strip() for block in fenced] + candidates

    # Last resort: the outermost {...} span anywhere in the reply.
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ExtractionError(f"no JSON object found in response: {text[:200]!r}")


def resolve_model(model: str) -> tuple[str, str, str, str]:
    """Map a model alias or name to (provider, model_name, base_url, api_key)."""
    custom_base = os.getenv("LLM_BASE_URL")
    if custom_base:
        # The aliases below name cloud models. A self-hosted server or proxy has its own
        # names, so the model string is passed through exactly as written — and `default`,
        # which names nothing there, is refused rather than 404'd on every ingest.
        if model == "default":
            raise LLMError(
                "LLM_BASE_URL is set but no model was named. A custom endpoint has its own "
                "model names: set MEMORY_MODEL (or --model) to one it serves."
            )
        return "openai", model, custom_base, os.getenv("LLM_API_KEY", "")

    if model in MODEL_ALIASES:
        provider, model_name = MODEL_ALIASES[model]
    elif model.startswith("claude"):
        provider, model_name = "anthropic", model
    elif model.startswith("gpt"):
        provider, model_name = "openai", model
    else:
        # Unknown names belong to whatever OpenAI-compatible endpoint is configured.
        provider, model_name = "openai", model

    if provider == "anthropic":
        return "anthropic", model_name, ANTHROPIC_BASE, os.getenv("ANTHROPIC_API_KEY", "")
    return "openai", model_name, OPENAI_BASE, os.getenv("OPENAI_API_KEY", "")


async def _chat_anthropic(session, model, system, message, base_url, api_key) -> str:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 2000,
        "system": system,
        "messages": [{"role": "user", "content": message}],
    }
    async with session.post(f"{base_url}/messages", headers=headers, json=payload) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise LLMError(f"Anthropic {resp.status} for model {model}: {body[:300]}")
        data = json.loads(body)
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


async def _chat_openai(session, model, system, message, base_url, api_key) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ],
        "temperature": 0.3,
        "max_tokens": 2000,
    }
    async with session.post(f"{base_url}/chat/completions", headers=headers, json=payload) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise LLMError(f"OpenAI-compatible {resp.status} for model {model}: {body[:300]}")
        return json.loads(body)["choices"][0]["message"]["content"]


async def embed(texts: list[str], model: str) -> list[list[float]]:
    """Embed texts through an OpenAI-compatible /embeddings endpoint.

    Anthropic publishes no embeddings API, so this deliberately has no Anthropic
    branch: an embedding model name routes to OPENAI_BASE, or to LLM_BASE_URL when
    a custom endpoint is configured. Ollama, vLLM and LM Studio all serve this
    shape, so a local embedder needs no special case.
    """
    if not texts:
        return []
    base_url = os.getenv("LLM_BASE_URL") or OPENAI_BASE
    api_key = os.getenv("LLM_API_KEY", "") if os.getenv("LLM_BASE_URL") else os.getenv(
        "OPENAI_API_KEY", ""
    )
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with aiohttp.ClientSession() as session, session.post(
        f"{base_url}/embeddings",
        headers=headers,
        json={"model": model, "input": texts},
    ) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise LLMError(f"Embeddings {resp.status} for model {model}: {body[:300]}")
        data = json.loads(body)["data"]
    # The index field is authoritative: the spec permits results out of order.
    return [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]


async def chat(model: str, system: str, message: str) -> str:
    """Send one turn, falling back to the other provider if the first is unreachable."""
    provider, model_name, base_url, api_key = resolve_model(model)

    if not api_key and not os.getenv("LLM_BASE_URL"):
        raise LLMError(
            "No API key found. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or LLM_BASE_URL + LLM_API_KEY."
        )

    async with aiohttp.ClientSession() as session:
        try:
            if provider == "anthropic":
                return await _chat_anthropic(session, model_name, system, message, base_url, api_key)
            return await _chat_openai(session, model_name, system, message, base_url, api_key)
        except Exception as primary_error:
            # A custom endpoint is usually chosen to keep memory content off third-party
            # infrastructure. Retrying against a cloud provider would send it there anyway.
            if os.getenv("LLM_BASE_URL"):
                raise
            fallback_key = os.getenv("OPENAI_API_KEY" if provider == "anthropic" else "ANTHROPIC_API_KEY")
            if not fallback_key:
                raise
            log.warning("Primary provider (%s) failed, trying fallback: %s", provider, primary_error)
            if provider == "anthropic":
                return await _chat_openai(
                    session, MODEL_ALIASES["gpt4mini"][1], system, message, OPENAI_BASE, fallback_key
                )
            return await _chat_anthropic(
                session, MODEL_ALIASES["haiku"][1], system, message, ANTHROPIC_BASE, fallback_key
            )
