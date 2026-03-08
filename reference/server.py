"""
jñāpakaṁ Reference Server

Always-on memory persistence for AI agents.
Uses any OpenAI-compatible or Anthropic API for LLM operations.

Usage:
    python server.py                         # defaults: port 8889, consolidate every 30m
    python server.py --port 9000 --consolidate-every 15
    python server.py --watch ./inbox         # auto-ingest files from a folder

Environment:
    ANTHROPIC_API_KEY    — Anthropic API key (preferred)
    OPENAI_API_KEY       — OpenAI API key (fallback)
    LLM_BASE_URL         — Custom OpenAI-compatible base URL (e.g., Ollama)
    LLM_API_KEY          — API key for custom provider
    MEMORY_MODEL         — Model alias: haiku, sonnet, gpt4mini, or full model name
    MEMORY_DB            — SQLite database path (default: ./memory.db)
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from llm import chat

# ─── Config ────────────────────────────────────────────────────

MODEL = os.getenv("MEMORY_MODEL", "haiku")
DB_PATH = os.getenv("MEMORY_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.db"))

TEXT_EXTENSIONS = {".txt", ".md", ".json", ".csv", ".log", ".xml", ".yaml", ".yml"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="[%H:%M]")
log = logging.getLogger("jnaapakam")

# ─── Database ──────────────────────────────────────────────────


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL DEFAULT '',
            raw_text TEXT NOT NULL,
            summary TEXT NOT NULL,
            entities TEXT NOT NULL DEFAULT '[]',
            topics TEXT NOT NULL DEFAULT '[]',
            connections TEXT NOT NULL DEFAULT '[]',
            importance REAL NOT NULL DEFAULT 0.5,
            created_at TEXT NOT NULL,
            consolidated INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS consolidations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_ids TEXT NOT NULL,
            summary TEXT NOT NULL,
            insight TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS processed_files (
            path TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL
        );
    """)
    return db


# ─── Memory Operations ────────────────────────────────────────


def store_memory(raw_text, summary, entities, topics, importance, source=""):
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        "INSERT INTO memories (source, raw_text, summary, entities, topics, importance, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source, raw_text, summary, json.dumps(entities), json.dumps(topics), importance, now),
    )
    db.commit()
    mid = cursor.lastrowid
    db.close()
    log.info(f"📥 Stored memory #{mid}: {summary[:60]}...")
    return {"memory_id": mid, "status": "stored", "summary": summary}


def read_all_memories(limit=50):
    db = get_db()
    rows = db.execute("SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    memories = [
        {
            "id": r["id"], "source": r["source"], "summary": r["summary"],
            "entities": json.loads(r["entities"]), "topics": json.loads(r["topics"]),
            "importance": r["importance"], "connections": json.loads(r["connections"]),
            "created_at": r["created_at"], "consolidated": bool(r["consolidated"]),
        }
        for r in rows
    ]
    db.close()
    return memories


def read_unconsolidated(limit=20):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM memories WHERE consolidated = 0 ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    memories = [
        {
            "id": r["id"], "summary": r["summary"],
            "entities": json.loads(r["entities"]), "topics": json.loads(r["topics"]),
            "importance": r["importance"], "created_at": r["created_at"],
        }
        for r in rows
    ]
    db.close()
    return memories


def store_consolidation(source_ids, summary, insight, connections):
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO consolidations (source_ids, summary, insight, created_at) VALUES (?, ?, ?, ?)",
        (json.dumps(source_ids), summary, insight, now),
    )
    for conn in connections:
        from_id, to_id = conn.get("from_id"), conn.get("to_id")
        rel = conn.get("relationship", "")
        if from_id and to_id:
            for mid in [from_id, to_id]:
                row = db.execute("SELECT connections FROM memories WHERE id = ?", (mid,)).fetchone()
                if row:
                    existing = json.loads(row["connections"])
                    existing.append({"linked_to": to_id if mid == from_id else from_id, "relationship": rel})
                    db.execute("UPDATE memories SET connections = ? WHERE id = ?", (json.dumps(existing), mid))
    if source_ids:
        placeholders = ",".join("?" * len(source_ids))
        db.execute(f"UPDATE memories SET consolidated = 1 WHERE id IN ({placeholders})", source_ids)
    db.commit()
    db.close()
    log.info(f"🔄 Consolidated {len(source_ids)} memories. Insight: {insight[:80]}...")
    return {"status": "consolidated", "memories_processed": len(source_ids), "insight": insight}


def get_stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) as c FROM memories").fetchone()["c"]
    unconsolidated = db.execute("SELECT COUNT(*) as c FROM memories WHERE consolidated = 0").fetchone()["c"]
    consolidations = db.execute("SELECT COUNT(*) as c FROM consolidations").fetchone()["c"]
    db.close()
    return {"total_memories": total, "unconsolidated": unconsolidated, "consolidations": consolidations, "version": "0.1"}


def get_consolidation_history(limit=10):
    db = get_db()
    rows = db.execute("SELECT * FROM consolidations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    result = [{"summary": r["summary"], "insight": r["insight"],
               "source_ids": json.loads(r["source_ids"]), "created_at": r["created_at"]} for r in rows]
    db.close()
    return result


def delete_memory(memory_id):
    db = get_db()
    row = db.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if not row:
        db.close()
        return {"status": "not_found", "memory_id": memory_id}
    db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    db.commit()
    db.close()
    return {"status": "deleted", "memory_id": memory_id}


def clear_all():
    db = get_db()
    count = db.execute("SELECT COUNT(*) as c FROM memories").fetchone()["c"]
    db.execute("DELETE FROM memories")
    db.execute("DELETE FROM consolidations")
    db.execute("DELETE FROM processed_files")
    db.commit()
    db.close()
    return {"status": "cleared", "memories_deleted": count}


def export_all():
    db = get_db()
    memories = [dict(r) for r in db.execute("SELECT * FROM memories ORDER BY id").fetchall()]
    consolidations = [dict(r) for r in db.execute("SELECT * FROM consolidations ORDER BY id").fetchall()]
    db.close()
    return {
        "version": "0.1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "memories": memories,
        "consolidations": consolidations,
    }


def import_all(data):
    db = get_db()
    count = 0
    for m in data.get("memories", []):
        db.execute(
            "INSERT INTO memories (source, raw_text, summary, entities, topics, connections, importance, created_at, consolidated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (m.get("source", ""), m["raw_text"], m["summary"], m.get("entities", "[]"),
             m.get("topics", "[]"), m.get("connections", "[]"), m.get("importance", 0.5),
             m["created_at"], m.get("consolidated", 0)),
        )
        count += 1
    for c in data.get("consolidations", []):
        db.execute(
            "INSERT INTO consolidations (source_ids, summary, insight, created_at) VALUES (?, ?, ?, ?)",
            (c["source_ids"], c["summary"], c["insight"], c["created_at"]),
        )
    db.commit()
    db.close()
    return {"status": "restored", "memories_imported": count, "consolidations_imported": len(data.get("consolidations", []))}


# ─── LLM-Powered Operations ───────────────────────────────────

INGEST_SYSTEM = """You are a Memory Ingest Agent. Given new information:
1. Create a concise 1-2 sentence summary
2. Extract key entities (people, companies, products, concepts)
3. Assign 2-4 topic tags
4. Rate importance from 0.0 to 1.0

Respond with a JSON object:
{"summary": "...", "entities": ["..."], "topics": ["..."], "importance": 0.0-1.0}

Be concise and accurate. Only output valid JSON."""

CONSOLIDATE_SYSTEM = """You are a Memory Consolidation Agent. Given a set of memories:
1. Find connections and patterns across them
2. Create a synthesized summary
3. Identify one key insight
4. Map connections between memory IDs

Respond with a JSON object:
{
  "summary": "synthesized summary across all memories",
  "insight": "one key pattern or insight discovered",
  "connections": [{"from_id": 1, "to_id": 2, "relationship": "description"}]
}

Think deeply about cross-cutting patterns. Only output valid JSON."""

QUERY_SYSTEM = """You are a Memory Query Agent. You have access to stored memories and consolidation insights.
Answer the question based ONLY on the provided memories. Reference memory IDs like [Memory #1].
If no relevant memories exist, say so honestly. Be thorough but concise."""


async def ingest_text(text, source=""):
    try:
        response = await chat(
            model=MODEL, system=INGEST_SYSTEM,
            message=f"Process this information:\n\n{text[:5000]}",
        )
        data = json.loads(response.strip().strip("```json").strip("```"))
        return store_memory(
            raw_text=text[:5000], summary=data.get("summary", text[:100]),
            entities=data.get("entities", []), topics=data.get("topics", []),
            importance=data.get("importance", 0.5), source=source,
        )
    except Exception as e:
        log.error(f"Ingest error: {e}")
        return store_memory(raw_text=text[:5000], summary=text[:100],
                           entities=[], topics=[], importance=0.5, source=source)


async def consolidate():
    memories = read_unconsolidated()
    if len(memories) < 2:
        return {"status": "skipped", "reason": f"Only {len(memories)} unconsolidated memories"}
    memories_text = "\n".join(
        f"Memory #{m['id']}: {m['summary']} (entities: {m['entities']}, topics: {m['topics']})"
        for m in memories
    )
    try:
        response = await chat(
            model=MODEL, system=CONSOLIDATE_SYSTEM,
            message=f"Consolidate these memories:\n\n{memories_text}",
        )
        data = json.loads(response.strip().strip("```json").strip("```"))
        return store_consolidation(
            source_ids=[m["id"] for m in memories],
            summary=data.get("summary", ""), insight=data.get("insight", ""),
            connections=data.get("connections", []),
        )
    except Exception as e:
        log.error(f"Consolidation error: {e}")
        return {"status": "error", "error": str(e)}


async def query(question):
    memories = read_all_memories(limit=50)
    history = get_consolidation_history(limit=5)
    context = "## Stored Memories\n"
    for m in memories:
        context += f"- Memory #{m['id']} [{m['created_at'][:10]}]: {m['summary']}"
        if m['entities']:
            context += f" (entities: {', '.join(m['entities'])})"
        context += "\n"
    if history:
        context += "\n## Consolidation Insights\n"
        for h in history:
            context += f"- {h['insight']} (from memories {h['source_ids']})\n"
    return await chat(model=MODEL, system=QUERY_SYSTEM, message=f"{context}\n\n---\nQuestion: {question}")


# ─── File Watcher ──────────────────────────────────────────────


async def watch_folder(folder, poll_interval=5):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    db = get_db()
    log.info(f"👁️  Watching: {folder}/")
    while True:
        try:
            for f in sorted(folder.iterdir()):
                if f.name.startswith(".") or f.suffix.lower() not in TEXT_EXTENSIONS:
                    continue
                row = db.execute("SELECT 1 FROM processed_files WHERE path = ?", (str(f),)).fetchone()
                if row:
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")[:10000]
                    if text.strip():
                        log.info(f"📄 Ingesting: {f.name}")
                        await ingest_text(text, source=f.name)
                except Exception as e:
                    log.error(f"Error ingesting {f.name}: {e}")
                db.execute("INSERT INTO processed_files (path, processed_at) VALUES (?, ?)",
                           (str(f), datetime.now(timezone.utc).isoformat()))
                db.commit()
        except Exception as e:
            log.error(f"Watch error: {e}")
        await asyncio.sleep(poll_interval)


# ─── Consolidation Timer ──────────────────────────────────────


async def consolidation_loop(interval_minutes=30):
    log.info(f"🔄 Consolidation: every {interval_minutes} minutes")
    while True:
        await asyncio.sleep(interval_minutes * 60)
        try:
            stats = get_stats()
            if stats["unconsolidated"] >= 2:
                log.info(f"🔄 Running consolidation ({stats['unconsolidated']} unconsolidated)...")
                await consolidate()
            else:
                log.info(f"🔄 Skipping ({stats['unconsolidated']} unconsolidated)")
        except Exception as e:
            log.error(f"Consolidation error: {e}")


# ─── HTTP API ──────────────────────────────────────────────────


def build_http():
    app = web.Application()

    async def handle_query(req):
        q = req.query.get("q", "").strip()
        if not q:
            return web.json_response({"error": "missing ?q= parameter"}, status=400)
        return web.json_response({"question": q, "answer": await query(q)})

    async def handle_ingest(req):
        try:
            data = await req.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        text = data.get("text", "").strip()
        if not text:
            return web.json_response({"error": "missing 'text' field"}, status=400)
        result = await ingest_text(text, source=data.get("source", "api"))
        return web.json_response({"status": "ingested", **result})

    async def handle_consolidate(req):
        return web.json_response(await consolidate())

    async def handle_status(req):
        return web.json_response(get_stats())

    async def handle_memories(req):
        limit = int(req.query.get("limit", "50"))
        memories = read_all_memories(limit=limit)
        return web.json_response({"memories": memories, "count": len(memories)})

    async def handle_delete(req):
        try:
            data = await req.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        mid = data.get("memory_id")
        if not mid:
            return web.json_response({"error": "missing 'memory_id'"}, status=400)
        return web.json_response(delete_memory(int(mid)))

    async def handle_clear(req):
        return web.json_response(clear_all())

    async def handle_backup(req):
        return web.json_response(export_all())

    async def handle_restore(req):
        try:
            data = await req.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        return web.json_response(import_all(data))

    app.router.add_get("/status", handle_status)
    app.router.add_get("/memories", handle_memories)
    app.router.add_get("/query", handle_query)
    app.router.add_get("/backup", handle_backup)
    app.router.add_post("/ingest", handle_ingest)
    app.router.add_post("/consolidate", handle_consolidate)
    app.router.add_post("/delete", handle_delete)
    app.router.add_post("/clear", handle_clear)
    app.router.add_post("/restore", handle_restore)

    return app


# ─── Main ──────────────────────────────────────────────────────


async def main_async(args):
    log.info("🧠 jñāpakaṁ starting")
    log.info(f"   Model: {MODEL}")
    log.info(f"   Database: {DB_PATH}")
    log.info(f"   Watch: {args.watch}")
    log.info(f"   Consolidate: every {args.consolidate_every}m")
    log.info(f"   API: http://localhost:{args.port}")

    tasks = [
        asyncio.create_task(watch_folder(args.watch)),
        asyncio.create_task(consolidation_loop(args.consolidate_every)),
    ]

    app = build_http()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", args.port)
    await site.start()
    log.info(f"✅ Ready — http://localhost:{args.port}")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description="jñāpakaṁ — AI agent memory persistence")
    parser.add_argument("--port", type=int, default=int(os.getenv("MEMORY_PORT", "8889")))
    parser.add_argument("--watch", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "inbox"))
    parser.add_argument("--consolidate-every", type=int, default=int(os.getenv("CONSOLIDATE_INTERVAL", "30")))
    parser.add_argument("--db", default=None, help="SQLite database path")
    args = parser.parse_args()

    if args.db:
        global DB_PATH
        DB_PATH = args.db

    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: [t.cancel() for t in asyncio.all_tasks(loop)])
    try:
        loop.run_until_complete(main_async(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        loop.close()
        log.info("🧠 jñāpakaṁ stopped.")


if __name__ == "__main__":
    main()
