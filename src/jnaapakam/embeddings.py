"""Vector arithmetic for semantic retrieval.

BM25 ranks memories that share words with the query. It has no way to reach the
memory that says the same thing in other words, which is most of what an agent is
asked to remember. Embeddings supply that, at the cost of a model call per memory
and a similarity scan per query — so they are opt-in, not the default.

Optional by design: `pip install jnaapakam[embeddings]`. Without numpy the
capability reports unavailable and retrieval stays lexical, rather than falling
back to a slow pure-Python scan nobody would want on a real corpus.

Vectors are stored as little-endian float32. That is half the size of float64 and
well inside the precision that cosine similarity over normalised embeddings needs.
"""

from __future__ import annotations

DTYPE = "<f4"


class EmbeddingsUnavailable(RuntimeError):
    """The optional embedding dependency is not installed."""


INSTALL_HINT = "semantic retrieval is not installed: pip install jnaapakam[embeddings]"


def _numpy():
    try:
        import numpy
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised by installs without the extra
        raise EmbeddingsUnavailable(INSTALL_HINT) from exc
    return numpy


def available() -> bool:
    """True when vectors can be stored and compared on this installation."""
    try:
        _numpy()
    except EmbeddingsUnavailable:
        return False
    return True


def pack(vector) -> bytes:
    """A vector as the bytes stored in SQLite."""
    return _numpy().asarray(vector, dtype=DTYPE).tobytes()


def unpack(blob: bytes) -> list[float]:
    return [float(value) for value in _numpy().frombuffer(blob, dtype=DTYPE)]


def similarity(a, b) -> float:
    """Cosine similarity, 0.0 when either vector has no magnitude."""
    numpy = _numpy()
    left = numpy.asarray(a, dtype="f4")
    right = numpy.asarray(b, dtype="f4")
    magnitude = float(numpy.linalg.norm(left) * numpy.linalg.norm(right))
    return float(numpy.dot(left, right) / magnitude) if magnitude else 0.0


def rank_by_similarity(query_vector, rows, limit: int) -> list[tuple[int, float]]:
    """Score `(memory_id, packed_vector)` rows against a query vector, best first.

    A full scan of the namespace, which is what makes a purely semantic match
    reachable at all: anything narrower would only re-rank what BM25 already found,
    and BM25 is precisely what misses the synonym.

    # ponytail: O(n) per query, vectorised. Fine into six figures of memories on
    # one machine; past that this wants an ANN index (sqlite-vec, faiss) rather
    # than a bigger scan.
    """
    numpy = _numpy()
    if not rows:
        return []
    query = numpy.asarray(query_vector, dtype="f4")
    query_norm = float(numpy.linalg.norm(query))
    if not query_norm:
        return []

    ids = [row[0] for row in rows]
    matrix = numpy.frombuffer(b"".join(row[1] for row in rows), dtype=DTYPE)
    # A row whose length does not divide evenly means a vector from another model
    # slipped through the model filter; refuse rather than reshape into nonsense.
    if matrix.size % len(ids):
        raise ValueError("stored vectors have inconsistent dimensions")
    matrix = matrix.reshape(len(ids), -1)

    norms = numpy.linalg.norm(matrix, axis=1)
    norms[norms == 0] = numpy.inf  # a zero vector scores 0, never divides by zero
    scores = (matrix @ query) / (norms * query_norm)

    best = numpy.argsort(-scores)[:limit]
    return [(ids[index], float(scores[index])) for index in best]
