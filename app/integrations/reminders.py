"""
Appointment reminders -- SMS or an outbound AI call before the appointment.

Why this matters commercially: no-shows cost a US dental clinic roughly the
value of the slot, and reminder programs are the standard fix. This is the
easiest feature to attach a dollar number to in a sales conversation, which is
what lets you quote a project price instead of an hourly rate.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Appointment, Reminder, Tenant
from app.integrations.notifications import send_sms

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


async def due_reminders(session: AsyncSession, now: datetime | None = None) -> list[Reminder]:
    now = now or datetime.utcnow()
    stmt = select(Reminder).where(Reminder.sent.is_(False), Reminder.send_at <= now)
    return list((await session.execute(stmt)).scalars().all())


async def send_reminder(
    session: AsyncSession, reminder: Reminder, *, dry_run: bool = False
) -> dict:
    appointment = await session.get(Appointment, reminder.appointment_id)
    tenant = await session.get(Tenant, reminder.tenant_id)
    if appointment is None or tenant is None:
        reminder.sent = True
        reminder.error = "appointment_or_tenant_missing"
        await session.commit()
        return {"ok": False, "reason": reminder.error}

    body = reminder_text(tenant.name, appointment.starts_at, appointment.customer_name)

    if dry_run:
        return {"ok": True, "dry_run": True, "to": appointment.customer_phone, "body": body}

    ok = await send_sms(appointment.customer_phone, body)
    reminder.sent = True
    if not ok:
        reminder.error = "send_failed"
    await session.commit()
    return {"ok": ok, "to": appointment.customer_phone}


async def run_reminder_tick(session: AsyncSession, *, dry_run: bool = False) -> dict:
    """Call every few minutes from cron/APScheduler."""
    pending = await due_reminders(session)
    results = [await send_reminder(session, r, dry_run=dry_run) for r in pending]
    return {"sent": sum(1 for r in results if r.get("ok")), "total": len(results)}

