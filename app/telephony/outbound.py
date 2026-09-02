"""
Outbound calling: cold-call campaigns, follow-ups and appointment reminders.

This is the half of the product the inbound receptionist does not cover, and
the half most Fiverr gigs charge extra for ("AI Cold Calling Agent",
"Lead Qualification & Follow-Up Agent", "Appointment Reminders").

Compliance is not optional here. US TCPA rules restrict *when* you may call and
require you to honour do-not-call. `is_call_window_open()` and the DNC check in
`next_callable_leads()` are the guardrails -- do not bypass them to hit a quota.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import (
    Call,
    CallDirection,
    CallStatus,
    Campaign,
    Lead,
    LeadStatus,
    Tenant,
)

log = structlog.get_logger()

# Back off longer after each failed try instead of hammering the same number.
RETRY_BACKOFF_HOURS = (1, 4, 24)


# ------------------------------------------------------------------ timing ---

def is_call_window_open(tenant: Tenant, now: datetime | None = None) -> bool:
    """True only inside the tenant's outbound window, in the tenant's timezone."""
    tz = ZoneInfo(tenant.timezone)
    now = (now.astimezone(tz) if now else datetime.now(tz))
    open_t: dtime = tenant.outbound_window_open
    close_t: dtime = tenant.outbound_window_close
    return open_t <= now.time() <= close_t


def next_window_start(tenant: Tenant, now: datetime | None = None) -> datetime:
    """When the window next opens -- used to schedule instead of drop."""
    tz = ZoneInfo(tenant.timezone)
    now = (now.astimezone(tz) if now else datetime.now(tz))
    today_open = now.replace(
        hour=tenant.outbound_window_open.hour,
        minute=tenant.outbound_window_open.minute,
        second=0, microsecond=0,
    )
    if now < today_open:
        return today_open
    if now.time() <= tenant.outbound_window_close:
        return now
    return today_open + timedelta(days=1)


def backoff_for(attempts: int) -> timedelta:
    idx = min(max(attempts - 1, 0), len(RETRY_BACKOFF_HOURS) - 1)
    return timedelta(hours=RETRY_BACKOFF_HOURS[idx])


# ------------------------------------------------------------------- queue ---

async def next_callable_leads(
    session: AsyncSession, tenant: Tenant, campaign: Campaign, limit: int = 10
) -> list[Lead]:
    """Leads that are due, under the attempt cap, and not on do-not-call."""
    now = datetime.utcnow()
    stmt = (
        select(Lead)
        .where(
            Lead.tenant_id == tenant.id,
            Lead.campaign_id == campaign.id,
            Lead.status.in_([LeadStatus.NEW, LeadStatus.QUEUED]),
            Lead.attempts < tenant.max_call_attempts,
        )
        .order_by(Lead.next_attempt_at.is_(None).desc(), Lead.next_attempt_at)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        lead for lead in rows
        if lead.status is not LeadStatus.DNC
        and (lead.next_attempt_at is None or lead.next_attempt_at <= now)
    ]


# ------------------------------------------------------------------ dialer ---

def _twilio_client():
    """Imported lazily so the package is not needed for tests."""
    from twilio.rest import Client
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


def build_outbound_twiml_url(campaign_id: str, lead_id: str) -> str:
    """Twilio fetches this when the callee picks up; it returns the <Connect>."""
    base = settings.public_base_url.rstrip("/")
    return f"{base}/telephony/outbound-answer?campaign_id={campaign_id}&lead_id={lead_id}"


async def place_call(
    session: AsyncSession,
    tenant: Tenant,
    campaign: Campaign,
    lead: Lead,
    *,
    dry_run: bool = False,
) -> dict:
    """Dial one lead. Records the attempt whether or not Twilio succeeds."""
    if not tenant.outbound_enabled:
        return {"ok": False, "reason": "outbound_disabled"}
    if lead.status is LeadStatus.DNC:
        return {"ok": False, "reason": "do_not_call"}
    if lead.attempts >= tenant.max_call_attempts:
        return {"ok": False, "reason": "max_attempts"}
    if not is_call_window_open(tenant):
        lead.next_attempt_at = next_window_start(tenant).replace(tzinfo=None)
        await session.commit()
        return {"ok": False, "reason": "outside_window",
                "retry_at": lead.next_attempt_at.isoformat()}

    caller_id = tenant.outbound_caller_id or tenant.twilio_number
    lead.attempts += 1
    lead.last_attempt_at = datetime.utcnow()
    lead.status = LeadStatus.QUEUED

    if dry_run:
        await session.commit()
        return {"ok": True, "dry_run": True, "to": lead.phone, "from": caller_id}

    try:
        client = _twilio_client()
        tw_call = client.calls.create(
            to=lead.phone,
            from_=caller_id,
            url=build_outbound_twiml_url(str(campaign.id), str(lead.id)),
            status_callback=f"{settings.public_base_url.rstrip('/')}/telephony/status",
            status_callback_event=["completed", "no-answer", "busy", "failed"],
            machine_detection="Enable",       # do not talk to voicemail
            timeout=25,
        )
    except Exception as exc:
        lead.status = LeadStatus.FAILED
        lead.next_attempt_at = datetime.utcnow() + backoff_for(lead.attempts)
        await session.commit()
        log.error("outbound.dial_failed", lead=str(lead.id), error=str(exc))
        return {"ok": False, "reason": "dial_failed", "error": str(exc)}

    session.add(Call(
        tenant_id=tenant.id,
        call_sid=tw_call.sid,
        from_number=caller_id,
        to_number=lead.phone,
        status=CallStatus.RINGING,
        direction=CallDirection.OUTBOUND,
        lead_id=lead.id,
    ))
    lead.next_attempt_at = datetime.utcnow() + backoff_for(lead.attempts)
    await session.commit()
    log.info("outbound.dialed", to=lead.phone, campaign=campaign.name, sid=tw_call.sid)
    return {"ok": True, "call_sid": tw_call.sid, "to": lead.phone}


async def run_campaign_tick(
    session: AsyncSession, tenant: Tenant, campaign: Campaign, *, dry_run: bool = False
) -> dict:
    """One throttled batch. Call this on a schedule (cron / APScheduler)."""
    if not campaign.is_active:
        return {"dialed": 0, "reason": "campaign_inactive"}
    if not is_call_window_open(tenant):
        return {"dialed": 0, "reason": "outside_window"}

    leads = await next_callable_leads(session, tenant, campaign, limit=campaign.calls_per_minute)
    results = [await place_call(session, tenant, campaign, l, dry_run=dry_run) for l in leads]
    return {"dialed": sum(1 for r in results if r.get("ok")), "results": results}
