"""
Usage reconciliation.

Requirement 34. The purpose is not to *fix* anything automatically — it is to
make a discrepancy visible with an audit trail. The last line of that
requirement is the design constraint: *"Never silently fix financial records
without an audit trail."*

So every check reports, and the only mutation offered is
`rebuild_summaries`, which recomputes a **cache** from the immutable events.
Correcting the events themselves is a signed `record_adjustment` call made by
a human who has read the report.

Four classes of discrepancy, each of which has a real cause:

* **summary drift** — the cached rollup disagrees with the events. Cause: a
  crash between writing an event and rebuilding the summary. Harmless and
  self-healing, but worth seeing because a persistent one means the rebuild
  is not running.
* **negative usage** — adjustments have pushed a metric below zero. Cause: a
  double-issued credit. Financially significant: it means somebody was
  refunded twice.
* **orphan usage** — an event whose source entity no longer exists. Cause: a
  deleted call, or usage attributed to the wrong id.
* **period mismatch** — an event filed under a period whose bounds do not
  contain its timestamp. Cause: a subscription period changing after the
  usage was recorded, which is exactly what a mid-cycle upgrade does.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import metering
from app.billing import plans as plan_catalogue
from app.billing.periods import BillingPeriod, now_utc, period_for
from app.core.logging import log
from app.db.models import (
    BillingPlan,
    Call,
    Subscription,
    Tenant,
    UsageEvent,
    UsageEventType,
    UsageMetric,
    UsageSummary,
)


@dataclass(frozen=True)
class Discrepancy:
    kind: str
    metric: str
    detail: str
    expected: int | None = None
    found: int | None = None
    severity: str = "warning"       # "warning" | "financial"


@dataclass(frozen=True)
class ReconciliationReport:
    tenant_id: uuid.UUID
    billing_period: str
    checked_at: datetime
    discrepancies: tuple[Discrepancy, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.discrepancies

    @property
    def financial(self) -> tuple[Discrepancy, ...]:
        """The ones that mean somebody was charged wrongly."""
        return tuple(d for d in self.discrepancies if d.severity == "financial")

    def as_dict(self) -> dict:
        return {
            "tenant_id": str(self.tenant_id),
            "billing_period": self.billing_period,
            "checked_at": self.checked_at.isoformat(),
            "clean": self.clean,
            "discrepancies": [
                {
                    "kind": d.kind, "metric": d.metric, "detail": d.detail,
                    "expected": d.expected, "found": d.found,
                    "severity": d.severity,
                }
                for d in self.discrepancies
            ],
        }


async def reconcile_tenant(
    session: AsyncSession,
    tenant: Tenant,
    *,
    period: BillingPeriod | None = None,
) -> ReconciliationReport:
    """Check one tenant's usage for a period. Read-only."""
    subscription = (
        await session.execute(
            select(Subscription).where(Subscription.tenant_id == tenant.id)
        )
    ).scalar_one_or_none()
    period = period or period_for(subscription)

    plan: BillingPlan | None = None
    if subscription is not None and subscription.plan_id is not None:
        plan = await session.get(BillingPlan, subscription.plan_id)
    if plan is None:
        plan = await plan_catalogue.default_plan(session)

    found: list[Discrepancy] = []
    found.extend(await _check_summary_drift(session, tenant.id, period, plan))
    found.extend(await _check_negative(session, tenant.id, period))
    found.extend(await _check_orphans(session, tenant.id, period))
    found.extend(await _check_period_bounds(session, tenant.id, period))

    report = ReconciliationReport(
        tenant_id=tenant.id, billing_period=period.label,
        checked_at=now_utc(), discrepancies=tuple(found),
    )

    if not report.clean:
        log.warning(
            "billing.reconciliation_discrepancy", tenant_id=str(tenant.id),
            billing_period=period.label, count=len(found),
            financial=len(report.financial),
            kinds=sorted({d.kind for d in found}),
        )
    return report


async def _check_summary_drift(
    session: AsyncSession, tenant_id: uuid.UUID, period: BillingPeriod,
    plan: BillingPlan | None,
) -> list[Discrepancy]:
    """
    Does the cached rollup match the events?

    A finalized period is reported but not rebuilt: its figures are what was
    invoiced, and quietly changing them would leave our records and the
    customer's disagreeing with no trace.
    """
    found: list[Discrepancy] = []
    for metric in UsageMetric:
        actual = await metering.used_quantity(
            session, tenant_id=tenant_id, metric=metric, period=period
        )
        summary = (
            await session.execute(
                select(UsageSummary).where(
                    UsageSummary.tenant_id == tenant_id,
                    UsageSummary.billing_period == period.label,
                    UsageSummary.metric == metric,
                )
            )
        ).scalar_one_or_none()

        if summary is None:
            if actual > 0:
                found.append(Discrepancy(
                    kind="missing_summary", metric=metric.value,
                    detail="usage events exist but no summary row does",
                    expected=actual, found=None,
                ))
            continue

        if int(summary.used_quantity) != int(actual):
            found.append(Discrepancy(
                kind="summary_drift", metric=metric.value,
                detail=(
                    "the cached summary disagrees with the events"
                    + (" (period is finalized)" if summary.finalized else "")
                ),
                expected=actual, found=int(summary.used_quantity),
                severity="financial" if summary.finalized else "warning",
            ))
    return found


async def _check_negative(
    session: AsyncSession, tenant_id: uuid.UUID, period: BillingPeriod
) -> list[Discrepancy]:
    """
    Has an adjustment pushed a metric below zero?

    Financially significant: a negative total means more was credited than was
    ever used, which is a double refund.
    """
    found: list[Discrepancy] = []
    for metric in UsageMetric:
        total = (
            await session.execute(
                select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                    UsageEvent.tenant_id == tenant_id,
                    UsageEvent.metric == metric,
                    UsageEvent.billing_period == period.label,
                )
            )
        ).scalar()
        if int(total or 0) < 0:
            found.append(Discrepancy(
                kind="negative_usage", metric=metric.value,
                detail="adjustments exceed recorded usage; a credit may be duplicated",
                found=int(total), expected=0, severity="financial",
            ))
    return found


async def _check_orphans(
    session: AsyncSession, tenant_id: uuid.UUID, period: BillingPeriod
) -> list[Discrepancy]:
    """
    Voice usage whose call no longer exists.

    Only voice is checked: it is the only metric with a first-class source
    row, and an orphan there means either a deleted call or usage attributed
    to an id that was never real.
    """
    rows = (
        (
            await session.execute(
                select(UsageEvent).where(
                    UsageEvent.tenant_id == tenant_id,
                    UsageEvent.billing_period == period.label,
                    UsageEvent.metric == UsageMetric.VOICE_MINUTE,
                    UsageEvent.event_type != UsageEventType.MANUAL_ADJUSTMENT,
                    UsageEvent.source_entity_id.isnot(None),
                )
            )
        )
        .scalars()
        .all()
    )

    found: list[Discrepancy] = []
    for event in rows:
        call = await session.get(Call, event.source_entity_id)
        if call is None:
            found.append(Discrepancy(
                kind="orphan_usage", metric=event.metric.value,
                detail=f"usage references call {event.source_entity_id} which does not exist",
                found=int(event.quantity), severity="financial",
            ))
        elif call.tenant_id != tenant_id:
            # Would be a cross-tenant billing error: charging one business for
            # another's call.
            found.append(Discrepancy(
                kind="cross_tenant_usage", metric=event.metric.value,
                detail="usage references a call belonging to another tenant",
                found=int(event.quantity), severity="financial",
            ))
    return found


async def _check_period_bounds(
    session: AsyncSession, tenant_id: uuid.UUID, period: BillingPeriod
) -> list[Discrepancy]:
    """
    Is every event filed under this period actually inside its bounds?

    Drifts when a subscription's period changes after usage was recorded --
    which is exactly what a mid-cycle upgrade does. Reported rather than
    silently re-filed: moving usage between periods changes two invoices.
    """
    rows = (
        (
            await session.execute(
                select(UsageEvent).where(
                    UsageEvent.tenant_id == tenant_id,
                    UsageEvent.billing_period == period.label,
                    UsageEvent.event_type != UsageEventType.MANUAL_ADJUSTMENT,
                )
            )
        )
        .scalars()
        .all()
    )

    outside = [e for e in rows if not period.contains(e.created_at)]
    if not outside:
        return []
    return [Discrepancy(
        kind="period_mismatch", metric="all",
        detail=(
            f"{len(outside)} event(s) are filed under {period.label} but were "
            f"created outside its bounds"
        ),
        found=len(outside),
    )]


async def rebuild_summaries(
    session: AsyncSession, tenant: Tenant, *, period: BillingPeriod | None = None
) -> list[UsageSummary]:
    """
    Recompute the cached rollups from the events.

    The only mutation this module offers, and it is safe because a summary is
    a cache: the events are untouched, and a finalized period is skipped by
    `metering.rebuild_summary`.
    """
    subscription = (
        await session.execute(
            select(Subscription).where(Subscription.tenant_id == tenant.id)
        )
    ).scalar_one_or_none()
    period = period or period_for(subscription)

    plan: BillingPlan | None = None
    if subscription is not None and subscription.plan_id is not None:
        plan = await session.get(BillingPlan, subscription.plan_id)
    if plan is None:
        plan = await plan_catalogue.default_plan(session)

    summaries = await metering.rebuild_all_summaries(
        session, tenant_id=tenant.id, period=period, plan=plan
    )
    await metering.sync_tenant_cache(session, tenant, period=period)
    await session.commit()

    log.info(
        "billing.summaries_rebuilt", tenant_id=str(tenant.id),
        billing_period=period.label,
    )
    return summaries


async def run_reconciliation_tick(
    session: AsyncSession, *, limit: int = 50
) -> dict:
    """
    One worker pass: rebuild every active tenant's summaries and report.

    Called from `scripts/scheduler.py`. Rebuilding is the routine half;
    reporting is what a human reads.
    """
    tenants = (
        (
            await session.execute(
                select(Tenant).where(Tenant.is_active.is_(True)).limit(limit)
            )
        )
        .scalars()
        .all()
    )

    counts = {"tenants": 0, "clean": 0, "discrepancies": 0, "financial": 0}
    for tenant in tenants:
        counts["tenants"] += 1
        await rebuild_summaries(session, tenant)
        report = await reconcile_tenant(session, tenant)
        if report.clean:
            counts["clean"] += 1
        else:
            counts["discrepancies"] += len(report.discrepancies)
            counts["financial"] += len(report.financial)
    return counts

