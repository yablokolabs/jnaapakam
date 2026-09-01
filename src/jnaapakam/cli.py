"""Command line entry point: `jnaapakam serve`, `init`, `agent`, and `generation`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from pathlib import Path

from aiohttp import web

from . import lineage, signing
from .config import Config, ConfigError
from .llm import chat
from .server import CONSOLIDATE_KEY, INGEST_KEY, STORE_KEY, build_app
from .store import Store

log = logging.getLogger("jnaapakam")

# The only filenames `generation seal` will read. An allowlist rather than a
# directory sweep: a continuity record should contain the agent's soul, not
# whatever else happens to be sitting in the folder — a stray .env in particular.
SOUL_FILES = ("SOUL.md", "IDENTITY.md", "MEMORY.md", "USER.md", "TOOLS.md", "HEARTBEAT.md")

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
                if await asyncio.to_thread(store.was_processed, str(path)):
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")[:10000]
                    if text.strip():
                        log.info("Ingesting %s", path.name)
                        await ingest(text, path.name)
                except Exception as exc:
                    log.error("Failed to ingest %s: %s", path.name, exc)
                await asyncio.to_thread(store.mark_processed, str(path))
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


async def _expiry_loop(days: int, store, sweep_every_minutes: int = 60) -> None:
    """Apply the age policy on a timer, starting immediately.

    # ponytail: fixed hourly sweep. The policy is measured in days, so the sweep
    # cadence only bounds how late a memory is retired; make it a knob if anyone
    # needs finer granularity than that.
    """
    log.info("Expiry: archiving memories untouched for %s days", days)
    while True:
        try:
            result = await asyncio.to_thread(store.expire, days)
            if result["archived"]:
                log.info("Expiry: archived %s memories", result["archived"])
        except Exception as exc:
            log.error("Expiry error: %s", exc)
        await asyncio.sleep(sweep_every_minutes * 60)


def _background_loops(config: Config, app) -> list:
    """Every loop the configuration asks for, as coroutines ready to schedule.

    Kept in one place because the failure mode is silent: a documented flag whose
    loop is never scheduled looks exactly like a working one.
    """
    store = app[STORE_KEY]
    loops = []
    if config.watch_dir:
        loops.append(_watch_folder(config.watch_dir, store, app[INGEST_KEY]))
    if config.consolidate_every_minutes > 0:
        loops.append(_consolidation_loop(config.consolidate_every_minutes, app[CONSOLIDATE_KEY]))
    if config.expire_after_days:
        loops.append(_expiry_loop(config.expire_after_days, store))
    return loops


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
    if config.expire_after_days:
        log.info("  expiry:   after %s days without use", config.expire_after_days)

    # Previously neither loop was ever scheduled, so --watch and --consolidate-every
    # were silently inert despite being documented and logged.
    background = [asyncio.create_task(loop) for loop in _background_loops(config, app)]

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
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError):
                await task
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
            expire_after_days=args.expire_after,
            watch_dir=args.watch,
        ).validate()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    asyncio.run(_serve(config))
    return 0


# ---- generational continuity -------------------------------------------
#
# These commands open the SQLite file directly, the way `init` writes soul files
# directly. Continuity is an operator concern and works offline: nothing here
# needs a running server, a token, or a network.


@contextlib.contextmanager
def _open_store(args):
    config = Config.from_env(db_path=args.db)
    store = Store(config.db_path, signing_key=config.signing_key).initialize()
    try:
        yield store
    finally:
        store.close()


def _guarded(args, action) -> int:
    """Run a store command, turning a refusal into an exit code rather than a traceback."""
    try:
        with _open_store(args) as store:
            return action(store)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _soul_digests(directory: Path) -> dict[str, dict]:
    """Hash the soul files present in a directory.

    This is the one place jnaapakam reads continuity artifacts from disk, and it
    reads only the names in SOUL_FILES from the directory the operator named. The
    server never does this at all — an endpoint that hashes a caller-supplied path
    is a file-read oracle, not an integrity feature.
    """
    digests = {}
    for name in SOUL_FILES:
        path = directory / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        digests[name] = {
            "name": name,
            "algorithm": "sha256",
            "digest": lineage.digest_bytes(data),
            "bytes": len(data),
        }
    return digests


def _describe_generation(generation: dict) -> str:
    parent = f"parent {generation['parent_id']}" if generation["parent_id"] else "root"
    label = f"  {generation['label']}" if generation["label"] else ""
    return (
        f"  {generation['id']:>3}  {generation['status']:<9} {generation['created_at'][:19]}  "
        f"({parent}){label}"
    )


def _agent_command(args) -> int:
    def action(store):
        agent = store.agent()
        current = agent["current_generation"]
        print(f"agent:       {agent['agent_id']}")
        print(f"created:     {agent['created_at']}")
        print(f"current:     generation {current}" if current else "current:     none")
        print(f"generations: {agent['generations']}")
        return 0

    return _guarded(args, action)


def _generation_list(args) -> int:
    def action(store):
        generations = store.list_generations()
        if not generations:
            print("no generations recorded yet")
            return 0
        current = store.current_generation()
        for generation in generations:
            marker = "*" if current and generation["id"] == current["id"] else " "
            print(marker + _describe_generation(generation))
        return 0

    return _guarded(args, action)


def _generation_show(args) -> int:
    def action(store):
        generation = store.get_generation(args.id)
        if generation is None:
            print(f"error: no generation with id {args.id}", file=sys.stderr)
            return 1
        print(f"generation:  {generation['id']} ({generation['status']})")
        print(f"agent:       {generation['agent_id']}")
        print(f"parent:      {generation['parent_id'] or 'none'}")
        print(f"label:       {generation['label'] or 'none'}")
        print(f"created:     {generation['created_at']}")
        print(f"ancestry:    {store.ancestry(generation['id']) or 'none'}")
        print("manifest:")
        print(json.dumps(generation["manifest"], indent=2, sort_keys=True))
        artifacts = store.artifacts(generation["id"])
        if artifacts:
            print("artifacts:")
            for artifact in artifacts:
                print(f"  {artifact['name']:<16} {artifact['algorithm']}:{artifact['digest'][:16]}…")
        return 0

    return _guarded(args, action)


def _generation_create(args) -> int:
    manifest = {}
    if args.manifest:
        try:
            manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"error: cannot read {args.manifest}: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"error: {args.manifest} is not valid JSON: {exc}", file=sys.stderr)
            return 1

    def action(store):
        generation = store.create_generation(
            parent=args.parent, label=args.label or "", manifest=manifest
        )
        print(f"created generation {generation['id']} ({generation['status']})")
        return 0

    return _guarded(args, action)


def _generation_seal(args) -> int:
    digests = _soul_digests(Path(args.soul_dir)) if args.soul_dir else {}

    def action(store):
        if digests:
            store.record_artifacts(args.id, list(digests.values()))
            for artifact in digests.values():
                print(f"  {artifact['name']:<16} sha256:{artifact['digest']}")
        corpus = store.seal_corpus(args.id)
        print(f"  {'memory_corpus':<16} sha256:{corpus['digest']}  ({corpus['records']} records)")
        signed = next((a for a in store.artifacts(args.id) if a["signature"]), None)
        if signed:
            print(f"  {'signed by':<16} {signing.fingerprint(signed['public_key'])}")
        print(f"sealed generation {args.id}")
        return 0

    return _guarded(args, action)


def _generation_validate(args) -> int:
    found = _soul_digests(Path(args.soul_dir)) if args.soul_dir else {}

    def action(store):
        # Only soul files live on disk. The corpus and state digests are computed
        # from the database, so they are never expected as files.
        recorded = {artifact["name"] for artifact in store.artifacts(args.id)} & set(SOUL_FILES)
        # Sealed but no longer on disk: reported here rather than quietly dropped,
        # because a missing soul file is exactly the kind of loss this catches.
        missing = sorted(recorded - set(found))
        result = store.validate_continuity(
            args.id,
            artifacts=list(found.values()) or None,
            probes=[{"query": q} for q in args.probe] or None,
            public_key=args.public_key,
        )
        for name, check in result["checks"].items():
            print(f"  {name:<15}{check['status']:<9}{check['detail']}")
        if missing:
            print(f"  {'artifacts':<15}{'fail':<9}sealed but missing from disk: {', '.join(missing)}")
        passed = result["passed"] and not missing
        print(f"generation {args.id}: {'continuity verified' if passed else 'validation FAILED'}")
        return 0 if passed else 1

    return _guarded(args, action)


def _generation_promote(args) -> int:
    def action(store):
        result = store.promote_generation(args.id, force=args.force)
        print(f"promoted generation {result['generation']}", end="")
        print(" (forced)" if result["forced"] else "")
        return 0

    return _guarded(args, action)


def _generation_reject(args) -> int:
    def action(store):
        store.reject_generation(args.id, reason=args.reason or "")
        print(f"rejected generation {args.id}")
        return 0

    return _guarded(args, action)


def _generation_rollback(args) -> int:
    def action(store):
        result = store.rollback_generation(args.id)
        print(f"rolled back to generation {result['generation']}")
        return 0

    return _guarded(args, action)


def _generation_diff(args) -> int:
    def action(store):
        difference = store.diff_generations(args.a, args.b)
        print(f"Generation {args.a} -> Generation {args.b}\n")
        identity = difference["agent_id"]
        print(f"agent_id:  {'unchanged' if identity['stable'] else 'DIFFERENT'}  {identity['value']}\n")
        for section, change in difference["sections"].items():
            print(f"{section}:")
            for field, (before, after) in change.get("changed", {}).items():
                print(f"  {field}: {before} -> {after}")
            for field, value in change.get("added", {}).items():
                print(f"  + {field}: {value}")
            for field, value in change.get("removed", {}).items():
                print(f"  - {field}: {value}")
            print()
        records = difference["memory"]["records"]
        memory = difference["memory"]
        print(f"memory:         {records[0]} -> {records[1]} records (content {memory['corpus']})")
        print(f"semantic state: {memory['state']}")
        for name, state in difference["artifacts"].items():
            print(f"{name + ':':<16}{state}")
        return 0

    return _guarded(args, action)


def _add_generation_commands(sub) -> None:
    generation = sub.add_parser("generation", help="Inspect and manage generational continuity")
    actions = generation.add_subparsers(dest="generation_command", required=True)

    def with_db(parser):
        parser.add_argument("--db", default=None, help="SQLite database path")
        return parser

    listing = with_db(actions.add_parser("list", help="List every generation"))
    listing.set_defaults(func=_generation_list)

    show = with_db(actions.add_parser("show", help="Show one generation in full"))
    show.add_argument("id", type=int)
    show.set_defaults(func=_generation_show)

    create = with_db(actions.add_parser("create", help="Record a new generation"))
    create.add_argument("--parent", type=int, default=None, help="Generation this one continues")
    create.add_argument("--label", default=None, help="Short human-readable name")
    create.add_argument("--manifest", default=None, help="JSON file describing runtime/model/hardware")
    create.set_defaults(func=_generation_create)

    seal = with_db(actions.add_parser("seal", help="Record digests of the soul files and memory corpus"))
    seal.add_argument("id", type=int)
    seal.add_argument("--soul-dir", default=None, dest="soul_dir", help="Directory holding SOUL.md etc.")
    seal.set_defaults(func=_generation_seal)

    validate = with_db(actions.add_parser("validate", help="Check a generation's continuity"))
    validate.add_argument("id", type=int)
    validate.add_argument("--soul-dir", default=None, dest="soul_dir")
    validate.add_argument(
        "--public-key", default=None, dest="public_key",
        help="Hex public key the seal must carry, to check provenance rather than self-consistency",
    )
    validate.add_argument(
        "--probe", action="append", default=[],
        help="Search text that must still return a memory. Repeatable.",
    )
    validate.set_defaults(func=_generation_validate)

    promote = with_db(actions.add_parser("promote", help="Make a generation the current one"))
    promote.add_argument("id", type=int)
    promote.add_argument("--force", action="store_true", help="Promote without a passing validation")
    promote.set_defaults(func=_generation_promote)

    reject = with_db(actions.add_parser("reject", help="Close a candidate generation off"))
    reject.add_argument("id", type=int)
    reject.add_argument("--reason", default=None)
    reject.set_defaults(func=_generation_reject)

    rollback = with_db(actions.add_parser("rollback", help="Return to an earlier generation"))
    rollback.add_argument("id", type=int)
    rollback.set_defaults(func=_generation_rollback)

    diff = with_db(actions.add_parser("diff", help="Compare two generations"))
    diff.add_argument("a", type=int)
    diff.add_argument("b", type=int)
    diff.set_defaults(func=_generation_diff)


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
    serve.add_argument(
        "--expire-after", type=int, default=None, dest="expire_after",
        help="Archive memories untouched for this many days (off unless set)",
    )
    serve.set_defaults(func=_serve_command)

    init = sub.add_parser("init", help="Create SOUL.md / IDENTITY.md / MEMORY.md")
    init.add_argument("directory", nargs="?", default=".", help="Where to write the soul files")
    init.set_defaults(func=_init)

    agent = sub.add_parser("agent", help="Show this agent's permanent identity")
    agent.add_argument("--db", default=None, help="SQLite database path")
    agent.set_defaults(func=_agent_command)

    _add_generation_commands(sub)

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
