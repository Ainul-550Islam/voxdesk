"""
The embedding abstraction.

The point of these tests is that nothing above the abstraction knows which
provider is configured, and that a vector from one model can never be compared
against a vector from another.
"""
from __future__ import annotations

import pytest

from app.knowledge.embeddings import (
    EmbeddingDimensionMismatch,
    HashingEmbedder,
    get_embedder,
    is_compatible,
    reset_embedder,
    set_embedder,
)
from app.knowledge.embeddings.base import Embedder


@pytest.fixture
def embedder():
    return HashingEmbedder(dimensions=256, model="hashing-v1")


async def test_embedding_has_the_declared_dimension(embedder):
    vectors = await embedder.embed(["hello there", "second document"])
    assert len(vectors) == 2
    assert all(len(v) == 256 for v in vectors)


async def test_embedding_is_deterministic(embedder):
    once = await embedder.embed_one("A cleaning costs 120 dollars.")
    twice = await embedder.embed_one("A cleaning costs 120 dollars.")
    assert once == twice


async def test_similar_text_scores_above_unrelated_text(embedder):
    from app.knowledge.vectorstore import cosine_similarity

    query = await embedder.embed_one("how much does a cleaning cost")
    relevant = await embedder.embed_one("A standard cleaning costs 120 dollars.")
    unrelated = await embedder.embed_one("Parking is free behind the building.")

    assert cosine_similarity(query, relevant) > cosine_similarity(query, unrelated)


async def test_empty_text_gives_a_zero_vector_not_a_crash(embedder):
    vector = await embedder.embed_one("   ")
    assert len(vector) == 256
    assert not any(vector)


async def test_batch_order_is_preserved(embedder):
    texts = ["alpha one", "beta two", "gamma three"]
    batch = await embedder.embed(texts)
    for text, vector in zip(texts, batch):
        assert vector == await embedder.embed_one(text)


def test_identity_includes_provider_model_and_dimensions(embedder):
    assert embedder.identity == "hashing:hashing-v1:256"


def test_vectors_from_a_different_model_are_incompatible(embedder):
    assert is_compatible("hashing:hashing-v1:256", embedder)
    # Same model, different width -- still a different vector space.
    assert not is_compatible("hashing:hashing-v1:512", embedder)
    assert not is_compatible("openai:text-embedding-3-small:1536", embedder)
    assert not is_compatible(None, embedder)


def test_dimension_mismatch_is_detected(embedder):
    with pytest.raises(EmbeddingDimensionMismatch):
        embedder.validate([0.1] * 128)


def test_provider_is_selected_from_config_not_hard_coded(monkeypatch):
    from app.core.config import settings

    reset_embedder()
    monkeypatch.setattr(settings, "knowledge_embedding_dimensions", 128)
    monkeypatch.setattr(settings, "knowledge_embedding_model", "hashing-v1")
    assert get_embedder().dimensions == 128


def test_unknown_provider_fails_loudly(monkeypatch):
    from app.core.config import settings
    from app.knowledge.embeddings import EmbeddingError

    reset_embedder()
    monkeypatch.setattr(settings, "knowledge_embedding_provider", "telepathy")
    with pytest.raises(EmbeddingError):
        get_embedder()


async def test_a_custom_embedder_can_be_injected():
    """
    Proves the abstraction is real: retrieval code never names a provider, so
    a test double satisfies it without touching any other module.
    """

    class Constant(Embedder):
        provider = "test"
        model = "constant"
        dimensions = 4

        async def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    set_embedder(Constant())
    assert get_embedder().identity == "test:constant:4"
    assert await get_embedder().embed_one("anything") == [1.0, 0.0, 0.0, 0.0]


def test_openai_embedder_rejects_an_impossible_dimension():
    """Configuration errors should surface at startup, not after 1000 chunks."""
    from app.knowledge.embeddings.base import EmbeddingError
    from app.knowledge.embeddings.openai import OpenAIEmbedder

    with pytest.raises(EmbeddingError):
        OpenAIEmbedder(api_key="sk-test", model="text-embedding-3-small", dimensions=99999)


def test_openai_embedder_requires_a_key():
    from app.knowledge.embeddings.base import EmbeddingError
    from app.knowledge.embeddings.openai import OpenAIEmbedder

    with pytest.raises(EmbeddingError):
        OpenAIEmbedder(api_key="")