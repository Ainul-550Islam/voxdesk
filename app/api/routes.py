"""REST API for the React dashboard (and for your own onboarding scripts)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent.llm_factory import PRESETS
from app.auth.dependencies import (
    TenantContext, get_context, get_owned, get_platform_admin, require_permission,
    scoped_permission,
)
from app.auth.permissions import Permission
from app.db.models import (
    Appointment, Call, CallDirection, CallStatus, CrmSync, Tenant, Turn,
)
from app.db.session import get_session
from app.integrations.crm import hooks as crm_hooks

router = APIRouter(prefix="/api", tags=["api"])


# ------------------------------------------------------------- schemas ----
class TenantCreate(BaseModel):
    name: str
    industry: str = "general"
    twilio_number: str
    agent_name: str = "Alex"
    greeting: str = "Thanks for calling. How can I help you today?"
    timezone: str = "America/New_York"
    knowledge_base: dict = {}
    google_calendar_id: str | None = None
    notify_sms_number: str | None = None
    escalation_number: str | None = None

    # কোন AI চালাবে
    llm_preset: str = "natural"          # fast(ChatGPT) | natural(Claude) | cheap(Gemini) | smart
    llm_provider: str | None = None      # অথবা সরাসরি
    llm_model: str | None = None

    # মানুষের মতো শোনানোর নব
    humanize: bool = True
    vad_stop_secs: float = 0.45          # 0.30 দ্রুত <-> 0.70 নিরাপদ
    speech_speed: float = 1.0
    temperature: float = 0.65
    voice_id: str | None = None
    language: str = "en-US"


class VoiceSettings(BaseModel):
    """লাইভ টিউনিং -- ক্লায়েন্টের সাথে কলে বসে নব ঘোরানোর জন্য।"""
    llm_preset: str | None = None
    humanize: bool | None = None
    vad_stop_secs: float | None = None
    speech_speed: float | None = None
    temperature: float | None = None
    voice_id: str | None = None


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    twilio_number: str
    agent_name: str
    plan: str
    minutes_used: float
    included_minutes: int

    class Config:
        from_attributes = True


# ------------------------------------------------------------- tenants ----
@router.post("/tenants", response_model=TenantOut, status_code=201,
             include_in_schema=False)
async def create_tenant(
    payload: TenantCreate,
    _: TenantContext = Depends(get_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    """
    Provisioning a whole new business is an operator action, not something a
    tenant user may do. `get_platform_admin` denies everyone; use
    `python -m scripts.seed_demo_tenant` or a dedicated ops path instead.
    """
    tenant = Tenant(**payload.model_dump())
    session.add(tenant)
    await session.commit()
    await session.refresh(tenant)
    return tenant


@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants(
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_READ)),
    session: AsyncSession = Depends(get_session),
):
    """
    Returns ONLY the caller's own tenant. This previously returned every
    tenant in the deployment to any anonymous caller.
    """
    return [ctx.tenant]


# --------------------------------------------------------- AI নির্বাচন ----
@router.get("/llm/presets")
async def llm_presets(
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_READ)),
):
    """ড্যাশবোর্ডে ড্রপডাউন ভরার জন্য -- ক্লায়েন্ট নিজেই AI বদলাতে পারবে।"""
    label = {"openai": "ChatGPT", "anthropic": "Claude", "google": "Gemini"}
    return [
        {
            "key": key,
            "brand": label[c.provider],
            "provider": c.provider,
            "model": c.model,
            "latency_ms": c.est_latency_ms,
            "notes": c.notes,
        }
        for key, c in PRESETS.items()
    ]


@router.patch("/tenants/{tenant_id}/voice")
async def update_voice_settings(
    tenant_id: uuid.UUID,
    payload: VoiceSettings,
    ctx: TenantContext = Depends(scoped_permission(Permission.TENANT_UPDATE)),
    session: AsyncSession = Depends(get_session),
):
    """কল চলাকালীন নয়, কিন্তু পরের কল থেকেই কার্যকর।

    Workflow যেটা ক্লায়েন্টকে মুগ্ধ করে:
      1. ক্লায়েন্ট বলল "রোবটটা তাড়াহুড়ো করছে"
      2. আপনি vad_stop_secs 0.45 -> 0.60 করলেন
      3. তাকে আবার কল দিতে বললেন -> ঠিক হয়ে গেছে
    """
    # scoped_permission already proved tenant_id == ctx.tenant_id; use the
    # authenticated object rather than re-fetching by a client-supplied id.
    tenant = ctx.tenant

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(tenant, field, value)
    await session.commit()

    return {
        "ok": True,
        "llm_preset": tenant.llm_preset,
        "humanize": tenant.humanize,
        "vad_stop_secs": tenant.vad_stop_secs,
        "speech_speed": tenant.speech_speed,
    }


# --------------------------------------------------------------- calls ----
@router.get("/tenants/{tenant_id}/calls")
async def list_calls(
    tenant_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None, description="a CallStatus value"),
    direction: str | None = Query(None, description="inbound | outbound"),
    booked: bool | None = Query(None),
    transferred: bool | None = Query(None),
    search: str | None = Query(None, max_length=64, description="phone fragment"),
    start: datetime | None = Query(None, description="UTC lower bound, inclusive"),
    end: datetime | None = Query(None, description="UTC upper bound, exclusive"),
    ctx: TenantContext = Depends(scoped_permission(Permission.CALL_READ)),
    session: AsyncSession = Depends(get_session),
):
    """
    Paginated, filtered call log.

    STEP 8 added everything except `limit`. The dashboard previously fetched
    whatever the default returned and filtered nothing, so there was no way to
    reach call 51 and no way to answer "show me yesterday's missed calls".

    Filtering happens **here**, not in the browser (requirement 6). Every
    predicate is ANDed into the same `WHERE` as the tenant scope, so a filter
    can only ever narrow the caller's own rows -- the same discipline the
    STEP 4 knowledge filters use. `search` is a parameterised `LIKE` on the
    phone numbers only; there is no free-form field selector, so a filter
    cannot become a way to probe columns.

    Returns an envelope rather than a bare list. A pager needs a total, and
    adding one later would be a breaking change for every client.
    """
    scope = [Call.tenant_id == ctx.tenant_id]

    if status:
        try:
            scope.append(Call.status == CallStatus(status.lower()))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown status {status!r}")
    if direction:
        try:
            scope.append(Call.direction == CallDirection(direction.lower()))
        except ValueError:
            raise HTTPException(
                status_code=422, detail=f"Unknown direction {direction!r}"
            )
    if booked is not None:
        scope.append(Call.booked.is_(booked))
    if transferred is not None:
        scope.append(Call.escalated.is_(transferred))
    if start is not None:
        scope.append(Call.started_at >= start)
    if end is not None:
        scope.append(Call.started_at < end)
    if search:
        fragment = f"%{search.strip()}%"
        scope.append(Call.from_number.ilike(fragment) | Call.to_number.ilike(fragment))

    total = (
        await session.execute(select(func.count(Call.id)).where(*scope))
    ).scalar() or 0

    rows = (
        await session.execute(
            select(Call)
            .where(*scope)
            .order_by(Call.started_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()

    return {
        "calls": [
            {
                "id": str(c.id),
                "from": c.from_number,
                "to": c.to_number,
                "direction": c.direction.value,
                "started_at": c.started_at,
                "duration": c.duration_seconds,
                "status": c.status,
                "intent": c.intent,
                "booked": c.booked,
                "escalated": c.escalated,
                "transfer_state": c.transfer_state.value,
                "summary": c.summary,
                "llm_used": c.llm_used,
                # Whether a recording *exists*, not where it lives. The URL is
                # served by the detail endpoint, which re-checks permission.
                "has_recording": bool(c.recording_url),
                "lead_id": str(c.lead_id) if c.lead_id else None,
            }
            for c in rows
        ],
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }


@router.get("/calls/{call_id}")
async def call_detail(
    call_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ)),
    session: AsyncSession = Depends(get_session),
):
    """
    One call, with the related records the detail page needs.

    Assembled server-side so the page is one request rather than five
    (requirement 32). `get_owned` 404s on another tenant's id rather than
    403ing, so ids cannot be probed for existence.

    `recording_url` requires `RECORDING_READ` on top of `CALL_READ`: a
    transcript is operational data, but the audio is the customer's voice.
    """
    call = await get_owned(session, Call, call_id, ctx)

    appointment = (
        await session.execute(
            select(Appointment).where(
                Appointment.tenant_id == ctx.tenant_id,
                Appointment.call_id == call.id,
            )
        )
    ).scalars().first()

    lead = None
    if call.lead_id:
        lead = await session.get(Lead, call.lead_id)
        if lead is not None and lead.tenant_id != ctx.tenant_id:
            lead = None          # defensive: an id is not authorization

    crm_syncs = (
        (
            await session.execute(
                select(CrmSync).where(
                    CrmSync.tenant_id == ctx.tenant_id,
                    CrmSync.entity_id == call.id,
                )
            )
        )
        .scalars()
        .all()
    )

    return {
        "id": str(call.id),
        "call_sid": call.call_sid,
        "direction": call.direction.value,
        "status": call.status.value,
        "from": call.from_number,
        "to": call.to_number,
        "started_at": call.started_at,
        "ended_at": call.ended_at,
        "duration": call.duration_seconds,
        "intent": call.intent,
        "summary": call.summary,
        "booked": call.booked,
        "escalated": call.escalated,
        "lead_score": call.lead_score,
        "llm_used": call.llm_used,
        "transfer": {
            "state": call.transfer_state.value,
            "reason": call.transfer_reason,
            "destination": call.transfer_destination,
            "started_at": call.transfer_started_at,
            "completed_at": call.transfer_completed_at,
            "failed_at": call.transfer_failed_at,
        },
        "appointment": None if appointment is None else {
            "id": str(appointment.id),
            "status": appointment.status.value,
            "starts_at": appointment.starts_at,
            "timezone": appointment.timezone,
            "customer_name": appointment.customer_name,
            "meeting_url": appointment.meeting_url,
        },
        "lead": None if lead is None else {
            "id": str(lead.id),
            "name": lead.name,
            "phone": lead.phone,
            "status": lead.status.value,
            "score": lead.score,
        },
        "crm_syncs": [
            {
                "provider": sync.provider.value,
                "status": sync.status.value,
                "attempt_count": sync.attempt_count,
                # Already scrubbed by `errors.safe_message` before storage.
                "last_error": sync.last_error,
                "synced_at": sync.synced_at,
            }
            for sync in crm_syncs
        ],
        "has_recording": bool(call.recording_url),
        "recording_url": (
            call.recording_url if ctx.can(Permission.RECORDING_READ) else None
        ),
    }


@router.get("/calls/{call_id}/transfer")
async def call_transfer_detail(
    call_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.CALL_READ)),
    session: AsyncSession = Depends(get_session),
):
    """
    How the human escalation on this call actually went.

    `get_owned` 404s for another tenant's call id, so transfer metadata is
    scoped exactly like transcripts are.

    `transfer_destination` is redacted: the dashboard needs to show *that* a
    transfer happened, not to become a directory of staff mobile numbers.
    """
    call = await get_owned(session, Call, call_id, ctx)
    return {
        "call_id": str(call.id),
        "status": call.status.value,
        "escalated": call.escalated,
        "transfer_state": call.transfer_state.value,
        "transfer_destination": phone.redact(call.transfer_destination),
        "transfer_reason": call.transfer_reason,
        "transfer_attempts": call.transfer_attempts,
        "transfer_error": call.transfer_error,
        "transfer_requested_at": call.transfer_requested_at,
        "transfer_started_at": call.transfer_started_at,
        "transfer_completed_at": call.transfer_completed_at,
        "transfer_failed_at": call.transfer_failed_at,
        "failure_reason": call.failure_reason,
    }


@router.get("/calls/{call_id}/transcript")
async def transcript(
    call_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.TRANSCRIPT_READ)),
    session: AsyncSession = Depends(get_session),
):
    # get_owned 404s when the call belongs to another tenant, so a call id
    # cannot be probed for existence across the deployment.
    await get_owned(session, Call, call_id, ctx)
    call = (
        await session.execute(
            select(Call).options(selectinload(Call.turns)).where(Call.id == call_id)
        )
    ).scalar_one_or_none()
    if call is None:
        raise HTTPException(404, "call not found")
    return [
        {"speaker": t.speaker, "text": t.text, "at": t.created_at}
        for t in sorted(call.turns, key=lambda x: x.created_at)
    ]


# ----------------------------------------------------------- analytics ----
@router.get("/tenants/{tenant_id}/stats")
async def stats(
    tenant_id: uuid.UUID,
    days: int = 30,
    ctx: TenantContext = Depends(scoped_permission(Permission.ANALYTICS_READ)),
    session: AsyncSession = Depends(get_session),
):
    since = datetime.utcnow() - timedelta(days=days)
    base = select(func.count()).select_from(Call).where(
        Call.tenant_id == ctx.tenant_id, Call.started_at >= since
    )
    total = (await session.execute(base)).scalar_one()
    booked = (await session.execute(base.where(Call.booked.is_(True)))).scalar_one()
    escalated = (await session.execute(base.where(Call.escalated.is_(True)))).scalar_one()
    minutes = (
        await session.execute(
            select(func.coalesce(func.sum(Call.duration_seconds), 0.0) / 60.0).where(
                Call.tenant_id == ctx.tenant_id, Call.started_at >= since
            )
        )
    ).scalar_one()
    return {
        "days": days,
        "calls": total,
        "booked": booked,
        "escalated": escalated,
        "booking_rate": round(booked / total, 3) if total else 0,
        "minutes": round(float(minutes), 1),
        # This is the number you put on the invoice.
        "estimated_value_usd": booked * 150,
    }


# =============================================================================
# v0.3 -- leads, campaigns, IVR, languages, compliance
# =============================================================================

from app.core import compliance as _compliance          # noqa: E402
from app.core.i18n import get_profile, supported_languages  # noqa: E402
from app.db.models import Campaign, Lead, LeadStatus, Reminder  # noqa: E402
from app.telephony import ivr as _ivr                   # noqa: E402
from app.telephony import phone
from app.telephony.outbound import run_campaign_tick    # noqa: E402


# ------------------------------------------------------------- languages ----
@router.get("/languages")
async def list_languages(
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_READ)),
):
    """Dashboard dropdown. Shows which STT model each language will use."""
    return {"default": "en-US", "languages": supported_languages()}


# ------------------------------------------------------------------ leads ----
class LeadIn(BaseModel):
    name: str = ""
    phone: str
    email: str | None = None
    company: str | None = None
    notes: str = ""
    custom_fields: dict = {}


class LeadBulkIn(BaseModel):
    campaign_id: uuid.UUID | None = None
    leads: list[LeadIn]


@router.post("/tenants/{tenant_id}/leads", status_code=201)
async def add_leads(
    tenant_id: uuid.UUID,
    payload: LeadBulkIn,
    ctx: TenantContext = Depends(scoped_permission(Permission.LEAD_CREATE)),
    session: AsyncSession = Depends(get_session),
):
    """Bulk import (CSV upload in the dashboard posts here). Skips duplicates."""
    existing = set(
        (await session.execute(
            select(Lead.phone).where(Lead.tenant_id == ctx.tenant_id)
        )).scalars().all()
    )

    # A campaign supplied in the body must belong to the caller's tenant,
    # otherwise leads could be injected into another tenant's dialer queue.
    if payload.campaign_id is not None:
        await get_owned(session, Campaign, payload.campaign_id, ctx)

    created, skipped = 0, 0
    new_leads: list[Lead] = []
    for item in payload.leads:
        phone = item.phone.strip()
        if not phone or phone in existing:
            skipped += 1
            continue
        lead = Lead(
            tenant_id=ctx.tenant_id, campaign_id=payload.campaign_id,
            name=item.name, phone=phone, email=item.email,
            company=item.company, notes=item.notes,
            custom_fields=item.custom_fields,
        )
        session.add(lead)
        new_leads.append(lead)
        existing.add(phone)
        created += 1

    # Flush once for the whole batch rather than once per lead: importing two
    # thousand leads should be one round trip's worth of id generation, not
    # two thousand.
    if new_leads:
        await session.flush()
        for lead in new_leads:
            await crm_hooks.on_lead_created(session, lead)

    await session.commit()
    return {"created": created, "skipped": skipped}


@router.get("/tenants/{tenant_id}/leads")
async def list_leads(
    tenant_id: uuid.UUID,
    status: str | None = None,
    limit: int = 100,
    ctx: TenantContext = Depends(scoped_permission(Permission.LEAD_READ)),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Lead).where(Lead.tenant_id == ctx.tenant_id)
    if status:
        stmt = stmt.where(Lead.status == status)
    rows = (await session.execute(
        stmt.order_by(Lead.score.desc().nullslast(), Lead.created_at.desc()).limit(limit)
    )).scalars().all()
    return [
        {
            "id": str(l.id), "name": l.name, "phone": l.phone, "email": l.email,
            "company": l.company, "status": l.status.value, "score": l.score,
            "attempts": l.attempts,
            "next_attempt_at": l.next_attempt_at.isoformat() if l.next_attempt_at else None,
        }
        for l in rows
    ]


@router.post("/tenants/{tenant_id}/leads/{lead_id}/do-not-call")
async def mark_lead_dnc(
    tenant_id: uuid.UUID, lead_id: uuid.UUID,
    ctx: TenantContext = Depends(scoped_permission(Permission.LEAD_UPDATE)),
    session: AsyncSession = Depends(get_session),
):
    lead = await get_owned(session, Lead, lead_id, ctx)
    lead.status = LeadStatus.DNC
    await session.commit()
    return {"ok": True, "phone": lead.phone, "status": "do_not_call"}


# -------------------------------------------------------------- campaigns ----
class CampaignIn(BaseModel):
    name: str
    goal: str = "qualify"                 # qualify | remind | followup | survey
    script_prompt: str = ""
    opening_line: str = "Hi, this is {agent} calling from {business}. Do you have a quick minute?"
    calls_per_minute: int = 2
    is_active: bool = False


@router.post("/tenants/{tenant_id}/campaigns", status_code=201)
async def create_campaign(
    tenant_id: uuid.UUID, payload: CampaignIn,
    ctx: TenantContext = Depends(scoped_permission(Permission.CAMPAIGN_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    campaign = Campaign(tenant_id=ctx.tenant_id, **payload.model_dump())
    session.add(campaign)
    await session.commit()
    await session.refresh(campaign)
    return {"id": str(campaign.id), "name": campaign.name, "is_active": campaign.is_active}


@router.get("/tenants/{tenant_id}/campaigns")
async def list_campaigns(
    tenant_id: uuid.UUID,
    ctx: TenantContext = Depends(scoped_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(
        select(Campaign).where(Campaign.tenant_id == ctx.tenant_id)
    )).scalars().all()
    out = []
    for c in rows:
        total = (await session.execute(
            select(func.count(Lead.id)).where(Lead.campaign_id == c.id)
        )).scalar_one()
        done = (await session.execute(
            select(func.count(Lead.id)).where(
                Lead.campaign_id == c.id,
                Lead.status.in_([LeadStatus.CALLED, LeadStatus.QUALIFIED,
                                 LeadStatus.UNQUALIFIED]),
            )
        )).scalar_one()
        out.append({
            "id": str(c.id), "name": c.name, "goal": c.goal,
            "is_active": c.is_active, "calls_per_minute": c.calls_per_minute,
            "leads_total": total, "leads_done": done,
        })
    return out


@router.post("/tenants/{tenant_id}/campaigns/{campaign_id}/run")
async def run_campaign(
    tenant_id: uuid.UUID, campaign_id: uuid.UUID, dry_run: bool = True,
    ctx: TenantContext = Depends(scoped_permission(Permission.CAMPAIGN_RUN)),
    session: AsyncSession = Depends(get_session),
):
    """
    One batch. `dry_run=true` is the default so nobody dials by accident.
    This endpoint spends real money, hence its own CAMPAIGN_RUN permission.
    """
    campaign = await get_owned(session, Campaign, campaign_id, ctx)
    return await run_campaign_tick(session, ctx.tenant, campaign, dry_run=dry_run)


# --------------------------------------------------------------------- IVR ----
class IvrIn(BaseModel):
    enabled: bool = True
    flow: dict


@router.get("/tenants/{tenant_id}/ivr")
async def get_ivr(
    tenant_id: uuid.UUID,
    ctx: TenantContext = Depends(scoped_permission(Permission.TENANT_READ)),
    session: AsyncSession = Depends(get_session),
):
    tenant = ctx.tenant
    return {
        "enabled": tenant.ivr_enabled,
        "flow": tenant.ivr_flow or _ivr.DEFAULT_FLOW,
        "default_flow": _ivr.DEFAULT_FLOW,
    }


@router.put("/tenants/{tenant_id}/ivr")
async def set_ivr(
    tenant_id: uuid.UUID, payload: IvrIn,
    ctx: TenantContext = Depends(scoped_permission(Permission.TENANT_UPDATE)),
    session: AsyncSession = Depends(get_session),
):
    """Rejects a broken flow instead of shipping a dead menu to real callers."""
    tenant = ctx.tenant

    problems = _ivr.validate_flow(payload.flow)
    if problems:
        raise HTTPException(422, {"errors": problems})

    tenant.ivr_flow = payload.flow
    tenant.ivr_enabled = payload.enabled
    await session.commit()
    return {"ok": True, "enabled": tenant.ivr_enabled}


@router.post("/ivr/validate")
async def validate_ivr(payload: IvrIn, 
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_READ)),
):
    """Live validation for the dashboard editor."""
    problems = _ivr.validate_flow(payload.flow)
    return {"valid": not problems, "errors": problems}


# -------------------------------------------------------------- compliance ----
class MessageCheckIn(BaseModel):
    body: str
    is_first_of_thread: bool = False


@router.post("/compliance/check-message")
async def check_message(payload: MessageCheckIn, 
    ctx: TenantContext = Depends(require_permission(Permission.COMPLIANCE_READ)),
):
    """Run before any bulk SMS. Also shows the segment count / billing impact."""
    issues = _compliance.check_message(
        payload.body, is_first_of_thread=payload.is_first_of_thread
    )
    return {
        "sendable": not any(i.severity == "error" for i in issues),
        "issues": [{"severity": i.severity, "message": i.message} for i in issues],
        "segments": _compliance.count_segments(payload.body),
    }


@router.get("/compliance/a2p-checklist")
async def a2p_checklist(business: str = "Your Business", 
    ctx: TenantContext = Depends(require_permission(Permission.COMPLIANCE_READ)),
):
    return {
        "checklist": _compliance.registration_checklist(),
        "sample_messages": _compliance.sample_messages(business),
    }


@router.get("/tenants/{tenant_id}/compliance")
async def tenant_compliance(
    tenant_id: uuid.UUID,
    ctx: TenantContext = Depends(scoped_permission(Permission.COMPLIANCE_READ)),
    session: AsyncSession = Depends(get_session),
):
    tenant = ctx.tenant
    dnc = (await session.execute(
        select(func.count(Lead.id)).where(
            Lead.tenant_id == ctx.tenant_id, Lead.status == LeadStatus.DNC
        )
    )).scalar_one()
    return {
        "a2p_status": tenant.a2p_status,
        "a2p_brand_sid": tenant.a2p_brand_sid,
        "a2p_campaign_sid": tenant.a2p_campaign_sid,
        "do_not_call_count": dnc,
        "outbound_window": (
            f"{tenant.outbound_window_open:%H:%M}-{tenant.outbound_window_close:%H:%M} "
            f"{tenant.timezone}"
        ),
        "recording_enabled": tenant.record_calls,
        "language": get_profile(tenant.language).name,
    }
