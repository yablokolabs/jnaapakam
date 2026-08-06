"""Command line entry point: `jnaapakam serve` and `jnaapakam init`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from aiohttp import web

from .config import Config, ConfigError
from .llm import chat
from .server import build_app
from .store import Store

log = logging.getLogger("jnaapakam")

TEXT_EXTENSIONS = {".txt", ".md", ".json", ".csv", ".log", ".xml", ".yaml", ".yml"}

SOUL_TEMPLATES = {
    "SOUL.md": """# SOUL.md

_Define who your agent is. This is loaded every session._

## Core Personality

<!-- How should your agent communicate? -->

## Boundaries

<!-- What should your agent never do? -->

## Preferences

<!-- Communication style preferences. -->
""",
    "IDENTITY.md": """# IDENTITY.md

- **Name:** <!-- Your agent's name -->
- **Emoji:** <!-- Signature emoji -->
- **Description:** <!-- One-line description -->
- **Created:** <!-- ISO date -->
""",
    "MEMORY.md": """# MEMORY.md

_Curated long-term memory. Organized by topic. Updated over time._

## User

## Projects

## Preferences

## Lessons Learned
""",
}


def _init(args) -> int:
    target = Path(args.directory)
    target.mkdir(parents=True, exist_ok=True)
    for name, body in SOUL_TEMPLATES.items():
        path = target / name
        if path.exists():
            print(f"  skip {path} (already exists)")
            continue
        path.write_text(body, encoding="utf-8")
        print(f"  create {path}")
    return 0


async def _watch_folder(folder: str, store: Store, ingest, poll_interval: int = 5) -> None:
    directory = Path(folder)
    directory.mkdir(parents=True, exist_ok=True)
    log.info("Watching %s/", directory)
    while True:
        try:
            for path in sorted(directory.iterdir()):
                if path.name.startswith(".") or path.suffix.lower() not in TEXT_EXTENSIONS:
                    continue
                seen = await asyncio.to_thread(
                    lambda p=str(path): store.db.execute(
                        "SELECT 1 FROM processed_files WHERE path = ?", (p,)
                    ).fetchone()
                )
                if seen:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")[:10000]
                    if text.strip():
                        log.info("Ingesting %s", path.name)
                        await ingest(text, path.name)
                except Exception as exc:
                    log.error("Failed to ingest %s: %s", path.name, exc)
                await asyncio.to_thread(
                    lambda p=str(path): (
                        store.db.execute(
                            "INSERT OR REPLACE INTO processed_files (path, processed_at) "
                            "VALUES (?, datetime('now'))",
                            (p,),
                        ),
                        store.db.commit(),
                    )
                )
        except Exception as exc:
            log.error("Watch error: %s", exc)
        await asyncio.sleep(poll_interval)


async def _consolidation_loop(interval_minutes: int, consolidate) -> None:
    log.info("Consolidation: every %s minutes", interval_minutes)
    while True:
        await asyncio.sleep(interval_minutes * 60)
        try:
            await consolidate()
        except Exception as exc:
            log.error("Consolidation error: %s", exc)


async def _serve(config: Config) -> None:
    app = build_app(config, chat=chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.port)
    await site.start()

    scheme_host = f"http://{config.host}:{config.port}"
    log.info("jnaapakam %s ready", scheme_host)
    log.info("  model:    %s", config.model)
    log.info("  database: %s", config.db_path)
    log.info("  auth:     %s", "required" if config.auth_required else "disabled (loopback only)")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # e.g. Windows
            pass
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
        log.info("jnaapakam stopped.")


def _serve_command(args) -> int:
    try:
        config = Config.from_env(
            db_path=args.db,
            host=args.host,
            port=args.port,
            model=args.model,
            consolidate_every_minutes=args.consolidate_every,
            watch_dir=args.watch,
        ).validate()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    asyncio.run(_serve(config))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jnaapakam", description="AI agent memory persistence")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the memory server")
    serve.add_argument("--host", default=None, help="Bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=None, help="Port (default 8889)")
    serve.add_argument("--db", default=None, help="SQLite database path")
    serve.add_argument("--model", default=None, help="LLM model alias or full name")
    serve.add_argument("--watch", default=None, help="Folder to auto-ingest text files from")
    serve.add_argument(
        "--consolidate-every", type=int, default=None, dest="consolidate_every",
        help="Minutes between consolidation cycles",
    )
    serve.set_defaults(func=_serve_command)

    init = sub.add_parser("init", help="Create SOUL.md / IDENTITY.md / MEMORY.md")
    init.add_argument("directory", nargs="?", default=".", help="Where to write the soul files")
    init.set_defaults(func=_init)

    return parser


def main(argv=None) -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(message)s",
        datefmt="[%H:%M]",
    )
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
