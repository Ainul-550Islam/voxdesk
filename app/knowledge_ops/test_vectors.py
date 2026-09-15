"""Unit tests for app.knowledge_ops.vectors.

Co-located with the package; run with: python -m pytest app/knowledge_ops/ -q
"""

from __future__ import annotations

import pytest

from app.knowledge_ops.vectors import (
    cosine_similarity,
    deduplicate,
    l2_norm,
    l2_normalize,
    top_k,
)


def test_l2_norm():
    assert l2_norm([3.0, 4.0]) == pytest.approx(5.0)
    assert l2_norm([]) == 0.0


def test_l2_normalize_unit_length():
    vector = l2_normalize([3.0, 4.0])
    assert l2_norm(vector) == pytest.approx(1.0)
    assert vector == pytest.approx([0.6, 0.8])


def test_l2_normalize_zero_vector_raises():
    with pytest.raises(ValueError):
        l2_normalize([0.0, 0.0])


def test_cosine_similarity_known_values():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_similarity_length_mismatch_raises():
    with pytest.raises(ValueError):
        cosine_similarity([1.0], [1.0, 2.0])


def test_cosine_similarity_zero_vector_returns_zero():
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine_similarity([1.0, 1.0], [0.0, 0.0]) == 0.0
    assert cosine_similarity([], []) == 0.0


def test_top_k_orders_and_truncates():
    query = [1.0, 0.0, 0.0]
    items = [
        {"id": "a", "vec": [1.0, 0.0, 0.0]},
        {"id": "b", "vec": [0.9, 0.1, 0.0]},
        {"id": "c", "vec": [0.0, 1.0, 0.0]},
    ]
    result = top_k(query, items, key=lambda item: item["vec"], k=2)
    assert [item["id"] for item in result] == ["a", "b"]


def test_top_k_zero_or_negative_is_empty():
    assert top_k([1.0], [{"vec": [1.0]}], key=lambda i: i["vec"], k=0) == []
    assert top_k([1.0], [{"vec": [1.0]}], key=lambda i: i["vec"], k=-3) == []


def test_top_k_stable_on_ties():
    query = [1.0]
    items = [{"id": "x", "vec": [1.0]}, {"id": "y", "vec": [1.0]}]
    result = top_k(query, items, key=lambda i: i["vec"], k=2)
    assert [i["id"] for i in result] == ["x", "y"]


def test_deduplicate_removes_near_duplicates():
    items = [
        {"id": "a", "vec": [1.0, 0.0]},
        {"id": "b", "vec": [0.99, 0.01]},  # near-duplicate of a
        {"id": "c", "vec": [0.0, 1.0]},
    ]
    result = deduplicate(items, key=lambda i: i["vec"], threshold=0.98)
    assert [i["id"] for i in result] == ["a", "c"]


def test_deduplicate_keeps_all_when_threshold_high():
    items = [{"id": "a", "vec": [1.0, 0.0]}, {"id": "b", "vec": [0.0, 1.0]}]
    result = deduplicate(items, key=lambda i: i["vec"], threshold=1.0001)
    assert [i["id"] for i in result] == ["a", "b"]
