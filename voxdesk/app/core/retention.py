"""Data retention enforcement (Step 9 compliance).

Deletes call records and their transcripts (Turn rows) older than the
configured retention window. Call records and transcripts hold personal and
health-adjacent data, so a documented, configurable retention policy with a
working deletion path is a hard requirement for GDPR/EU AI Act posture and for
healthcare buyers.

Recording / object-storage boundary
-----------------------------------
VoxDesk does not copy recording media into its own storage. `Call.recording_url`
is an optional pointer to provider-hosted media (Twilio) and is never populated
by the current pipeline, so there is no VoxDesk-side object to expunge here.
When an object-storage recording backend is added, its deletion MUST be wired
into this same purge (next to the Turn/Call deletes below) so recordings never
outlive the policy. Deleting media that lives in a provider account is a
provider-account operation, not VoxDesk storage.

Deliberate scope decisions:

* Audit logs are NOT deleted -- SOC 2 requires the audit trail to outlive the
  operational data it describes.
* Deletion is bounded per run (``limit``) so a first run against a large table
  does not stall the scheduler; the next tick resumes where it left off.
* The scheduler calls this once a day (``RETENTION_INTERVAL_SECONDS`` in
  scripts/scheduler.py).

Pure policy helpers (cutoff/is_expired) live in app/core/data_policy.py so they
are importable without touching the database.
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.data_policy import retention_cutoff
from app.core.logging import log
from app.db.models import Call, Turn


async def prune_webhook_receipts(session: AsyncSession) -> dict:
    """
    Drop inbound-webhook replay records past their redelivery window.

    Each channel keeps a receipt table whose only job is to make a redelivered
    provider event harmless (Step 6 exactly-once). Those receipts only need to
    cover the window in which the provider might plausibly redeliver, so they
    must be pruned or they grow without bound. The feature modules own their
    own prune window; this is just the retention seam the scheduler calls.

    Imports are lazy so `app.core.retention` does not depend on the API/feature
    modules at import time (and cannot create an import cycle with them).
    """
    from app.api.calendar_webhook_routes import (
        prune_receipts as prune_calendar_receipts,
    )
    from app.api.crm_webhook_routes import prune_receipts as prune_crm_receipts
    from app.billing.webhooks import prune_receipts as prune_billing_receipts
    from app.channels.messaging import prune_receipts as prune_message_receipts

    counts = {
        "calendar": await prune_calendar_receipts(session),
        "crm": await prune_crm_receipts(session),
        "billing": await prune_billing_receipts(session),
        "message": await prune_message_receipts(session),
    }
    if any(counts.values()):
        log.info("retention.pruned_webhook_receipts", **counts)
    return counts


async def purge_expired_calls(
    session: AsyncSession,
    *,
    days: int | None = None,
    limit: int = 1000,
) -> dict:
    """Delete calls (and their transcript turns) older than the retention
    window. Returns a summary suitable for logging."""
    days = settings.call_retention_days if days is None else days
    cutoff = retention_cutoff(days)

    stale_calls = select(Call.id).where(Call.started_at < cutoff).limit(limit)
    call_ids = [row[0] for row in (await session.execute(stale_calls)).all()]
    if not call_ids:
        return {"purged_calls": 0, "purged_turns": 0, "cutoff": cutoff.isoformat()}

    turns_deleted = await session.execute(
        delete(Turn).where(Turn.call_id.in_(call_ids))
    )
    calls_deleted = await session.execute(delete(Call).where(Call.id.in_(call_ids)))
    await session.commit()

    summary = {
        "purged_calls": calls_deleted.rowcount or 0,
        "purged_turns": turns_deleted.rowcount or 0,
        "cutoff": cutoff.isoformat(),
    }
    log.info("retention.purge", **summary)
    return summary
