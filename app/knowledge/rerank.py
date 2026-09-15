"""
Optional reranking of vector candidates.

Vector search retrieves on semantic similarity alone, which has two well-known
failure modes: a chunk that is topically close but does not contain the actual
answer outranks one that does, and near-duplicate chunks from an overlapping
window fill the whole top-k with the same sentence.

A cross-encoder would fix the first properly, but it means a model download, a
GPU-shaped latency budget, and a dependency -- for a 1.5 second voice deadline
that trade is wrong. So the default reranker is lexical and local: it costs
microseconds and fixes the failure modes that actually show up.

`Reranker` is a Protocol, so dropping in a Cohere or cross-encoder reranker
later is a new class and a config value, not a rewrite. Retrieval works
identically with reranking disabled -- it is a reordering of candidates, never
a source of them.
"""
from __future__ import annotations

import re
from typing import Protocol

import structlog

log = structlog.get_logger()

_WORD = re.compile(r"[a-z0-9]+")

#: How much the lexical signal may move a result. Vector score stays dominant;
#: the reranker breaks ties and demotes duplicates rather than reordering
#: wholesale on keyword overlap.
_LEXICAL_WEIGHT = 0.35
#: Jaccard overlap above which two chunks are treated as saying the same thing.
_DUPLICATE_THRESHOLD = 0.8
#: Multiplier applied to a near-duplicate of an already-selected chunk.
_DUPLICATE_PENALTY = 0.5


class Reranker(Protocol):
    def rerank(self, query: str, candidates: list, top_k: int) -> list:
        """Return at most `top_k` candidates, best first."""


def _terms(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


class LexicalReranker:
    """
    Blends vector score with query-term coverage and suppresses duplicates.

    Coverage is the fraction of the query's distinct terms that appear in the
    chunk. It rewards a passage that literally contains the asked-about words
    -- prices, opening hours and phone numbers are exactly the facts a caller
    asks for verbatim, and those are the queries where pure vector similarity
    is weakest.
    """

    name = "lexical"

    def rerank(self, query: str, candidates: list, top_k: int) -> list:
        if not candidates:
            return []

        query_terms = _terms(query)
        scored: list[tuple[float, object, set[str]]] = []
        for candidate in candidates:
            chunk_terms = _terms(getattr(candidate, "text", ""))
            coverage = (
                len(query_terms & chunk_terms) / len(query_terms)
                if query_terms
                else 0.0
            )
            base = float(getattr(candidate, "score", 0.0))
            combined = base * (1.0 - _LEXICAL_WEIGHT) + coverage * _LEXICAL_WEIGHT
            scored.append((combined, candidate, chunk_terms))

        # Stable ordering: equal scores keep their vector-search order.
        scored.sort(key=lambda item: -item[0])

        selected: list = []
        selected_terms: list[set[str]] = []
        for combined, candidate, chunk_terms in scored:
            penalty = 1.0
            for existing in selected_terms:
                union = chunk_terms | existing
                if union and len(chunk_terms & existing) / len(union) >= _DUPLICATE_THRESHOLD:
                    penalty = _DUPLICATE_PENALTY
                    break
            candidate.rerank_score = combined * penalty
            selected.append(candidate)
            selected_terms.append(chunk_terms)

        selected.sort(key=lambda c: -getattr(c, "rerank_score", 0.0))
        return selected[:top_k]


class NoopReranker:
    """Truncates to top_k and changes nothing else."""

    name = "noop"

    def rerank(self, query: str, candidates: list, top_k: int) -> list:
        return candidates[:top_k]


def get_reranker(enabled: bool = True) -> Reranker:
    return LexicalReranker() if enabled else NoopReranker()