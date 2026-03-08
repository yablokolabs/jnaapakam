"""
jñāpakaṁ LLM Client — Multi-provider with automatic fallback.

Supports:
  - Anthropic (ANTHROPIC_API_KEY)
  - OpenAI (OPENAI_API_KEY)
  - Any OpenAI-compatible API (LLM_BASE_URL + LLM_API_KEY)

Model aliases: haiku, sonnet, gpt4mini, gpt4, default
"""

import logging
import os

import aiohttp

log = logging.getLogger("jnaapakam.llm")

# ─── Provider configs ─────────────────────────────────────────

ANTHROPIC_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_BASE = "https://api.openai.com/v1"

MODEL_ALIASES = {
    # Anthropic
    "haiku": ("anthropic", "claude-3-haiku-20240307"),
    "sonnet": ("anthropic", "claude-sonnet-4-20250514"),
    # OpenAI
    "gpt4mini": ("openai", "gpt-4.1-mini"),
    "gpt4": ("openai", "gpt-4.1"),
    # Default
    "default": ("anthropic", "claude-3-haiku-20240307"),
}


def _detect_provider(model: str) -> tuple[str, str, str, str]:
    """Returns (provider, model_name, base_url, api_key)."""
    # Check aliases first
    if model in MODEL_ALIASES:
        provider, model_name = MODEL_ALIASES[model]
    elif model.startswith("claude"):
        provider, model_name = "anthropic", model
    elif model.startswith("gpt"):
        provider, model_name = "openai", model
    else:
        provider, model_name = "openai", model  # assume OpenAI-compatible

    # Custom provider override
    custom_base = os.getenv("LLM_BASE_URL")
    custom_key = os.getenv("LLM_API_KEY")
    if custom_base:
        return "openai", model_name, custom_base, custom_key or ""

    if provider == "anthropic":
        key = os.getenv("ANTHROPIC_API_KEY", "")
        return "anthropic", model_name, ANTHROPIC_BASE, key
    else:
        key = os.getenv("OPENAI_API_KEY", "")
        return "openai", model_name, OPENAI_BASE, key


async def _chat_anthropic(model: str, system: str, message: str, base_url: str, api_key: str) -> str:
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
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{base_url}/messages", headers=headers, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Anthropic {resp.status}: {text[:200]}")
            data = await resp.json()
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


async def _chat_openai(model: str, system: str, message: str, base_url: str, api_key: str) -> str:
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
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{base_url}/chat/completions", headers=headers, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"OpenAI-compat {resp.status}: {text[:200]}")
            data = await resp.json()
            return data["choices"][0]["message"]["content"]


async def chat(model: str, system: str, message: str) -> str:
    """Send a chat message. Auto-detects provider from model name or env vars."""
    provider, model_name, base_url, api_key = _detect_provider(model)

    if not api_key and not os.getenv("LLM_BASE_URL"):
        raise RuntimeError(
            "No API key found. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or LLM_BASE_URL + LLM_API_KEY"
        )

    try:
        if provider == "anthropic":
            return await _chat_anthropic(model_name, system, message, base_url, api_key)
        else:
            return await _chat_openai(model_name, system, message, base_url, api_key)
    except Exception as e:
        # Try fallback: if primary was Anthropic, try OpenAI and vice versa
        fallback_key = os.getenv("OPENAI_API_KEY" if provider == "anthropic" else "ANTHROPIC_API_KEY")
        if fallback_key:
            log.warning(f"Primary ({provider}) failed, trying fallback: {e}")
            if provider == "anthropic":
                fallback_model = "gpt-4.1-mini"
                return await _chat_openai(fallback_model, system, message, OPENAI_BASE, fallback_key)
            else:
                fallback_model = "claude-3-haiku-20240307"
                return await _chat_anthropic(fallback_model, system, message, ANTHROPIC_BASE, fallback_key)
        raise
