"""Tests for RAG / AI helpers."""
import pytest
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
