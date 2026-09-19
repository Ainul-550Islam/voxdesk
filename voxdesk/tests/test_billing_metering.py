"""
Usage metering, billing periods, overage and idempotency.

The financial core. Requirement 14 calls idempotency critical and it is: a
duplicate here is a duplicate charge, and the customer finds out before we do.

The audit found the pre-STEP-7 counter billing the same 120-second call as
2.00, 1.00 or 0.50 minutes depending on webhook ordering
(`docs/BILLING-AUDIT.md` F1). `TestVoiceBilling` is the regression suite for
that, replayed through the real call-state machine.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.billing import metering
from app.billing import plans as plan_catalogue
from app.billing.periods import (
    BillingPeriod,
    PeriodError,
    add_months,
    calendar_period,
    elapsed_fraction,
    period_for,
    period_label,
    previous_period,
)
from app.db.models import (
    UsageEvent,
    UsageEventType,
    UsageMetric,
)
from tests.conftest import add_usage, subscribe

UTC = timezone.utc


def utc(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# =============================================================== periods ===

class TestPeriodArithmetic:
    @pytest.mark.parametrize("start,expected", [
        (utc(2026, 1, 31), "2026-02-28"),      # clamps, does not overflow
        (utc(2024, 1, 31), "2024-02-29"),      # leap year
        (utc(2026, 3, 31), "2026-04-30"),
        (utc(2026, 1, 15), "2026-02-15"),
    ])
    def test_add_months_clamps_the_day(self, start, expected):
        """
        `Jan 31 + 1 month` is `Feb 28`, not `Mar 3`.

        Naive day arithmetic walks a 31st-anchored subscription forward
        through the calendar until it renews on a different date than the
        customer agreed to.
        """
        assert add_months(start, 1).strftime("%Y-%m-%d") == expected

    def test_a_year_of_months_returns_to_the_same_day(self):
        assert add_months(utc(2026, 1, 31), 12).strftime("%Y-%m-%d") == "2027-01-31"

    def test_the_label_is_the_month_the_period_started(self):
        """
        A period running 15 March to 15 April is entirely `2026-03`. Labelling
        by end date, or splitting across buckets, means the usage page and the
        invoice never agree.
        """
        assert period_label(utc(2026, 3, 15)) == "2026-03"
        assert period_label(utc(2026, 3, 31, 23, 59)) == "2026-03"

    def test_a_period_is_half_open(self):
        """
        The instant a period ends belongs to the next one. Closed bounds would
        put one call in two periods and bill it twice.
        """
        period = calendar_period(utc(2026, 3, 10))
        assert period.contains(period.start)
        assert not period.contains(period.end)
        assert period.contains(period.end - timedelta(microseconds=1))

    def test_bounds_must_be_aware(self):
        with pytest.raises(PeriodError):
            BillingPeriod(
                label="2026-03", start=datetime(2026, 3, 1), end=datetime(2026, 4, 1)
            )

    def test_a_backwards_period_is_rejected(self):
        with pytest.raises(PeriodError):
            BillingPeriod(label="x", start=utc(2026, 4, 1), end=utc(2026, 3, 1))

    def test_previous_period_walks_back_one_month(self):
        assert previous_period(calendar_period(utc(2026, 3, 10))).label == "2026-02"

    @pytest.mark.parametrize("moment,expected", [
        (utc(2026, 3, 1), 0.0),
        (utc(2026, 3, 16, 12), pytest.approx(0.5, abs=0.02)),
        (utc(2026, 4, 1), 1.0),
    ])
    def test_elapsed_fraction(self, moment, expected):
        assert elapsed_fraction(calendar_period(utc(2026, 3, 10)), moment) == expected


class TestSubscriptionPeriods:
    async def test_the_period_follows_the_subscription_not_the_calendar(
        self, db, tenant_a, billing_plans
    ):
        """
        A tenant who subscribed on the 15th is billed the 15th to the 15th.
        Rolling their usage up by calendar month would split every period
        across two buckets.
        """
        subscription = await subscribe(
            db, tenant_a, current_period_start=utc(2026, 3, 15, 9, 30)
        )
        for probe, expected in [
            (utc(2026, 3, 20), "2026-03"),
            (utc(2026, 4, 1), "2026-03"),       # still the March-anchored period
            (utc(2026, 4, 14), "2026-03"),
            (utc(2026, 4, 16), "2026-04"),
        ]:
            assert period_for(subscription, probe).label == expected, probe

    async def test_a_tenant_with_no_subscription_falls_back_to_the_calendar(self):
        assert period_for(None, utc(2026, 3, 10)).label == "2026-03"

    async def test_provider_bounds_win_when_they_contain_the_moment(
        self, db, tenant_a, billing_plans
    ):
        """
        The provider's bounds are what the invoice will be cut against; any
        local calculation that disagrees is wrong by definition.
        """
        subscription = await subscribe(
            db, tenant_a,
            current_period_start=utc(2026, 3, 15),
        )
        subscription.current_period_end = utc(2026, 4, 20)   # unusual, but theirs
        period = period_for(subscription, utc(2026, 4, 18))
        assert period.start == utc(2026, 3, 15)
        assert period.end == utc(2026, 4, 20)

    async def test_a_stale_provider_period_is_walked_forward(
        self, db, tenant_a, billing_plans
    ):
        """
        Between a renewal happening and its webhook arriving, calls are still
        being made. They must be filed into the new period, not a closed one.
        """
        subscription = await subscribe(
            db, tenant_a, current_period_start=utc(2026, 3, 15)
        )
        period = period_for(subscription, utc(2026, 5, 20))
        assert period.label == "2026-05"
        assert period.contains(utc(2026, 5, 20))


# ================================================================ recording ===

class TestUsageRecording:
    async def test_a_usage_event_is_written(self, db, tenant_a, billing_plans):
        result = await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 120)
        assert result.created
        assert result.event.quantity == 120
        assert result.event.unit == "second"

    async def test_seconds_are_stored_not_minutes(self, db, tenant_a, billing_plans):
        """
        Integers in the smallest unit. Summing a few thousand floats produces
        a total that does not match the sum of its parts, and an invoice that
        disagrees with its own line items cannot be defended.
        """
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 90)
        event = (await db.execute(select(UsageEvent))).scalars().one()
        assert isinstance(event.quantity, int)
        assert event.quantity == 90

    async def test_zero_quantity_writes_nothing(self, db, tenant_a, billing_plans):
        """A zero-second call is real; a row for it is noise on every invoice."""
        result = await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 0)
        assert not result.created
        assert (await db.execute(select(func.count(UsageEvent.id)))).scalar() == 0

    async def test_negative_usage_is_refused_outside_an_adjustment(
        self, db, tenant_a, billing_plans
    ):
        """
        A negative row would quietly credit a customer. Only an explicit,
        audited adjustment may be negative.
        """
        from app.billing.periods import calendar_period

        with pytest.raises(ValueError):
            await metering.record_usage(
                db, tenant_id=tenant_a.id, metric=UsageMetric.VOICE_MINUTE,
                quantity=-60, idempotency_key="bad", period=calendar_period(),
            )

    async def test_usage_is_summed_from_events(self, db, tenant_a, billing_plans):
        from app.billing.periods import calendar_period

        period = calendar_period()
        for seconds in (60, 120, 30):
            await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, seconds, period=period)

        assert await metering.used_quantity(
            db, tenant_id=tenant_a.id, metric=UsageMetric.VOICE_MINUTE, period=period
        ) == 210

    async def test_usage_is_scoped_to_its_period(self, db, tenant_a, billing_plans):
        march = calendar_period(utc(2026, 3, 10))
        april = calendar_period(utc(2026, 4, 10))

        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 600, period=march)
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 60, period=april)

        assert await metering.used_quantity(
            db, tenant_id=tenant_a.id, metric=UsageMetric.VOICE_MINUTE, period=march
        ) == 600
        assert await metering.used_quantity(
            db, tenant_id=tenant_a.id, metric=UsageMetric.VOICE_MINUTE, period=april
        ) == 60

    async def test_usage_is_scoped_to_its_tenant(
        self, db, tenant_a, tenant_b, billing_plans
    ):
        from app.billing.periods import calendar_period

        period = calendar_period()
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 600, period=period)

        assert await metering.used_quantity(
            db, tenant_id=tenant_b.id, metric=UsageMetric.VOICE_MINUTE, period=period
        ) == 0


# ============================================================= idempotency ===

class TestIdempotency:
    def test_the_key_is_derived_not_generated(self):
        """
        Requirement 12: a random UUID per callback is not duplicate
        protection, it is the opposite.
        """
        call_id = uuid.uuid4()
        first = metering.usage_idempotency_key(UsageMetric.VOICE_MINUTE, call_id)
        second = metering.usage_idempotency_key(UsageMetric.VOICE_MINUTE, call_id)

        assert first == second
        assert first == f"voice_minute:{call_id}"

    def test_the_key_is_legible(self):
        """
        Not hashed. `voice_minute:3f2a…` in a support query says what it is,
        and the person reading it is usually looking at a disputed invoice.
        """
        key = metering.usage_idempotency_key(UsageMetric.SMS_SEGMENT, "msg-1")
        assert key.startswith("sms_segment:")

    def test_a_discriminator_separates_genuinely_distinct_facts(self):
        call_id = uuid.uuid4()
        assert metering.usage_idempotency_key(
            UsageMetric.LLM_TOKEN, call_id, discriminator="turn:1"
        ) != metering.usage_idempotency_key(
            UsageMetric.LLM_TOKEN, call_id, discriminator="turn:2"
        )

    async def test_the_same_key_twice_records_once(self, db, tenant_a, billing_plans):
        from app.billing.periods import calendar_period

        period = calendar_period()
        first = await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 120, key="k1", period=period
        )
        second = await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 120, key="k1", period=period
        )

        assert first.created
        assert not second.created and second.duplicate
        assert (await db.execute(select(func.count(UsageEvent.id)))).scalar() == 1
        assert await metering.used_quantity(
            db, tenant_id=tenant_a.id, metric=UsageMetric.VOICE_MINUTE, period=period
        ) == 120

    async def test_ten_replays_record_once(self, db, tenant_a, billing_plans):
        """Requirement 14's explicit test: same callback × 10, one event."""
        from app.billing.periods import calendar_period

        period = calendar_period()
        for _ in range(10):
            await add_usage(
                db, tenant_a, UsageMetric.VOICE_MINUTE, 120, key="same", period=period
            )

        assert (await db.execute(select(func.count(UsageEvent.id)))).scalar() == 1

    async def test_the_same_key_in_two_tenants_is_two_events(
        self, db, tenant_a, tenant_b, billing_plans
    ):
        """
        Keys are tenant-scoped. A global namespace would let one tenant's
        event suppress another's -- a cross-tenant denial of service through
        a shared key space.
        """
        from app.billing.periods import calendar_period

        period = calendar_period()
        for tenant in (tenant_a, tenant_b):
            await add_usage(
                db, tenant, UsageMetric.VOICE_MINUTE, 60, key="shared", period=period
            )
        assert (await db.execute(select(func.count(UsageEvent.id)))).scalar() == 2

    async def test_the_database_constraint_is_the_real_guarantee(
        self, db, tenant_a, billing_plans
    ):
        """
        Application logic is the fast path; the unique constraint is what
        holds when two workers interleave. Asserted by bypassing the service
        entirely.
        """
        from sqlalchemy.exc import IntegrityError

        from app.billing.periods import calendar_period

        period = calendar_period()
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 60, key="dup", period=period
        )

        db.add(UsageEvent(
            tenant_id=tenant_a.id, billing_period=period.label,
            metric=UsageMetric.VOICE_MINUTE,
            event_type=UsageEventType.VOICE_MINUTE_USED,
            quantity=60, unit="second", idempotency_key="dup",
        ))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


class TestConcurrentMetering:
    async def test_two_workers_recording_the_same_event_produce_one_row(
        self, concurrent_sessionmaker, db, tenant_a
    ):
        """
        Requirement 31. Two genuinely concurrent sessions on separate
        connections, the real constraint, no mocking.
        """
        from app.billing.periods import calendar_period
        from app.db.models import Tenant

        maker = concurrent_sessionmaker
        async with maker() as setup:
            tenant = Tenant(name="Race", twilio_number="+15550001111")
            setup.add(tenant)
            await setup.commit()
            await setup.refresh(tenant)
            tenant_id = tenant.id

        period = calendar_period()

        async def attempt():
            async with maker() as session:
                result = await metering.record_usage(
                    session, tenant_id=tenant_id,
                    metric=UsageMetric.VOICE_MINUTE, quantity=120,
                    idempotency_key="voice_minute:the-same-call", period=period,
                )
                await session.commit()
                return result

        results = await asyncio.gather(
            *(attempt() for _ in range(5)), return_exceptions=True
        )
        raised = [r for r in results if isinstance(r, BaseException)]
        assert not raised, f"a worker saw an exception: {raised}"

        created = [r for r in results if r.created]
        assert len(created) == 1, [r.created for r in results]

        async with maker() as session:
            total = (
                await session.execute(select(func.count(UsageEvent.id)))
            ).scalar()
            billed = await metering.used_quantity(
                session, tenant_id=tenant_id,
                metric=UsageMetric.VOICE_MINUTE, period=period,
            )
        assert total == 1
        assert billed == 120, "the customer must be billed once, not five times"


# ================================================================= overage ===

class TestOverage:
    @pytest.fixture
    async def pro(self, db, billing_plans):
        return await plan_catalogue.get_plan_by_code(db, "pro")

    def test_under_the_allowance_is_free(self, pro):
        overage, cost = metering.compute_overage(
            used=1000 * 60, included=2000 * 60,
            metric=UsageMetric.VOICE_MINUTE, plan=pro,
        )
        assert (overage, cost) == (0, 0)

    def test_exactly_the_allowance_is_free(self, pro):
        """The boundary. `>=` here would charge every customer who used
        precisely what they paid for."""
        overage, cost = metering.compute_overage(
            used=2000 * 60, included=2000 * 60,
            metric=UsageMetric.VOICE_MINUTE, plan=pro,
        )
        assert (overage, cost) == (0, 0)

    def test_one_second_over_bills_one_minute(self, pro):
        """Rounding up is the policy, and it is applied once."""
        overage, cost = metering.compute_overage(
            used=2000 * 60 + 1, included=2000 * 60,
            metric=UsageMetric.VOICE_MINUTE, plan=pro,
        )
        assert overage == 1
        assert cost == 1_000            # 10c in millicents

    def test_the_documented_example(self, pro):
        """included 500, used 620 -> overage 120, at the plan's rate."""
        overage, cost = metering.compute_overage(
            used=620 * 60, included=500 * 60,
            metric=UsageMetric.VOICE_MINUTE, plan=pro,
        )
        assert overage == 120
        assert cost == 120 * 1_000      # 120 x 10c

    def test_rounding_happens_on_the_period_not_per_call(self, db, pro):
        """
        **The policy that matters commercially.** Two hundred 20-second calls
        is 67 billable minutes, not 200. Per-call rounding is legal, common,
        and the single most frequent cause of a telephony billing dispute.
        """
        total_seconds = 200 * 20        # 4000s = 66.67 min
        overage, _ = metering.compute_overage(
            used=total_seconds, included=0,
            metric=UsageMetric.VOICE_MINUTE, plan=pro,
        )
        assert overage == 67
        assert overage != 200

    def test_discrete_metrics_are_not_rounded(self, pro):
        overage, cost = metering.compute_overage(
            used=2_600, included=2_500,
            metric=UsageMetric.SMS_SEGMENT, plan=pro,
        )
        assert overage == 100
        assert cost == 100 * 150

    async def test_different_plans_charge_different_rates(self, db, billing_plans):
        starter = await plan_catalogue.get_plan_by_code(db, "starter")
        enterprise = await plan_catalogue.get_plan_by_code(db, "enterprise")

        _, starter_cost = metering.compute_overage(
            used=60 * 60, included=0, metric=UsageMetric.VOICE_MINUTE, plan=starter
        )
        _, enterprise_cost = metering.compute_overage(
            used=60 * 60, included=0, metric=UsageMetric.VOICE_MINUTE, plan=enterprise
        )
        assert starter_cost == 60 * 1_200        # 12c
        assert enterprise_cost == 60 * 800       # 8c
        assert enterprise_cost < starter_cost

    def test_no_plan_means_no_charge(self):
        """
        A tenant whose plan row vanished must not be invoiced from thin air.
        `included_units(None)` is 0 and the rate is 0, so the overage is
        counted but priced at nothing until a human looks.
        """
        overage, cost = metering.compute_overage(
            used=1000, included=0, metric=UsageMetric.VOICE_MINUTE, plan=None
        )
        assert overage == 17
        assert cost == 0

    def test_millicents_express_sub_cent_rates(self):
        """
        Token pricing is genuinely below one cent per unit. Cents cannot
        express it, and rounding each token to a cent would overcharge by
        orders of magnitude.
        """
        from app.db.models import BillingPlan

        plan = BillingPlan(
            code="x", name="x", overage_llm_token_millicents=2,   # $0.0002
        )
        _, cost = metering.compute_overage(
            used=1_000_000, included=0, metric=UsageMetric.LLM_TOKEN, plan=plan
        )
        assert cost == 2_000_000                 # millicents
        assert cost / 100 / 100 == 200.0         # $200 per million tokens


class TestBillableSeconds:
    @pytest.mark.parametrize("duration,expected", [
        (0, 0), (None, 0), (-5, 0),
        (0.4, 1),          # a real connection gets the minimum
        (99.4, 99),        # floors
        (99.9, 99),
        (120.0, 120),
    ])
    def test_flooring_not_rounding(self, duration, expected):
        """
        Rounding up on average is a systematic overcharge across thousands of
        calls that nobody consented to.
        """
        assert metering.billable_seconds(duration) == expected


# ================================================================ summaries ===

class TestSummaries:
    async def test_a_summary_is_rebuilt_from_events(self, db, tenant_a, billing_plans):
        from app.billing.periods import calendar_period

        plan = await plan_catalogue.get_plan_by_code(db, "pro")
        period = calendar_period()
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 2100 * 60, period=period)

        summary = await metering.rebuild_summary(
            db, tenant_id=tenant_a.id, period=period, plan=plan,
            metric=UsageMetric.VOICE_MINUTE,
        )
        await db.commit()

        assert summary.used_quantity == 2100 * 60
        assert summary.included_quantity == 2000 * 60
        assert summary.overage_quantity == 100
        assert summary.estimated_overage_millicents == 100 * 1_000

    async def test_a_summary_is_only_a_cache(self, db, tenant_a, billing_plans):
        """
        The property that makes a metering bug recoverable: corrupt the
        summary, rebuild, and it is correct again because the events are
        untouched.
        """
        from app.billing.periods import calendar_period

        plan = await plan_catalogue.get_plan_by_code(db, "pro")
        period = calendar_period()
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 600, period=period)

        summary = await metering.rebuild_summary(
            db, tenant_id=tenant_a.id, period=period, plan=plan,
            metric=UsageMetric.VOICE_MINUTE,
        )
        await db.commit()
        summary.used_quantity = 999_999
        await db.commit()

        rebuilt = await metering.rebuild_summary(
            db, tenant_id=tenant_a.id, period=period, plan=plan,
            metric=UsageMetric.VOICE_MINUTE,
        )
        assert rebuilt.used_quantity == 600

    async def test_a_finalized_period_is_not_silently_rewritten(
        self, db, tenant_a, billing_plans
    ):
        """
        Once an invoice has been cut, changing the figure would leave our
        records and the customer's disagreeing with no trace.
        """
        from app.billing.periods import calendar_period

        plan = await plan_catalogue.get_plan_by_code(db, "pro")
        period = calendar_period()
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 600, period=period)
        await metering.finalize_period(
            db, tenant_id=tenant_a.id, period=period, plan=plan
        )
        await db.commit()

        # A late event arrives for the closed period.
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 300, key="late", period=period
        )
        summary = await metering.rebuild_summary(
            db, tenant_id=tenant_a.id, period=period, plan=plan,
            metric=UsageMetric.VOICE_MINUTE,
        )
        await db.commit()

        assert summary.finalized
        assert summary.used_quantity == 600, "the invoiced figure must not move"
        # ...but the discrepancy is visible.
        assert summary.event_count == 2

    async def test_the_tenant_cache_is_kept_in_step(self, db, tenant_a, billing_plans):
        """
        `Tenant.minutes_used` is demoted to a cache but still maintained: the
        dashboard reads it, and dropping a column external tooling may read is
        a separate announced change.
        """
        from app.billing.periods import calendar_period

        period = calendar_period()
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 150, period=period)
        await metering.sync_tenant_cache(db, tenant_a, period=period)
        await db.commit()

        assert tenant_a.minutes_used == 2.5


# ============================================================= adjustments ===

class TestAdjustments:
    async def test_a_credit_is_a_new_row_not_an_edit(
        self, db, tenant_a, billing_plans
    ):
        """
        Requirement 35: never modify historical usage destructively. The
        original figure survives next to the correction, which is the only way
        an invoice dispute is answerable.
        """
        from app.billing.periods import calendar_period

        period = calendar_period()
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 600, period=period)

        await metering.record_adjustment(
            db, tenant_id=tenant_a.id, metric=UsageMetric.VOICE_MINUTE,
            quantity=-300, reason="goodwill: dropped call",
            actor="owner@example.com", period=period,
        )
        await db.commit()

        rows = (await db.execute(select(UsageEvent))).scalars().all()
        assert len(rows) == 2
        assert sorted(r.quantity for r in rows) == [-300, 600]
        assert await metering.used_quantity(
            db, tenant_id=tenant_a.id, metric=UsageMetric.VOICE_MINUTE, period=period
        ) == 300

    async def test_an_adjustment_records_who_and_why(
        self, db, tenant_a, billing_plans
    ):
        from app.billing.periods import calendar_period

        await metering.record_adjustment(
            db, tenant_id=tenant_a.id, metric=UsageMetric.VOICE_MINUTE,
            quantity=-60, reason="billing error", actor="owner@example.com",
            period=calendar_period(),
        )
        await db.commit()

        event = (await db.execute(select(UsageEvent))).scalars().one()
        assert event.event_type is UsageEventType.MANUAL_ADJUSTMENT
        assert event.event_metadata["reason"] == "billing error"
        assert event.event_metadata["actor"] == "owner@example.com"

    async def test_the_same_adjustment_replayed_is_recorded_once(
        self, db, tenant_a, billing_plans
    ):
        from app.billing.periods import calendar_period

        period = calendar_period()
        for _ in range(3):
            await metering.record_adjustment(
                db, tenant_id=tenant_a.id, metric=UsageMetric.VOICE_MINUTE,
                quantity=-60, reason="dup", actor="owner", period=period,
            )
        await db.commit()
        assert (await db.execute(select(func.count(UsageEvent.id)))).scalar() == 1

    async def test_reporting_clamps_a_negative_total_but_storage_does_not(
        self, db, tenant_a, billing_plans
    ):
        """
        A negative *bill* is impossible; a negative *event log* is evidence.
        Clamping happens at the reporting boundary so reconciliation can still
        see that something went wrong.
        """
        from app.billing.periods import calendar_period

        period = calendar_period()
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 60, period=period)
        await metering.record_adjustment(
            db, tenant_id=tenant_a.id, metric=UsageMetric.VOICE_MINUTE,
            quantity=-600, reason="over-credited", actor="x", period=period,
        )
        await db.commit()

        assert await metering.used_quantity(
            db, tenant_id=tenant_a.id, metric=UsageMetric.VOICE_MINUTE, period=period
        ) == 0
        raw = (
            await db.execute(
                select(func.sum(UsageEvent.quantity)).where(
                    UsageEvent.tenant_id == tenant_a.id
                )
            )
        ).scalar()
        assert raw == -540