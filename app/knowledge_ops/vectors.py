"""Vector operations for retrieval and deduplication (Phase 4, knowledge slice).

Pure, stdlib-only mirror of the math in ``app/knowledge/vectorstore.py``'s
JSON-scan fallback (cosine similarity over dense vectors), lifted out of the
SQLAlchemy module so retrieval scoring, top-k and near-duplicate detection can
run — and be tested — without a database or numpy.

All functions treat vectors as ``Sequence[float]`` and are deterministic.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import TypeVar

T = TypeVar("T")


def l2_norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def l2_normalize(vector: Sequence[float]) -> list[float]:
    """Scale to unit length. Raises ``ValueError`` on a zero vector, because a
    zero vector has no direction to normalize."""
    norm = l2_norm(vector)
    if norm == 0.0:
        raise ValueError("cannot normalize a zero vector")
    return [value / norm for value in vector]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in [-1, 1], tolerant of unnormalized input.

    Raises ``ValueError`` on mismatched lengths; returns 0.0 when either side
    is a zero vector (no direction to compare).
    """
    if len(a) != len(b):
        raise ValueError("vectors must have equal length")
    if not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = l2_norm(a)
    norm_b = l2_norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def top_k(
    query: Sequence[float],
    items: Sequence[T],
    key: Callable[[T], Sequence[float]],
    k: int,
) -> list[T]:
    """The ``k`` items whose vector is most similar to ``query``, most similar
    first. Ties keep input order (stable sort). ``k <= 0`` returns ``[]``."""
    if k <= 0:
        return []
    scored = [(cosine_similarity(query, key(item)), index, item)
              for index, item in enumerate(items)]
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return [item for _, _, item in scored[:k]]


def deduplicate(
    items: Sequence[T],
    key: Callable[[T], Sequence[float]],
    threshold: float,
) -> list[T]:
    """Greedy near-duplicate removal: keep an item only if it is less than
    ``threshold``-similar to *every* item already kept. The first of a set of
    near-duplicates wins; order is otherwise preserved."""
    kept: list[T] = []
    for item in items:
        vector = key(item)
        if any(cosine_similarity(vector, key(existing)) >= threshold for existing in kept):
            continue
        kept.append(item)
    return kept
