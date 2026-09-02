"""
Embedder selection.

One entry point, driven entirely by settings, so no retrieval or ingestion
code ever imports a concrete provider.
"""
from __future__ import annotations

import structlog

from app.core.config import settings
from app.knowledge.embeddings.base import (
    Embedder,
    EmbeddingDimensionMismatch,
    EmbeddingError,
    is_compatible,
)
from app.knowledge.embeddings.hashing import HashingEmbedder

__all__ = [
    "Embedder", "EmbeddingError", "EmbeddingDimensionMismatch", "is_compatible",
    "HashingEmbedder", "get_embedder", "set_embedder", "reset_embedder",
]

log = structlog.get_logger()

_embedder: Embedder | None = None


def _build() -> Embedder:
    provider = (settings.knowledge_embedding_provider or "hashing").strip().lower()
    model = settings.knowledge_embedding_model
    dimensions = settings.knowledge_embedding_dimensions

    if provider == "hashing":
        return HashingEmbedder(dimensions=dimensions, model=model or "hashing-v1")

    if provider in ("openai", "openai-compatible"):
        from app.knowledge.embeddings.openai import OpenAIEmbedder

        return OpenAIEmbedder(
            api_key=settings.openai_api_key,
            model=model or "text-embedding-3-small",
            dimensions=dimensions,
            timeout=settings.knowledge_embedding_timeout_seconds,
            batch_size=settings.knowledge_embedding_batch_size,
        )

    raise EmbeddingError(f"unknown embedding provider: {provider!r}")


def get_embedder() -> Embedder:
    """The configured embedder, built once per process."""
    global _embedder
    if _embedder is None:
        _embedder = _build()
        log.info(
            "embeddings.provider_selected",
            provider=_embedder.provider,
            model=_embedder.model,
            dimensions=_embedder.dimensions,
        )
    return _embedder


def set_embedder(embedder: Embedder | None) -> None:
    """Override the embedder. For tests only."""
    global _embedder
    _embedder = embedder


def reset_embedder() -> None:
    set_embedder(None)

