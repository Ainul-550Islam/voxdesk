"""Campaign API (Batch 01 enterprise expansion).

Tenant-scoped campaign management over ``app.services.campaign_service``.
The cardinal rule, stated once: **no endpoint in this file places a call.**

* ``plan`` produces safe execution *intents* that the existing outbound
  mechanism may pick up; it never dials.
* DNC, call-window, attempt-limit and daily-limit checks are enforced in the
  service with the same helpers the dialer uses, and cannot be bypassed by any
  request body — there is no override flag, by design.
* RBAC: ``CAMPAIGN_READ`` (viewer+) to read, ``CAMPAIGN_WRITE`` (manager+) to
  mutate, ``CAMPAIGN_RUN`` (manager+) to produce an execution plan.

Route registration (``app/main.py``) is outside the allowed file set for this
batch and is reported as an integration dependency.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import TenantContext, require_permission
from app.auth.permissions import Permission
from app.core.errors import BadRequestError, NotFoundError
from app.db.session import get_session
from app.domain.campaign_models import (
    Audience,
    CampaignChannel,
    CampaignDefinition,
    CampaignGoal,
    CampaignSchedule,
    CampaignState,
    ComplianceGate,
    Throttle,
)
from app.domain.agent_models import stable_id
from app.services import campaign_service

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


# ----------------------------------------------------------------- schemas ---

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CampaignCreateRequest(_Strict):
    name: str = Field(min_length=1, max_length=200)
    goal: str = "qualify"
    channel: str = "voice"
    script_prompt: str = Field(default="", max_length=20000)
    opening_line: str = Field(default="", max_length=2000)
    lead_ids: list[str] = Field(default_factory=list)
    segment_ids: list[str] = Field(default_factory=list)
    daily_start: str = "09:00"
    daily_end: str = "20:00"
    days_of_week: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    timezone: str = "UTC"
    calls_per_minute: int = 2
    daily_limit: int = 200
    max_attempts_per_lead: int = 3


class CampaignUpdateRequest(CampaignCreateRequest):
    pass


class CampaignOut(_Strict):
    id: str
    tenant_id: str
    name: str
    goal: str
    channel: str
    state: str
    script_prompt: str
    opening_line: str
    lead_ids: list[str]
    segment_ids: list[str]
    daily_start: str
    daily_end: str
    days_of_week: list[int]
    timezone: str
    calls_per_minute: int
    daily_limit: int
    max_attempts_per_lead: int


class ExecutionIntentOut(_Strict):
    id: str
    campaign_id: str
    lead_id: str
    channel: str
    skipped: bool
    reason_skipped: str


class EligibilityOut(_Strict):
    eligible: int
    skipped: dict[str, int]
    intents: list[ExecutionIntentOut]


class ResultsOut(_Strict):
    metrics: dict[str, int]
    eligibility: dict[str, int]


# ---------------------------------------------------------------- helpers ---

def _enum_of(enum_cls, value: str, default):
    try:
        return enum_cls(value)
    except ValueError:
        return default


def _build_definition(tenant_id: str, campaign_id: str,
                      payload: CampaignCreateRequest) -> CampaignDefinition:
    return CampaignDefinition(
        id=campaign_id,
        tenant_id=tenant_id,
        name=payload.name,
        goal=_enum_of(CampaignGoal, payload.goal, CampaignGoal.QUALIFY),
        channel=_enum_of(CampaignChannel, payload.channel, CampaignChannel.VOICE),
        script_prompt=payload.script_prompt,
        opening_line=payload.opening_line,
        audience=Audience(tenant_id=tenant_id,
                          segment_ids=tuple(payload.segment_ids),
                          lead_ids=tuple(payload.lead_ids)),
        schedule=CampaignSchedule(
            daily_start=_parse_hhmm(payload.daily_start, 9, 0),
            daily_end=_parse_hhmm(payload.daily_end, 20, 0),
            days_of_week=tuple(payload.days_of_week),
            timezone=payload.timezone,
        ),
        throttle=Throttle(
            calls_per_minute=payload.calls_per_minute,
            daily_limit=payload.daily_limit,
            max_attempts_per_lead=payload.max_attempts_per_lead,
        ),
        compliance=ComplianceGate(),
        state=CampaignState.DRAFT,
    )


def _parse_hhmm(value: str, default_hour: int, default_minute: int):
    from datetime import time as _time

    try:
        hour, minute = (value or "").split(":")[:2]
        return _time(int(hour), int(minute))
    except (ValueError, TypeError):
        return _time(default_hour, default_minute)


def _out(definition: CampaignDefinition) -> CampaignOut:
    return CampaignOut(
        id=definition.id,
        tenant_id=definition.tenant_id,
        name=definition.name,
        goal=definition.goal.value,
        channel=definition.channel.value,
        state=definition.state.value,
        script_prompt=definition.script_prompt,
        opening_line=definition.opening_line,
        lead_ids=list(definition.audience.lead_ids),
        segment_ids=list(definition.audience.segment_ids),
        daily_start=definition.schedule.daily_start.strftime("%H:%M"),
        daily_end=definition.schedule.daily_end.strftime("%H:%M"),
        days_of_week=list(definition.schedule.days_of_week),
        timezone=definition.schedule.timezone,
        calls_per_minute=definition.throttle.calls_per_minute,
        daily_limit=definition.throttle.daily_limit,
        max_attempts_per_lead=definition.throttle.max_attempts_per_lead,
    )


def _intent_out(intent) -> ExecutionIntentOut:
    return ExecutionIntentOut(
        id=intent.id,
        campaign_id=intent.campaign_id,
        lead_id=intent.lead_id,
        channel=intent.channel.value,
        skipped=intent.skipped,
        reason_skipped=intent.reason_skipped,
    )


# ------------------------------------------------------------------- routes ---

@router.post("", response_model=CampaignOut, status_code=201)
async def create_campaign(
    payload: CampaignCreateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    provisional_id = stable_id(str(ctx.tenant_id), payload.name)
    definition = _build_definition(str(ctx.tenant_id), provisional_id, payload)
    try:
        row = await campaign_service.create_campaign(session, ctx.tenant, definition)
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    saved = await campaign_service.get_campaign(session, ctx.tenant, str(row.id))
    return _out(saved)


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    return [_out(d) for d in await campaign_service.list_campaigns(session, ctx.tenant)]


@router.get("/{campaign_id}", response_model=CampaignOut)
async def get_campaign(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    try:
        return _out(await campaign_service.get_campaign(session, ctx.tenant, campaign_id))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None


@router.patch("/{campaign_id}", response_model=CampaignOut)
async def update_campaign(
    campaign_id: str,
    payload: CampaignUpdateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    definition = _build_definition(str(ctx.tenant_id), campaign_id, payload)
    try:
        updated = await campaign_service.update_campaign(session, ctx.tenant, campaign_id, definition)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(updated)


@router.post("/{campaign_id}/schedule", response_model=CampaignOut)
async def schedule_campaign(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    try:
        return _out(await campaign_service.schedule_campaign(session, ctx.tenant, campaign_id))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/{campaign_id}/pause", response_model=CampaignOut)
async def pause_campaign(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    try:
        return _out(await campaign_service.pause_campaign(session, ctx.tenant, campaign_id))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/{campaign_id}/resume", response_model=CampaignOut)
async def resume_campaign(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    try:
        return _out(await campaign_service.resume_campaign(session, ctx.tenant, campaign_id))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/{campaign_id}/cancel", response_model=CampaignOut)
async def cancel_campaign(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    try:
        return _out(await campaign_service.cancel_campaign(session, ctx.tenant, campaign_id))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/{campaign_id}/audience", response_model=dict)
async def audience_preview(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    try:
        definition = await campaign_service.get_campaign(session, ctx.tenant, campaign_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return await campaign_service.validate_audience(session, ctx.tenant, definition)


@router.get("/{campaign_id}/eligibility", response_model=EligibilityOut)
async def eligibility_preview(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    try:
        definition = await campaign_service.get_campaign(session, ctx.tenant, campaign_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    intents = await campaign_service.execution_plan(session, ctx.tenant, definition, limit=500)
    skipped = {}
    eligible = 0
    for intent in intents:
        if intent.skipped:
            skipped[intent.reason_skipped] = skipped.get(intent.reason_skipped, 0) + 1
        else:
            eligible += 1
    return EligibilityOut(eligible=eligible, skipped=skipped,
                          intents=[_intent_out(i) for i in intents])


@router.get("/{campaign_id}/execution-status", response_model=CampaignOut)
async def execution_status(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    try:
        return _out(await campaign_service.get_campaign(session, ctx.tenant, campaign_id))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None


@router.post("/{campaign_id}/plan", response_model=list[ExecutionIntentOut])
async def execution_plan(
    campaign_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_RUN)),
    session: AsyncSession = Depends(get_session),
):
    try:
        definition = await campaign_service.get_campaign(session, ctx.tenant, campaign_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    intents = await campaign_service.execution_plan(session, ctx.tenant, definition, limit=limit)
    return [_intent_out(i) for i in intents]


@router.get("/{campaign_id}/results", response_model=ResultsOut)
async def campaign_results(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    try:
        definition = await campaign_service.get_campaign(session, ctx.tenant, campaign_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    results = await campaign_service.aggregate_results(session, ctx.tenant, definition)
    return ResultsOut(metrics=results["metrics"], eligibility=results["eligibility"])


@router.get("/{campaign_id}/kpis", response_model=dict)
async def campaign_kpis(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    try:
        definition = await campaign_service.get_campaign(session, ctx.tenant, campaign_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    metrics = await campaign_service.progress(session, ctx.tenant, definition)
    return {"total_leads": metrics.total_leads, "attempted": metrics.attempted,
            "conversions": metrics.conversions, "dnc_skipped": metrics.dnc_skipped,
            "failed": metrics.failed}
