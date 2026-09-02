"""
Usage metering.

The audit found `minutes_used` being used as billing authority while built as
a display counter: a mutable float on a mutable row, with no history, no
period and no idempotency key. This module replaces it with an append-only
event log, and demotes the float to a cache that is kept in sync.

Three properties, each of which the old design lacked:

**Immutable.** Nothing here updates or deletes a `UsageEvent`. A correction is
a new row with a negative quantity. That is requirement 35's "never modify
historical usage quantities destructively", and it is also the only way an
invoice dispute is answerable — the original figure survives next to the
correction.

**Idempotency-keyed, with a database constraint behind it.** The key is
derived from the business fact, never generated per attempt. `UNIQUE
(tenant_id, idempotency_key)` is the guarantee; the pre-insert `SELECT` is an
optimisation that makes the common case cheap.

**Integer quantities in the metric's smallest unit.** Seconds, not minutes.
Summing a few thousand floats produces a total that does not match the sum of
its parts, and an invoice that disagrees with its own line items is a support
ticket that cannot be closed.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import plans as plan_catalogue
from app.billing.periods import BillingPeriod, now_utc
from app.core.logging import log
from app.db.models import (
    BillingPlan,
    UsageEvent,
    UsageEventType,
    UsageMetric,
    UsageSummary,
)

#: Metric -> the event type that normally produces it.
_DEFAULT_EVENT_TYPE = {
    UsageMetric.VOICE_MINUTE: UsageEventType.VOICE_MINUTE_USED,
    UsageMetric.SMS_SEGMENT: UsageEventType.SMS_SEGMENT_USED,
    UsageMetric.LLM_TOKEN: UsageEventType.LLM_TOKEN_USED,
    UsageMetric.TTS_CHARACTER: UsageEventType.TTS_CHARACTER_USED,
}

#: The smallest span a voice call is billed for.
#:
#: Requirement 16 asks for a documented rounding policy. Ours: **round the
#: total period up to whole minutes, not each call.** A tenant making two
#: hundred 20-second calls is billed 67 minutes, not 200. Per-call rounding is
#: legal and common, and it is also the thing customers notice and resent, so
#: seconds are stored per call and the rounding happens once at the boundary
#: between "used" and "overage".
MINIMUM_BILLABLE_SECONDS = 1


@dataclass(frozen=True)
class UsageResult:
    """What `record_usage` did. Never raises for a duplicate."""

    event: UsageEvent | None
    created: bool
    #: True when an identical key already existed — idempotency working, not
    #: a failure.
    duplicate: bool = False


def usage_idempotency_key(
    metric: UsageMetric,
    entity_id: uuid.UUID | str,
    *,
    discriminator: str = "",
) -> str:
    """
    The stable identity of one unit of consumption.

    `"{metric}:{entity}"` — deliberately not hashed and not timestamped:

    * **Deterministic.** The same call finalizing twice, from two retried
      Twilio callbacks or two workers, computes the same key. Requirement 12
      is explicit that a random UUID per callback is not duplicate protection.
    * **Legible.** `voice_minute:3f2a…` in a log or a support query says what
      it is. A sha256 would need a lookup to mean anything, and the person
      reading it is usually looking at a disputed invoice.

    `discriminator` separates facts that genuinely recur for one entity — the
    two legs of a transferred call, or a correction issued twice for different
    reasons.
    """
    base = f"{metric.value}:{entity_id}"
    return f"{base}:{discriminator}" if discriminator else base


async def record_usage(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    metric: UsageMetric,
    quantity: int,
    idempotency_key: str,
    period: BillingPeriod,
    event_type: UsageEventType | None = None,
    source_entity_id: uuid.UUID | None = None,
    source_reference: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> UsageResult:
    """
    Append one usage event. Idempotent, and does **not** commit.

    Not committing is deliberate: the caller owns the transaction, so usage
    lands in the same commit as the business fact that caused it. A call
    finalizing and its minutes being counted either both happen or neither
    does — which is what the old code got wrong in the other direction, by
    committing the flag before the work.
    """
    quantity = int(quantity)
    if quantity == 0:
        # Not an error. A zero-second call is real (an immediate hangup) and
        # writing a row for it would add noise to every invoice.
        return UsageResult(event=None, created=False)

    if quantity < 0 and (event_type is not UsageEventType.MANUAL_ADJUSTMENT):
        # Only an explicit, audited adjustment may be negative. Anything else
        # is a bug, and a negative usage row would quietly credit a customer.
        raise ValueError(
            "negative usage is only valid for MANUAL_ADJUSTMENT; "
            f"got {quantity} for {metric.value}"
        )

    existing = (
        await session.execute(
            select(UsageEvent).where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        log.info(
            "billing.usage_duplicate", tenant_id=str(tenant_id),
            metric=metric.value, idempotency_key=idempotency_key,
        )
        return UsageResult(event=existing, created=False, duplicate=True)

    event = UsageEvent(
        tenant_id=tenant_id,
        billing_period=period.label,
        metric=metric,
        event_type=event_type or _DEFAULT_EVENT_TYPE[metric],
        source_entity_id=source_entity_id,
        source_reference=source_reference,
        quantity=quantity,
        unit=plan_catalogue.metric_unit(metric),
        idempotency_key=idempotency_key,
        event_metadata=metadata or {},
    )

    try:
        # A SAVEPOINT, not a bare flush: a concurrent worker winning the race
        # must not tear down the caller's transaction -- the call itself has
        # already been finalized in it.
        #
        # `session.add` goes *inside* the savepoint, and that is load-bearing
        # rather than stylistic. Adding it outside leaves the object pending
        # after the savepoint rolls back, and it is re-flushed by the very
        # next statement -- so the session raises `PendingRollbackError` and
        # the loser of the race cannot even read back who won. Measured: with
        # `add` outside, two of three concurrent workers ended with an
        # unusable session; with it inside, all three recover.
        async with session.begin_nested():
            session.add(event)
            await session.flush()
    except IntegrityError:
        # Another worker inserted the same key. The constraint arbitrated,
        # which is the point: no interleaving of application code can produce
        # two billable rows for one fact.
        log.info(
            "billing.usage_race_lost", tenant_id=str(tenant_id),
            metric=metric.value, idempotency_key=idempotency_key,
        )
        found = (
            await session.execute(
                select(UsageEvent).where(
                    UsageEvent.tenant_id == tenant_id,
                    UsageEvent.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        return UsageResult(event=found, created=False, duplicate=True)

    log.info(
        "billing.usage_recorded", tenant_id=str(tenant_id), metric=metric.value,
        quantity=quantity, unit=event.unit, billing_period=period.label,
        idempotency_key=idempotency_key,
    )
    return UsageResult(event=event, created=True)


async def record_adjustment(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    metric: UsageMetric,
    quantity: int,
    reason: str,
    actor: str,
    period: BillingPeriod,
    reference: str | None = None,
) -> UsageResult:
    """
    A signed correction (requirement 35).

    Positive or negative, always a *new* row, always carrying who did it and
    why. The idempotency key includes the reason and actor so two genuinely
    different corrections do not collide, while the same correction replayed
    does.
    """
    key = usage_idempotency_key(
        metric, f"adjust:{reference or reason}",
        discriminator=f"{actor}:{quantity}",
    )
    return await record_usage(
        session,
        tenant_id=tenant_id,
        metric=metric,
        quantity=quantity,
        idempotency_key=key,
        period=period,
        event_type=UsageEventType.MANUAL_ADJUSTMENT,
        source_reference=reference,
        metadata={"reason": reason[:500], "actor": actor[:120]},
    )


# ------------------------------------------------------------ aggregation ---

async def used_quantity(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    metric: UsageMetric,
    period: BillingPeriod,
) -> int:
    """
    Total consumption in a period, summed from events.

    **This is the authoritative number.** `UsageSummary` is a cache of it and
    `Tenant.minutes_used` is a cache of that; both can be rebuilt from here,
    which is what makes a metering bug recoverable rather than a corrupted
    ledger.
    """
    total = (
        await session.execute(
            select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.metric == metric,
                UsageEvent.billing_period == period.label,
            )
        )
    ).scalar()
    # Adjustments can legitimately push a metric negative in a period; a
    # negative *bill* cannot. Clamp at the reporting boundary, not in storage,
    # so the events still show what happened.
    return max(0, int(total or 0))


def compute_overage(
    *, used: int, included: int, metric: UsageMetric, plan: BillingPlan | None
) -> tuple[int, int]:
    """
    Returns `(overage_units, cost_millicents)`.

    The rounding policy, stated once and applied here only:

    * Voice is stored in **seconds** and billed in **whole minutes**, rounded
      up **on the period total**. 620 minutes and 1 second of usage against a
      500-minute allowance is 121 billable overage minutes, not 120.9 and not
      620 individually-rounded calls.
    * Everything else is already a discrete unit and is not rounded.

    Rounding the period rather than each call is a deliberate choice in the
    customer's favour: per-call rounding on a business making many short calls
    can inflate a bill by 50% and is the single most common cause of a
    telephony billing dispute.
    """
    used = max(0, int(used))
    included = max(0, int(included))
    raw_overage = max(0, used - included)
    if raw_overage == 0:
        return 0, 0

    if metric is UsageMetric.VOICE_MINUTE:
        # Seconds -> whole minutes, rounded up, once.
        billable = -(-raw_overage // 60)
    else:
        billable = raw_overage

    rate = plan_catalogue.overage_rate_millicents(plan, metric)
    return billable, billable * rate


@dataclass(frozen=True)
class MetricUsage:
    """One metric's position in a period. All quantities in smallest units."""

    metric: UsageMetric
    included: int
    used: int
    overage: int
    overage_millicents: int
    unit: str
    display_unit: str

    @property
    def percent_used(self) -> float:
        if self.included <= 0:
            return 100.0 if self.used > 0 else 0.0
        return round(self.used / self.included * 100, 1)

    @property
    def remaining(self) -> int:
        return max(0, self.included - self.used)

    def display(self, quantity: int) -> float:
        """
        Convert a **smallest-unit** quantity into the unit a human reads.

        Applies to `included`, `used` and `remaining`, which are stored in
        seconds for voice. It must **not** be applied to `overage`, which
        `compute_overage` already returns in billable units (whole minutes) --
        dividing it again turned a 100-minute overage into 1.67. Use
        `display_overage` for that, which is why the two are separate methods
        rather than one that callers have to remember the rules for.
        """
        divisor = plan_catalogue.METRIC_CONFIG[self.metric]["units_per_included"]
        return round(quantity / divisor, 2) if divisor > 1 else quantity

    @property
    def display_overage(self) -> int:
        """The overage, already in billable units. Never re-divided."""
        return self.overage


async def metric_usage(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    metric: UsageMetric,
    period: BillingPeriod,
    plan: BillingPlan | None,
) -> MetricUsage:
    used = await used_quantity(
        session, tenant_id=tenant_id, metric=metric, period=period
    )
    included = plan_catalogue.included_units(plan, metric)
    overage, cost = compute_overage(
        used=used, included=included, metric=metric, plan=plan
    )
    config = plan_catalogue.METRIC_CONFIG[metric]
    return MetricUsage(
        metric=metric, included=included, used=used, overage=overage,
        overage_millicents=cost, unit=config["unit"],
        display_unit=config["display_unit"],
    )


async def period_usage(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    period: BillingPeriod,
    plan: BillingPlan | None,
) -> dict[UsageMetric, MetricUsage]:
    return {
        metric: await metric_usage(
            session, tenant_id=tenant_id, metric=metric, period=period, plan=plan
        )
        for metric in UsageMetric
    }


# --------------------------------------------------------------- summaries ---

async def rebuild_summary(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    period: BillingPeriod,
    plan: BillingPlan | None,
    metric: UsageMetric,
) -> UsageSummary:
    """
    Recompute one summary row from events.

    Safe to run at any time. A finalized period's figures are **not**
    overwritten: once an invoice has been cut against them, changing the
    number silently would mean the customer's records and ours diverge with no
    trace. Late events for a closed period are still stored and are surfaced
    by reconciliation as a discrepancy for a human to decide about.
    """
    usage = await metric_usage(
        session, tenant_id=tenant_id, metric=metric, period=period, plan=plan
    )
    count = (
        await session.execute(
            select(func.count(UsageEvent.id)).where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.metric == metric,
                UsageEvent.billing_period == period.label,
            )
        )
    ).scalar() or 0

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
        summary = UsageSummary(
            tenant_id=tenant_id, billing_period=period.label, metric=metric
        )
        session.add(summary)
    elif summary.finalized:
        # Closed. Refresh only the event count so a discrepancy is visible.
        summary.event_count = int(count)
        return summary

    summary.included_quantity = usage.included
    summary.used_quantity = usage.used
    summary.overage_quantity = usage.overage
    summary.estimated_overage_millicents = usage.overage_millicents
    summary.event_count = int(count)
    summary.updated_at = now_utc()
    return summary


async def rebuild_all_summaries(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    period: BillingPeriod,
    plan: BillingPlan | None,
) -> list[UsageSummary]:
    return [
        await rebuild_summary(
            session, tenant_id=tenant_id, period=period, plan=plan, metric=metric
        )
        for metric in UsageMetric
    ]


async def finalize_period(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    period: BillingPeriod,
    plan: BillingPlan | None,
) -> list[UsageSummary]:
    """
    Close a period. The figures become what was invoiced.

    Called when a renewal is observed, not on a calendar schedule — the
    provider's period boundary is the authority, and finalizing early would
    close a period the provider is still adding to.
    """
    summaries = await rebuild_all_summaries(
        session, tenant_id=tenant_id, period=period, plan=plan
    )
    for summary in summaries:
        if not summary.finalized:
            summary.finalized = True
            summary.finalized_at = now_utc()
    log.info(
        "billing.period_finalized", tenant_id=str(tenant_id),
        billing_period=period.label,
    )
    return summaries


async def sync_tenant_cache(
    session: AsyncSession, tenant, *, period: BillingPeriod
) -> None:
    """
    Keep the pre-STEP-7 `Tenant.minutes_used` float in step with the events.

    Demoted from authority to cache (see `docs/BILLING-AUDIT.md`). It stays
    because the dashboard reads it and because deleting a column that external
    tooling may read is a separate, announced change — not something to bundle
    into the release that fixes the metering.
    """
    seconds = await used_quantity(
        session, tenant_id=tenant.id, metric=UsageMetric.VOICE_MINUTE,
        period=period,
    )
    tenant.minutes_used = round(seconds / 60.0, 2)


# ------------------------------------------------------------ voice helper ---

def billable_seconds(duration_seconds: float | None) -> int:
    """
    A call's duration as whole seconds.

    Floors rather than rounds, and applies a minimum. Flooring means a
    99.4-second call bills 99 seconds; the alternative rounds *up* on average
    and, across thousands of calls, is a systematic overcharge nobody
    consented to.
    """
    if not duration_seconds or duration_seconds <= 0:
        return 0
    return max(MINIMUM_BILLABLE_SECONDS, int(duration_seconds))
