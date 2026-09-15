"""
Background worker: fires appointment reminders and outbound campaign batches.

Run it as a second process next to the API:

    python -m scripts.scheduler

It is deliberately a separate process. If a campaign loop hangs, your phone
webhooks must keep answering -- a stuck scheduler should never make the
business miss an incoming call.
"""
from __future__ import annotations

import asyncio
import signal

import structlog
from sqlalchemy import select

from app.core import observability
from app.core.config import settings
from app.core.logging import log  # noqa: F401  (configures structlog)
from app.db.models import Campaign, Tenant
from app.db.session import get_sessionmaker
from app.billing.reconciliation import run_reconciliation_tick
from app.integrations.crm.service import run_sync_tick
from app.integrations.reminders import run_reminder_tick
from app.telephony.outbound import run_campaign_tick

logger = structlog.get_logger()

REMINDER_INTERVAL_SECONDS = 120
CAMPAIGN_INTERVAL_SECONDS = 60

_stop = asyncio.Event()

#: Ingestion polling. Short, because a tenant who just uploaded a price list
#: is watching the dashboard for it to turn READY.
KNOWLEDGE_INTERVAL_SECONDS = 15
KNOWLEDGE_BATCH_SIZE = 5


#: CRM delivery polling. Slower than knowledge ingestion because nobody is
#: watching a screen for it, and faster than the reminder loop because a lead
#: that reaches the CRM twenty minutes late is a lead the business called back
#: twenty minutes late.
CRM_INTERVAL_SECONDS = 20


#: Usage reconciliation. Hourly: it rebuilds a cache and reports
#: discrepancies, so running it more often buys nothing and running it less
#: often means a metering bug is invisible for a day.
BILLING_INTERVAL_SECONDS = 3600

#: Data retention. Daily: recordings and transcripts past CALL_RETENTION_DAYS
#: are purged (app/core/retention.py). A day is plenty; the window is large.
RETENTION_INTERVAL_SECONDS = 86400

#: Step 7 observability: how often to re-publish the stuck-side-effect gauge.
#: Read-only counts, so a minute is cheap and keeps the dashboard/alert current.
STUCK_SWEEP_INTERVAL_SECONDS = 60


async def billing_reconciliation_loop() -> None:
    """
    Rebuild usage summaries and report discrepancies.

    Deliberately does **not** contact a billing provider. Requirement 37 keeps
    Stripe out of the voice path; this keeps it out of the periodic path too,
    so a Stripe outage cannot stall the worker that every other loop shares.
    Provider state is refreshed by webhooks and by an explicit reconcile
    request.
    """
    maker = get_sessionmaker()
    while not _stop.is_set():
        try:
            async with maker() as session:
                result = await run_reconciliation_tick(session)
            if result.get("discrepancies"):
                logger.warning("scheduler.billing_discrepancies", **result)
            elif result.get("tenants"):
                logger.info("scheduler.billing_reconciled", **result)
            observability.record_job_run("billing_reconciliation", ok=True)
        except Exception as exc:
            observability.record_job_run("billing_reconciliation", ok=False)
            logger.error("scheduler.billing_failed", error=str(exc))
        await _sleep(BILLING_INTERVAL_SECONDS)


async def crm_sync_loop() -> None:
    """
    Deliver queued CRM syncs.

    In the same process as the other loops, for the reason STEP 4 gave for
    keeping ingestion here: the product needs a background worker, not a
    distributed system, and requirement 14 explicitly says not to introduce a
    second job system.

    Each pass reaps syncs abandoned in PROCESSING by a worker that died, so a
    restart at the wrong moment cannot strand a lead forever.
    """
    maker = get_sessionmaker()
    while not _stop.is_set():
        try:
            async with maker() as session:
                result = await run_sync_tick(
                    session, limit=settings.crm_sync_batch_size
                )
            if result["attempted"]:
                logger.info("scheduler.crm_sync", **result)
            observability.record_job_run("crm_sync", ok=True)
        except Exception as exc:
            # The loop must survive anything. A CRM outage is routine; a
            # scheduler that exits because of one is not.
            observability.record_job_run("crm_sync", ok=False)
            logger.error("scheduler.crm_sync_failed", error=str(exc))
        await _sleep(settings.crm_sync_interval_seconds)


async def reminder_loop() -> None:
    maker = get_sessionmaker()
    while not _stop.is_set():
        try:
            async with maker() as session:
                result = await run_reminder_tick(session)
            if result["total"]:
                logger.info("scheduler.reminders", **result)
            observability.record_job_run("reminders", ok=True)
        except Exception as exc:
            observability.record_job_run("reminders", ok=False)
            logger.error("scheduler.reminder_failed", error=str(exc))
        await _sleep(REMINDER_INTERVAL_SECONDS)


async def campaign_loop() -> None:
    maker = get_sessionmaker()
    while not _stop.is_set():
        try:
            async with maker() as session:
                campaigns = (await session.execute(
                    select(Campaign).where(Campaign.is_active.is_(True))
                )).scalars().all()
                for campaign in campaigns:
                    tenant = await session.get(Tenant, campaign.tenant_id)
                    if tenant is None or not tenant.outbound_enabled:
                        continue
                    result = await run_campaign_tick(session, tenant, campaign)
                    if result.get("dialed"):
                        logger.info("scheduler.campaign",
                                    campaign=campaign.name, **result)
            observability.record_job_run("campaigns", ok=True)
        except Exception as exc:
            observability.record_job_run("campaigns", ok=False)
            logger.error("scheduler.campaign_failed", error=str(exc))
        await _sleep(CAMPAIGN_INTERVAL_SECONDS)


async def retention_loop() -> None:
    """Daily purge of calls/transcripts past the retention window, plus the
    webhook-replay receipts that only need to outlive their redelivery
    window."""
    from app.core.retention import prune_webhook_receipts, purge_expired_calls

    maker = get_sessionmaker()
    while not _stop.is_set():
        try:
            async with maker() as session:
                await purge_expired_calls(session)
                await prune_webhook_receipts(session)
            observability.record_job_run("retention", ok=True)
        except Exception as exc:
            observability.record_job_run("retention", ok=False)
            logger.error("scheduler.retention_failed", error=str(exc))
        await _sleep(RETENTION_INTERVAL_SECONDS)


async def knowledge_ingestion_loop() -> None:
    """
    Index documents that are waiting.

    Runs in the same process as the other loops for the same reason they do:
    the product needs a background worker, not a distributed system. Each pass
    also reaps documents abandoned in PROCESSING by a worker that died, so a
    restart at the wrong moment cannot strand a job forever.

    In the default "inline" ingest mode the API already indexes uploads in a
    background task, so this loop normally finds nothing and costs one query
    per interval. Setting knowledge_ingest_mode="worker" makes this the only
    path, which is what a production deployment should do -- an app process
    restarting mid-ingestion then loses nothing.
    """
    from app.knowledge.jobs import process_pending

    while not _stop.is_set():
        try:
            handled = await process_pending(limit=KNOWLEDGE_BATCH_SIZE)
            if handled:
                logger.info("scheduler.knowledge_indexed", documents=handled)
            observability.record_job_run("knowledge", ok=True)
        except Exception as exc:
            # Never let an ingestion problem kill the loop; the next pass
            # retries, and the document itself carries its own FAILED state.
            observability.record_job_run("knowledge", ok=False)
            logger.error("scheduler.knowledge_failed", error=str(exc)[:300])
        await _sleep(KNOWLEDGE_INTERVAL_SECONDS)


async def _sleep(seconds: int) -> None:
    """Sleep that wakes immediately on shutdown."""
    try:
        await asyncio.wait_for(_stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def stuck_sweep_loop() -> None:
    """Publish the stuck-side-effect gauge (Step 7 observability).

    Read-only: the *recovery* of a stuck row is the reapers' job
    (``crm.service.reap_stuck_syncs``, ``knowledge.ingest.reap_stuck_documents``).
    This loop only counts what is still stuck past its window and mirrors it
    into ``voxdesk_stuck_side_effects`` so an alert can fire before a customer
    notices a reminder or CRM write that never arrived.
    """
    maker = get_sessionmaker()
    while not _stop.is_set():
        try:
            async with maker() as session:
                counts = await observability.stuck_side_effect_counts(session)
            observability.set_stuck_side_effects(counts)
            if any(counts.values()):
                logger.warning("scheduler.stuck_side_effects", **counts)
        except Exception as exc:
            logger.error("scheduler.stuck_sweep_failed", error=str(exc))
        await _sleep(STUCK_SWEEP_INTERVAL_SECONDS)


def _start_metrics_server() -> None:
    """Serve /metrics for the scheduler's own Prometheus registry.

    The worker is a separate process from the API, so its job metrics
    (``voxdesk_job_runs_total``, ``voxdesk_job_last_success_timestamp_seconds``,
    ``voxdesk_stuck_side_effects``) are not visible through the API's scrape
    target. prometheus_client ships a tiny threaded HTTP server; binding it on
    a dedicated port gives Prometheus a second job to scrape with no web
    framework added. 0 (or unset) disables it.
    """
    port = settings.scheduler_metrics_port
    if port and port > 0:
        from prometheus_client import start_http_server

        start_http_server(port)
        logger.info("scheduler.metrics_server", port=port)


async def main() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop.set)

    _start_metrics_server()
    logger.info("scheduler.started")
    await asyncio.gather(
        reminder_loop(), campaign_loop(), knowledge_ingestion_loop(),
        crm_sync_loop(), billing_reconciliation_loop(), retention_loop(),
        stuck_sweep_loop(),
    )
    logger.info("scheduler.stopped")


if __name__ == "__main__":
    asyncio.run(main())