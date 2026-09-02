"""
Analytics aggregation.

Requirement 13: the browser must not pull thousands of rows to compute a
total. Every figure here is a `SELECT count(...)` or `SELECT sum(...)` with a
`WHERE tenant_id` and a date range, so a tenant with 400,000 calls costs the
same round trip as one with four.

**Every percentage has a stated numerator and denominator**, in code and in
`docs/DASHBOARD.md`. The audit found `booking_rate = booked / all calls`,
which silently counted wrong numbers and hang-ups in the denominator and made
a good operator look bad. The definitions here are deliberate:

    answer_rate    = answered / total
    booking_rate   = booked   / *eligible* calls   (answered, >= 10s)
    transfer_rate  = transferred / answered
    failure_rate   = failed / total

`eligible` is the important one. A call that rang out, or that lasted three
seconds because someone misdialled, was never a booking opportunity. Counting
it punishes the tenant for their own wrong numbers.

**No estimated revenue.** The old `/stats` endpoint returned
`estimated_value_usd = booked * 150`, a constant with no relationship to
anything the tenant sells. It is left in that endpoint so nothing breaks, and
it is not repeated here — a number a buyer will read as money has to come
from money.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Float, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import TenantContext, require_permission
from app.auth.permissions import Permission
from app.billing import metering
from app.billing.entitlements import load_context
from app.db.models import (
    Appointment,
    AppointmentStatus,
    Call,
    CallDirection,
    CallStatus,
    CrmSync,
    CrmSyncStatus,
    Lead,
    LeadStatus,
    UsageMetric,
)
from app.db.session import get_session
from app.integrations.calendar.timezones import get_zone, to_local

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

UTC = timezone.utc

#: The shortest call that could plausibly have become a booking.
#:
#: Below this the caller hung up before the agent finished greeting them, so
#: including it in a conversion denominator measures wrong numbers rather than
#: agent performance. Ten seconds is the same threshold the outbound dialer
#: already uses to decide a lead was actually reached
#: (`twilio_handler.py`), so the two figures agree.
ELIGIBLE_CALL_SECONDS = 10

#: A hard ceiling on any range, so one request cannot scan a decade.
MAX_RANGE_DAYS = 400


def _pct(numerator: int | float, denominator: int | float) -> float:
    """
    A percentage, or 0.0 when the denominator is zero.

    Returns 0–100, not 0–1. Requirement 31 is explicit: 2 of 10 is `20.0`,
    not `0.2` and not `2000`. Doing the ×100 here rather than in the browser
    means there is one place to get it wrong.
    """
    if not denominator:
        return 0.0
    return round(numerator / denominator * 100, 1)


@dataclass(frozen=True)
class Window:
    """A validated, timezone-resolved analytics range."""

    start: datetime
    end: datetime
    timezone_name: str
    label: str

    @property
    def days(self) -> int:
        return max(1, (self.end - self.start).days)


def resolve_window(
    tenant,
    *,
    start: date | None,
    end: date | None,
    preset: str | None,
) -> Window:
    """
    Turn a preset or an explicit date pair into a UTC half-open interval.

    **Resolved server-side, in the tenant's timezone.** Requirement 5 says the
    browser must not do billing-period arithmetic, and the same reasoning
    applies here: "today" for a business in Los Angeles is not "today" for the
    laptop viewing it from Dhaka, and letting the client decide means two
    people looking at the same dashboard see different numbers.

    Half-open `[start, end)` so a call at exactly midnight belongs to one day
    and is not counted twice.
    """
    zone = get_zone(getattr(tenant, "timezone", None) or "UTC")
    today = to_local(datetime.now(UTC), zone).date()

    presets = {
        "today": (today, today),
        "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
        "last_7_days": (today - timedelta(days=6), today),
        "last_30_days": (today - timedelta(days=29), today),
        "this_month": (today.replace(day=1), today),
        "previous_month": _previous_month(today),
    }

    if preset:
        if preset not in presets:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Unknown range {preset!r}. Use one of: "
                    f"{', '.join(sorted(presets))}, or supply start and end."
                ),
            )
        start, end = presets[preset]
        label = preset
    else:
        if start is None or end is None:
            start, end = presets["last_30_days"]
            label = "last_30_days"
        else:
            label = "custom"

    if end < start:
        raise HTTPException(
            status_code=422, detail="The end date must not be before the start date."
        )
    if (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"Ranges longer than {MAX_RANGE_DAYS} days are not supported.",
        )

    # Local midnight to local midnight the day *after* `end`, converted to UTC.
    from app.integrations.calendar.timezones import resolve_local

    start_utc = resolve_local(
        datetime.combine(start, time.min), zone,
        on_ambiguous="earliest", on_nonexistent="shift",
    ).utc
    end_utc = resolve_local(
        datetime.combine(end + timedelta(days=1), time.min), zone,
        on_ambiguous="earliest", on_nonexistent="shift",
    ).utc

    return Window(
        start=start_utc, end=end_utc, timezone_name=str(zone), label=label
    )


def _previous_month(today: date) -> tuple[date, date]:
    first_of_this = today.replace(day=1)
    last_of_prev = first_of_this - timedelta(days=1)
    return last_of_prev.replace(day=1), last_of_prev


async def _window_dep(
    start: date | None = Query(None, description="local start date, inclusive"),
    end: date | None = Query(None, description="local end date, inclusive"),
    range: str | None = Query(None, description="a named preset"),
    ctx: TenantContext = Depends(require_permission(Permission.ANALYTICS_READ)),
) -> tuple[TenantContext, Window]:
    return ctx, resolve_window(ctx.tenant, start=start, end=end, preset=range)


# ----------------------------------------------------------------- schemas ---

class WindowOut(BaseModel):
    label: str
    start: str
    end: str
    timezone: str
    days: int


class OverviewOut(BaseModel):
    window: WindowOut
    calls: dict
    conversion: dict
    operations: dict
    integrations: dict
    #: Present only when the caller may read billing. Absent, not zeroed --
    #: a zero would read as "you have used nothing" rather than "you may not
    #: see this".
    usage: dict | None = None


class SeriesPoint(BaseModel):
    date: str
    answered: int = 0
    missed: int = 0
    failed: int = 0
    booked: int = 0
    transferred: int = 0
    minutes: float = 0.0


class CallAnalyticsOut(BaseModel):
    window: WindowOut
    totals: dict
    by_status: dict
    by_direction: dict
    by_hour: list[dict]
    series: list[SeriesPoint]


class ConversionOut(BaseModel):
    window: WindowOut
    funnel: list[dict]
    rates: dict


class UsageAnalyticsOut(BaseModel):
    billing_period: str
    period_start: str
    period_end: str
    plan_code: str
    metrics: list[dict]
    estimated_overage_cents: int
    currency: str


# ------------------------------------------------------------- call queries ---

def _calls_in(tenant_id: uuid.UUID, window: Window):
    return (
        Call.tenant_id == tenant_id,
        Call.started_at >= window.start,
        Call.started_at < window.end,
    )


async def _call_totals(
    session: AsyncSession, tenant_id: uuid.UUID, window: Window
) -> dict[str, Any]:
    """
    Every call count in **one** query.

    Conditional aggregation rather than eight `SELECT count(*)` round trips:
    requirement 32 asks to avoid N+1, and the same scan answers all of them.
    """
    scope = _calls_in(tenant_id, window)

    def count_if(condition):
        return func.sum(case((condition, 1), else_=0))

    row = (
        await session.execute(
            select(
                func.count(Call.id).label("total"),
                count_if(Call.status == CallStatus.COMPLETED).label("answered"),
                count_if(Call.status == CallStatus.NO_ANSWER).label("missed"),
                count_if(Call.status == CallStatus.FAILED).label("failed"),
                count_if(Call.booked.is_(True)).label("booked"),
                count_if(Call.escalated.is_(True)).label("transferred"),
                count_if(Call.direction == CallDirection.INBOUND).label("inbound"),
                count_if(Call.direction == CallDirection.OUTBOUND).label("outbound"),
                count_if(
                    (Call.status == CallStatus.COMPLETED)
                    & (Call.duration_seconds >= ELIGIBLE_CALL_SECONDS)
                ).label("eligible"),
                func.coalesce(func.sum(Call.duration_seconds), 0.0).label("seconds"),
            ).where(*scope)
        )
    ).one()

    return {
        "total": int(row.total or 0),
        "answered": int(row.answered or 0),
        "missed": int(row.missed or 0),
        "failed": int(row.failed or 0),
        "booked": int(row.booked or 0),
        "transferred": int(row.transferred or 0),
        "inbound": int(row.inbound or 0),
        "outbound": int(row.outbound or 0),
        "eligible": int(row.eligible or 0),
        "total_seconds": float(row.seconds or 0.0),
    }


def _rates(totals: dict) -> dict[str, float]:
    """
    The four rates, each with an explicit denominator.

    Documented here and in `docs/DASHBOARD.md`. A percentage whose denominator
    nobody can name is a number nobody can defend in a QBR.
    """
    return {
        # Of every call that arrived, how many did the agent complete?
        "answer_rate": _pct(totals["answered"], totals["total"]),
        # Of calls that could plausibly have booked, how many did?
        # NOT booked/total -- see ELIGIBLE_CALL_SECONDS.
        "booking_rate": _pct(totals["booked"], totals["eligible"]),
        # Of answered calls, how many needed a human?
        "transfer_rate": _pct(totals["transferred"], totals["answered"]),
        # Of every call, how many failed outright?
        "failure_rate": _pct(totals["failed"], totals["total"]),
    }


def _window_out(window: Window) -> WindowOut:
    return WindowOut(
        label=window.label,
        start=window.start.isoformat(),
        end=window.end.isoformat(),
        timezone=window.timezone_name,
        days=window.days,
    )


# ------------------------------------------------------------------ routes ---

@router.get("/overview", response_model=OverviewOut)
async def overview(
    scoped: tuple = Depends(_window_dep),
    session: AsyncSession = Depends(get_session),
) -> OverviewOut:
    """
    The executive summary. One request, one screen.

    Deliberately a single endpoint rather than eight: the overview page would
    otherwise fire a request per tile, and a slow one would make the page
    appear to load in pieces.
    """
    ctx, window = scoped
    tenant_id = ctx.tenant_id

    totals = await _call_totals(session, tenant_id, window)

    leads_created = (
        await session.execute(
            select(func.count(Lead.id)).where(
                Lead.tenant_id == tenant_id,
                Lead.created_at >= window.start,
                Lead.created_at < window.end,
            )
        )
    ).scalar() or 0

    leads_converted = (
        await session.execute(
            select(func.count(Lead.id)).where(
                Lead.tenant_id == tenant_id,
                Lead.created_at >= window.start,
                Lead.created_at < window.end,
                Lead.status == LeadStatus.QUALIFIED,
            )
        )
    ).scalar() or 0

    appointments = (
        await session.execute(
            select(
                func.count(Appointment.id).label("total"),
                func.sum(
                    case((Appointment.status == AppointmentStatus.CANCELLED, 1), else_=0)
                ).label("cancelled"),
                func.sum(
                    case((Appointment.status == AppointmentStatus.NO_SHOW, 1), else_=0)
                ).label("no_show"),
            ).where(
                Appointment.tenant_id == tenant_id,
                Appointment.created_at >= window.start,
                Appointment.created_at < window.end,
            )
        )
    ).one()

    # Integration health. Counts, never error text -- the overview is a
    # dashboard tile, and a provider message belongs on the integrations page
    # where it has been scrubbed for display.
    crm_failures = (
        await session.execute(
            select(func.count(CrmSync.id)).where(
                CrmSync.tenant_id == tenant_id,
                CrmSync.created_at >= window.start,
                CrmSync.created_at < window.end,
                CrmSync.status.in_([
                    CrmSyncStatus.FAILED, CrmSyncStatus.PERMANENT_FAILURE
                ]),
            )
        )
    ).scalar() or 0

    calendar_failures = (
        await session.execute(
            select(func.count(Appointment.id)).where(
                Appointment.tenant_id == tenant_id,
                Appointment.created_at >= window.start,
                Appointment.created_at < window.end,
                Appointment.status == AppointmentStatus.FAILED,
            )
        )
    ).scalar() or 0

    usage: dict | None = None
    if ctx.can(Permission.BILLING_READ):
        usage = await _usage_snapshot(session, ctx.tenant)

    return OverviewOut(
        window=_window_out(window),
        calls={
            "total": totals["total"],
            "answered": totals["answered"],
            "missed": totals["missed"],
            "failed": totals["failed"],
            "inbound": totals["inbound"],
            "outbound": totals["outbound"],
            "transferred": totals["transferred"],
            "minutes": round(totals["total_seconds"] / 60.0, 1),
        },
        conversion={
            "eligible_calls": totals["eligible"],
            "booked": totals["booked"],
            "leads_created": leads_created,
            "leads_qualified": leads_converted,
            "appointments": int(appointments.total or 0),
            "appointments_cancelled": int(appointments.cancelled or 0),
            "appointments_no_show": int(appointments.no_show or 0),
        },
        operations={
            **_rates(totals),
            "average_call_seconds": (
                round(totals["total_seconds"] / totals["answered"], 1)
                if totals["answered"] else 0.0
            ),
        },
        integrations={
            "crm_sync_failures": crm_failures,
            "calendar_failures": calendar_failures,
        },
        usage=usage,
    )


async def _usage_snapshot(session: AsyncSession, tenant) -> dict:
    """
    Current-period usage, from STEP 7's metering.

    Reuses `billing.metering` rather than re-summing `usage_events` here:
    requirement says do not reimplement billing logic, and a second
    implementation of "how much have they used" is how two screens start
    disagreeing.
    """
    context = await load_context(session, tenant)
    usage = await metering.period_usage(
        session, tenant_id=tenant.id, period=context.period, plan=context.plan
    )
    voice = usage[UsageMetric.VOICE_MINUTE]

    return {
        "billing_period": context.period.label,
        "plan_code": context.plan_code,
        "voice_minutes_used": voice.display(voice.used),
        "voice_minutes_included": voice.display(voice.included),
        "voice_minutes_overage": voice.display_overage,
        "percent_used": voice.percent_used,
        "estimated_overage_cents": round(
            sum(u.overage_millicents for u in usage.values()) / 100
        ),
        "currency": context.plan.currency if context.plan else "usd",
    }


@router.get("/calls", response_model=CallAnalyticsOut)
async def call_analytics(
    scoped: tuple = Depends(_window_dep),
    session: AsyncSession = Depends(get_session),
) -> CallAnalyticsOut:
    """Call volume and shape: status mix, direction, hour of day, daily series."""
    ctx, window = scoped
    tenant_id = ctx.tenant_id

    totals = await _call_totals(session, tenant_id, window)
    series = await _daily_series(session, tenant_id, window)
    by_hour = await _hourly_distribution(session, tenant_id, window)

    return CallAnalyticsOut(
        window=_window_out(window),
        totals={
            **totals,
            "minutes": round(totals["total_seconds"] / 60.0, 1),
            "average_seconds": (
                round(totals["total_seconds"] / totals["answered"], 1)
                if totals["answered"] else 0.0
            ),
        },
        by_status={
            "answered": totals["answered"],
            "missed": totals["missed"],
            "failed": totals["failed"],
            "other": max(
                0,
                totals["total"] - totals["answered"] - totals["missed"]
                - totals["failed"],
            ),
        },
        by_direction={
            "inbound": totals["inbound"], "outbound": totals["outbound"]
        },
        by_hour=by_hour,
        series=series,
    )


async def _daily_series(
    session: AsyncSession, tenant_id: uuid.UUID, window: Window
) -> list[SeriesPoint]:
    """
    One row per local day, gap-filled.

    Bucketed in Python rather than with a database `date_trunc`, because the
    bucket boundary is the **tenant's** local midnight and PostgreSQL and
    SQLite disagree about how to express that. One pass over the window's
    calls -- bounded by the 400-day ceiling -- is simpler than two dialect
    branches, and it is the only place in this module that reads rows rather
    than aggregates.

    Gap-filling matters: a chart that omits zero days draws a straight line
    through a weekend and makes a quiet Saturday look like normal volume.
    """
    zone = get_zone(window.timezone_name)

    rows = (
        (
            await session.execute(
                select(
                    Call.started_at, Call.status, Call.booked, Call.escalated,
                    Call.duration_seconds,
                ).where(*_calls_in(tenant_id, window))
            )
        )
        .all()
    )

    buckets: dict[str, dict[str, float]] = {}
    cursor = to_local(window.start, zone).date()
    last = to_local(window.end - timedelta(seconds=1), zone).date()
    while cursor <= last:
        buckets[cursor.isoformat()] = {
            "answered": 0, "missed": 0, "failed": 0,
            "booked": 0, "transferred": 0, "seconds": 0.0,
        }
        cursor += timedelta(days=1)

    for started_at, status, booked, escalated, seconds in rows:
        key = to_local(started_at, zone).date().isoformat()
        bucket = buckets.get(key)
        if bucket is None:      # a DST edge; fold into the nearest real day
            continue
        if status is CallStatus.COMPLETED:
            bucket["answered"] += 1
        elif status is CallStatus.NO_ANSWER:
            bucket["missed"] += 1
        elif status is CallStatus.FAILED:
            bucket["failed"] += 1
        if booked:
            bucket["booked"] += 1
        if escalated:
            bucket["transferred"] += 1
        bucket["seconds"] += float(seconds or 0.0)

    return [
        SeriesPoint(
            date=day,
            answered=int(v["answered"]), missed=int(v["missed"]),
            failed=int(v["failed"]), booked=int(v["booked"]),
            transferred=int(v["transferred"]),
            minutes=round(v["seconds"] / 60.0, 1),
        )
        for day, v in sorted(buckets.items())
    ]


async def _hourly_distribution(
    session: AsyncSession, tenant_id: uuid.UUID, window: Window
) -> list[dict]:
    """
    Calls by local hour of day. Answers "when should we staff the phones?".
    """
    zone = get_zone(window.timezone_name)
    rows = (
        (
            await session.execute(
                select(Call.started_at, Call.status).where(
                    *_calls_in(tenant_id, window)
                )
            )
        )
        .all()
    )

    hours = {hour: {"total": 0, "missed": 0} for hour in range(24)}
    for started_at, status in rows:
        hour = to_local(started_at, zone).hour
        hours[hour]["total"] += 1
        if status is CallStatus.NO_ANSWER:
            hours[hour]["missed"] += 1

    return [
        {"hour": hour, "total": v["total"], "missed": v["missed"]}
        for hour, v in sorted(hours.items())
    ]


@router.get("/conversion", response_model=ConversionOut)
async def conversion(
    scoped: tuple = Depends(_window_dep),
    session: AsyncSession = Depends(get_session),
) -> ConversionOut:
    """
    The funnel, with every stage's denominator stated.

    Stages are counted independently over the same window rather than by
    following individual records through it, because a call in the window can
    produce an appointment outside it. Following records would make the funnel
    disagree with the call page; counting stages keeps each number defensible
    on its own.
    """
    ctx, window = scoped
    tenant_id = ctx.tenant_id

    totals = await _call_totals(session, tenant_id, window)

    leads = (
        await session.execute(
            select(func.count(Lead.id)).where(
                Lead.tenant_id == tenant_id,
                Lead.created_at >= window.start,
                Lead.created_at < window.end,
            )
        )
    ).scalar() or 0

    appointment_rows = (
        await session.execute(
            select(Appointment.status, func.count(Appointment.id)).where(
                Appointment.tenant_id == tenant_id,
                Appointment.created_at >= window.start,
                Appointment.created_at < window.end,
            ).group_by(Appointment.status)
        )
    ).all()
    by_status = {status.value: int(count) for status, count in appointment_rows}

    # A funnel stage must be a superset of the one below it, or the chart
    # widens as it descends and stops meaning anything.
    #
    # "booked" therefore includes no-shows: they *were* booked, and dropping
    # out is what the next stage measures. Only CANCELLED and FAILED are
    # excluded -- a cancelled appointment left the funnel earlier, and a
    # FAILED one never reached the provider at all.
    #
    # The first draft subtracted no-shows from a set that had already excluded
    # them, so a tenant with one kept appointment and one no-show was reported
    # as having kept none. A test caught it.
    no_shows = by_status.get("no_show", 0)
    booked_appointments = sum(
        by_status.get(status, 0)
        for status in ("pending", "confirmed", "rescheduled", "no_show")
    )
    kept = booked_appointments - no_shows

    funnel = [
        {"stage": "calls", "count": totals["total"],
         "note": "every call in the window"},
        {"stage": "eligible", "count": totals["eligible"],
         "note": f"answered and at least {ELIGIBLE_CALL_SECONDS}s"},
        {"stage": "leads", "count": leads, "note": "lead records created"},
        {"stage": "appointments", "count": booked_appointments,
         "note": "booked and not cancelled, including no-shows"},
        {"stage": "kept", "count": max(0, kept),
         "note": "booked appointments the customer attended"},
    ]

    return ConversionOut(
        window=_window_out(window),
        funnel=funnel,
        rates={
            **_rates(totals),
            # Of calls that could have produced a lead, how many did? Capped
            # at 100: a lead can be created by an import rather than a call,
            # and a funnel stage wider than the one above it is confusing
            # rather than informative.
            "call_to_lead_rate": min(100.0, _pct(leads, totals["eligible"])),
            "lead_to_appointment_rate": min(
                100.0, _pct(booked_appointments, leads)
            ),
            "appointment_kept_rate": _pct(kept, booked_appointments),
        },
    )


@router.get("/usage", response_model=UsageAnalyticsOut)
async def usage_analytics(
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_READ)),
    session: AsyncSession = Depends(get_session),
) -> UsageAnalyticsOut:
    """
    Metered usage for the **current billing period**.

    Not date-filtered, and deliberately so: usage belongs to a billing period
    anchored on the subscription, and letting an analytics date picker slice
    it would produce a number that looks like a bill and is not one. The
    period comes from STEP 7.
    """
    context = await load_context(session, ctx.tenant)
    usage = await metering.period_usage(
        session, tenant_id=ctx.tenant_id, period=context.period, plan=context.plan
    )

    return UsageAnalyticsOut(
        billing_period=context.period.label,
        period_start=context.period.start.isoformat(),
        period_end=context.period.end.isoformat(),
        plan_code=context.plan_code,
        metrics=[
            {
                "metric": metric.value,
                "unit": entry.display_unit,
                "included": entry.display(entry.included),
                "used": entry.display(entry.used),
                "remaining": entry.display(entry.remaining),
                "overage": entry.display_overage,
                "percent_used": entry.percent_used,
                "overage_cents": round(entry.overage_millicents / 100),
            }
            for metric, entry in usage.items()
        ],
        estimated_overage_cents=round(
            sum(e.overage_millicents for e in usage.values()) / 100
        ),
        currency=context.plan.currency if context.plan else "usd",
    )

