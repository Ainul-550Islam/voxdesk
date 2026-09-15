"""Aggregate analytics service (Batch 01 enterprise expansion).

Every figure here is a tenant-scoped ``SELECT count/sum/avg`` over the existing
tables, using the same rate definitions the dashboard already documents
(answer rate = answered/total, booking rate = booked/eligible, and so on).
Two rules from ``app/api/analytics_routes.py`` are honoured:

* **No raw customer data.** Output is counts, rates and money-shaped estimates;
  ``KpiSnapshot.assert_no_pii`` is asserted in the test suite.
* **Ranges are bounded.** Any custom range longer than ``MAX_RANGE_DAYS`` is
  rejected, so a single request can never scan a decade of history.

Money is an *estimate* derived from configured unit prices and metered usage —
it is never an invoice (billing owns that).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import BadRequestError
from app.db.models import (
    Appointment,
    AppointmentStatus,
    Call,
    CallStatus,
    Lead,
    LeadStatus,
    UsageEvent,
    UsageMetric,
)
from app.domain.analytics_models import (
    AppointmentKpi,
    CallKpi,
    CostKpi,
    FunnelKpi,
    KpiGranularity,
    KpiKind,
    KpiPoint,
    KpiSnapshot,
    LeadKpi,
    QualityKpi,
    SlaKpi,
)

#: Same threshold the dashboard uses to define an "eligible" (answerable) call.
ELIGIBLE_CALL_SECONDS = 10
MAX_RANGE_DAYS = 400
SLA_TARGET_SECONDS = 30


def _pct(num: int | float, den: int | float) -> float:
    return round(num / den * 100, 1) if den else 0.0


def _parse_utc(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)


def _validate_range(start: datetime | date, end: datetime | date) -> tuple[datetime, datetime]:
    s, e = _parse_utc(start), _parse_utc(end)
    if s >= e:
        raise BadRequestError("range start must be before range end")
    if (e - s).days > MAX_RANGE_DAYS:
        raise BadRequestError(f"range must be at most {MAX_RANGE_DAYS} days")
    return s, e


async def _count(session: AsyncSession, model, *conditions) -> int:
    stmt = select(func.count()).select_from(model).where(*conditions)
    return (await session.execute(stmt)).scalar() or 0


# --------------------------------------------------------------------- calls ---

async def call_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> CallKpi:
    s, e = _validate_range(start, end)
    base = [Call.tenant_id == uuid.UUID(str(tenant_id)), Call.started_at >= s, Call.started_at <= e]
    total = await _count(session, Call, *base)
    answered = await _count(session, Call, *base, Call.status == CallStatus.COMPLETED)
    failed = await _count(session, Call, *base, Call.status == CallStatus.FAILED)
    no_answer = await _count(session, Call, *base, Call.status == CallStatus.NO_ANSWER)
    transferred = await _count(session, Call, *base, Call.status == CallStatus.TRANSFERRED)
    escalated = await _count(session, Call, *base, Call.escalated.is_(True))
    avg = (await session.execute(
        select(func.avg(Call.duration_seconds)).where(*base)
    )).scalar() or 0.0
    return CallKpi(
        total=total, answered=answered, completed=answered, failed=failed,
        no_answer=no_answer, transferred=transferred, escalated=escalated,
        avg_duration_seconds=round(float(avg), 2),
    )


async def funnel_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> FunnelKpi:
    s, e = _validate_range(start, end)
    base = [Call.tenant_id == uuid.UUID(str(tenant_id)), Call.started_at >= s, Call.started_at <= e]
    total = await _count(session, Call, *base)
    answered = await _count(session, Call, *base, Call.duration_seconds >= ELIGIBLE_CALL_SECONDS)
    booked = await _count(session, Call, *base, Call.booked.is_(True))
    qualified = await _count(session, Lead, Lead.tenant_id == uuid.UUID(str(tenant_id)),
                             Lead.status == LeadStatus.QUALIFIED)
    return FunnelKpi(total=total, answered=answered, engaged=answered,
                     qualified=qualified, booked=booked)


async def appointment_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> AppointmentKpi:
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    base = [Appointment.tenant_id == tid, Appointment.created_at >= s, Appointment.created_at <= e]
    total = await _count(session, Appointment, *base)
    confirmed = await _count(session, Appointment, *base, Appointment.status == AppointmentStatus.CONFIRMED)
    cancelled = await _count(session, Appointment, *base, Appointment.status == AppointmentStatus.CANCELLED)
    no_show = await _count(session, Appointment, *base, Appointment.status == AppointmentStatus.NO_SHOW)
    return AppointmentKpi(total=total, confirmed=confirmed, cancelled=cancelled, no_show=no_show)


async def lead_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> LeadKpi:
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    base = [Lead.tenant_id == tid, Lead.created_at >= s, Lead.created_at <= e]
    total = await _count(session, Lead, *base)
    qualified = await _count(session, Lead, *base, Lead.status == LeadStatus.QUALIFIED)
    unqualified = await _count(session, Lead, *base, Lead.status == LeadStatus.UNQUALIFIED)
    dnc = await _count(session, Lead, *base, Lead.status == LeadStatus.DNC)
    return LeadKpi(total=total, qualified=qualified, unqualified=unqualified, dnc=dnc,
                   converted=qualified)


async def agent_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> list[dict[str, Any]]:
    """Per-agent performance. ``agent_id`` is the tenant itself in this batch
    (agents map onto the tenant's live configuration until an agent table
    exists), so the result is the single live-agent view — honest, aggregate-safe."""
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    base = [Call.tenant_id == tid, Call.started_at >= s, Call.started_at <= e]
    calls = await _count(session, Call, *base)
    bookings = await _count(session, Call, *base, Call.booked.is_(True))
    escalations = await _count(session, Call, *base, Call.escalated.is_(True))
    avg = (await session.execute(
        select(func.avg(Call.duration_seconds)).where(*base)
    )).scalar() or 0.0
    return [{
        "agent_id": str(tenant_id),
        "calls": calls,
        "bookings": bookings,
        "escalations": escalations,
        "avg_duration_seconds": round(float(avg), 2),
        "booking_rate": _pct(bookings, calls),
        "escalation_rate": _pct(escalations, calls),
    }]


async def provider_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> list[dict[str, Any]]:
    """Per-LLM-provider call counts and latency (from the real ``llm_used`` column)."""
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    stmt = (
        select(Call.llm_used, func.count(), func.avg(Call.avg_response_ms))
        .where(Call.tenant_id == tid, Call.started_at >= s, Call.started_at <= e,
               Call.llm_used.is_not(None))
        .group_by(Call.llm_used)
    )
    rows = (await session.execute(stmt)).all()
    providers = []
    for provider, calls, avg_latency in rows:
        provider_name = provider or "unknown"
        providers.append({
            "provider": provider_name,
            "calls": calls,
            "avg_latency_ms": round(float(avg_latency), 2) if avg_latency is not None else None,
        })
    return providers


async def campaign_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> list[dict[str, Any]]:
    """Per-campaign lead outcomes (aggregate-safe, no lead identities)."""
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    stmt = (
        select(Lead.campaign_id, func.count(), func.sum(case((Lead.status == LeadStatus.QUALIFIED, 1), else_=0)))
        .where(Lead.tenant_id == tid, Lead.created_at >= s, Lead.created_at <= e,
               Lead.campaign_id.is_not(None))
        .group_by(Lead.campaign_id)
    )
    rows = (await session.execute(stmt)).all()
    results = []
    for campaign_id, total, converted in rows:
        results.append({
            "campaign_id": str(campaign_id),
            "total": total,
            "conversions": int(converted or 0),
            "conversion_rate": _pct(int(converted or 0), total),
        })
    return results


async def quality_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> QualityKpi:
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    base = [Call.tenant_id == tid, Call.started_at >= s, Call.started_at <= e]
    avg = (await session.execute(
        select(func.avg(Call.avg_response_ms)).where(*base, Call.avg_response_ms.is_not(None))
    )).scalar()
    resolved = await _count(session, Call, *base, Call.status == CallStatus.COMPLETED)
    total = await _count(session, Call, *base)
    return QualityKpi(
        avg_response_ms=round(float(avg), 2) if avg is not None else None,
        p95_response_ms=None,          # percentile needs window functions; deferred
        positive_sentiment_rate=0.0,   # sentiment has no persisted column yet
        negative_sentiment_rate=0.0,
        resolved_rate=_pct(resolved, total),
    )


async def sla_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> SlaKpi:
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    base = [Call.tenant_id == tid, Call.started_at >= s, Call.started_at <= e]
    within = await _count(session, Call, *base, Call.avg_response_ms <= SLA_TARGET_SECONDS,
                          Call.avg_response_ms.is_not(None))
    breached = await _count(session, Call, *base, Call.avg_response_ms > SLA_TARGET_SECONDS)
    return SlaKpi(within_target=within, breached=breached, target_seconds=SLA_TARGET_SECONDS)


async def cost_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> CostKpi:
    """Usage × configured unit price. An estimate — never an invoice."""
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    stmt = (
        select(UsageEvent.metric, func.sum(UsageEvent.quantity))
        .where(UsageEvent.tenant_id == tid, UsageEvent.created_at >= s, UsageEvent.created_at <= e)
        .group_by(UsageEvent.metric)
    )
    rows = dict((await session.execute(stmt)).all())
    prices = settings.cost_unit_prices or {}

    def _quantity(metric: UsageMetric) -> int:
        value = rows.get(metric)
        return int(value) if value is not None else 0

    # UsageEvent quantities are in smallest units (seconds for voice); prices
    # are per-minute, so convert before pricing. Estimates only, never invoices.
    minutes = _quantity(UsageMetric.VOICE_MINUTE) / 60.0
    sms = _quantity(UsageMetric.SMS_SEGMENT)
    tokens = _quantity(UsageMetric.LLM_TOKEN)
    cost = 0
    cost += int(minutes * prices.get("voice_minute", 0))
    cost += int(sms * prices.get("sms_segment", 0))
    token_price = next(
        (v for k, v in prices.items() if k.startswith("llm_1k_tokens:")), 0
    )
    cost += int((tokens / 1000.0) * token_price)
    return CostKpi(
        minutes=round(float(minutes), 2),
        sms_segments=sms,
        llm_tokens=tokens,
        estimated_cost_millicents=int(cost),
    )


# ----------------------------------------------------------------- snapshot ---

async def snapshot(
    session: AsyncSession,
    tenant_id: str,
    kind: KpiKind,
    *,
    start: datetime | date,
    end: datetime | date,
    granularity: KpiGranularity = KpiGranularity.CUSTOM,
) -> KpiSnapshot:
    """Assemble one KPI point for the requested kind/range (tenant-scoped)."""
    s, e = _validate_range(start, end)
    tid = str(tenant_id)

    if kind is KpiKind.CALL:
        kpi = await call_kpis(session, tid, start=s, end=e)
        metrics = {**kpi.__dict__, "rates": kpi.rates()}
    elif kind is KpiKind.FUNNEL:
        kpi = await funnel_kpis(session, tid, start=s, end=e)
        metrics = {**kpi.__dict__, "rates": kpi.rates()}
    elif kind is KpiKind.APPOINTMENT:
        kpi = await appointment_kpis(session, tid, start=s, end=e)
        metrics = {**kpi.__dict__, "rates": kpi.rates()}
    elif kind is KpiKind.LEAD:
        kpi = await lead_kpis(session, tid, start=s, end=e)
        metrics = {**kpi.__dict__, "rates": kpi.rates()}
    elif kind is KpiKind.AGENT:
        metrics = {"agents": await agent_kpis(session, tid, start=s, end=e)}
    elif kind is KpiKind.PROVIDER:
        metrics = {"providers": await provider_kpis(session, tid, start=s, end=e)}
    elif kind is KpiKind.CAMPAIGN:
        metrics = {"campaigns": await campaign_kpis(session, tid, start=s, end=e)}
    elif kind is KpiKind.QUALITY:
        kpi = await quality_kpis(session, tid, start=s, end=e)
        metrics = kpi.__dict__
    elif kind is KpiKind.SLA:
        kpi = await sla_kpis(session, tid, start=s, end=e)
        metrics = {**kpi.__dict__, "rates": kpi.rates()}
    elif kind is KpiKind.COST:
        kpi = await cost_kpis(session, tid, start=s, end=e)
        metrics = kpi.__dict__
    else:
        metrics = {}

    point = KpiPoint(
        kind=kind,
        period_start=s.isoformat(),
        period_end=e.isoformat(),
        metrics=metrics,
    )
    return KpiSnapshot.build(
        tenant_id=tid,
        range_start=s.isoformat(),
        range_end=e.isoformat(),
        granularity=granularity,
        points=(point,),
    )
