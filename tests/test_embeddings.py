"""Optional semantic retrieval.

BM25 finds memories that share words with the query. It cannot find the memory
that says the same thing in different words, and that is most of what an agent is
asked to remember. Embeddings close that gap — when they are configured, which
they are not by default.

The embedding endpoint here is a real HTTP server running a deterministic
rule-based embedder, not a mock: the tests exercise the real request, the real
response parsing, the real storage format and the real ranking.
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from jnaapakam import embeddings
from jnaapakam.server import STORE_KEY, build_app

pytestmark = pytest.mark.skipif(
    not embeddings.available(), reason="numpy not installed"
)

# A three-axis toy semantic space: databases, deployment, scheduling. Words that
# mean the same thing land on the same axis, which is exactly the property BM25
# lacks and embeddings are supposed to supply.
AXES = {
    "db": ("postgres", "postgresql", "clickhouse", "database", "warehouse", "analytics"),
    "deploy": ("deploy", "deployment", "release", "runbook", "rollout", "ship"),
    "when": ("standup", "meeting", "schedule", "tuesday", "calendar", "09:30"),
}


def rule_based_embedding(text: str) -> list[float]:
    """A real (tiny) embedder: project text onto three topic axes and normalise."""
    words = text.lower().replace(",", " ").replace(".", " ").split()
    vector = [sum(word in words for word in terms) for terms in AXES.values()]
    magnitude = sum(value * value for value in vector) ** 0.5
    return [value / magnitude for value in vector] if magnitude else [0.0, 0.0, 0.0]


@pytest.fixture
async def embedder():
    """A local OpenAI-compatible /v1/embeddings endpoint, recording what it was asked."""
    calls = []

    async def handler(request):
        body = await request.json()
        inputs = body["input"]
        inputs = [inputs] if isinstance(inputs, str) else inputs
        calls.append(body)
        return web.json_response(
            {
                "object": "list",
                "model": body.get("model", ""),
                "data": [
                    {"object": "embedding", "index": i, "embedding": rule_based_embedding(text)}
                    for i, text in enumerate(inputs)
                ],
            }
        )

    app = web.Application()
    app.router.add_post("/v1/embeddings", handler)
    server = TestServer(app)
    await server.start_server()
    server.calls = calls
    yield server
    await server.close()


@pytest.fixture
def embedding_config(config, embedder, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", str(embedder.make_url("/v1")))
    monkeypatch.setenv("LLM_API_KEY", "not-checked-by-the-test-server")
    config.embedding_model = "toy-3-axis"
    config.model = "toy-chat"
    return config


@pytest.fixture
async def client(embedding_config, llm):
    async with TestClient(TestServer(build_app(embedding_config, chat=llm))) as c:
        yield c


async def _ingest(client, text):
    resp = await client.post("/ingest", json={"text": text, "source": "test"})
    assert resp.status == 200, await resp.text()
    return (await resp.json())["memory_id"]


# ---- the storage format -------------------------------------------------


def test_a_vector_survives_the_round_trip_through_the_database(store):
    memory_id = store.add_memory(
        raw_text="the team standardised on ClickHouse", summary="clickhouse",
        entities=[], topics=[], importance=0.5, source="test",
    )
    vector = [0.5, -0.25, 0.125]

    store.set_embedding(memory_id, "toy-3-axis", vector)

    assert store.get_embedding(memory_id) == pytest.approx(vector)


def test_similarity_is_one_for_identical_vectors_and_zero_for_orthogonal_ones():
    a, b = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]

    assert embeddings.similarity(a, a) == pytest.approx(1.0)
    assert embeddings.similarity(a, b) == pytest.approx(0.0)


# ---- retrieval that BM25 cannot do --------------------------------------


async def test_a_memory_with_no_shared_words_is_retrieved_by_meaning(client):
    """The whole point: 'database choice' finds the ClickHouse note."""
    target = await _ingest(client, "the team standardised on ClickHouse for analytics")
    await _ingest(client, "the release runbook lives in the ops repository")

    body = await (await client.get("/search", params={"q": "which data warehouse"})).json()

    assert target in [memory["id"] for memory in body["memories"]]


async def test_lexical_matches_still_win_when_they_are_also_relevant(client):
    exact = await _ingest(client, "the deployment runbook lives in the ops repository")
    await _ingest(client, "the standup moved to 09:30 on Tuesday")

    body = await (await client.get("/search", params={"q": "deployment runbook"})).json()

    assert body["memories"][0]["id"] == exact


async def test_embeddings_are_stored_at_ingest(client):
    memory_id = await _ingest(client, "the team standardised on ClickHouse for analytics")

    store = client.app[STORE_KEY]

    assert store.embedding_coverage()["embedded"] == 1
    assert store.get_embedding(memory_id) is not None


# ---- the capability is off unless configured ----------------------------


async def test_nothing_is_embedded_when_no_model_is_configured(config, llm, embedder, monkeypatch):
    """An unconfigured deployment must not call an embedding endpoint at all."""
    monkeypatch.setenv("LLM_BASE_URL", str(embedder.make_url("/v1")))
    config.embedding_model = None

    async with TestClient(TestServer(build_app(config, chat=llm))) as client:
        await _ingest(client, "the team standardised on ClickHouse for analytics")
        body = await (await client.get("/search", params={"q": "data warehouse"})).json()

    assert embedder.calls == []
    assert body["memories"] == []


async def test_search_still_works_when_the_embedding_endpoint_is_down(client, embedder):
    """Semantic retrieval is an enhancement; losing it must not lose search."""
    target = await _ingest(client, "the deployment runbook lives in the ops repository")
    await embedder.close()

    body = await (await client.get("/search", params={"q": "deployment runbook"})).json()

    assert target in [memory["id"] for memory in body["memories"]]


async def test_an_ingest_still_succeeds_when_embedding_fails(config, llm, monkeypatch):
    """A memory that could not be embedded is stored and lexically searchable."""
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1/v1")
    config.embedding_model = "toy-3-axis"

    async with TestClient(TestServer(build_app(config, chat=llm))) as client:
        memory_id = await _ingest(client, "the deployment runbook lives in the ops repository")
        body = await (await client.get("/search", params={"q": "runbook"})).json()

    assert memory_id in [memory["id"] for memory in body["memories"]]


# ---- backfill and reporting ---------------------------------------------


async def test_status_reports_embedding_coverage(client):
    await _ingest(client, "the team standardised on ClickHouse for analytics")

    body = await (await client.get("/status")).json()

    assert body["embeddings"]["model"] == "toy-3-axis"
    assert body["embeddings"]["embedded"] == 1
    assert body["embeddings"]["total"] == 1


async def test_memories_stored_before_embeddings_can_be_backfilled(client):
    store = client.app[STORE_KEY]
    old = store.add_memory(
        raw_text="the team standardised on ClickHouse for analytics",
        summary="the team standardised on ClickHouse for analytics",
        entities=[], topics=[], importance=0.5, source="before",
    )
    assert store.get_embedding(old) is None

    body = await (await client.post("/embed")).json()

    assert body["embedded"] == 1
    assert store.get_embedding(old) is not None


async def test_backfill_is_idempotent(client):
    await _ingest(client, "the team standardised on ClickHouse for analytics")

    body = await (await client.post("/embed")).json()

    assert body["embedded"] == 0, "already-embedded memories are not re-sent"


# ---- the request the endpoint actually receives -------------------------


async def test_the_configured_model_is_the_one_requested(client, embedder):
    await _ingest(client, "the release runbook lives in the ops repository")

    assert embedder.calls, "ingest embeds the memory"
    assert embedder.calls[0]["model"] == "toy-3-axis"
