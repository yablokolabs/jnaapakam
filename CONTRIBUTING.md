# Contributing to jñāpakaṁ

Thanks for your interest in contributing! Here's how to get started.

## Areas We Need Help With

- 🔌 **Integration examples** — More framework integrations (AutoGen, Semantic Kernel, ADK, etc.)
- 🤖 **LLM providers** — Additional backend support (Groq, Together, Mistral, local models)
- 📊 **Memory visualization** — Dashboard or CLI tools to browse memories
- 🔒 **Security** — Encryption at rest, auth for the HTTP API
- 📏 **Benchmarks** — Performance testing at different memory scales
- 📝 **Documentation** — Tutorials, guides, use case examples

## How to Contribute

1. Fork the repo
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make your changes
4. Test locally: `python reference/server.py`
5. Submit a PR

## Guidelines

- Keep the protocol simple — complexity is the enemy
- No vendor lock-in — everything should work with any LLM provider
- Privacy first — no telemetry, no external calls except to the configured LLM
- Document your changes

## Code Style

- Python: Follow PEP 8, type hints appreciated
- Keep dependencies minimal (just `aiohttp` for the reference implementation)
- Docstrings on public functions

## Protocol Changes

Changes to `PROTOCOL.md` require discussion in an issue first. The protocol should remain:
- Simple to implement
- Framework agnostic
- Backward compatible when possible

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
