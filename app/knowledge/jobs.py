"""
Ingestion job dispatch.

There is no Celery here, and that is a decision rather than an omission. The
project already has a worker pattern -- `scripts/scheduler.py`, a plain
asyncio process with polling loops -- and introducing a broker, a result
backend and a second deployment unit to index a few PDFs would be
infrastructure the product does not need.

So this is a small abstraction with two implementations:

* **inline** (default, and correct for development): the work runs in a
  FastAPI `BackgroundTask` after the response is sent. The HTTP request is not
  blocked, and there is nothing extra to deploy.
* **worker**: the upload only writes the row; `scripts/scheduler.py` polls for
  UPLOADED documents and processes them. This is what production runs, because
  an app process restarting mid-ingestion should not lose the job.

Both paths call the same `process_document()`, so behaviour cannot diverge.
Choosing between them is `settings.knowledge_ingest_mode`.

Safety properties in both modes:

* Each job opens its **own** database session. Reusing the request's session
  in a background task is a use-after-close bug waiting to happen, and the
  request's transaction is already committed by then anyway.
* Jobs are idempotent. A document processed twice ends in the same state with
  the same chunks -- see the delete-then-insert in `ingest.process_document`.
* A job never raises into its caller. A failure becomes a FAILED document.
"""
from __future__ import annotations

import asyncio
import uuid

from app.core.config import settings
from app.core.logging import log

#: Documents currently being processed in this process. Stops a double-click
#: on "reindex" from running two overlapping jobs for the same document.
#: Process-local by design: the cross-process guarantee is the PROCESSING
#: status plus the unique constraint on (document_id, version, chunk_index),
#: not this set.
_in_flight: set[str] = set()


async def run_ingestion(document_id: uuid.UUID, *, reindex: bool = False) -> None:
    """
    Process one document in its own session and transaction.

    Safe to call from anywhere: a background task, the worker loop, or a test.
    """
    key = str(document_id)
    if key in _in_flight:
        log.info("knowledge.job_already_running", document_id=key)
        return
    _in_flight.add(key)

    try:
        from app.db.models import DocumentStatus, KnowledgeDocument
        from app.db.session import get_sessionmaker
        from app.knowledge.ingest import process_document

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            document = await session.get(KnowledgeDocument, document_id)
            if document is None:
                log.warning("knowledge.job_document_missing", document_id=key)
                return
            if document.status == DocumentStatus.ARCHIVED:
                # Archived between enqueue and execution. Honour the archive.
                log.info("knowledge.job_skipped_archived", document_id=key)
                return
            await process_document(session, document, bump_version=reindex)
    except Exception as exc:
        # The job must never take the worker or the request handler with it.
        log.error(
            "knowledge.job_crashed", document_id=key, error=str(exc)[:300]
        )
    finally:
        _in_flight.discard(key)


def enqueue_ingestion(background, document_id: uuid.UUID, *, reindex: bool = False) -> None:
    """
    Schedule ingestion for a document.

    `background` is the FastAPI `BackgroundTasks` from the request. In worker
    mode it is ignored entirely and the scheduler picks the document up from
    its UPLOADED status instead.
    """
    mode = (getattr(settings, "knowledge_ingest_mode", "inline") or "inline").lower()

    if mode == "worker":
        log.info("knowledge.job_queued_for_worker", document_id=str(document_id))
        return

    if background is not None:
        background.add_task(run_ingestion, document_id, reindex=reindex)
    else:
        # No request context (a script, a test). Fire and forget on the
        # running loop, or run it synchronously if there isn't one.
        try:
            asyncio.get_running_loop().create_task(
                run_ingestion(document_id, reindex=reindex)
            )
        except RuntimeError:
            asyncio.run(run_ingestion(document_id, reindex=reindex))


async def process_pending(limit: int = 5) -> int:
    """
    One pass of the worker loop: reap stuck jobs, then process what's waiting.

    Returns how many documents were handled, so the caller can back off when
    there is nothing to do.
    """
    from app.db.session import get_sessionmaker
    from app.knowledge.ingest import pending_documents, reap_stuck_documents

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Before claiming new work, release anything a dead worker abandoned.
        await reap_stuck_documents(
            session,
            older_than_seconds=getattr(
                settings, "knowledge_processing_timeout_seconds", 900
            ),
        )
        documents = await pending_documents(session, limit=limit)
        ids = [document.id for document in documents]

    for document_id in ids:
        await run_ingestion(document_id)
    return len(ids)
