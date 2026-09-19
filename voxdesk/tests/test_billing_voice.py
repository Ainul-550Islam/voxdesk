"""
Voice-minute billing and entitlement enforcement.

`TestVoiceBillingRegression` is the regression suite for the audit's F1: the
pre-STEP-7 counter billed the same 120-second call as 2.00, 1.00 or 0.50
minutes depending on the order Twilio delivered its webhooks. These replay
real callback sequences through the real `call_state` machine and assert the
billed figure is the same every time.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.billing import hooks as billing_hooks
from app.billing import metering
from app.billing.entitlements import (
    Decision,
    WARNING_PERCENT,
    check_feature,
    check_feature_live,
    check_voice_minutes,
    load_context,
    threshold_crossed,
)
from app.billing.periods import calendar_period, period_for
from app.db.models import (
    Call,
    CallDirection,
    CallStatus,
    SubscriptionStatus,
    UsageEvent,
    UsageMetric,
)
from app.telephony import call_state
from tests.conftest import add_usage, subscribe

UTC = timezone.utc


async def make_call(db, tenant, *, duration=120.0, status=CallStatus.COMPLETED, **over):
    fields = dict(
        tenant_id=tenant.id,
        direction=CallDirection.INBOUND,
        status=status,
        from_number="+15551230000",
        to_number=tenant.twilio_number,
        call_sid=f"CA{uuid.uuid4().hex}",
        started_at=datetime.now(UTC),
        duration_seconds=duration,
    )
    fields.update(over)
    call = Call(**fields)
    db.add(call)
    await db.commit()
    await db.refresh(call)
    return call


async def billed_seconds(db, tenant) -> int:
    subscription = None
    from app.billing.service import get_subscription

    subscription = await get_subscription(db, tenant.id)
    return await metering.used_quantity(
        db, tenant_id=tenant.id, metric=UsageMetric.VOICE_MINUTE,
        period=period_for(subscription),
    )


# ============================================== the F1 regression suite ===

class TestVoiceBillingRegression:
    """
    **The audit's F1.**

    The old code billed `duration - previous_duration`, subtracting whatever
    an earlier callback had stamped. Intermediate callbacks were stamped but
    never billed, so their seconds vanished. Direction of the error was
    *under*-billing, so nobody complained and it was never found.
    """

    @pytest.mark.parametrize("callbacks,label", [
        ([("completed", 120)], "completed only"),
        ([("in-progress", 0), ("completed", 120)], "in-progress(0s) first"),
        ([("in-progress", 60), ("completed", 120)], "in-progress(60s) first"),
        ([("answered", 90), ("completed", 120)], "answered(90s) first"),
        ([("completed", 120)] * 10, "completed x10"),
        ([("in-progress", 30), ("completed", 120), ("completed", 120)], "mixed + retry"),
    ])
    async def test_a_120_second_call_always_bills_120_seconds(
        self, db, tenant_a, billing_plans, callbacks, label
    ):
        """
        Whatever Twilio sends, in whatever order, the customer is billed for
        the call they actually had.
        """
        await subscribe(db, tenant_a, "pro")
        call = await make_call(db, tenant_a, duration=0.0, status=CallStatus.RINGING)

        for status, duration in callbacks:
            result = call_state.apply_provider_status(
                call, status, duration_seconds=float(duration), source="twilio_status"
            )
            if result.applied and call_state.is_terminal(call.status):
                await billing_hooks.on_call_finalized(db, tenant_a, call)
            await db.commit()

        assert await billed_seconds(db, tenant_a) == 120, label

    async def test_one_call_produces_exactly_one_event(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        call = await make_call(db, tenant_a, duration=0.0, status=CallStatus.RINGING)

        for _ in range(10):
            result = call_state.apply_provider_status(
                call, "completed", duration_seconds=120.0, source="twilio_status"
            )
            if result.applied and call_state.is_terminal(call.status):
                await billing_hooks.on_call_finalized(db, tenant_a, call)
        await db.commit()

        assert (await db.execute(select(func.count(UsageEvent.id)))).scalar() == 1

    async def test_a_worker_retry_bills_once(self, db, tenant_a, billing_plans):
        """
        The hook itself is idempotent, independently of the state machine —
        so a retry that reaches it directly is also safe.
        """
        await subscribe(db, tenant_a, "pro")
        call = await make_call(db, tenant_a, duration=95.0)

        first = await billing_hooks.on_call_finalized(db, tenant_a, call)
        second = await billing_hooks.on_call_finalized(db, tenant_a, call)
        third = await billing_hooks.on_call_finalized(db, tenant_a, call)
        await db.commit()

        assert first is True
        assert second is False and third is False
        assert await billed_seconds(db, tenant_a) == 95

    async def test_a_transferred_call_is_billed_once(
        self, db, tenant_a, billing_plans
    ):
        """
        **The transfer billing policy (requirement 13), asserted.**

        Total customer-visible duration, not per provider leg. A transferred
        call is one call from the customer's point of view and one line on the
        invoice. Billing the parent and dial legs separately would roughly
        double every escalated call.
        """
        await subscribe(db, tenant_a, "pro")
        call = await make_call(
            db, tenant_a, duration=0.0, status=CallStatus.RINGING, escalated=True
        )

        # The parent leg and the dial leg both report, out of order.
        for status, duration in [
            ("in-progress", 45),        # before the transfer
            ("completed", 45),          # dial leg finishing
            ("completed", 180),         # parent leg, the full call
        ]:
            result = call_state.apply_provider_status(
                call, status, duration_seconds=float(duration), source="twilio"
            )
            if result.applied and call_state.is_terminal(call.status):
                await billing_hooks.on_call_finalized(db, tenant_a, call)
        await db.commit()

        events = (await db.execute(select(UsageEvent))).scalars().all()
        assert len(events) == 1, "a transferred call must not be billed twice"
        assert events[0].event_metadata["transferred"] is True

    async def test_a_zero_length_call_is_not_billed(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        call = await make_call(db, tenant_a, duration=0.0)

        assert await billing_hooks.on_call_finalized(db, tenant_a, call) is False
        await db.commit()
        assert (await db.execute(select(func.count(UsageEvent.id)))).scalar() == 0

    async def test_many_calls_aggregate(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro")
        for seconds in (60, 90, 30, 240):
            call = await make_call(db, tenant_a, duration=float(seconds))
            await billing_hooks.on_call_finalized(db, tenant_a, call)
        await db.commit()

        assert await billed_seconds(db, tenant_a) == 420

    async def test_usage_records_the_source_call(self, db, tenant_a, billing_plans):
        """An invoice line must be traceable to the thing the customer did."""
        await subscribe(db, tenant_a, "pro")
        call = await make_call(db, tenant_a, duration=120.0)
        await billing_hooks.on_call_finalized(db, tenant_a, call)
        await db.commit()

        event = (await db.execute(select(UsageEvent))).scalars().one()
        assert event.source_entity_id == call.id
        assert event.source_reference == call.call_sid
        assert event.event_metadata["direction"] == "inbound"

    async def test_billing_never_raises_into_the_call_path(
        self, db, tenant_a, billing_plans, monkeypatch
    ):
        """
        A billing problem must not make VoxDesk return non-2xx to Twilio,
        which would make Twilio retry and attempt the same billing again.
        """
        async def boom(*args, **kwargs):
            raise RuntimeError("the billing layer is on fire")

        monkeypatch.setattr(metering, "record_usage", boom)
        call = await make_call(db, tenant_a, duration=120.0)

        assert await billing_hooks.on_call_finalized(db, tenant_a, call) is False

    async def test_the_tenant_cache_tracks_the_events(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        call = await make_call(db, tenant_a, duration=150.0)
        await billing_hooks.on_call_finalized(db, tenant_a, call)
        await db.commit()

        await db.refresh(tenant_a)
        assert tenant_a.minutes_used == 2.5


class TestOtherMetrics:
    async def test_sms_segments(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro")
        assert await billing_hooks.on_sms_sent(
            db, tenant_a, message_id="SM123", segments=3
        )
        # A retried send the provider deduplicated must not bill twice.
        await billing_hooks.on_sms_sent(db, tenant_a, message_id="SM123", segments=3)
        await db.commit()

        subscription = None
        from app.billing.service import get_subscription

        subscription = await get_subscription(db, tenant_a.id)
        assert await metering.used_quantity(
            db, tenant_id=tenant_a.id, metric=UsageMetric.SMS_SEGMENT,
            period=period_for(subscription),
        ) == 3

    async def test_llm_tokens_are_discriminated_by_turn(
        self, db, tenant_a, billing_plans
    ):
        """
        A call has many turns and each is a distinct fact. Without the
        discriminator the second turn would collide with the first and only
        one would ever be billed.
        """
        await subscribe(db, tenant_a, "pro")
        call_id = uuid.uuid4()

        await billing_hooks.on_llm_tokens(
            db, tenant_a, call_id=call_id, tokens=500, turn=1
        )
        await billing_hooks.on_llm_tokens(
            db, tenant_a, call_id=call_id, tokens=700, turn=2
        )
        # ...but turn 1 replayed is still one event.
        await billing_hooks.on_llm_tokens(
            db, tenant_a, call_id=call_id, tokens=500, turn=1
        )
        await db.commit()

        from app.billing.service import get_subscription

        subscription = await get_subscription(db, tenant_a.id)
        assert await metering.used_quantity(
            db, tenant_id=tenant_a.id, metric=UsageMetric.LLM_TOKEN,
            period=period_for(subscription),
        ) == 1200

    async def test_tts_characters(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro")
        await billing_hooks.on_tts_characters(
            db, tenant_a, call_id=uuid.uuid4(), characters=4200, turn=1
        )
        await db.commit()

        from app.billing.service import get_subscription

        subscription = await get_subscription(db, tenant_a.id)
        assert await metering.used_quantity(
            db, tenant_id=tenant_a.id, metric=UsageMetric.TTS_CHARACTER,
            period=period_for(subscription),
        ) == 4200


# ============================================================ entitlements ===

class TestMeteredEntitlements:
    async def test_well_under_the_limit_allows(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro")
        context = await load_context(db, tenant_a)
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 100 * 60, period=context.period
        )

        entitlement = await check_voice_minutes(db, context)
        assert entitlement.decision is Decision.ALLOW
        assert entitlement.allowed

    async def test_eighty_percent_warns_without_blocking(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        context = await load_context(db, tenant_a)
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 1_700 * 60, period=context.period
        )

        entitlement = await check_voice_minutes(db, context)
        assert entitlement.decision is Decision.WARN
        assert entitlement.allowed
        assert entitlement.percent_used >= WARNING_PERCENT

    async def test_over_the_allowance_with_overage_enabled_continues(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        context = await load_context(db, tenant_a)
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 2_500 * 60, period=context.period
        )

        entitlement = await check_voice_minutes(db, context)
        assert entitlement.decision is Decision.ALLOW_WITH_OVERAGE
        assert entitlement.allowed

    async def test_over_the_allowance_with_overage_disabled_blocks(
        self, db, tenant_a, billing_plans
    ):
        """
        A trial that can run up an overage bill is not a trial. Requirement 26:
        refuse *before* the provider call.
        """
        await subscribe(db, tenant_a, "trial")
        context = await load_context(db, tenant_a)
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 61 * 60, period=context.period
        )

        entitlement = await check_voice_minutes(db, context)
        assert entitlement.decision is Decision.DENY
        assert not entitlement.allowed
        assert "included" in entitlement.message

    async def test_the_projected_call_is_counted_not_just_the_current_position(
        self, db, tenant_a, billing_plans
    ):
        """
        "Am I already over" is a different question from "will this call put
        me over", and requirement 26 asks the second one.
        """
        await subscribe(db, tenant_a, "trial")
        context = await load_context(db, tenant_a)
        # 59 of 60 included minutes used.
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 59 * 60, period=context.period
        )

        assert (await check_voice_minutes(db, context)).allowed
        # ...but a two-minute call would cross the line.
        assert not (
            await check_voice_minutes(db, context, estimated_seconds=120)
        ).allowed

    async def test_an_inactive_subscription_denies(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro", status=SubscriptionStatus.CANCELED)
        context = await load_context(db, tenant_a)

        entitlement = await check_voice_minutes(db, context)
        assert entitlement.decision is Decision.DENY
        assert "not active" in entitlement.message

    async def test_past_due_still_entitles(self, db, tenant_a, billing_plans):
        """
        Stripe is still retrying the card. Cutting a business's phone line off
        on the first failed charge costs more goodwill than the few days of
        service it saves.
        """
        await subscribe(db, tenant_a, "pro", status=SubscriptionStatus.PAST_DUE)
        context = await load_context(db, tenant_a)
        assert (await check_voice_minutes(db, context)).allowed

    async def test_a_tenant_with_no_subscription_is_not_blocked(
        self, db, tenant_a, billing_plans
    ):
        """
        Requirement 25: every tenant created before STEP 7 is in this state,
        and they must not suddenly stop working.
        """
        context = await load_context(db, tenant_a)
        assert context.subscription is None
        assert context.plan is not None          # falls back to the default
        assert (await check_voice_minutes(db, context)).allowed

    async def test_the_legacy_plan_string_is_honoured(
        self, db, tenant_a, billing_plans
    ):
        """
        A pre-STEP-7 tenant with `plan="pro"` and no subscription keeps the
        Pro entitlements they were sold.
        """
        tenant_a.plan = "pro"
        await db.commit()

        context = await load_context(db, tenant_a)
        assert context.plan.code == "pro"

    async def test_unlimited_mode_allows_everything(
        self, db, tenant_a, billing_plans, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "billing_unlimited_entitlements", True)
        await subscribe(db, tenant_a, "trial")
        context = await load_context(db, tenant_a)
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 99_999 * 60, period=context.period
        )

        entitlement = await check_voice_minutes(db, context)
        assert entitlement.decision is Decision.ALLOW
        assert entitlement.reason == "unlimited_mode"

    async def test_a_missing_catalogue_degrades_open_rather_than_denying(
        self, db, tenant_a
    ):
        """
        An unseeded catalogue is *our* failure. Denying service for it
        punishes the customer for our bookkeeping.
        """
        context = await load_context(db, tenant_a)      # no billing_plans fixture
        assert context.plan is None

        entitlement = await check_voice_minutes(db, context)
        assert entitlement.decision is Decision.ALLOW
        assert entitlement.reason == "no_plan_resolved"
        assert entitlement.details["degraded"] is True


class TestFeatureEntitlements:
    async def test_a_numeric_limit(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "starter")       # 3 team members
        context = await load_context(db, tenant_a)

        assert check_feature(context, "team_members", current=1).allowed
        assert check_feature(context, "team_members", current=2).allowed
        assert not check_feature(context, "team_members", current=3).allowed

    async def test_the_denial_message_names_the_plan(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "starter")
        context = await load_context(db, tenant_a)

        entitlement = check_feature(context, "team_members", current=10)
        assert "starter" in entitlement.message
        assert "3" in entitlement.message

    async def test_a_boolean_feature(self, db, tenant_a, billing_plans):
        from app.billing.plans import get_plan_by_code

        subscription = await subscribe(db, tenant_a, "starter")   # whatsapp: False
        assert not check_feature(await load_context(db, tenant_a), "whatsapp").allowed

        # Move the *same* subscription to Pro rather than creating a second
        # one -- `uq_subscription_tenant_provider` allows only one per tenant,
        # which is the constraint that makes "the tenant's subscription" a
        # well-defined phrase.
        subscription.plan_id = (await get_plan_by_code(db, "pro")).id
        await db.commit()
        assert check_feature(await load_context(db, tenant_a), "whatsapp").allowed

    async def test_unlimited_is_minus_one(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "enterprise")
        context = await load_context(db, tenant_a)
        assert check_feature(context, "team_members", current=10_000).allowed

    async def test_an_unlisted_feature_defaults_permissively(
        self, db, tenant_a, billing_plans
    ):
        """
        A limit that matters is written into the plan. An unlisted one must
        not silently become zero for every pre-STEP-7 tenant.
        """
        from app.db.models import BillingPlan
        from app.billing.entitlements import BillingContext

        context = BillingContext(
            tenant_id=tenant_a.id,
            plan=BillingPlan(code="bare", name="Bare", feature_entitlements={}),
            subscription=None, period=calendar_period(),
        )
        assert check_feature(context, "team_members", current=500).allowed

    async def test_check_feature_live_counts_the_real_rows(
        self, db, tenant_a, owner_a, admin_a, billing_plans
    ):
        await subscribe(db, tenant_a, "starter")       # 3 team members
        context = await load_context(db, tenant_a)

        # Two users already exist from the fixtures.
        entitlement = await check_feature_live(db, context, "team_members", adding=1)
        assert entitlement.allowed
        assert entitlement.used == 3

        assert not (
            await check_feature_live(db, context, "team_members", adding=2)
        ).allowed


class TestThresholds:
    async def test_a_threshold_fires_once(self, db, tenant_a, billing_plans):
        """
        Stateful by design: a tenant who passes 80% must not get a warning on
        every subsequent call for the rest of the month.
        """
        from app.billing.periods import BillingPeriod, now_utc

        await subscribe(db, tenant_a, "pro")
        context = await load_context(db, tenant_a)

        # Early in the period, so pace matters.
        early = BillingPeriod(
            label=context.period.label, start=now_utc() - timedelta(days=2),
            end=now_utc() + timedelta(days=28),
        )
        context = type(context)(
            tenant_id=context.tenant_id, plan=context.plan,
            subscription=context.subscription, period=early,
        )
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 1_700 * 60, period=early
        )
        await metering.rebuild_all_summaries(
            db, tenant_id=tenant_a.id, period=early, plan=context.plan
        )
        await db.commit()

        assert await threshold_crossed(db, context, UsageMetric.VOICE_MINUTE) == 80
        await db.commit()
        assert await threshold_crossed(db, context, UsageMetric.VOICE_MINUTE) is None

    async def test_eighty_percent_late_in_the_period_is_not_a_signal(
        self, db, tenant_a, billing_plans
    ):
        """
        80% on day 27 is a customer using what they paid for. Only the early
        crossing is worth flagging.
        """
        from app.billing.periods import BillingPeriod, now_utc

        await subscribe(db, tenant_a, "pro")
        context = await load_context(db, tenant_a)
        late = BillingPeriod(
            label=context.period.label, start=now_utc() - timedelta(days=27),
            end=now_utc() + timedelta(days=3),
        )
        context = type(context)(
            tenant_id=context.tenant_id, plan=context.plan,
            subscription=context.subscription, period=late,
        )
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 1_700 * 60, period=late
        )
        await metering.rebuild_all_summaries(
            db, tenant_id=tenant_a.id, period=late, plan=context.plan
        )
        await db.commit()

        assert await threshold_crossed(db, context, UsageMetric.VOICE_MINUTE) is None

    async def test_a_hundred_percent_fires_whenever_it_happens(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        context = await load_context(db, tenant_a)
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 2_100 * 60, period=context.period
        )
        await metering.rebuild_all_summaries(
            db, tenant_id=tenant_a.id, period=context.period, plan=context.plan
        )
        await db.commit()

        assert await threshold_crossed(db, context, UsageMetric.VOICE_MINUTE) == 100


# ================================================= enforcement at the edge ===

class TestOutboundEnforcement:
    async def test_an_outbound_call_is_allowed_under_the_limit(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        allowed, _ = await billing_hooks.may_place_outbound_call(db, tenant_a)
        assert allowed

    async def test_an_outbound_call_is_refused_before_the_provider_call(
        self, db, tenant_a, billing_plans
    ):
        """
        **Requirement 26.** The tenant is on a plan with overage disabled and
        is out of minutes, so no money is spent finding that out.
        """
        await subscribe(db, tenant_a, "trial")
        context = await load_context(db, tenant_a)
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 60 * 60, period=context.period
        )

        allowed, reason = await billing_hooks.may_place_outbound_call(db, tenant_a)
        assert not allowed
        assert reason == "over_included_no_overage"

    async def test_an_overage_plan_keeps_dialling(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro")
        context = await load_context(db, tenant_a)
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 3_000 * 60, period=context.period
        )

        allowed, reason = await billing_hooks.may_place_outbound_call(db, tenant_a)
        assert allowed
        assert reason == "allow_with_overage"

    async def test_a_billing_failure_fails_open(
        self, db, tenant_a, billing_plans, monkeypatch
    ):
        """
        A billing lookup failing must not stop a business operating. The
        metering still records what happens, so nothing is lost, and the
        alternative is an outage caused by our own bookkeeping.
        """
        from app.billing import entitlements

        async def boom(*args, **kwargs):
            raise RuntimeError("database on fire")

        monkeypatch.setattr(entitlements, "load_context", boom)
        allowed, reason = await billing_hooks.may_place_outbound_call(db, tenant_a)
        assert allowed
        assert reason == "check_failed_open"

    async def test_enforcement_can_be_switched_off(
        self, db, tenant_a, billing_plans, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "billing_enforce_entitlements", False)
        await subscribe(db, tenant_a, "trial")
        context = await load_context(db, tenant_a)
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 999 * 60, period=context.period
        )

        allowed, reason = await billing_hooks.may_place_outbound_call(db, tenant_a)
        assert allowed
        assert reason == "enforcement_disabled"


class TestInboundIsNeverBlocked:
    async def test_the_inbound_voice_route_has_no_usage_cap(self):
        """
        **The audit's F10, asserted structurally.**

        The old code hung up on a dentist's patients because the dentist owed
        money — comparing a lifetime counter against a monthly allowance
        (F2), so it eventually bricked every account permanently.

        The customer being cut off is not the customer who owes us. Entitlement
        is enforced where the spend originates; the inbound line stays open.
        """
        import pathlib

        # Comments are stripped first: this file *explains* the removed cap at
        # length, and matching that prose would make the test pass or fail on
        # the wording of a comment rather than on the code.
        source = "\n".join(
            line for line in
            pathlib.Path("app/telephony/twilio_handler.py").read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "minutes_used >= " not in source
        assert "reached its usage limit" not in source
        # ...and nothing reads the lifetime counter for a decision any more.
        assert "tenant.minutes_used" not in source

    async def test_an_over_limit_tenant_still_answers_inbound_calls(
        self, db, tenant_a, billing_plans
    ):
        """The behavioural half of the same guarantee."""
        await subscribe(db, tenant_a, "trial")
        context = await load_context(db, tenant_a)
        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 999 * 60, period=context.period
        )

        # Outbound is refused...
        allowed, _ = await billing_hooks.may_place_outbound_call(db, tenant_a)
        assert not allowed

        # ...but an inbound call is still finalized and metered.
        call = await make_call(db, tenant_a, duration=180.0)
        assert await billing_hooks.on_call_finalized(db, tenant_a, call) is True