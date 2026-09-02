"""
Vector storage and similarity search.

Two backends, one interface:

* **pgvector** -- used when PostgreSQL has the extension. The nearest-neighbour
  ordering happens in the database with an ivfflat/hnsw index.
* **JSON scan** -- the portable fallback. Vectors live in a JSON column and
  cosine similarity is computed in Python over the candidate rows.

The fallback is not a toy: it is what makes the whole system testable on
SQLite, and for the few thousand chunks a small business actually uploads it
is fast enough to serve production. It degrades linearly, which is why the
pgvector path exists.

**The rule this module exists to enforce:** the tenant filter is part of the
SQL WHERE clause, always. Not a Python filter over the results, not a check on
the way out. A retrieval query that somehow lost its tenant predicate must
return zero rows, not another business's price list. Every query in this file
is built through `_base_query`, which cannot be constructed without a tenant.
"""
from __future__ import annotations

import math

import structlog
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    SEARCHABLE_DOCUMENT_STATUSES,
    KnowledgeChunk,
    KnowledgeDocument,
)
from app.knowledge.embeddings import Embedder, is_compatible

log = structlog.get_logger()

#: Rows pulled from the database before scoring in the fallback path. Large
#: enough that recall is not the bottleneck, small enough to bound memory.
_SCAN_LIMIT = 5000


class VectorStoreError(Exception):
    pass


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """
    Cosine similarity, tolerant of unnormalized input.

    Vectors from `HashingEmbedder` and from OpenAI both arrive L2-normalized,
    which would make a plain dot product sufficient -- but a future provider
    may not, and a silently wrong score is worse than a few extra multiplies.
    """
    if not a or not b or len(a) != len(b):
        return 0.0

    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


#: Document fields a caller may filter on, and the column each maps to.
#:
#: An allowlist rather than "pass a dict to filter()" on purpose. Every filter
#: is a potential discovery oracle -- a caller who can filter on arbitrary
#: fields and watch the result count change can enumerate data they cannot
#: read. Restricting filters to concrete, non-sensitive document columns keeps
#: that surface fixed and reviewable, and the tenant predicate is ANDed in
#: regardless, so a filter can only ever *narrow* one tenant's own rows.
_FILTERABLE_FIELDS = {
    "source_type": "source_type",
    "title": "title",
    "original_filename": "original_filename",
    "file_type": "mime_type",
}


class UnknownFilter(VectorStoreError):
    """A filter field that is not on the allowlist."""


def _base_query(
    tenant_id,
    *,
    document_ids: list | None = None,
    embedding_identity: str | None = None,
    filters: dict | None = None,
) -> Select:
    """
    The only way to build a retrieval query.

    Joins chunks to their document and applies, in SQL:

    * `tenant_id` on the **chunk** -- the denormalised column, so this holds
      even if the join were wrong;
    * `tenant_id` on the **document** -- belt and braces, and it makes a
      cross-tenant row impossible rather than merely unlikely;
    * document status in `SEARCHABLE_DOCUMENT_STATUSES`, which is how archived
      and failed documents stop being retrievable without being deleted.
    """
    if tenant_id is None:
        # Not a defensive nicety. A None tenant in SQLAlchemy becomes
        # `IS NULL`, which would quietly match nothing today and something
        # unpleasant the day a nullable column appears.
        raise VectorStoreError("retrieval requires a tenant_id")

    query = (
        select(KnowledgeChunk, KnowledgeDocument)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeChunk.tenant_id == tenant_id,
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.status.in_(list(SEARCHABLE_DOCUMENT_STATUSES)),
            # Only chunks belonging to the document's current version. A
            # reindex that died halfway leaves stale rows; they must not be
            # served.
            KnowledgeChunk.version == KnowledgeDocument.version,
        )
    )

    if document_ids:
        query = query.where(KnowledgeChunk.document_id.in_(document_ids))

    for field, value in (filters or {}).items():
        column_name = _FILTERABLE_FIELDS.get(field)
        if column_name is None:
            # Fail loudly. Silently ignoring an unknown filter would return a
            # wider result set than the caller asked for, which for a
            # knowledge base means answering from documents they meant to
            # exclude.
            raise UnknownFilter(
                f"cannot filter on {field!r}; allowed: "
                f"{sorted(_FILTERABLE_FIELDS)}"
            )
        column = getattr(KnowledgeDocument, column_name)
        # Inside the same WHERE clause as the tenant predicate, never after.
        query = query.where(
            column.in_(list(value))
            if isinstance(value, (list, tuple, set))
            else column == value
        )

    if embedding_identity:
        # Vectors from a different model are not comparable. Filtering here
        # rather than after scoring means a model change makes documents
        # disappear from search until reindexed, instead of returning noise.
        query = query.where(KnowledgeChunk.embedding_model == embedding_identity)

    return query


async def has_pgvector(session: AsyncSession) -> bool:
    """
    Whether the connected database can do vector search natively.

    Checked at runtime rather than assumed from the driver name, because a
    Postgres deployment without the extension installed is common.
    """
    # The native path additionally needs the `embedding_vector` column added
    # by the optional pgvector migration and mapped onto KnowledgeChunk. Until
    # that is present this returns False and the scan path serves every query.
    if not hasattr(KnowledgeChunk, "embedding_vector"):
        return False

    bind = session.get_bind() if hasattr(session, "get_bind") else None
    dialect = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect != "postgresql":
        return False
    try:
        from sqlalchemy import text

        result = await session.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        )
        return result.first() is not None
    except Exception as exc:            # pragma: no cover - needs Postgres
        log.warning("vectorstore.pgvector_probe_failed", error=str(exc)[:200])
        return False


class ScoredChunk:
    """A chunk plus its score and the document it came from."""

    __slots__ = ("chunk", "document", "score")

    def __init__(self, chunk: KnowledgeChunk, document: KnowledgeDocument, score: float):
        self.chunk = chunk
        self.document = document
        self.score = score

    def __repr__(self) -> str:        # pragma: no cover - debugging aid
        return f"<ScoredChunk {self.chunk.id} score={self.score:.4f}>"


async def search(
    session: AsyncSession,
    *,
    tenant_id,
    query_vector: list[float],
    embedder: Embedder,
    limit: int = 10,
    document_ids: list | None = None,
    filters: dict | None = None,
    min_score: float = 0.0,
) -> list[ScoredChunk]:
    """
    Nearest chunks for `query_vector`, within one tenant.

    Returns at most `limit` results ordered by descending similarity, already
    filtered by `min_score`. An empty list is a normal outcome and means the
    tenant has no evidence for this question -- callers must treat it as
    "say you don't know", never as "answer from general knowledge".
    """
    if not query_vector:
        return []

    query = _base_query(
        tenant_id,
        document_ids=document_ids,
        embedding_identity=embedder.identity,
        filters=filters,
    )

    # The pgvector path would add `.order_by(KnowledgeChunk.embedding.cosine_
    # distance(query_vector)).limit(limit)` here, keeping the same WHERE
    # clause. It is deliberately gated behind a runtime probe so this file
    # imports cleanly with no pgvector package present.
    if await has_pgvector(session):    # pragma: no cover - needs Postgres
        try:
            return await _search_pgvector(
                session, query, query_vector, limit, min_score
            )
        except Exception as exc:
            # A vector index problem must degrade to a correct slow answer,
            # not to no answer during a live call.
            log.warning(
                "vectorstore.pgvector_failed_falling_back", error=str(exc)[:200]
            )

    return await _search_scan(session, query, query_vector, limit, min_score)


async def _search_scan(
    session: AsyncSession,
    query: Select,
    query_vector: list[float],
    limit: int,
    min_score: float,
) -> list[ScoredChunk]:
    """Portable path: fetch the tenant's candidate rows, score in Python."""
    rows = (await session.execute(query.limit(_SCAN_LIMIT))).all()

    scored: list[ScoredChunk] = []
    for chunk, document in rows:
        vector = chunk.embedding
        if not vector:
            continue
        score = cosine_similarity(query_vector, list(vector))
        if score >= min_score:
            scored.append(ScoredChunk(chunk, document, score))

    # Ties broken by chunk index then id so results are stable across runs --
    # a flapping top-k makes evaluation meaningless.
    scored.sort(key=lambda s: (-s.score, s.chunk.chunk_index, str(s.chunk.id)))
    return scored[:limit]


async def _search_pgvector(     # pragma: no cover - needs Postgres + pgvector
    session: AsyncSession,
    query: Select,
    query_vector: list[float],
    limit: int,
    min_score: float,
) -> list[ScoredChunk]:
    """
    Native path.

    Kept structurally identical to the scan path: same `query` object, same
    WHERE clause, only the ordering and limit move into the database. That is
    what stops the two backends from diverging on tenant safety.
    """
    from sqlalchemy import literal

    distance = KnowledgeChunk.embedding_vector.cosine_distance(  # type: ignore[attr-defined]
        literal(query_vector)
    )
    rows = (
        await session.execute(
            query.add_columns(distance.label("distance"))
            .order_by(distance)
            .limit(limit)
        )
    ).all()

    results: list[ScoredChunk] = []
    for chunk, document, distance_value in rows:
        score = 1.0 - float(distance_value)
        if score >= min_score:
            results.append(ScoredChunk(chunk, document, score))
    return results


async def count_searchable_chunks(session: AsyncSession, *, tenant_id) -> int:
    """How many chunks a tenant could actually retrieve right now."""
    from sqlalchemy import func

    query = _base_query(tenant_id).with_only_columns(func.count(KnowledgeChunk.id))
    return int((await session.execute(query)).scalar() or 0)

