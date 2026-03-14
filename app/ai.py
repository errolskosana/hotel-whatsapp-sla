from dataclasses import dataclass
from typing import List
import numpy as np
from sentence_transformers import SentenceTransformer

# MVP embedding model; upgrade to pgvector + ANN in production
_embedder = SentenceTransformer("all-MiniLM-L6-v2")


@dataclass
class Chunk:
    text: str
    score: float


def embed(text: str) -> np.ndarray:
    v = _embedder.encode([text], normalize_embeddings=True)[0]
    return np.asarray(v, dtype=np.float32)


def top_k_chunks(question: str, chunks: List[str], k: int = 4) -> List[Chunk]:
    if not chunks:
        return []
    qv = embed(question)
    scored: List[Chunk] = []
    for c in chunks:
        cv = embed(c)
        score = float(np.dot(qv, cv))
        scored.append(Chunk(text=c, score=score))
    scored.sort(key=lambda x: x.score, reverse=True)
    return scored[:k]


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
