"""Guardrailed retrieval over hotel knowledge.

Scoring is cosine similarity between normalised sentence embeddings.

Knowledge chunks change rarely but are re-scored on every inbound message, so
their vectors are cached. Without the cache, answering a single message costs
one model inference per chunk in the hotel's knowledge base — a hotel with 50
chunks paid 51 inferences per message, all of them synchronous.

Embedding is CPU-bound and blocking. Callers on an async path must run
top_k_chunks in a worker thread (asyncio.to_thread) or they will stall the
event loop for the duration.
"""
import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Sequence

import numpy as np
from sentence_transformers import SentenceTransformer

# MVP embedding model; upgrade to pgvector + ANN in production
_embedder = SentenceTransformer("all-MiniLM-L6-v2")

# Chunk text -> unit vector. Bounded so that many tenants with large knowledge
# bases cannot grow this without limit; eviction is least-recently-used.
_VECTOR_CACHE_LIMIT = 4096
_vector_cache: "OrderedDict[str, np.ndarray]" = OrderedDict()
_cache_lock = threading.Lock()


@dataclass
class Chunk:
    text: str
    score: float


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _encode(texts: Sequence[str]) -> np.ndarray:
    """One batched model call. Rows are unit vectors, so a dot product is cosine."""
    vectors = _embedder.encode(list(texts), normalize_embeddings=True)
    return np.asarray(vectors, dtype=np.float32).reshape(len(texts), -1)


def embed(text: str) -> np.ndarray:
    return _encode([text])[0]


def clear_vector_cache() -> None:
    """Drop all cached chunk vectors (tests, or after a bulk knowledge import)."""
    with _cache_lock:
        _vector_cache.clear()


def _chunk_vectors(chunks: Sequence[str]) -> np.ndarray:
    """Vectors for every chunk, embedding only the ones not already cached.

    Edited chunk text hashes differently, so an edit produces a fresh vector
    rather than a stale hit; the superseded entry ages out via LRU.
    """
    keys = [_key(c) for c in chunks]

    with _cache_lock:
        vectors = {}
        for k in keys:
            cached = _vector_cache.get(k)
            if cached is not None:
                _vector_cache.move_to_end(k)
                vectors[k] = cached

    missing = [i for i, k in enumerate(keys) if keys[i] not in vectors]
    if missing:
        # Deduplicate before encoding: a batch can repeat the same chunk text.
        unique = list(dict.fromkeys(keys[i] for i in missing))
        texts_by_key = {keys[i]: chunks[i] for i in missing}
        fresh = _encode([texts_by_key[k] for k in unique])
        with _cache_lock:
            for pos, k in enumerate(unique):
                vectors[k] = fresh[pos]
                _vector_cache[k] = fresh[pos]
                _vector_cache.move_to_end(k)
            while len(_vector_cache) > _VECTOR_CACHE_LIMIT:
                _vector_cache.popitem(last=False)

    return np.vstack([vectors[k] for k in keys])


def top_k_chunks(question: str, chunks: List[str], k: int = 4) -> List[Chunk]:
    if not chunks:
        return []
    qv = embed(question)
    scores = _chunk_vectors(chunks) @ qv
    # Stable sort so equally-scoring chunks keep their input order.
    order = np.argsort(-scores, kind="stable")[:k]
    return [Chunk(text=chunks[i], score=float(scores[i])) for i in order]


def should_auto_answer(retrieved: List[Chunk], threshold: float = 0.62) -> bool:
    return bool(retrieved) and retrieved[0].score >= threshold


def compose_grounded_answer(question: str, retrieved: List[Chunk]) -> str:
    # Guardrail: only use retrieved text; do not invent facts.
    context = "\n".join([f"- {c.text}" for c in retrieved])
    return (
        "Here's what I found from the hotel info:\n"
        f"{context}\n\n"
        "If you need something else, tell me what you're looking for and I'll get staff to help."
    )
