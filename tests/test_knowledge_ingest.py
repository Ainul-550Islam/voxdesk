"""
Ingestion: validation, deduplication, lifecycle and retry safety.

Everything here runs the real pipeline against a real database. The one
substitution is object storage, pointed at a temp directory by the autouse
fixture in conftest.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.db.models import DocumentStatus, KnowledgeChunk, KnowledgeDocument
from app.knowledge import ingest
from tests.conftest import add_document
from tests.knowledge_fixtures import make_docx, make_pdf

POLICY = (
    "# Refunds\n\nRefunds are issued within fourteen days of purchase.\n\n"
    "# Parking\n\nParking is free in the lot behind our building.\n"
)


async def _chunk_count(db, document) -> int:
    return int(
        (
            await db.execute(
                select(func.count(KnowledgeChunk.id)).where(
                    KnowledgeChunk.document_id == document.id
                )
            )
        ).scalar()
        or 0
    )


# ------------------------------------------------------------ happy path ---

async def test_upload_then_process_reaches_ready(db, tenant_a):
    document = await add_document(db, tenant_a, text=POLICY, filename="policy.md")

    assert document.status is DocumentStatus.READY
    assert document.chunk_count > 0
    assert document.char_count > 0
    assert document.token_estimate > 0
    assert document.ingested_at is not None
    assert document.embedding_model
    assert document.embedding_dimensions
    assert document.is_searchable


async def test_upload_starts_in_uploaded_not_ready(db, tenant_a):
    """
    A document is not knowledge the moment it lands. Nothing is retrievable
    until extraction, chunking and embedding have all succeeded.
    """
    document = await add_document(db, tenant_a, text=POLICY, process=False)
    assert document.status is DocumentStatus.UPLOADED
    assert document.chunk_count == 0
    assert not document.is_searchable


async def test_pdf_ingests_with_page_metadata_on_chunks(db, tenant_a):
    data = make_pdf(["Refund Policy. Fourteen days.", "Pricing. 120 dollars."])
    document = await add_document(db, tenant_a, text=data, filename="p.pdf")

    chunks = (
        (
            await db.execute(
                select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
            )
        )
        .scalars()
        .all()
    )
    assert {c.chunk_metadata.get("page") for c in chunks} == {1, 2}


async def test_docx_ingests(db, tenant_a):
    data = make_docx([("Heading 1", "Hours"), ("Normal", "Open nine to five.")])
    document = await add_document(db, tenant_a, text=data, filename="h.docx")
    assert document.status is DocumentStatus.READY


async def test_stored_file_key_is_opaque_and_tenant_scoped(db, tenant_a):
    """
    `source_uri` is a storage key, never a filesystem path -- requirement 28
    is satisfied structurally rather than by remembering to redact.
    """
    document = await add_document(db, tenant_a, text=POLICY, process=False)
    assert document.source_uri.startswith(f"tenant/{tenant_a.id}/")
    assert not document.source_uri.startswith("/")
    assert ".." not in document.source_uri


# ------------------------------------------------------------ validation ---

async def test_empty_file_is_rejected(db, tenant_a):
    with pytest.raises(ingest.ValidationError):
        await ingest.create_document(
            db, tenant_id=tenant_a.id, data=b"", filename="empty.txt"
        )


async def test_oversized_file_is_rejected_before_processing(db, tenant_a, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "knowledge_max_file_mb", 1)
    with pytest.raises(ingest.ValidationError) as exc:
        await ingest.create_document(
            db, tenant_id=tenant_a.id, data=b"x" * (2 * 1024 * 1024),
            filename="big.txt",
        )
    assert "too large" in str(exc.value)


async def test_executable_is_rejected(db, tenant_a):
    with pytest.raises(ingest.ValidationError):
        await ingest.create_document(
            db, tenant_id=tenant_a.id, data=b"MZ\x90\x00" * 20, filename="setup.exe"
        )


async def test_document_limit_is_enforced(db, tenant_a, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "knowledge_max_documents_per_tenant", 1)
    await add_document(db, tenant_a, text="First document.", filename="a.txt",
                       process=False)
    with pytest.raises(ingest.ValidationError) as exc:
        await ingest.create_document(
            db, tenant_id=tenant_a.id, data=b"Second document.", filename="b.txt"
        )
    assert "limit" in str(exc.value)


# --------------------------------------------------------- deduplication ---

async def test_identical_content_in_the_same_tenant_is_deduplicated(db, tenant_a):
    first = await add_document(db, tenant_a, text=POLICY, filename="policy.md")

    with pytest.raises(ingest.DuplicateDocument) as exc:
        await ingest.create_document(
            db, tenant_id=tenant_a.id, data=POLICY.encode(),
            filename="policy-copy.md",
        )
    assert exc.value.document.id == first.id


async def test_different_tenants_may_hold_identical_content(db, tenant_a, tenant_b):
    """
    Two businesses uploading the same supplier price list is normal. Dedup
    that crossed tenants would be a data leak wearing an optimisation's
    clothes.
    """
    a = await add_document(db, tenant_a, text=POLICY, filename="policy.md")
    b = await add_document(db, tenant_b, text=POLICY, filename="policy.md")

    assert a.id != b.id
    assert a.content_hash == b.content_hash
    assert a.tenant_id != b.tenant_id


async def test_duplicate_does_not_create_a_second_set_of_embeddings(db, tenant_a):
    document = await add_document(db, tenant_a, text=POLICY)
    before = await _chunk_count(db, document)

    with pytest.raises(ingest.DuplicateDocument):
        await ingest.create_document(
            db, tenant_id=tenant_a.id, data=POLICY.encode(), filename="again.md"
        )

    total = int(
        (
            await db.execute(
                select(func.count(KnowledgeChunk.id)).where(
                    KnowledgeChunk.tenant_id == tenant_a.id
                )
            )
        ).scalar()
    )
    assert total == before


# -------------------------------------------------------------- failures ---

async def test_malformed_pdf_lands_in_failed_with_a_safe_message(db, tenant_a):
    document = await add_document(
        db, tenant_a, text=b"%PDF-1.4\n" + b"garbage" * 40,
        filename="broken.pdf", process=True,
    )
    assert document.status is DocumentStatus.FAILED
    assert document.error_message
    # A tenant-safe summary, not a stack trace.
    assert "Traceback" not in document.error_message
    assert len(document.error_message) <= 500
    assert not document.is_searchable


async def test_a_failed_document_produces_no_chunks(db, tenant_a):
    document = await add_document(
        db, tenant_a, text=b"%PDF-1.4\nbroken" * 30, filename="x.pdf"
    )
    assert await _chunk_count(db, document) == 0
    assert document.chunk_count == 0


async def test_embedding_failure_marks_the_document_failed_not_ready(db, tenant_a):
    from app.knowledge.embeddings import set_embedder
    from app.knowledge.embeddings.base import Embedder, EmbeddingError

    class Broken(Embedder):
        provider, model, dimensions = "broken", "broken", 8

        async def embed(self, texts):
            raise EmbeddingError("provider timed out")

    document = await add_document(db, tenant_a, text=POLICY, process=False)
    set_embedder(Broken())
    result = await ingest.process_document(db, document)

    assert result.document.status is DocumentStatus.FAILED
    assert "embedding" in result.document.error_message.lower()


async def test_a_processing_document_that_died_is_reaped(db, tenant_a):
    """
    No job may sit in PROCESSING forever. A worker killed mid-ingestion leaves
    nothing behind to advance the document, so the reaper resets it.
    """
    from datetime import datetime, timedelta

    document = await add_document(db, tenant_a, text=POLICY, process=False)
    document.status = DocumentStatus.PROCESSING
    document.processing_started_at = datetime.utcnow() - timedelta(hours=2)
    await db.commit()

    reaped = await ingest.reap_stuck_documents(db, older_than_seconds=900)
    await db.refresh(document)

    assert reaped == 1
    assert document.status is DocumentStatus.FAILED
    assert document.processing_started_at is None


async def test_a_recently_started_document_is_not_reaped(db, tenant_a):
    from datetime import datetime

    document = await add_document(db, tenant_a, text=POLICY, process=False)
    document.status = DocumentStatus.PROCESSING
    document.processing_started_at = datetime.utcnow()
    await db.commit()

    assert await ingest.reap_stuck_documents(db, older_than_seconds=900) == 0


# ------------------------------------------------------ retry idempotency ---

async def test_reprocessing_the_same_version_does_not_duplicate_chunks(db, tenant_a):
    document = await add_document(db, tenant_a, text=POLICY)
    before = await _chunk_count(db, document)

    await ingest.process_document(db, document)   # a retry after a crash
    assert await _chunk_count(db, document) == before


async def test_reindexing_twice_leaves_one_set_of_chunks(db, tenant_a):
    document = await add_document(db, tenant_a, text=POLICY)
    before = await _chunk_count(db, document)

    await ingest.reindex_document(db, document)
    await ingest.reindex_document(db, document)

    assert await _chunk_count(db, document) == before
    assert document.version == 3
    assert document.status is DocumentStatus.READY


async def test_reindex_bumps_the_version_and_retires_old_chunks(db, tenant_a):
    document = await add_document(db, tenant_a, text=POLICY)
    await ingest.reindex_document(db, document)

    versions = (
        (
            await db.execute(
                select(KnowledgeChunk.version).where(
                    KnowledgeChunk.document_id == document.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(versions) == {document.version}


# ----------------------------------------------- archive, restore, purge ---

async def test_archiving_keeps_the_row_and_the_chunks(db, tenant_a):
    document = await add_document(db, tenant_a, text=POLICY)
    chunks = await _chunk_count(db, document)

    await ingest.archive_document(db, document)

    assert document.status is DocumentStatus.ARCHIVED
    assert not document.is_searchable
    # Audit history survives a "delete".
    assert await _chunk_count(db, document) == chunks
    assert await db.get(KnowledgeDocument, document.id) is not None


async def test_reindexing_an_archived_document_is_refused(db, tenant_a):
    document = await add_document(db, tenant_a, text=POLICY)
    await ingest.archive_document(db, document)

    with pytest.raises(ingest.ValidationError) as exc:
        await ingest.reindex_document(db, document)
    assert "archived" in str(exc.value)


async def test_restoring_makes_a_document_searchable_again(db, tenant_a):
    document = await add_document(db, tenant_a, text=POLICY)
    await ingest.archive_document(db, document)
    await ingest.restore_document(db, document)

    assert document.status is DocumentStatus.READY
    assert document.is_searchable


async def test_re_uploading_archived_content_revives_it(db, tenant_a):
    """A tenant re-uploading a file they archived means to bring it back."""
    document = await add_document(db, tenant_a, text=POLICY)
    await ingest.archive_document(db, document)

    with pytest.raises(ingest.DuplicateDocument) as exc:
        await ingest.create_document(
            db, tenant_id=tenant_a.id, data=POLICY.encode(), filename="policy.md"
        )
    assert exc.value.document.status is DocumentStatus.UPLOADED


async def test_purge_removes_the_row_the_chunks_and_the_file(db, tenant_a):
    from app.knowledge.storage import get_storage

    document = await add_document(db, tenant_a, text=POLICY)
    key = document.source_uri
    document_id = document.id

    await ingest.purge_document(db, document)

    assert await db.get(KnowledgeDocument, document_id) is None
    assert (
        int(
            (
                await db.execute(
                    select(func.count(KnowledgeChunk.id)).where(
                        KnowledgeChunk.document_id == document_id
                    )
                )
            ).scalar()
        )
        == 0
    )
    assert not await get_storage().exists(key)

