"""Tests for RAG / AI helpers."""
import numpy as np
import pytest

import app.ai as ai
from app.ai import top_k_chunks, should_auto_answer, compose_grounded_answer


CHUNKS = [
    "Breakfast is served from 06:30 to 10:30 daily in the main restaurant.",
    "Pool hours are 08:00 to 20:00. Towels available at reception.",
    "Check-out is at 11:00. Late check-out is subject to availability.",
    "Wi-Fi password is on your keycard sleeve.",
]


def test_top_k_chunks_returns_k():
    results = top_k_chunks("What time is breakfast?", CHUNKS, k=2)
    assert len(results) == 2


def test_top_k_chunks_sorted_by_score():
    results = top_k_chunks("What time is breakfast?", CHUNKS, k=4)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_top_k_empty_chunks():
    results = top_k_chunks("anything", [], k=3)
    assert results == []


def test_should_auto_answer_high_score():
    results = top_k_chunks("When is breakfast served?", CHUNKS, k=3)
    # Breakfast question should match well
    assert should_auto_answer(results, threshold=0.4)


def test_should_auto_answer_empty():
    assert not should_auto_answer([])


def test_compose_grounded_answer_contains_chunks():
    results = top_k_chunks("What time is check-out?", CHUNKS, k=2)
    answer = compose_grounded_answer("What time is check-out?", results)
    assert "hotel info" in answer
    # Should contain at least one chunk text
    assert any(r.text[:20] in answer for r in results)


# ---------------------------------------------------------------------------
# Chunk vector caching
# ---------------------------------------------------------------------------

@pytest.fixture
def count_encodes(monkeypatch):
    """Count how many texts are actually pushed through the model."""
    ai.clear_vector_cache()
    encoded = []
    real = ai._embedder.encode

    def _counting(texts, **kwargs):
        encoded.extend(texts)
        return real(texts, **kwargs)

    monkeypatch.setattr(ai._embedder, "encode", _counting)
    yield encoded
    ai.clear_vector_cache()


def test_chunks_are_embedded_once_across_messages(count_encodes):
    """Regression: every chunk used to be re-embedded for every message."""
    top_k_chunks("What time is breakfast?", CHUNKS, k=2)
    assert count_encodes.count(CHUNKS[0]) == 1

    for _ in range(5):
        top_k_chunks("Is there a pool?", CHUNKS, k=2)

    # Six messages, four chunks: the chunks are embedded once, and each
    # question once. Previously this was 6 * (4 + 1) = 30 inferences.
    assert len(count_encodes) == len(CHUNKS) + 6


def test_edited_chunk_is_re_embedded(count_encodes):
    top_k_chunks("q", CHUNKS, k=1)
    edited = list(CHUNKS)
    edited[0] = "Breakfast is served from 07:00 to 11:00 in the atrium."

    top_k_chunks("q", edited, k=1)
    assert edited[0] in count_encodes          # new text, fresh vector
    assert count_encodes.count(CHUNKS[1]) == 1  # unchanged text still cached


def test_caching_does_not_change_scores(count_encodes):
    cold = top_k_chunks("What time is breakfast?", CHUNKS, k=4)
    warm = top_k_chunks("What time is breakfast?", CHUNKS, k=4)

    assert [c.text for c in cold] == [c.text for c in warm]
    for a, b in zip(cold, warm):
        assert a.score == pytest.approx(b.score)


def test_scores_match_direct_cosine_similarity():
    """The vectorised path must agree with a naive per-chunk dot product."""
    ai.clear_vector_cache()
    question = "When is check-out?"
    qv = ai.embed(question)
    expected = {c: float(np.dot(qv, ai.embed(c))) for c in CHUNKS}

    for chunk in top_k_chunks(question, CHUNKS, k=4):
        assert chunk.score == pytest.approx(expected[chunk.text], abs=1e-5)


def test_duplicate_chunks_are_embedded_once(count_encodes):
    top_k_chunks("q", [CHUNKS[0], CHUNKS[0], CHUNKS[1]], k=3)
    assert count_encodes.count(CHUNKS[0]) == 1


def test_cache_is_bounded(monkeypatch):
    ai.clear_vector_cache()
    monkeypatch.setattr(ai, "_VECTOR_CACHE_LIMIT", 8)
    for i in range(40):
        top_k_chunks("q", [f"knowledge chunk number {i}"], k=1)
    assert len(ai._vector_cache) <= 8
    ai.clear_vector_cache()
