"""
The retrieval entry point.

Everything above this layer -- the voice agent, the text agent, the evaluation
suite -- calls `retrieve()` and gets back plain result objects. Nothing above
this layer knows what an embedding is, which provider is configured, or how
the vector store works.

Two guarantees this module makes:

1. **A tenant is mandatory.** There is no "search everything" mode, not even
   for an admin. The signature cannot express one.
2. **A result carries no internal machinery.** Scores and vectors stay on the
   internal object; `to_source()` produces the trimmed dict that is safe to
   log, return over the API, or hand to prompt construction.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.knowledge import vectorstore
from app.knowledge.embeddings import EmbeddingError, get_embedder
from app.knowledge.rerank import get_reranker

log = structlog.get_logger()


@dataclass
class RetrievedChunk:
    chunk_id: str
    document_id: str
    text: str
    score: float
    title: str
    #: Page, heading, row range -- whatever the extractor recorded.
    metadata: dict = field(default_factory=dict)
    #: Set by the reranker when one runs. Internal only.
    rerank_score: float | None = None

    def to_source(self) -> dict:
        """
        The traceability record.

        Enough to answer "which document said that?" in a log, an audit trail
        or an evaluation run -- and nothing more. No score, no vector, no
        storage key, no filesystem path.
        """
        source = {
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "title": self.title,
        }
        for key in ("page", "heading", "part", "row_start", "row_end"):
            if key in self.metadata:
                source[key] = self.metadata[key]
        return source


class RetrievalTimeout(Exception):
    """Retrieval exceeded its deadline. The caller must fall back, not guess."""


async def retrieve(
    session: AsyncSession,
    *,
    tenant_id,
    query: str,
    top_k: int | None = None,
    min_score: float | None = None,
    document_ids: list | None = None,
    filters: dict | None = None,
    rerank: bool | None = None,
) -> list[RetrievedChunk]:
    """
    Find the passages of this tenant's knowledge that bear on `query`.

    `filters` narrows by document metadata (see `_FILTERABLE_FIELDS`); an
    unknown field raises rather than being ignored. Filters can only ever
    narrow the caller's own tenant -- the tenant predicate is ANDed in
    regardless, so no filter combination reaches another tenant's rows.

    Returns an empty list when there is no sufficiently similar content. That
    is a meaningful answer -- it means the business has not documented this --
    and the caller must never paper over it with model knowledge.
    """
    if not query or not query.strip():
        return []
    if tenant_id is None:
        raise vectorstore.VectorStoreError("retrieval requires a tenant_id")

    top_k = top_k if top_k is not None else settings.knowledge_top_k
    min_score = min_score if min_score is not None else settings.knowledge_min_score
    use_rerank = (
        rerank if rerank is not None else settings.knowledge_rerank_enabled
    )
    # Reranking can only reorder what vector search returned, so it needs a
    # wider candidate pool than the final k to be worth anything.
    candidate_limit = (
        max(top_k, settings.knowledge_rerank_candidates) if use_rerank else top_k
    )

    embedder = get_embedder()
    try:
        query_vector = await embedder.embed_one(query)
    except EmbeddingError as exc:
        # Log and return nothing. An embedding outage must degrade to "I don't
        # have that information", never to an ungrounded answer.
        log.warning("retrieval.embedding_failed", error=str(exc)[:200])
        return []

    scored = await vectorstore.search(
        session,
        tenant_id=tenant_id,
        query_vector=query_vector,
        embedder=embedder,
        limit=candidate_limit,
        document_ids=document_ids,
        filters=filters,
        min_score=min_score,
    )

    results = [
        RetrievedChunk(
            chunk_id=str(item.chunk.id),
            document_id=str(item.document.id),
            text=item.chunk.text,
            score=item.score,
            title=item.document.title,
            metadata=dict(item.chunk.chunk_metadata or {}),
        )
        for item in scored
    ]

    if use_rerank and results:
        results = get_reranker(True).rerank(query, results, top_k)
    else:
        results = results[:top_k]

    log.debug(
        "retrieval.completed",
        tenant_id=str(tenant_id),
        results=len(results),
        reranked=use_rerank,
    )
    return results


async def retrieve_with_timeout(
    session: AsyncSession,
    *,
    tenant_id,
    query: str,
    timeout: float | None = None,
    **kwargs,
) -> list[RetrievedChunk]:
    """
    Retrieval with a hard deadline, for the live voice path.

    A caller on the phone will not wait three seconds in silence. On timeout
    this returns an empty list rather than raising: the agent then says it
    cannot confirm and offers the existing escalation, which is a good outcome.
    Raising here would risk an unhandled error mid-call, which is not.
    """
    timeout = (
        timeout if timeout is not None
        else settings.knowledge_retrieval_timeout_seconds
    )
    try:
        return await asyncio.wait_for(
            retrieve(session, tenant_id=tenant_id, query=query, **kwargs),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.warning(
            "retrieval.timeout", tenant_id=str(tenant_id), timeout_seconds=timeout
        )
        return []
    except Exception as exc:
        # Vector store down, database blip, anything. Same rule: no knowledge
        # is safe, a fabricated answer is not.
        log.warning(
            "retrieval.failed", tenant_id=str(tenant_id), error=str(exc)[:200]
        )
        return []

