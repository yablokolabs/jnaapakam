# Contributing to jñāpakaṁ

Thanks for your interest in contributing! Here's how to get started.

## Areas We Need Help With

- 🏷️ **Multi-agent namespacing** — the largest open design question; see PROTOCOL.md §6
- 🧬 **Signed continuity records** — v0.3 gives integrity, not authenticity; see PROTOCOL.md §10.6
- 🔌 **Integration examples** — more framework integrations (AutoGen, Semantic Kernel, ADK, etc.)
- 🤖 **LLM providers** — additional backend support (Groq, Together, Mistral, local models)
- 📊 **Memory visualization** — dashboard or CLI tools to browse memories
- 🔒 **Security** — encryption at rest
- 📏 **Benchmarks** — retrieval quality and latency at realistic memory scales
- 📝 **Documentation** — tutorials, guides, use case examples

## Development Setup

```bash
git clone https://github.com/yablokolabs/jnaapakam.git
cd jnaapakam
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Running the Test Suite

```bash
pytest                    # all tests
pytest -q tests/test_store.py
ruff check src tests      # lint
```

Tests need no API key, no network, and no database setup: the suite uses a
deterministic rule-based LLM stand-in and temporary SQLite files.

To run the server locally:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # or OPENAI_API_KEY, or LLM_BASE_URL
jnaapakam serve
```

## How to Contribute

1. Fork the repo
2. Create a feature branch: `git checkout -b feature/my-feature`
3. **Write a failing test first**, then make it pass
4. Run `pytest` and `ruff check src tests`
5. Submit a PR

## Guidelines

- Keep the protocol simple — complexity is the enemy
- No vendor lock-in — everything should work with any LLM provider
- Privacy first — no telemetry, no external calls except to the configured LLM
- Document your changes

## Testing Guidelines

The suite is the specification. A few rules that keep it useful:

- **Test behavior, not implementation.** Assert on what an endpoint returns or what
  ends up in the store — never that a particular function was called.
- **Don't assert on mocks.** The LLM stand-in in `tests/conftest.py` is a real
  implementation with deterministic rules. Prefer extending it over patching.
- **Don't test data shapes for their own sake.** A test that only checks a dict has
  certain keys tells you nothing about whether the system works.
- **Failure paths matter as much as success paths.** A silently degraded memory is
  worse than a loud error; several tests exist specifically to prevent that.
- **New model IDs need a guard.** Provider models get retired, and a retired ID
  returns 404. `tests/test_model_selection.py` fails the build if a shipped alias
  points at a known-retired model — add to its list when a model is withdrawn.

## Code Style

- Python: PEP 8, type hints appreciated. `ruff` config lives in `pyproject.toml`
- Keep runtime dependencies minimal — `aiohttp` is the only one, and additions need
  a strong justification. Anything that requires a daemon, a compiled extension, or
  a network call at first run must be optional and runtime-detected
- Docstrings on public functions; explain *why*, not *what*

## Protocol Changes

Changes to `PROTOCOL.md` require discussion in an issue first. The protocol should remain:
- Simple to implement
- Framework agnostic
- Backward compatible when possible

When a change is not backward compatible, say so explicitly in the version history
table and describe what breaks.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
