"""
Retrieval behaviour and the lifecycle gate.

The most important assertions in this file are the negative ones: what
retrieval must *not* return. An empty result is a correct, meaningful answer
-- it means the business never documented this -- and every layer above must
be able to rely on that.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.knowledge import ingest
from app.knowledge.retrieval import retrieve, retrieve_with_timeout
from app.knowledge.vectorstore import VectorStoreError, count_searchable_chunks
from tests.conftest import add_document

KB = (
    "# Pricing\n\nA standard cleaning costs 120 dollars and takes 45 minutes.\n\n"
    "# Refunds\n\nRefunds are issued within fourteen days of purchase.\n\n"
    "# Parking\n\nParking is free in the lot behind our building.\n"
)


@pytest.fixture
async def indexed(db, tenant_a):
    return await add_document(db, tenant_a, text=KB, filename="kb.md", title="Handbook")


# ------------------------------------------------------------- relevance ---

async def test_the_relevant_chunk_is_returned(db, tenant_a, indexed):
    hits = await retrieve(db, tenant_id=tenant_a.id, query="how much does a cleaning cost")
    assert hits
    assert "120 dollars" in hits[0].text


async def test_a_different_question_finds_a_different_chunk(db, tenant_a, indexed):
    hits = await retrieve(db, tenant_id=tenant_a.id, query="where can I park")
    assert hits
    assert "Parking" in hits[0].text


async def test_an_unrelated_question_returns_nothing(db, tenant_a, indexed):
    """
    The refusal path. If this ever returns a chunk, the agent starts answering
    questions the business never documented.
    """
    hits = await retrieve(db, tenant_id=tenant_a.id, query="do you repair bicycles")
    assert hits == []


async def test_top_k_is_respected(db, tenant_a, indexed):
    hits = await retrieve(
        db, tenant_id=tenant_a.id, query="cleaning refund parking cost days",
        top_k=1, min_score=0.0,
    )
    assert len(hits) <= 1


async def test_the_minimum_score_filters_weak_matches(db, tenant_a, indexed):
    loose = await retrieve(db, tenant_id=tenant_a.id, query="cleaning", min_score=0.0)
    strict = await retrieve(db, tenant_id=tenant_a.id, query="cleaning", min_score=0.99)
    assert len(strict) < len(loose)


async def test_an_empty_query_returns_nothing(db, tenant_a, indexed):
    assert await retrieve(db, tenant_id=tenant_a.id, query="") == []
    assert await retrieve(db, tenant_id=tenant_a.id, query="   ") == []


async def test_a_tenant_with_no_documents_gets_a_clean_empty_result(db, tenant_b):
    assert await retrieve(db, tenant_id=tenant_b.id, query="anything at all") == []


async def test_results_carry_the_metadata_a_citation_needs(db, tenant_a, indexed):
    hits = await retrieve(db, tenant_id=tenant_a.id, query="refund policy")
    assert hits
    hit = hits[0]
    assert hit.chunk_id and hit.document_id
    assert hit.title == "Handbook"
    assert hit.score > 0
    assert hit.metadata.get("heading")


async def test_document_id_filter_narrows_the_search(db, tenant_a):
    prices = await add_document(
        db, tenant_a, text="A cleaning costs 120 dollars.", filename="prices.md"
    )
    await add_document(
        db, tenant_a, text="Parking is free behind the building.", filename="park.md"
    )

    hits = await retrieve(
        db, tenant_id=tenant_a.id, query="cleaning parking cost",
        document_ids=[prices.id], min_score=0.0,
    )
    assert hits
    assert all(hit.document_id == str(prices.id) for hit in hits)


# -------------------------------------------------------------- lifecycle ---

async def test_only_ready_documents_are_retrievable(db, tenant_a):
    document = await add_document(db, tenant_a, text=KB, process=False)
    assert await retrieve(db, tenant_id=tenant_a.id, query="cleaning cost") == []

    await ingest.process_document(db, document)
    assert await retrieve(db, tenant_id=tenant_a.id, query="cleaning cost")


async def test_a_document_being_processed_is_not_searchable(db, tenant_a, indexed):
    """
    PROCESSING is a half-built state: chunks for the new version may already
    be partly written. Serving them would answer a caller from a document
    that is still being rebuilt, so the status gate excludes it in SQL just
    like every other non-READY state.
    """
    from app.db.models import DocumentStatus

    assert await retrieve(db, tenant_id=tenant_a.id, query="cleaning cost")

    indexed.status = DocumentStatus.PROCESSING
    await db.commit()

    assert await retrieve(db, tenant_id=tenant_a.id, query="cleaning cost") == []
    assert not indexed.is_searchable


async def test_a_reindex_in_flight_never_serves_a_mix_of_versions(db, tenant_a, indexed):
    """
    Retrieval joins on `chunk.version == document.version`, so the instant a
    reindex bumps the version the old chunks stop being served -- even before
    the new ones exist. A half-finished rebuild can return nothing, but it can
    never return a blend of two versions.
    """
    from app.db.models import DocumentStatus, KnowledgeChunk

    original_version = indexed.version

    # Simulate a worker that bumped the version and then died mid-write.
    indexed.version += 1
    indexed.status = DocumentStatus.READY
    await db.commit()

    stale = (
        (
            await db.execute(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.document_id == indexed.id,
                    KnowledgeChunk.version == original_version,
                )
            )
        )
        .scalars()
        .all()
    )
    assert stale, "the old chunks are still in the table"
    # ...and yet none of them are retrievable.
    assert await retrieve(db, tenant_id=tenant_a.id, query="cleaning cost") == []


async def test_archived_documents_stop_being_retrievable(db, tenant_a, indexed):
    assert await retrieve(db, tenant_id=tenant_a.id, query="cleaning cost")

    await ingest.archive_document(db, indexed)
    assert await retrieve(db, tenant_id=tenant_a.id, query="cleaning cost") == []


async def test_restoring_makes_a_document_retrievable_again(db, tenant_a, indexed):
    await ingest.archive_document(db, indexed)
    await ingest.restore_document(db, indexed)
    assert await retrieve(db, tenant_id=tenant_a.id, query="cleaning cost")


async def test_failed_documents_are_never_retrievable(db, tenant_a):
    await add_document(
        db, tenant_a, text=b"%PDF-1.4\nbroken" * 30, filename="broken.pdf"
    )
    assert await retrieve(db, tenant_id=tenant_a.id, query="broken pdf") == []


async def test_a_model_change_makes_documents_unsearchable_until_reindexed(
    db, tenant_a, indexed
):
    """
    Vectors from a different model are not comparable. Serving them would give
    plausible-looking nonsense, so they are filtered out in SQL and the
    document simply disappears from search until it is reindexed.
    """
    from app.knowledge.embeddings import HashingEmbedder, set_embedder

    assert await retrieve(db, tenant_id=tenant_a.id, query="cleaning cost")

    set_embedder(HashingEmbedder(dimensions=64, model="hashing-v2"))
    assert await retrieve(db, tenant_id=tenant_a.id, query="cleaning cost") == []

    await ingest.reindex_document(db, indexed)
    assert await retrieve(db, tenant_id=tenant_a.id, query="cleaning cost")


# --------------------------------------------------------------- guards ----

async def test_retrieval_without_a_tenant_is_impossible(db, indexed):
    """There is no "search everything" mode, not even by accident."""
    with pytest.raises(VectorStoreError):
        await retrieve(db, tenant_id=None, query="cleaning")


async def test_count_searchable_chunks_requires_a_tenant(db):
    with pytest.raises(VectorStoreError):
        await count_searchable_chunks(db, tenant_id=None)


# ------------------------------------------------------------- timeouts ----

async def test_the_timeout_wrapper_returns_empty_rather_than_raising(
    db, tenant_a, indexed, monkeypatch
):
    """
    A caller is on the phone. A slow knowledge base must degrade to "I'm not
    certain", never to an exception mid-call and never to a guess.
    """
    import asyncio

    import app.knowledge.retrieval as retrieval_module

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)
        return ["should never be used"]

    monkeypatch.setattr(retrieval_module, "retrieve", slow)
    hits = await retrieve_with_timeout(
        db, tenant_id=tenant_a.id, query="cleaning cost", timeout=0.05
    )
    assert hits == []


async def test_the_timeout_wrapper_swallows_backend_failures(
    db, tenant_a, monkeypatch
):
    import app.knowledge.retrieval as retrieval_module

    async def broken(*args, **kwargs):
        raise RuntimeError("vector store is on fire")

    monkeypatch.setattr(retrieval_module, "retrieve", broken)
    assert await retrieve_with_timeout(db, tenant_id=tenant_a.id, query="x") == []


async def test_an_embedding_outage_returns_no_knowledge_not_a_guess(
    db, tenant_a, indexed
):
    from app.knowledge.embeddings import set_embedder
    from app.knowledge.embeddings.base import Embedder, EmbeddingError

    class Down(Embedder):
        provider, model, dimensions = "down", "down", 8

        async def embed(self, texts):
            raise EmbeddingError("service unavailable")

    set_embedder(Down())
    assert await retrieve(db, tenant_id=tenant_a.id, query="cleaning cost") == []


# ------------------------------------------------------------ reranking ----

async def test_retrieval_works_identically_with_reranking_off(db, tenant_a, indexed):
    with_rerank = await retrieve(
        db, tenant_id=tenant_a.id, query="how much is a cleaning", rerank=True
    )
    without = await retrieve(
        db, tenant_id=tenant_a.id, query="how much is a cleaning", rerank=False
    )
    assert with_rerank and without
    assert with_rerank[0].text == without[0].text


def test_the_reranker_promotes_a_chunk_that_contains_the_query_terms():
    from app.knowledge.rerank import LexicalReranker
    from app.knowledge.retrieval import RetrievedChunk

    vague = RetrievedChunk(
        chunk_id="1", document_id="d", title="t", score=0.42,
        text="Our team is happy to discuss service options with you.",
    )
    exact = RetrievedChunk(
        chunk_id="2", document_id="d", title="t", score=0.40,
        text="A standard cleaning costs 120 dollars.",
    )

    ranked = LexicalReranker().rerank(
        "how much does a standard cleaning cost", [vague, exact], top_k=2
    )
    assert ranked[0].chunk_id == "2"


def test_the_reranker_demotes_near_duplicate_chunks():
    """Overlapping windows must not fill the whole top-k with one sentence."""
    from app.knowledge.rerank import LexicalReranker
    from app.knowledge.retrieval import RetrievedChunk

    text = "A standard cleaning costs 120 dollars and takes 45 minutes."
    first = RetrievedChunk(chunk_id="1", document_id="d", title="t", score=0.9, text=text)
    duplicate = RetrievedChunk(chunk_id="2", document_id="d", title="t", score=0.89, text=text)
    other = RetrievedChunk(
        chunk_id="3", document_id="d", title="t", score=0.5,
        text="Cleaning appointments can be booked online.",
    )

    ranked = LexicalReranker().rerank("cleaning cost", [first, duplicate, other], top_k=3)
    assert ranked[0].chunk_id == "1"
    assert ranked[1].chunk_id == "3"


def test_the_noop_reranker_only_truncates():
    from app.knowledge.rerank import NoopReranker
    from app.knowledge.retrieval import RetrievedChunk

    items = [
        RetrievedChunk(chunk_id=str(i), document_id="d", title="t", score=1.0, text="x")
        for i in range(5)
    ]
    assert [c.chunk_id for c in NoopReranker().rerank("q", items, 2)] == ["0", "1"]
