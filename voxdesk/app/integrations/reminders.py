"""
Appointment reminders -- SMS or an outbound AI call before the appointment.

Why this matters commercially: no-shows cost a US dental clinic roughly the
value of the slot, and reminder programs are the standard fix. This is the
easiest feature to attach a dollar number to in a sales conversation, which is
what lets you quote a project price instead of an hourly rate.

Exactly-once (Step 6, scale-compliance)
---------------------------------------
Twilio's Messages API has **no** idempotency key, so the provider cannot
dedupe a retried send for us. The guarantee therefore lives in the database:

* ``_claim`` takes a durable lease with a single conditional UPDATE
  (``sent = false AND (claimed_at IS NULL OR claimed_at < now - lease)``).
  Only one worker's UPDATE affects a row, so two overlapping ticks can never
  both send the same reminder.
* The lease is committed *before* the SMS is sent, so a crash after Twilio
  accepted the message but before we recorded ``sent`` cannot cause an
  immediate re-send: the row is still leased until it expires.
* ``release_stuck_reminders`` reclaims leases abandoned by a dead worker after
  ``reminder_lease_seconds``.

The residual duplicate window is exactly the lease duration around a crash
that lands between "Twilio accepted" and "we committed ``sent``". That is the
best achievable under Twilio's at-least-once contract; it is a bounded,
documented window, not an unbounded retry.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.metrics import record_side_effect
from app.db.models import Appointment, Reminder, Tenant
from app.integrations.notifications import send_sms
from app.telephony import phone

log = structlog.get_logger()


def reminder_text(business: str, when: datetime, customer_name: str = "") -> str:
    who = f"Hi {customer_name}, " if customer_name else "Hi, "
    return (
        f"{who}this is a reminder of your appointment with {business} on "
        f"{when:%A %B %-d} at {when:%-I:%M %p}. "
        f"Reply C to confirm or R to reschedule."
    )


def reminder_call_script(business: str, when: datetime, customer_name: str = "") -> str:
    who = customer_name or "there"
    return (
        f"Hi {who}, this is a quick reminder from {business}. "
        f"You have an appointment on {when:%A} at {when:%-I:%M %p}. "
        f"Are you still able to make it?"
    )


async def schedule_for_appointment(
    session: AsyncSession, tenant: Tenant, appointment: Appointment, channel: str = "sms"
) -> Reminder | None:
    """Queue one reminder N hours before. No-op if it would already be in the past."""
    if not tenant.reminder_enabled:
        return None

    send_at = appointment.starts_at - timedelta(hours=tenant.reminder_hours_before)
    if send_at <= datetime.utcnow().replace(tzinfo=send_at.tzinfo):
        return None

    reminder = Reminder(
        tenant_id=tenant.id,
        appointment_id=appointment.id,
        channel=channel,
        send_at=send_at,
    )
    session.add(reminder)
    await session.commit()
    log.info("reminder.scheduled", at=send_at.isoformat(), channel=channel)
    return reminder


def _worker_id() -> str:
    """A bounded worker identity for the lease. Never a secret or a phone."""
    return f"worker-{uuid.uuid4().hex[:12]}"


def _lease_expired(claimed_at: datetime | None, now: datetime, lease: int) -> bool:
    if claimed_at is None:
        return True
    return claimed_at < now - timedelta(seconds=lease)


async def _claim(
    session: AsyncSession,
    reminder_id: uuid.UUID,
    *,
    worker: str,
    now: datetime | None = None,
) -> bool:
    """
    Atomically take the send lease. True iff this worker now owns the row.

    A single conditional UPDATE arbitrates the race: two workers issuing it
    for the same reminder can never both get a rowcount of 1. The lease is
    committed immediately so a crash right after claiming still leaves the row
    visibly owned for the lease duration.
    """
    now = now or datetime.utcnow()
    lease = settings.reminder_lease_seconds
    result = await session.execute(
        update(Reminder)
        .where(
            Reminder.id == reminder_id,
            Reminder.sent.is_(False),
            or_(
                Reminder.claimed_at.is_(None),
                Reminder.claimed_at < now - timedelta(seconds=lease),
            ),
        )
        .values(claimed_at=now, claimed_by=worker)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1


async def release_stuck_reminders(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """
    Reclaim leases abandoned by a worker that died mid-send.

    A reminder is only released once its lease has expired, so a healthy,
    in-flight send is never double-claimed. Returns how many were released.
    """
    now = now or datetime.utcnow()
    lease = settings.reminder_lease_seconds
    cutoff = now - timedelta(seconds=lease)
    # A single conditional UPDATE rather than load-modify-commit: the session's
    # identity map may still hold a stale copy of a row (its claimed_at was set
    # by an earlier Core UPDATE), and mutating that stale object would commit
    # nothing. The UPDATE writes the real current row atomically.
    result = await session.execute(
        update(Reminder)
        .where(
            Reminder.sent.is_(False),
            Reminder.claimed_at.isnot(None),
            Reminder.claimed_at < cutoff,
        )
        .values(claimed_at=None, claimed_by=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    count = result.rowcount or 0
    if count:
        log.warning(
            "reminder.lease_released",
            count=count,
            reason="worker did not finish; lease reclaimed",
        )
        record_side_effect("reminder", "reconciled", n=count)
    return count


async def due_reminders(session: AsyncSession, now: datetime | None = None) -> list[Reminder]:
    """
    Reminders that are due and not currently leased to a live worker.

    The lease filter here is an optimisation (the atomic claim in `_claim` is
    the real guarantee): it stops an unleased row from being offered to a
    worker that would just lose the claim.
    """
    now = now or datetime.utcnow()
    lease = settings.reminder_lease_seconds
    cutoff = now - timedelta(seconds=lease)
    stmt = (
        select(Reminder)
        .where(
            Reminder.sent.is_(False),
            Reminder.send_at <= now,
            or_(Reminder.claimed_at.is_(None), Reminder.claimed_at < cutoff),
        )
        .order_by(Reminder.send_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def send_reminder(
    session: AsyncSession, reminder: Reminder, *, dry_run: bool = False
) -> dict:
    """
    Send one reminder, assuming the caller already holds the lease.

    Finalizes the row (``sent`` + clear lease) in the same commit as the
    outcome, so a success is durable and the lease is released atomically.
    """
    appointment = await session.get(Appointment, reminder.appointment_id)
    tenant = await session.get(Tenant, reminder.tenant_id)
    if appointment is None or tenant is None:
        reminder.sent = True
        reminder.claimed_at = None
        reminder.claimed_by = None
        reminder.error = "appointment_or_tenant_missing"
        await session.commit()
        record_side_effect("reminder", "failure")
        return {"ok": False, "reason": reminder.error}

    body = reminder_text(tenant.name, appointment.starts_at, appointment.customer_name)

    if dry_run:
        return {"ok": True, "dry_run": True, "to": appointment.customer_phone, "body": body}

    record_side_effect("reminder", "attempt")
    ok = await send_sms(appointment.customer_phone, body)
    reminder.sent = True
    reminder.claimed_at = None
    reminder.claimed_by = None
    if not ok:
        reminder.error = "send_failed"
    await session.commit()
    record_side_effect("reminder", "success" if ok else "failure")
    return {"ok": ok, "to": phone.redact(appointment.customer_phone)}


async def run_reminder_tick(session: AsyncSession, *, dry_run: bool = False) -> dict:
    """
    One worker pass: reclaim dead leases, then claim and send what is due.

    Every send is behind the atomic lease claim, so a retried tick — or two
    scheduler processes overlapping — sends each reminder at most once per
    lease window.
    """
    released = await release_stuck_reminders(session)
    pending = await due_reminders(session)
    worker = _worker_id()

    sent = 0
    claimed = 0
    for reminder in pending:
        if not dry_run:
            owned = await _claim(session, reminder.id, worker=worker)
            if not owned:
                # Another worker owns it. Its send is in flight; nothing to do.
                record_side_effect("reminder", "duplicate")
                continue
        claimed += 1
        result = await send_reminder(session, reminder, dry_run=dry_run)
        if result.get("ok"):
            sent += 1

    return {
        "sent": sent,
        "total": len(pending),
        "claimed": claimed,
        "released": released,
    }
