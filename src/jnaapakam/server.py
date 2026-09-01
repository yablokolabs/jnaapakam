"""HTTP + MCP surface.

SQLite calls run in a worker thread so a slow query cannot stall the event loop,
and every destructive endpoint sits behind the same auth middleware.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging

from aiohttp import web

from .config import Config
from .llm import ExtractionError, extract_json
from .reconcile import Reconciler
from .store import Store

log = logging.getLogger("jnaapakam.server")

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

QUERY_SYSTEM = """You are a Memory Query Agent. You have access to retrieved memories.
Answer the question based ONLY on the provided memories. Reference memory IDs like [Memory #1].
If no relevant memories exist, say so honestly. Be thorough but concise."""

MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_SERVER_INFO = {"name": "jnaapakam", "version": "0.4.0"}

NAMESPACE_PARAM = {
    "type": "string",
    "description": "Scope memories to one project or agent. Omit for the shared namespace.",
}

CONFIG_KEY: web.AppKey[Config] = web.AppKey("config", Config)
STORE_KEY: web.AppKey[Store] = web.AppKey("store", Store)

# Routes that probes, discovery clients, and the MCP bridge can hit without
# credentials. They expose no destructive operations: / is server metadata,
# /health is a liveness check, and the MCP JSON-RPC surface (POST / and POST
# /mcp) carries only the MCP tools — search, ingest, query, list, status,
# consolidate. The MCPize in-container HTTP bridge cannot attach a bearer token,
# so that surface must stay reachable without one; every REST data endpoint
# (including /clear, /restore, /delete, /backup) remains behind auth.
AUTH_PUBLIC_ROUTES = frozenset(
    {("GET", "/"), ("GET", "/health"), ("GET", "/mcp"), ("POST", "/"), ("POST", "/mcp")}
)
# Exposed so the CLI's background loops reuse the same LLM-backed operations the
# HTTP handlers use, rather than reimplementing them.
INGEST_KEY: web.AppKey = web.AppKey("ingest")
CONSOLIDATE_KEY: web.AppKey = web.AppKey("consolidate")

MCP_TOOLS = [
    {
        "name": "search_memory",
        "description": (
            "Search stored memories by content and return ranked records with their sources and "
            "scores. Use this when you need the memories themselves, with provenance intact."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 12},
                "namespace": NAMESPACE_PARAM,
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ingest_memory",
        "description": "Store text in persistent memory with an optional source label.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to store in memory."},
                "source": {"type": "string", "description": "Optional source label."},
                "namespace": NAMESPACE_PARAM,
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "query_memory",
        "description": (
            "Answer a question from stored memories as prose with citations. Prefer search_memory "
            "when you want the records rather than a synthesized answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "namespace": NAMESPACE_PARAM,
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_memories",
        "description": "List the most recently stored memories.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                "namespace": NAMESPACE_PARAM,
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_memory_status",
        "description": "Return memory database counts and server version.",
        "inputSchema": {
            "type": "object",
            "properties": {"namespace": NAMESPACE_PARAM},
            "additionalProperties": False,
        },
    },
    {
        "name": "consolidate_memories",
        "description": "Consolidate pending memories into higher-level insights.",
        "inputSchema": {
            "type": "object",
            "properties": {"namespace": NAMESPACE_PARAM},
            "additionalProperties": False,
        },
    },
    # The generational tools are read-only by design. Creating, promoting and
    # rolling back a generation decide which runtime *is* the agent, and that is
    # an operator decision — the same reason /clear and /restore are not MCP
    # tools. A model can inspect its own lineage; it cannot rewrite it.
    {
        "name": "get_agent_identity",
        "description": (
            "Return this agent's permanent identifier and current generation. The identifier is "
            "stable across changes of model, runtime, host and hardware."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_generations",
        "description": "List the agent's generations with their status and declared environment.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "diff_generations",
        "description": "Compare two generations: runtime, model, environment, hardware, capabilities.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "The earlier generation id."},
                "b": {"type": "integer", "description": "The later generation id."},
            },
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    },
]


class MCPProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _positive_int(raw, name: str, default: int, maximum: int = 1000) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(reason=f"{name} must be an integer") from None
    if value < 1:
        raise web.HTTPBadRequest(reason=f"{name} must be >= 1")
    return min(value, maximum)


async def _json_body(request: web.Request) -> dict:
    if request.content_length and request.content_length > request.app[CONFIG_KEY].max_body_bytes:
        raise web.HTTPRequestEntityTooLarge(
            max_size=request.app[CONFIG_KEY].max_body_bytes, actual_size=request.content_length
        )
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="invalid JSON body") from None
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(reason="body must be a JSON object")
    return body


def build_app(config: Config, chat) -> web.Application:
    app = web.Application(client_max_size=config.max_body_bytes)
    store = Store(
        config.db_path,
        max_query_terms=config.max_query_terms,
        signing_key=config.signing_key,
    ).initialize()
    app[CONFIG_KEY] = config
    app[STORE_KEY] = store

    async def in_thread(fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ---- LLM-backed operations ----------------------------------------

    async def ingest_text(text: str, source: str = "", namespace: str = "", kind: str = "factual") -> dict:
        """Extract structure, then store.

        An LLM or parsing failure propagates. v0.1 swallowed it and stored a
        summary-less memory while returning HTTP 200 "stored", so a dead model
        looked exactly like a healthy one.
        """
        reply = await chat(config.model, INGEST_SYSTEM, f"Process this information:\n\n{text[:5000]}")
        data = extract_json(reply)
        memory_id = await in_thread(
            store.add_memory,
            text[:5000],
            str(data.get("summary") or text[:120]),
            data.get("entities") or [],
            data.get("topics") or [],
            float(data.get("importance", 0.5)),
            source,
            namespace,
            kind,
        )
        log.info("Stored memory #%s", memory_id)
        return {
            "status": "stored",
            "memory_id": memory_id,
            "namespace": namespace,
            "summary": data.get("summary", ""),
        }

    async def consolidate(namespace: str = "") -> dict:
        memories = await in_thread(store.unconsolidated, 20, namespace)
        if len(memories) < 2:
            return {"status": "skipped", "reason": f"only {len(memories)} pending memories"}
        rendered = "\n".join(
            f"Memory #{m['id']}: {m['summary']} (entities: {m['entities']}, topics: {m['topics']})"
            for m in memories
        )
        reply = await chat(config.model, CONSOLIDATE_SYSTEM, f"Consolidate these memories:\n\n{rendered}")
        data = extract_json(reply)
        return await in_thread(
            store.record_consolidation,
            [m["id"] for m in memories],
            data.get("summary", ""),
            data.get("insight", ""),
            data.get("connections", []),
        )

    async def answer(question: str, namespace: str = "") -> dict:
        memories = await in_thread(store.search, question, config.retrieval_limit, namespace)
        history = await in_thread(store.consolidation_history, 5)
        lines = ["## Retrieved Memories"]
        for memory in memories:
            entities = ", ".join(memory["entities"])
            lines.append(
                f"- Memory #{memory['id']} [{memory['created_at'][:10]}]: {memory['summary']}"
                + (f" (entities: {entities})" if entities else "")
            )
        if history:
            lines.append("\n## Consolidation Insights")
            lines += [f"- {h['insight']} (from memories {h['source_ids']})" for h in history]
        context = "\n".join(lines)
        text = await chat(config.model, QUERY_SYSTEM, f"{context}\n\n---\nQuestion: {question}")
        return {"question": question, "namespace": namespace, "answer": text, "memories": memories}

    # ---- middleware ----------------------------------------------------

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        if config.auth_required and (request.method, request.path) not in AUTH_PUBLIC_ROUTES:
            header = request.headers.get("Authorization", "")
            token = header[7:].strip() if header.lower().startswith("bearer ") else ""
            # compare_digest: a plain != leaks the shared prefix length through timing.
            if not hmac.compare_digest(token, config.auth_token):
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    @web.middleware
    async def origin_middleware(request: web.Request, handler):
        """Reject cross-origin writes.

        A page the user visits can POST to 127.0.0.1 with no preflight, so an
        unauthenticated local server would otherwise expose /clear and /restore to
        any website. Non-browser clients send no Origin header and are unaffected.
        """
        origin = request.headers.get("Origin")
        if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
            allowed = {f"http://{request.host}", f"https://{request.host}"}
            if origin not in allowed:
                log.warning("Rejected cross-origin %s %s from %s", request.method, request.path, origin)
                return web.json_response(
                    {"error": "cross-origin request refused"}, status=403
                )
        return await handler(request)

    @web.middleware
    async def error_middleware(request: web.Request, handler):
        try:
            return await handler(request)
        except web.HTTPException as exc:
            if exc.status >= 400:
                return web.json_response({"error": exc.reason}, status=exc.status)
            raise
        except (ExtractionError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    app.middlewares.extend([error_middleware, origin_middleware, auth_middleware])

    # ---- handlers ------------------------------------------------------

    async def handle_info(request):
        return web.json_response(
            {
                "name": "jnaapakam",
                "version": "0.4.0",
                "transport": "streamable-http",
                "mcp_endpoint": "/mcp",
                "tools": [tool["name"] for tool in MCP_TOOLS],
            }
        )

    async def handle_health(request):
        """Liveness for platform startup probes: reachable without credentials."""
        return web.json_response({"status": "ok", "name": "jnaapakam", "version": "0.4.0"})

    async def handle_status(request):
        namespace = request.query.get("namespace")
        return web.json_response(await in_thread(store.stats, namespace))

    async def handle_namespaces(request):
        return web.json_response({"namespaces": await in_thread(store.namespaces)})

    async def handle_memories(request):
        limit = _positive_int(request.query.get("limit"), "limit", 50, maximum=500)
        namespace = request.query.get("namespace") or ""
        memories = await in_thread(store.list_memories, limit, namespace)
        return web.json_response({"memories": memories, "count": len(memories)})

    async def handle_search(request):
        query = (request.query.get("q") or "").strip()
        if not query:
            raise web.HTTPBadRequest(reason="missing ?q= parameter")
        limit = _positive_int(request.query.get("limit"), "limit", config.retrieval_limit, maximum=100)
        namespace = request.query.get("namespace") or ""
        include_superseded = request.query.get("include_superseded") == "true"
        include_archived = request.query.get("include_archived") == "true"
        memories = await in_thread(
            store.search, query, limit, namespace, include_superseded, include_archived,
            200, True, config.weights, config.recency_halflife_days,
        )
        return web.json_response(
            {"query": query, "namespace": namespace, "memories": memories, "count": len(memories)}
        )

    async def handle_query(request):
        query = (request.query.get("q") or "").strip()
        if not query:
            raise web.HTTPBadRequest(reason="missing ?q= parameter")
        return web.json_response(await answer(query, request.query.get("namespace") or ""))

    async def handle_ingest(request):
        body = await _json_body(request)
        text = str(body.get("text") or "").strip()
        if not text:
            raise web.HTTPBadRequest(reason="missing 'text' field")
        source = body.get("source") or "api"
        namespace = body.get("namespace") or ""
        kind = body.get("kind") or "factual"
        for name, value in (("source", source), ("namespace", namespace), ("kind", kind)):
            if not isinstance(value, str):
                raise web.HTTPBadRequest(reason=f"'{name}' must be a string")
        return web.json_response(await ingest_text(text, source, namespace, kind))

    async def handle_consolidate(request):
        return web.json_response(await consolidate(request.query.get("namespace") or ""))

    async def handle_delete(request):
        body = await _json_body(request)
        raw_id = body.get("memory_id")
        if raw_id is None:
            raise web.HTTPBadRequest(reason="missing 'memory_id'")
        try:
            memory_id = int(raw_id)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(reason="'memory_id' must be an integer") from None
        if not await in_thread(store.delete_memory, memory_id):
            raise web.HTTPNotFound(reason=f"no memory with id {memory_id}")
        return web.json_response({"status": "deleted", "memory_id": memory_id})

    async def handle_clear(request):
        namespace = request.query.get("namespace")
        deleted = await in_thread(store.clear, namespace)
        return web.json_response(
            {"status": "cleared", "namespace": namespace, "memories_deleted": deleted}
        )

    async def handle_reconcile(request):
        namespace = request.query.get("namespace") or ""
        limit = _positive_int(request.query.get("limit"), "limit", 50, maximum=200)
        result = await Reconciler(store, config, chat=chat, in_thread=in_thread).run(
            namespace=namespace, limit=limit
        )
        status = 200 if result["status"] != "disabled" else 409
        return web.json_response(result, status=status)

    async def handle_prune(request):
        """Apply a retention policy: an age limit, a count cap, or both.

        Both are additive and independent — a namespace under its cap can still hold
        memories nobody has read in a year — so when both are given the age policy
        runs first and the counts are summed.
        """
        keep_param = request.query.get("keep")
        age_param = request.query.get("older_than_days")
        if not keep_param and not age_param:
            raise web.HTTPBadRequest(reason="missing 'keep' or 'older_than_days' parameter")
        namespace = request.query.get("namespace") or ""

        archived = 0
        result = None
        if age_param:
            days = _positive_int(age_param, "older_than_days", 90, maximum=100_000)
            result = await in_thread(store.expire, days, namespace)
            archived += result["archived"]
        if keep_param:
            keep = _positive_int(keep_param, "keep", 1000, maximum=1_000_000)
            result = await in_thread(store.prune, keep, namespace)
            archived += result["archived"]
        return web.json_response(result | {"archived": archived})

    async def handle_archive(request):
        body = await _json_body(request)
        raw_id = body.get("memory_id")
        if raw_id is None:
            raise web.HTTPBadRequest(reason="missing 'memory_id'")
        try:
            memory_id = int(raw_id)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(reason="'memory_id' must be an integer") from None
        restore = bool(body.get("restore"))
        action = store.unarchive if restore else store.archive
        if not await in_thread(action, memory_id):
            raise web.HTTPNotFound(reason=f"no memory with id {memory_id} in that state")
        return web.json_response(
            {"status": "unarchived" if restore else "archived", "memory_id": memory_id}
        )

    async def handle_supersede(request):
        body = await _json_body(request)
        try:
            old_id, new_id = int(body["old_id"]), int(body["new_id"])
        except (KeyError, TypeError, ValueError):
            raise web.HTTPBadRequest(reason="'old_id' and 'new_id' must be integers") from None
        if not await in_thread(store.supersede, old_id, new_id):
            raise web.HTTPBadRequest(
                reason="cannot supersede: unknown memory, same id, or different namespaces"
            )
        return web.json_response({"status": "superseded", "old_id": old_id, "new_id": new_id})

    # ---- generational continuity ---------------------------------------

    def generation_id(raw, name: str = "generation") -> int:
        if raw is None or raw == "":
            raise web.HTTPBadRequest(reason=f"missing '{name}'")
        # bool is an int in Python; {"generation": true} would become generation 1.
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise web.HTTPBadRequest(reason=f"'{name}' must be an integer")
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(reason=f"'{name}' must be an integer") from None

    async def require_generation(identifier: int) -> dict:
        generation = await in_thread(store.get_generation, identifier)
        if generation is None:
            raise web.HTTPNotFound(reason=f"no generation with id {identifier}")
        return generation

    async def handle_agent(request):
        return web.json_response(await in_thread(store.agent))

    async def handle_generations(request):
        raw = request.query.get("id")
        if raw:
            identifier = generation_id(raw, "id")
            generation = await require_generation(identifier)
            return web.json_response(
                {
                    "generation": generation,
                    "ancestry": await in_thread(store.ancestry, identifier),
                    "artifacts": await in_thread(store.artifacts, identifier),
                }
            )
        generations = await in_thread(store.list_generations)
        current = await in_thread(store.current_generation)
        return web.json_response(
            {
                "generations": generations,
                "count": len(generations),
                "current_generation": current["id"] if current else None,
            }
        )

    async def handle_create_generation(request):
        body = await _json_body(request)
        parent = body.get("parent")
        if parent is not None:
            parent = generation_id(parent, "parent")
            await require_generation(parent)
        return web.json_response(
            await in_thread(
                store.create_generation, parent, body.get("label") or "", body.get("manifest")
            )
        )

    async def handle_generation_artifacts(request):
        """Record digests the caller computed, and optionally seal the memory corpus.

        Digests arrive precomputed: the server never opens a file to hash it, so
        no request can turn integrity checking into a filesystem read.
        """
        body = await _json_body(request)
        identifier = generation_id(body.get("generation"))
        await require_generation(identifier)
        if body.get("artifacts") is None and not body.get("seal_corpus"):
            raise web.HTTPBadRequest(reason="supply 'artifacts', 'seal_corpus', or both")
        recorded = corpus = None
        if body.get("artifacts") is not None:
            recorded = await in_thread(store.record_artifacts, identifier, body["artifacts"])
        if body.get("seal_corpus"):
            corpus = await in_thread(store.seal_corpus, identifier, body.get("namespace"))
        return web.json_response(
            {"status": "recorded", "generation": identifier, "artifacts": recorded, "corpus": corpus}
        )

    async def handle_validate_generation(request):
        body = await _json_body(request)
        identifier = generation_id(body.get("generation"))
        await require_generation(identifier)
        return web.json_response(
            await in_thread(
                store.validate_continuity,
                identifier,
                body.get("artifacts"),
                body.get("probes"),
                body.get("behavioral"),
                body.get("public_key"),
            )
        )

    async def handle_promote_generation(request):
        body = await _json_body(request)
        identifier = generation_id(body.get("generation"))
        await require_generation(identifier)
        return web.json_response(
            await in_thread(store.promote_generation, identifier, bool(body.get("force")))
        )

    async def handle_reject_generation(request):
        body = await _json_body(request)
        identifier = generation_id(body.get("generation"))
        await require_generation(identifier)
        return web.json_response(
            await in_thread(store.reject_generation, identifier, str(body.get("reason") or ""))
        )

    async def handle_rollback_generation(request):
        body = await _json_body(request)
        identifier = generation_id(body.get("generation"))
        await require_generation(identifier)
        return web.json_response(await in_thread(store.rollback_generation, identifier))

    async def handle_generation_diff(request):
        before = generation_id(request.query.get("a"), "a")
        after = generation_id(request.query.get("b"), "b")
        await require_generation(before)
        await require_generation(after)
        return web.json_response(await in_thread(store.diff_generations, before, after))

    async def handle_migrations(request):
        limit = _positive_int(request.query.get("limit"), "limit", 50, maximum=500)
        entries = await in_thread(store.migrations, limit)
        return web.json_response({"migrations": entries, "count": len(entries)})

    async def handle_backup(request):
        return web.json_response(await in_thread(store.export_all))

    async def handle_restore(request):
        body = await _json_body(request)
        return web.json_response(await in_thread(store.import_all, body))

    # ---- MCP -----------------------------------------------------------

    async def call_tool(name: str, arguments: dict):
        arguments = arguments or {}
        if not isinstance(arguments, dict):
            raise MCPProtocolError(-32602, "Tool arguments must be an object")

        def text_result(text, structured=None, is_error=False):
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            if structured is not None:
                result["structuredContent"] = structured
            return result

        def json_result(data):
            return text_result(json.dumps(data, ensure_ascii=False, indent=2), structured=data)

        def required_string(key):
            value = arguments.get(key)
            if not isinstance(value, str) or not value.strip():
                raise MCPProtocolError(-32602, f"Missing required string argument: {key}")
            return value.strip()

        namespace = arguments.get("namespace") or ""
        if not isinstance(namespace, str):
            raise MCPProtocolError(-32602, "Argument must be a string: namespace")

        if name == "search_memory":
            limit = min(int(arguments.get("limit") or config.retrieval_limit), 100)
            memories = await in_thread(store.search, required_string("query"), limit, namespace)
            return json_result({"memories": memories, "count": len(memories)})
        if name == "ingest_memory":
            source = arguments.get("source") or "mcp"
            return json_result(await ingest_text(required_string("text"), source, namespace))
        if name == "query_memory":
            result = await answer(required_string("question"), namespace)
            return text_result(result["answer"], structured=result)
        if name == "list_memories":
            limit = min(int(arguments.get("limit") or 50), 100)
            memories = await in_thread(store.list_memories, limit, namespace)
            return json_result({"memories": memories, "count": len(memories)})
        if name == "get_memory_status":
            return json_result(await in_thread(store.stats, namespace or None))
        if name == "consolidate_memories":
            return json_result(await consolidate(namespace))
        if name == "get_agent_identity":
            return json_result(await in_thread(store.agent))
        if name == "list_generations":
            generations = await in_thread(store.list_generations)
            current = await in_thread(store.current_generation)
            return json_result(
                {
                    "generations": generations,
                    "count": len(generations),
                    "current_generation": current["id"] if current else None,
                }
            )
        if name == "diff_generations":
            def required_generation(key):
                value = arguments.get(key)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise MCPProtocolError(-32602, f"Argument must be a generation id: {key}")
                return value

            before, after = required_generation("a"), required_generation("b")
            for identifier in (before, after):
                if await in_thread(store.get_generation, identifier) is None:
                    raise MCPProtocolError(-32602, f"No generation with id {identifier}")
            return json_result(await in_thread(store.diff_generations, before, after))
        raise MCPProtocolError(-32602, f"Unknown tool: {name}")

    async def dispatch(method: str, params: dict):
        params = params or {}
        if method in ("initialize", "server/discover"):
            payload = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": MCP_SERVER_INFO,
                "instructions": "Search, store, and consolidate persistent agent memories.",
            }
            if method == "server/discover":
                payload |= {"resultType": "complete", "supportedVersions": [MCP_PROTOCOL_VERSION]}
            return payload
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": MCP_TOOLS}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or not name:
                raise MCPProtocolError(-32602, "Missing required tool name")
            return await call_tool(name, params.get("arguments", {}))
        if method == "resources/list":
            return {"resources": []}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "prompts/list":
            return {"prompts": []}
        raise MCPProtocolError(-32601, f"Method not found: {method}")

    async def handle_message(message):
        if not isinstance(message, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        request_id = message.get("id")
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32600, "message": "Invalid Request"},
            }
        if "id" not in message:
            return None
        try:
            result = await dispatch(method, message.get("params"))
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except MCPProtocolError as exc:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": exc.message}}
        except Exception:  # surface as a JSON-RPC error, not a dropped connection
            # The detail goes to the operator's log, not over the wire: provider errors
            # routinely quote API keys and filesystem paths back at us.
            log.exception("MCP method failed: %s", method)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "internal error"},
            }

    async def handle_mcp(request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status=400,
            )
        if isinstance(payload, list):
            if not payload:
                return web.json_response(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
                    status=400,
                )
            responses = [r for r in [await handle_message(item) for item in payload] if r]
            return web.json_response(responses) if responses else web.Response(status=202)
        response = await handle_message(payload)
        return web.json_response(response) if response is not None else web.Response(status=202)

    app[INGEST_KEY] = ingest_text
    app[CONSOLIDATE_KEY] = consolidate

    async def close_store(_app):
        await in_thread(store.close)

    app.on_cleanup.append(close_store)

    app.router.add_get("/", handle_info)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/mcp", handle_info)
    app.router.add_post("/", handle_mcp)
    app.router.add_post("/mcp", handle_mcp)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/namespaces", handle_namespaces)
    app.router.add_get("/memories", handle_memories)
    app.router.add_get("/search", handle_search)
    app.router.add_get("/query", handle_query)
    app.router.add_get("/backup", handle_backup)
    app.router.add_get("/agent", handle_agent)
    app.router.add_get("/generations", handle_generations)
    app.router.add_get("/generations/diff", handle_generation_diff)
    app.router.add_get("/migrations", handle_migrations)
    app.router.add_post("/generations", handle_create_generation)
    app.router.add_post("/generations/artifacts", handle_generation_artifacts)
    app.router.add_post("/generations/validate", handle_validate_generation)
    app.router.add_post("/generations/promote", handle_promote_generation)
    app.router.add_post("/generations/reject", handle_reject_generation)
    app.router.add_post("/generations/rollback", handle_rollback_generation)
    app.router.add_post("/ingest", handle_ingest)
    app.router.add_post("/consolidate", handle_consolidate)
    app.router.add_post("/delete", handle_delete)
    app.router.add_post("/clear", handle_clear)
    app.router.add_post("/supersede", handle_supersede)
    app.router.add_post("/reconcile", handle_reconcile)
    app.router.add_post("/prune", handle_prune)
    app.router.add_post("/archive", handle_archive)
    app.router.add_post("/restore", handle_restore)
    return app
