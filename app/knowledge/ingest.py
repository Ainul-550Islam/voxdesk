"""
The ingestion pipeline: bytes in, retrievable chunks out.

    validate -> hash/dedup -> store -> extract -> clean -> chunk -> embed
             -> persist chunks -> READY

Design commitments:

* **Idempotent.** Reindexing bumps the document version and writes chunks
  under that new version inside one transaction. A retry after a crash rewrites
  the same rows rather than adding a second copy, and the unique constraint on
  (document_id, version, chunk_index) makes that a guarantee rather than a hope.
* **No stuck jobs.** `processing_started_at` is stamped when PROCESSING
  begins; `reap_stuck_documents()` fails anything that has been processing
  past the deadline so it can be retried instead of sitting there forever.
* **Failure is a state, not an exception.** Any error inside the pipeline
  lands the document in FAILED with a short, tenant-safe message. The worker
  keeps running.
* **Uploading is not knowing.** Nothing here makes a document's text available
  to the model. It only becomes reachable through retrieval, one chunk at a
  time.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import DocumentStatus, KnowledgeChunk, KnowledgeDocument
from app.knowledge.chunking import chunk_sections, config_from_settings
from app.knowledge.embeddings import EmbeddingError, get_embedder
from app.knowledge.extractors import ExtractionError, detect_format, extract
from app.knowledge.storage import get_storage
from app.knowledge.storage.base import StorageError, build_key, safe_filename

log = structlog.get_logger()


class ValidationError(Exception):
    """The upload was rejected before any expensive work. Safe to show a user."""


class DuplicateDocument(Exception):
    """This tenant already has this exact content."""

    def __init__(self, document: KnowledgeDocument):
        self.document = document
        super().__init__(f"document already exists: {document.id}")


@dataclass
class IngestResult:
    document: KnowledgeDocument
    chunks_created: int
    reused: bool = False


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def max_file_bytes() -> int:
    return int(settings.knowledge_max_file_mb) * 1024 * 1024


def validate_upload(
    data: bytes, *, filename: str | None, declared_mime: str | None = None
) -> str:
    """
    Cheap gate in front of everything expensive.

    Size is checked before format, and format before extraction, so a 500 MB
    file costs one comparison rather than a parse. Returns the detected format
    name. Raises `ValidationError` with a message safe to return over the API.
    """
    if data is None or len(data) == 0:
        raise ValidationError("the file is empty")

    limit = max_file_bytes()
    if len(data) > limit:
        raise ValidationError(
            f"file is too large ({len(data) / 1_048_576:.1f} MB); "
            f"the limit is {settings.knowledge_max_file_mb} MB"
        )

    try:
        # Detection reads magic bytes, so a renamed executable is caught here
        # rather than by whichever parser it was disguised as.
        return detect_format(data, filename=filename, declared_mime=declared_mime)
    except ExtractionError as exc:
        raise ValidationError(str(exc)) from exc


async def _tenant_document_count(session: AsyncSession, tenant_id) -> int:
    return int(
        (
            await session.execute(
                select(func.count(KnowledgeDocument.id)).where(
                    KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.status != DocumentStatus.ARCHIVED,
                )
            )
        ).scalar()
        or 0
    )


async def find_duplicate(
    session: AsyncSession, *, tenant_id, digest: str
) -> KnowledgeDocument | None:
    """
    Look for the same content in *this tenant only*.

    The tenant predicate is not optional. Two businesses uploading an
    identical supplier PDF must each get their own document; dedup that
    crossed tenants would be a data leak wearing an optimisation's clothes.
    """
    return (
        await session.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.content_hash == digest,
            )
        )
    ).scalar_one_or_none()


async def create_document(
    session: AsyncSession,
    *,
    tenant_id,
    data: bytes,
    filename: str,
    title: str | None = None,
    declared_mime: str | None = None,
    metadata: dict | None = None,
) -> IngestResult:
    """
    Validate, deduplicate, store the bytes and register the document.

    Returns immediately with status UPLOADED. Extraction and embedding happen
    in `process_document`, which the worker calls -- an HTTP request must not
    block on a 200-page PDF.
    """
    fmt = validate_upload(data, filename=filename, declared_mime=declared_mime)

    if await _tenant_document_count(session, tenant_id) >= settings.knowledge_max_documents_per_tenant:
        raise ValidationError(
            f"document limit reached "
            f"({settings.knowledge_max_documents_per_tenant} documents)"
        )

    digest = content_hash(data)
    existing = await find_duplicate(session, tenant_id=tenant_id, digest=digest)
    if existing is not None:
        # Re-uploading identical content is almost always an accident or a
        # retry. Returning the existing document is idempotent and avoids
        # paying to embed the same text twice.
        if existing.status == DocumentStatus.ARCHIVED:
            # Except when it was archived: the tenant is deliberately
            # restoring it, so revive rather than confusingly 409.
            existing.status = DocumentStatus.UPLOADED
            existing.error_message = None
            await session.commit()
        log.info(
            "ingest.duplicate", tenant_id=str(tenant_id), document_id=str(existing.id)
        )
        raise DuplicateDocument(existing)

    clean_name = safe_filename(filename)
    document = KnowledgeDocument(
        tenant_id=tenant_id,
        title=(title or clean_name or "Untitled").strip()[:300],
        original_filename=clean_name,
        # The *detected* type is recorded, never the client's claim.
        mime_type=fmt,
        file_size=len(data),
        content_hash=digest,
        status=DocumentStatus.UPLOADED,
        version=1,
        doc_metadata=dict(metadata or {}),
    )
    session.add(document)
    await session.flush()

    key = build_key(tenant_id, document.id, clean_name)
    try:
        await get_storage().put(key, data, content_type=fmt)
    except StorageError as exc:
        await session.rollback()
        log.error("ingest.storage_failed", error=str(exc)[:200])
        raise ValidationError("the file could not be stored; please try again") from exc

    document.source_uri = key
    await session.commit()
    await session.refresh(document)

    log.info(
        "ingest.document_created",
        tenant_id=str(tenant_id),
        document_id=str(document.id),
        format=fmt,
        bytes=len(data),
    )
    return IngestResult(document=document, chunks_created=0)


async def process_document(
    session: AsyncSession, document: KnowledgeDocument, *, bump_version: bool = False
) -> IngestResult:
    """
    Extract, chunk, embed and persist. Moves the document to READY or FAILED.

    Never raises for a bad document -- that is a recorded outcome. It only
    propagates genuinely unexpected errors after marking the document FAILED,
    so a worker loop can log and continue.
    """
    tenant_id = document.tenant_id
    document.status = DocumentStatus.PROCESSING
    document.processing_started_at = datetime.utcnow()
    document.error_message = None
    if bump_version:
        # A new version means the old chunks are superseded. Retrieval filters
        # on version == document.version, so they stop being served the moment
        # this commits, even before the new ones are written.
        document.version += 1
    await session.commit()

    version = document.version
    try:
        data = await get_storage().get(document.source_uri)
    except StorageError as exc:
        return await _fail(session, document, "the stored file could not be read", exc)

    try:
        extracted = extract(
            data,
            filename=document.original_filename,
            declared_mime=document.mime_type,
        )
    except ExtractionError as exc:
        return await _fail(session, document, str(exc), exc)
    except Exception as exc:
        # An unexpected parser bug is still the document's problem, not the
        # worker's. Log the detail, show the tenant something generic.
        log.exception("ingest.extract_crashed", document_id=str(document.id))
        return await _fail(session, document, "this file could not be processed", exc)

    if extracted.is_empty:
        return await _fail(session, document, "no readable text found in this file")

    chunks = chunk_sections(extracted.sections, config_from_settings(settings))
    if not chunks:
        return await _fail(session, document, "this file produced no usable content")

    embedder = get_embedder()
    try:
        vectors = await embedder.embed([c.text for c in chunks])
    except EmbeddingError as exc:
        # Retryable: the document stays FAILED but reindexing will try again,
        # and because chunks are keyed by version nothing was half-written.
        return await _fail(session, document, "the embedding service is unavailable", exc)

    if len(vectors) != len(chunks):
        return await _fail(session, document, "the embedding service returned bad data")

    # Delete-then-insert for this version makes a retry produce exactly the
    # same rows instead of a second set. Both statements are in one
    # transaction, so a crash between them rolls back.
    await session.execute(
        delete(KnowledgeChunk).where(
            KnowledgeChunk.document_id == document.id,
            KnowledgeChunk.version == version,
        )
    )

    total_chars = 0
    total_tokens = 0
    for chunk, vector in zip(chunks, vectors):
        try:
            embedder.validate(vector)
        except EmbeddingError as exc:
            return await _fail(session, document, "embedding dimensions did not match", exc)
        session.add(
            KnowledgeChunk(
                document_id=document.id,
                tenant_id=tenant_id,
                chunk_index=chunk.index,
                version=version,
                text=chunk.text,
                token_estimate=chunk.token_estimate,
                content_hash=hashlib.sha256(chunk.text.encode()).hexdigest(),
                chunk_metadata=chunk.metadata,
                embedding=list(vector),
                # The identity string, not just the model name -- dimensions
                # are part of compatibility.
                embedding_model=embedder.identity,
            )
        )
        total_chars += chunk.char_count
        total_tokens += chunk.token_estimate

    # Older versions are dropped once the new set is staged. Keeping them
    # would double storage for no benefit: retrieval only serves the current
    # version, and the source file is still in object storage.
    await session.execute(
        delete(KnowledgeChunk).where(
            KnowledgeChunk.document_id == document.id,
            KnowledgeChunk.version != version,
        )
    )

    document.status = DocumentStatus.READY
    document.chunk_count = len(chunks)
    document.char_count = total_chars
    document.token_estimate = total_tokens
    document.embedding_model = embedder.model
    document.embedding_dimensions = embedder.dimensions
    document.ingested_at = datetime.utcnow()
    document.processing_started_at = None
    document.doc_metadata = {
        **(document.doc_metadata or {}),
        **{k: v for k, v in extracted.metadata.items() if k != "format"},
    }
    await session.commit()
    await session.refresh(document)

    log.info(
        "ingest.document_ready",
        tenant_id=str(tenant_id),
        document_id=str(document.id),
        version=version,
        chunks=len(chunks),
    )
    return IngestResult(document=document, chunks_created=len(chunks))


async def _fail(
    session: AsyncSession,
    document: KnowledgeDocument,
    message: str,
    exc: Exception | None = None,
) -> IngestResult:
    """Record a failure that a tenant can read and an engineer can debug."""
    await session.rollback()
    document.status = DocumentStatus.FAILED
    document.error_message = message[:500]
    document.processing_started_at = None
    document.chunk_count = 0
    await session.commit()
    log.warning(
        "ingest.document_failed",
        document_id=str(document.id),
        reason=message,
        error=str(exc)[:200] if exc else None,
    )
    return IngestResult(document=document, chunks_created=0)


async def reindex_document(
    session: AsyncSession, document: KnowledgeDocument
) -> IngestResult:
    """
    Rebuild a document's chunks under a new version.

    Archived documents are refused: reindexing one would quietly make it
    retrievable again, which is the opposite of what archiving meant. The
    tenant must restore it first.
    """
    if document.status == DocumentStatus.ARCHIVED:
        raise ValidationError(
            "this document is archived; restore it before reindexing"
        )
    if not document.source_uri:
        raise ValidationError("this document has no stored file to reindex")
    return await process_document(session, document, bump_version=True)


async def archive_document(session: AsyncSession, document: KnowledgeDocument) -> None:
    """
    Soft delete.

    The row, the chunks and the stored file all survive -- audit history and
    "who told the customer that?" investigations need them. Retrieval stops
    immediately because ARCHIVED is not in `SEARCHABLE_DOCUMENT_STATUSES`.
    """
    document.status = DocumentStatus.ARCHIVED
    document.processing_started_at = None
    await session.commit()
    log.info("ingest.document_archived", document_id=str(document.id))


async def restore_document(session: AsyncSession, document: KnowledgeDocument) -> None:
    """Undo an archive. The document must be reindexed to become searchable."""
    if document.status != DocumentStatus.ARCHIVED:
        return
    document.status = (
        DocumentStatus.READY if document.chunk_count else DocumentStatus.UPLOADED
    )
    await session.commit()
    log.info("ingest.document_restored", document_id=str(document.id))


async def purge_document(session: AsyncSession, document: KnowledgeDocument) -> None:
    """
    Hard delete: chunks, stored bytes, row.

    Separate from archiving and gated behind `knowledge:delete` because it is
    the one operation that destroys evidence.
    """
    key = document.source_uri
    await session.execute(
        delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
    )
    await session.delete(document)
    await session.commit()

    if key:
        try:
            await get_storage().delete(key)
        except StorageError as exc:
            # The database is already consistent; an orphaned blob is a
            # cleanup problem, not a correctness one.
            log.warning("ingest.storage_delete_failed", key=key, error=str(exc)[:200])
    log.info("ingest.document_purged", document_id=str(document.id))


async def reap_stuck_documents(
    session: AsyncSession, *, older_than_seconds: int = 900
) -> int:
    """
    Fail documents that have been PROCESSING too long.

    A worker that is killed mid-job leaves its document in PROCESSING with
    nothing left to advance it. Without this, requirement "no jobs stuck in
    PROCESSING forever" is violated by any deploy that restarts a pod at the
    wrong moment.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=older_than_seconds)
    # A single conditional UPDATE rather than load-modify-commit: a stale copy
    # of the row in the session's identity map would make the mutation a no-op
    # (the same class of bug the reminder reaper had). The UPDATE writes the
    # real current row atomically.
    result = await session.execute(
        update(KnowledgeDocument)
        .where(
            KnowledgeDocument.status == DocumentStatus.PROCESSING,
            KnowledgeDocument.processing_started_at.isnot(None),
            KnowledgeDocument.processing_started_at < cutoff,
        )
        .values(
            status=DocumentStatus.FAILED,
            error_message="processing timed out and was reset; try reindexing",
            processing_started_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    count = result.rowcount or 0
    if count:
        log.warning("ingest.reaped_stuck_documents", count=count)
    return count


async def pending_documents(
    session: AsyncSession, *, limit: int = 10
) -> list[KnowledgeDocument]:
    """Documents waiting for a worker, oldest first."""
    return list(
        (
            await session.execute(
                select(KnowledgeDocument)
                .where(KnowledgeDocument.status == DocumentStatus.UPLOADED)
                .order_by(KnowledgeDocument.created_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def claim_uploaded_document(
    session: AsyncSession, document_id, *, now: datetime | None = None
) -> bool:
    """
    Atomically move a UPLOADED document to PROCESSING. True iff this caller won.

    Step 6 (scale-compliance). `pending_documents` + `process_document` used
    to be a check-then-act: two workers could both select the same UPLOADED
    document and both run extraction and embedding, paying for the embedding
    twice. A single conditional UPDATE arbitrates the race, so exactly one
    worker embeds each version.
    """
    now = now or datetime.utcnow()
    result = await session.execute(
        update(KnowledgeDocument)
        .where(
            KnowledgeDocument.id == document_id,
            KnowledgeDocument.status == DocumentStatus.UPLOADED,
        )
        .values(
            status=DocumentStatus.PROCESSING,
            processing_started_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1