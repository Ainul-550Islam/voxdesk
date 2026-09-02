"""
Subscription lifecycle, Stripe adapter, webhooks and provider contract.

The theme: **provider state is the only thing that establishes billing
truth.** Creating a checkout session establishes nothing; a browser reaching
the success URL establishes nothing; only a verified webhook or a direct
provider read does.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app.billing import service, webhooks
from app.billing.base import (
    BillingCapability,
    BillingContextConfig,
    BillingProvider,
    RemoteSubscription,
)
from app.billing.errors import (
    BillingAuthError,
    BillingCardError,
    BillingConfigurationError,
    BillingError,
    BillingNotFoundError,
    BillingRateLimited,
    BillingTemporaryError,
    BillingTimeout,
    BillingValidationError,
    PlanNotFound,
)
from app.billing.plans import get_plan_by_code, resolve_price_id
from app.billing.providers.stripe import StripeProvider
from app.billing.registry import PROVIDERS, build, capabilities_of
from app.db.models import (
    BillingInterval,
    BillingProviderType,
    BillingWebhookReceipt,
    InvoiceStatus,
    Subscription,
    SubscriptionStatus,
)
from tests.conftest import FakeTransport, stripe_event, stripe_signature, subscribe

UTC = timezone.utc
ALL_PROVIDERS = list(BillingProviderType)

_CONFIG = {
    BillingProviderType.STRIPE: BillingContextConfig(
        secret_key="sk_test_SECRET_KEY_VALUE",
        webhook_secret="whsec_SECRET_HOOK_VALUE",
        base_url="https://stripe.test/v1",
        success_url="https://app.test/ok",
        cancel_url="https://app.test/no",
        return_url="https://app.test/back",
    ),
    BillingProviderType.MANUAL: BillingContextConfig(),
}


def make_provider(provider: BillingProviderType) -> BillingProvider:
    return build(provider, _CONFIG[provider])


def secrets_for(provider: BillingProviderType) -> list[str]:
    config = _CONFIG[provider]
    return [v for v in (config.secret_key, config.webhook_secret) if v]


def stripe_sub(**over) -> dict:
    body = {
        "id": "sub_FAKE123",
        "object": "subscription",
        "status": "active",
        "customer": "cus_FAKE123",
        "cancel_at_period_end": False,
        "current_period_start": 1_770_000_000,
        "current_period_end": 1_772_678_400,
        "items": {"data": [{
            "id": "si_FAKE",
            "price": {"id": "price_FAKE_pro_month", "recurring": {"interval": "month"}},
        }]},
    }
    body.update(over)
    return body


# ============================================================ plan catalogue ===

class TestPlanCatalogue:
    async def test_a_plan_resolves_by_code(self, db, billing_plans):
        plan = await get_plan_by_code(db, "pro")
        assert plan.name == "Pro"
        assert plan.included_voice_minutes == 2_000

    async def test_an_inactive_plan_is_refused_like_a_missing_one(
        self, db, billing_plans
    ):
        """
        Same error for both, so a grandfathered plan code cannot be
        discovered by probing the checkout endpoint.
        """
        from app.billing.plans import require_plan

        plan = await get_plan_by_code(db, "pro")
        plan.is_active = False
        await db.commit()

        with pytest.raises(PlanNotFound):
            await require_plan(db, "pro")
        with pytest.raises(PlanNotFound):
            await require_plan(db, "no-such-plan")

    async def test_the_price_id_comes_from_the_catalogue(self, db, billing_plans):
        """
        **The trust boundary.** A checkout request names a plan code; the
        price comes from here and never from a client.
        """
        plan = await get_plan_by_code(db, "pro")
        assert resolve_price_id(plan, BillingInterval.MONTH) == "price_FAKE_pro_month"
        assert resolve_price_id(plan, BillingInterval.YEAR) == "price_FAKE_pro_year"

    async def test_a_plan_with_no_price_id_fails_loudly(self, db, billing_plans):
        """
        Never defaults. Silently falling back to the monthly price when a
        customer asked for annual is a billing dispute.
        """
        plan = await get_plan_by_code(db, "pro")
        plan.provider_price_ids = {"month": "price_FAKE_pro_month"}
        await db.commit()

        with pytest.raises(BillingConfigurationError):
            resolve_price_id(plan, BillingInterval.YEAR)

    async def test_seeding_is_idempotent_and_never_clobbers_prices(
        self, db, billing_plans
    ):
        """
        An operator who repriced Pro in production must not have that undone
        by the next deploy -- that is an outage where every customer's next
        invoice is wrong.
        """
        from app.billing.plans import list_plans, sync_seed_plans

        plan = await get_plan_by_code(db, "pro")
        plan.monthly_price_cents = 59_900
        plan.provider_price_ids = {"month": "price_OPERATOR_EDITED"}
        await db.commit()

        await sync_seed_plans(db)
        await db.refresh(plan)

        assert plan.monthly_price_cents == 59_900
        assert plan.provider_price_ids["month"] == "price_OPERATOR_EDITED"
        assert len(await list_plans(db)) == 4

    def test_feature_entitlements_are_validated_on_write(self):
        """
        A typo like `team_memebers` would store cleanly and then grant the
        service default forever -- a limit that exists in the sales
        conversation and nowhere in the product.
        """
        from app.billing.plans import validate_feature_entitlements

        assert validate_feature_entitlements({"team_members": 5}) == {"team_members": 5}
        with pytest.raises(BillingConfigurationError):
            validate_feature_entitlements({"team_memebers": 5})
        with pytest.raises(BillingConfigurationError):
            validate_feature_entitlements({"team_members": "lots"})
        with pytest.raises(BillingConfigurationError):
            validate_feature_entitlements({"team_members": -7})

    async def test_production_requires_price_ids_for_active_paid_plans(
        self, db, billing_plans
    ):
        """Requirement 6, checked at boot rather than at checkout."""
        from app.billing.plans import configuration_problems, list_plans

        plans = await list_plans(db)
        assert configuration_problems(plans, is_production=True) == []

        pro = await get_plan_by_code(db, "pro")
        pro.provider_price_ids = {}
        await db.commit()

        problems = configuration_problems(await list_plans(db), is_production=True)
        assert any("pro" in p for p in problems)
        # ...but development is not blocked.
        assert configuration_problems(await list_plans(db), is_production=False) == []


# ================================================================= customer ===

class TestCustomer:
    async def test_a_customer_is_created_once(self, db, tenant_a, billing_plans):
        subscription, first = await service.ensure_customer(db, tenant_a)
        await db.commit()
        _, second = await service.ensure_customer(db, tenant_a)

        assert first == second
        count = (await db.execute(select(func.count(Subscription.id)))).scalar()
        assert count == 1

    async def test_an_existing_provider_customer_is_reused(
        self, db, tenant_a, billing_plans, monkeypatch
    ):
        """
        Covers a previous attempt that created a customer and then failed
        before we stored the id. Without this every retry makes another.
        """
        from app.billing.base import RemoteCustomer
        from app.billing.providers.manual import ManualBillingProvider

        calls = {"create": 0}

        async def counted(self, **kwargs):
            calls["create"] += 1
            return RemoteCustomer(external_id="should_not_happen")

        monkeypatch.setattr(ManualBillingProvider, "create_customer", counted)

        _, external = await service.ensure_customer(db, tenant_a)
        assert calls["create"] == 0
        assert external.startswith("manual_cus_")

    async def test_a_timeout_reconciles_rather_than_creating_twice(
        self, db, tenant_a, billing_plans, monkeypatch
    ):
        """
        A timeout on create means the provider may well have succeeded.
        Retrying blindly is how a tenant ends up with two customer records.
        """
        from app.billing.base import RemoteCustomer
        from app.billing.providers.manual import ManualBillingProvider

        calls = {"create": 0, "find": 0}

        async def timeout(self, **kwargs):
            calls["create"] += 1
            raise BillingTimeout("no response", provider="manual")

        async def found(self, tenant_id):
            calls["find"] += 1
            # First call (the pre-check) finds nothing; the recovery finds it.
            if calls["find"] == 1:
                return None
            return RemoteCustomer(external_id="cus_RECOVERED", already_existed=True)

        monkeypatch.setattr(ManualBillingProvider, "create_customer", timeout)
        monkeypatch.setattr(ManualBillingProvider, "find_customer_by_tenant", found)

        _, external = await service.ensure_customer(db, tenant_a)
        assert external == "cus_RECOVERED"
        assert calls["create"] == 1

    async def test_a_timeout_that_cannot_be_confirmed_raises(
        self, db, tenant_a, billing_plans, monkeypatch
    ):
        """
        "I could not check" is not evidence of absence. Creating anyway is the
        duplicate this path exists to prevent.
        """
        from app.billing.providers.manual import ManualBillingProvider

        async def timeout(self, **kwargs):
            raise BillingTimeout("no response", provider="manual")

        async def cannot_check(self, tenant_id):
            return None

        monkeypatch.setattr(ManualBillingProvider, "create_customer", timeout)
        monkeypatch.setattr(
            ManualBillingProvider, "find_customer_by_tenant", cannot_check
        )

        with pytest.raises(BillingTimeout):
            await service.ensure_customer(db, tenant_a)

    async def test_two_concurrent_checkouts_create_one_subscription_row(
        self, concurrent_sessionmaker
    ):
        """Requirement 31: no uncontrolled duplicate."""
        from app.billing.plans import SEED_PLANS, validate_feature_entitlements
        from app.db.models import BillingPlan, Tenant

        maker = concurrent_sessionmaker
        async with maker() as setup:
            tenant = Tenant(name="Race", twilio_number="+15550002222")
            setup.add(tenant)
            setup.add_all([
                BillingPlan(
                    code=s.code, name=s.name, monthly_price_cents=s.monthly_price_cents,
                    included_voice_minutes=s.included_voice_minutes,
                    feature_entitlements=validate_feature_entitlements(
                        s.feature_entitlements
                    ),
                    provider_price_ids={"month": f"price_{s.code}"},
                )
                for s in SEED_PLANS
            ])
            await setup.commit()
            await setup.refresh(tenant)
            tenant_id = tenant.id

        async def attempt():
            async with maker() as session:
                own = await session.get(Tenant, tenant_id)
                result = await service.ensure_customer(session, own)
                await session.commit()
                return result[1]

        results = await asyncio.gather(
            *(attempt() for _ in range(4)), return_exceptions=True
        )
        raised = [r for r in results if isinstance(r, BaseException)]
        assert not raised, f"a request saw an exception: {raised}"
        assert len(set(results)) == 1, "every request must get the same customer"

        async with maker() as session:
            count = (
                await session.execute(select(func.count(Subscription.id)))
            ).scalar()
        assert count == 1


# ============================================================= plan changes ===

class TestPlanChanges:
    async def test_an_upgrade_is_immediate(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "starter")
        subscription = await service.change_plan(db, tenant_a, plan_code="pro")

        pro = await get_plan_by_code(db, "pro")
        assert subscription.plan_id == pro.id
        assert subscription.pending_plan_id is None

    async def test_a_downgrade_is_scheduled(self, db, tenant_a, billing_plans):
        """
        Applying it immediately would strand a customer who has already used
        more than the smaller plan includes, and refunding mid-period is an
        accounting system this step deliberately does not build.
        """
        await subscribe(db, tenant_a, "pro")
        subscription = await service.change_plan(db, tenant_a, plan_code="starter")

        pro = await get_plan_by_code(db, "pro")
        starter = await get_plan_by_code(db, "starter")

        assert subscription.plan_id == pro.id            # unchanged for now
        assert subscription.pending_plan_id == starter.id

    async def test_a_scheduled_downgrade_lands_at_renewal(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        subscription = await service.change_plan(db, tenant_a, plan_code="starter")

        assert await service.apply_pending_downgrade(db, subscription)
        await db.commit()

        starter = await get_plan_by_code(db, "starter")
        assert subscription.plan_id == starter.id
        assert subscription.pending_plan_id is None

    async def test_a_downgrade_can_be_cancelled_by_reselecting(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        await service.change_plan(db, tenant_a, plan_code="starter")

        subscription = await service.change_plan(db, tenant_a, plan_code="pro")
        assert subscription.pending_plan_id is None

    async def test_an_unknown_plan_is_refused(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro")
        with pytest.raises(PlanNotFound):
            await service.change_plan(db, tenant_a, plan_code="platinum")

    async def test_a_plan_with_no_price_is_refused_before_the_provider_call(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "starter")
        pro = await get_plan_by_code(db, "pro")
        pro.provider_price_ids = {}
        await db.commit()

        with pytest.raises(BillingConfigurationError):
            await service.change_plan(db, tenant_a, plan_code="pro")


class TestCancellation:
    async def test_cancel_defaults_to_period_end(self, db, tenant_a, billing_plans):
        """
        The customer has paid for the month; cutting them off the moment they
        click cancel takes money for service not delivered.
        """
        await subscribe(db, tenant_a, "pro")
        subscription = await service.cancel(db, tenant_a)

        assert subscription.cancel_at_period_end
        assert subscription.status is SubscriptionStatus.CANCELING
        assert subscription.canceled_at is None

    async def test_immediate_cancel(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro")
        subscription = await service.cancel(db, tenant_a, immediately=True)

        assert subscription.status is SubscriptionStatus.CANCELED
        assert subscription.canceled_at is not None

    async def test_cancelling_twice_is_idempotent(
        self, db, tenant_a, billing_plans, monkeypatch
    ):
        """
        A second click must not hit the provider again -- the audit trail
        would then show two cancellations for one decision.
        """
        from app.billing.providers.manual import ManualBillingProvider

        calls = {"cancel": 0}
        original = ManualBillingProvider.cancel_subscription

        async def counted(self, external_id, **kwargs):
            calls["cancel"] += 1
            return await original(self, external_id, **kwargs)

        monkeypatch.setattr(ManualBillingProvider, "cancel_subscription", counted)

        await subscribe(db, tenant_a, "pro")
        await service.cancel(db, tenant_a)
        await service.cancel(db, tenant_a)

        assert calls["cancel"] == 1

    async def test_a_provider_failure_still_records_the_intent(
        self, db, tenant_a, billing_plans, monkeypatch
    ):
        """
        The customer asked to cancel. Refusing to remember that because a
        vendor API was slow means they ask again and get charged again.
        """
        from app.billing.providers.manual import ManualBillingProvider

        async def fail(self, external_id, **kwargs):
            raise BillingTemporaryError("provider down", provider="manual")

        monkeypatch.setattr(ManualBillingProvider, "cancel_subscription", fail)

        await subscribe(db, tenant_a, "pro")
        subscription = await service.cancel(db, tenant_a)

        assert subscription.cancel_at_period_end
        assert subscription.last_error


class TestOrdering:
    async def test_a_stale_event_does_not_overwrite_newer_state(
        self, db, tenant_a, billing_plans
    ):
        """
        **Requirement 23.** Applying a stale event would downgrade a customer
        who just upgraded, or resurrect a cancelled subscription.
        """
        subscription = await subscribe(db, tenant_a, "pro")
        now = datetime.now(UTC)

        newer = RemoteSubscription(
            external_id="sub_1", status=SubscriptionStatus.ACTIVE, updated_at=now
        )
        assert service._apply_remote(subscription, newer)
        assert subscription.status is SubscriptionStatus.ACTIVE

        older = RemoteSubscription(
            external_id="sub_1", status=SubscriptionStatus.CANCELED,
            updated_at=now - timedelta(hours=1),
        )
        assert service._apply_remote(subscription, older) is False
        assert subscription.status is SubscriptionStatus.ACTIVE

    async def test_a_newer_event_is_applied(self, db, tenant_a, billing_plans):
        subscription = await subscribe(db, tenant_a, "pro")
        now = datetime.now(UTC)

        service._apply_remote(subscription, RemoteSubscription(
            external_id="sub_1", status=SubscriptionStatus.ACTIVE, updated_at=now
        ))
        service._apply_remote(subscription, RemoteSubscription(
            external_id="sub_1", status=SubscriptionStatus.PAST_DUE,
            updated_at=now + timedelta(minutes=5),
        ))
        assert subscription.status is SubscriptionStatus.PAST_DUE


class TestReconciliation:
    async def test_a_lost_subscription_is_adopted_not_recreated(
        self, db, tenant_a, billing_plans, monkeypatch
    ):
        """
        **Requirement 32.** A timeout after `create_subscription` leaves the
        provider holding a subscription we never recorded. Listing by customer
        finds it.
        """
        from app.billing.providers.manual import ManualBillingProvider

        subscription = await subscribe(db, tenant_a, "pro")
        subscription.external_subscription_id = None
        subscription.status = SubscriptionStatus.INCOMPLETE
        await db.commit()

        async def lists(self, external_customer_id):
            return [RemoteSubscription(
                external_id="sub_ORPHANED",
                status=SubscriptionStatus.ACTIVE,
                external_customer_id=external_customer_id,
                external_price_id="price_FAKE_pro_month",
                current_period_start=datetime.now(UTC),
                current_period_end=datetime.now(UTC) + timedelta(days=30),
            )]

        monkeypatch.setattr(ManualBillingProvider, "list_subscriptions", lists)
        monkeypatch.setattr(
            ManualBillingProvider, "capabilities",
            ManualBillingProvider.capabilities | {BillingCapability.LIST_SUBSCRIPTIONS},
        )

        recovered = await service.reconcile_subscription(db, tenant_a)
        assert recovered.external_subscription_id == "sub_ORPHANED"
        assert recovered.status is SubscriptionStatus.ACTIVE

        count = (await db.execute(select(func.count(Subscription.id)))).scalar()
        assert count == 1


# ============================================================ Stripe adapter ===

class TestStripeMapping:
    @pytest.mark.parametrize("raw,expected", [
        ("trialing", SubscriptionStatus.TRIALING),
        ("active", SubscriptionStatus.ACTIVE),
        ("past_due", SubscriptionStatus.PAST_DUE),
        ("unpaid", SubscriptionStatus.PAST_DUE),
        ("canceled", SubscriptionStatus.CANCELED),
        ("incomplete", SubscriptionStatus.INCOMPLETE),
        ("incomplete_expired", SubscriptionStatus.INCOMPLETE_EXPIRED),
        ("paused", SubscriptionStatus.PAUSED),
    ])
    def test_status_normalization(self, raw, expected):
        provider = make_provider(BillingProviderType.STRIPE)
        assert provider._to_subscription(stripe_sub(status=raw)).status is expected

    def test_cancel_at_period_end_becomes_a_state(self):
        """
        Stripe expresses "winding down" as `active` plus a boolean, which
        loses the distinction every UI needs.
        """
        provider = make_provider(BillingProviderType.STRIPE)
        remote = provider._to_subscription(
            stripe_sub(status="active", cancel_at_period_end=True)
        )
        assert remote.status is SubscriptionStatus.CANCELING
        assert remote.cancel_at_period_end

    def test_an_unmapped_status_is_read_safely(self):
        """
        A Stripe change we have not caught up with. INCOMPLETE grants no
        service and cancels nobody.
        """
        provider = make_provider(BillingProviderType.STRIPE)
        remote = provider._to_subscription(stripe_sub(status="something_new"))
        assert remote.status is SubscriptionStatus.INCOMPLETE

    def test_period_bounds_are_read_from_the_item_when_absent_on_the_root(self):
        """
        Stripe moved these onto items in a 2025 API version, and the version
        is set per-account -- we do not control which shape arrives.
        """
        provider = make_provider(BillingProviderType.STRIPE)
        body = stripe_sub()
        del body["current_period_start"]
        del body["current_period_end"]
        body["items"]["data"][0]["current_period_start"] = 1_770_000_000
        body["items"]["data"][0]["current_period_end"] = 1_772_678_400

        remote = provider._to_subscription(body)
        assert remote.current_period_start == datetime.fromtimestamp(
            1_770_000_000, tz=UTC
        )

    def test_a_related_object_is_accepted_as_an_id_or_an_object(self):
        provider = make_provider(BillingProviderType.STRIPE)
        assert provider._to_subscription(
            stripe_sub(customer="cus_A")
        ).external_customer_id == "cus_A"
        assert provider._to_subscription(
            stripe_sub(customer={"id": "cus_B", "object": "customer"})
        ).external_customer_id == "cus_B"

    def test_an_annual_price_is_detected(self):
        provider = make_provider(BillingProviderType.STRIPE)
        body = stripe_sub()
        body["items"]["data"][0]["price"]["recurring"]["interval"] = "year"
        assert provider._to_subscription(body).interval is BillingInterval.YEAR

    @pytest.mark.parametrize("raw,expected", [
        ("draft", InvoiceStatus.DRAFT), ("open", InvoiceStatus.OPEN),
        ("paid", InvoiceStatus.PAID), ("void", InvoiceStatus.VOID),
        ("uncollectible", InvoiceStatus.UNCOLLECTIBLE),
        ("something_new", InvoiceStatus.DRAFT),
    ])
    def test_invoice_status_normalization(self, raw, expected):
        """Requirement 20: do not invent invoice status."""
        provider = make_provider(BillingProviderType.STRIPE)
        invoice = provider._to_invoice({
            "id": "in_FAKE", "status": raw, "currency": "usd",
            "amount_due": 19900, "amount_paid": 19900,
        })
        assert invoice.status is expected


class TestStripeRequests:
    async def test_subscription_creation_payload(self, monkeypatch):
        transport = FakeTransport((200, stripe_sub())).install(monkeypatch)
        provider = make_provider(BillingProviderType.STRIPE)

        await provider.create_subscription(
            external_customer_id="cus_FAKE", price_id="price_FAKE_pro_month",
            trial_days=14, idempotency_key="sub:tenant-1",
            metadata={"voxdesk_tenant_id": "tenant-1"},
        )

        request = transport.last()
        # Stripe is form-encoded, so the payload is in `form`, not `json`.
        assert request["form"]["items[0][price]"] == "price_FAKE_pro_month"
        assert request["form"]["trial_period_days"] == 14
        assert request["form"]["metadata[voxdesk_tenant_id]"] == "tenant-1"
        assert request["headers"]["Idempotency-Key"] == "sub:tenant-1"
        assert request["headers"]["Authorization"].startswith("Bearer sk_test")

    async def test_the_idempotency_key_is_sent_on_every_mutating_call(
        self, monkeypatch
    ):
        """
        Stripe honours `Idempotency-Key`, and it is the difference between a
        timeout costing a retry and a timeout costing a second subscription.
        """
        transport = FakeTransport((200, {"id": "cus_FAKE"})).install(monkeypatch)
        provider = make_provider(BillingProviderType.STRIPE)

        await provider.create_customer(
            tenant_id="t-1", email=None, name="Acme", idempotency_key="customer:t-1",
        )
        assert transport.last()["headers"]["Idempotency-Key"] == "customer:t-1"

    async def test_updating_a_price_reads_the_item_id_first(self, monkeypatch):
        """
        Sending `items[0][price]` without an id *adds* a second item and bills
        the customer for both.
        """
        transport = FakeTransport(
            (200, stripe_sub()),      # the read
            (200, stripe_sub()),      # the update
        ).install(monkeypatch)
        provider = make_provider(BillingProviderType.STRIPE)

        await provider.update_subscription(
            "sub_FAKE123", price_id="price_NEW", idempotency_key="k"
        )
        assert transport.call_count == 2
        assert transport.requests[0]["method"] == "GET"

    async def test_cancelling_a_missing_subscription_succeeds(self, monkeypatch):
        FakeTransport((404, {"error": {"message": "No such subscription"}})).install(
            monkeypatch
        )
        provider = make_provider(BillingProviderType.STRIPE)

        remote = await provider.cancel_subscription("sub_GONE", at_period_end=False)
        assert remote.status is SubscriptionStatus.CANCELED

    async def test_checkout_requires_configured_urls(self, monkeypatch):
        provider = StripeProvider(BillingContextConfig(secret_key="sk_test_x"))
        with pytest.raises(BillingConfigurationError):
            await provider.create_checkout_session(
                external_customer_id="cus_x", price_id="price_x", tenant_id="t",
            )

    async def test_the_tenant_id_is_stamped_on_the_session_and_subscription(
        self, monkeypatch
    ):
        """
        So a webhook can be attributed to a tenant even when the local row is
        not written yet -- requirement 23's ordering problem.
        """
        transport = FakeTransport(
            (200, {"id": "cs_FAKE", "url": "https://checkout.test/x"})
        ).install(monkeypatch)
        provider = make_provider(BillingProviderType.STRIPE)

        await provider.create_checkout_session(
            external_customer_id="cus_x", price_id="price_x",
            tenant_id="tenant-42", trial_days=0,
        )
        form = transport.last()["form"]
        assert transport.last()["url"].endswith("/checkout/sessions")
        assert form["metadata[voxdesk_tenant_id]"] == "tenant-42"
        assert form["subscription_data[metadata][voxdesk_tenant_id]"] == "tenant-42"

    @pytest.mark.parametrize("status,expected,retryable", [
        (401, BillingAuthError, False),
        (402, BillingCardError, False),
        (404, BillingNotFoundError, False),
        (400, BillingValidationError, False),
        (429, BillingRateLimited, True),
        (500, BillingTemporaryError, True),
        (503, BillingTemporaryError, True),
    ])
    async def test_error_mapping(self, status, expected, retryable, monkeypatch):
        FakeTransport((status, {"error": {"message": "nope"}})).install(monkeypatch)
        provider = make_provider(BillingProviderType.STRIPE)

        with pytest.raises(BillingError) as caught:
            await provider.request("POST", "https://stripe.test/v1/x")
        assert isinstance(caught.value, expected)
        assert caught.value.retryable is retryable

    async def test_a_timeout_is_retryable(self, monkeypatch):
        FakeTransport(httpx.ReadTimeout("slow")).install(monkeypatch)
        provider = make_provider(BillingProviderType.STRIPE)

        with pytest.raises(BillingTimeout) as caught:
            await provider.request("POST", "https://stripe.test/v1/x")
        assert caught.value.retryable is True


# ================================================== webhook signature (21) ===

class TestWebhookSignature:
    SECRET = "whsec_SECRET_HOOK_VALUE"

    def provider(self):
        return make_provider(BillingProviderType.STRIPE)

    def test_a_valid_signature_is_accepted(self):
        body = stripe_event("invoice.paid", {"id": "in_1"})
        event = self.provider().verify_webhook(
            raw_body=body, signature_header=stripe_signature(body, self.SECRET)
        )
        assert event.event_type == "invoice.paid"

    def test_a_wrong_secret_is_rejected(self):
        body = stripe_event("invoice.paid", {"id": "in_1"})
        with pytest.raises(BillingAuthError):
            self.provider().verify_webhook(
                raw_body=body,
                signature_header=stripe_signature(body, "whsec_WRONG"),
            )

    def test_a_tampered_body_is_rejected(self):
        body = stripe_event("invoice.paid", {"id": "in_1", "amount_paid": 100})
        header = stripe_signature(body, self.SECRET)
        tampered = body.replace(b'"amount_paid": 100', b'"amount_paid": 999')

        with pytest.raises(BillingAuthError):
            self.provider().verify_webhook(
                raw_body=tampered, signature_header=header
            )

    def test_re_serialization_would_break_it(self):
        """
        Requirement 21: verification must use the exact raw body. This is why
        the route reads `await request.body()` and never `request.json()`.
        """
        body = stripe_event("invoice.paid", {"id": "in_1"})
        header = stripe_signature(body, self.SECRET)
        # A parser that round-trips with different separators changes bytes.
        reserialized = json.dumps(json.loads(body), indent=2).encode()

        assert reserialized != body
        with pytest.raises(BillingAuthError):
            self.provider().verify_webhook(
                raw_body=reserialized, signature_header=header
            )

    def test_a_stale_timestamp_is_rejected(self):
        """The replay defence. The timestamp is inside the signed payload."""
        body = stripe_event("invoice.paid", {"id": "in_1"})
        header = stripe_signature(
            body, self.SECRET, timestamp=int(time.time()) - 3600
        )
        with pytest.raises(BillingAuthError) as caught:
            self.provider().verify_webhook(raw_body=body, signature_header=header)
        assert "old" in str(caught.value)

    def test_a_future_timestamp_is_rejected(self):
        body = stripe_event("invoice.paid", {"id": "in_1"})
        header = stripe_signature(
            body, self.SECRET, timestamp=int(time.time()) + 3600
        )
        with pytest.raises(BillingAuthError):
            self.provider().verify_webhook(raw_body=body, signature_header=header)

    def test_several_v1_values_are_all_checked(self):
        """
        Stripe sends several during a signing-secret rotation. Accepting only
        the first would drop valid events for the whole rotation window.
        """
        body = stripe_event("invoice.paid", {"id": "in_1"})
        valid = stripe_signature(body, self.SECRET)
        header = valid + ",v1=" + "0" * 64

        assert self.provider().verify_webhook(
            raw_body=body, signature_header=header
        ).event_type == "invoice.paid"

    def test_an_unknown_scheme_is_ignored(self):
        body = stripe_event("invoice.paid", {"id": "in_1"})
        header = "v0=deadbeef," + stripe_signature(body, self.SECRET)
        assert self.provider().verify_webhook(
            raw_body=body, signature_header=header
        )

    @pytest.mark.parametrize("header", [
        "", "garbage", "t=notanumber,v1=abc", "v1=abc", "t=123",
    ])
    def test_a_malformed_header_is_rejected(self, header):
        body = stripe_event("invoice.paid", {"id": "in_1"})
        with pytest.raises(BillingAuthError):
            self.provider().verify_webhook(raw_body=body, signature_header=header)

    def test_a_malformed_body_is_rejected_after_verification(self):
        body = b"not json at all"
        with pytest.raises(BillingValidationError):
            self.provider().verify_webhook(
                raw_body=body, signature_header=stripe_signature(body, self.SECRET)
            )

    def test_an_event_with_no_id_is_rejected(self):
        body = json.dumps({"type": "invoice.paid"}).encode()
        with pytest.raises(BillingValidationError):
            self.provider().verify_webhook(
                raw_body=body, signature_header=stripe_signature(body, self.SECRET)
            )

    def test_verification_without_a_configured_secret_refuses(self):
        provider = StripeProvider(BillingContextConfig(secret_key="sk_test_x"))
        body = stripe_event("invoice.paid", {"id": "in_1"})
        with pytest.raises(BillingConfigurationError):
            provider.verify_webhook(
                raw_body=body, signature_header=stripe_signature(body, "x")
            )


# =================================================== webhook processing (22) ===

class TestWebhookProcessing:
    @pytest.fixture(autouse=True)
    def _stripe(self, stripe_settings):
        return stripe_settings

    async def _event(self, db, body: bytes):
        provider = make_provider(BillingProviderType.STRIPE)
        event = provider.verify_webhook(
            raw_body=body,
            signature_header=stripe_signature(body, "whsec_SECRET_HOOK_VALUE"),
        )
        return await webhooks.process_event(db, event)

    async def test_subscription_updated_applies_state(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(
            db, tenant_a, "starter",
            provider=BillingProviderType.STRIPE,
            external_subscription_id="sub_FAKE123",
        )
        body = stripe_event("customer.subscription.updated", stripe_sub(
            status="past_due", metadata={"voxdesk_tenant_id": str(tenant_a.id)}
        ))

        outcome = await self._event(db, body)
        assert outcome.handled

        subscription = await service.get_subscription(db, tenant_a.id)
        assert subscription.status is SubscriptionStatus.PAST_DUE

    async def test_a_duplicate_event_is_processed_once(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(
            db, tenant_a, "starter", provider=BillingProviderType.STRIPE,
            external_subscription_id="sub_FAKE123",
        )
        body = stripe_event(
            "customer.subscription.updated",
            stripe_sub(metadata={"voxdesk_tenant_id": str(tenant_a.id)}),
            event_id="evt_SAME",
        )

        first = await self._event(db, body)
        second = await self._event(db, body)

        assert first.handled
        assert second.duplicate and not second.handled

        count = (
            await db.execute(select(func.count(BillingWebhookReceipt.id)))
        ).scalar()
        assert count == 1

    async def test_an_out_of_order_event_does_not_regress_state(
        self, db, tenant_a, billing_plans
    ):
        """
        `customer.subscription.updated` routinely arrives before
        `checkout.session.completed`, and a retried old event can land after a
        newer one.
        """
        await subscribe(
            db, tenant_a, "starter", provider=BillingProviderType.STRIPE,
            external_subscription_id="sub_FAKE123",
        )
        now = int(time.time())
        meta = {"voxdesk_tenant_id": str(tenant_a.id)}

        await self._event(db, stripe_event(
            "customer.subscription.updated",
            stripe_sub(status="active", metadata=meta),
            event_id="evt_new", created=now,
        ))
        await self._event(db, stripe_event(
            "customer.subscription.updated",
            stripe_sub(status="canceled", metadata=meta),
            event_id="evt_old", created=now - 3600,
        ))

        subscription = await service.get_subscription(db, tenant_a.id)
        assert subscription.status is SubscriptionStatus.ACTIVE

    async def test_a_stale_delete_does_not_cancel_a_reactivated_subscription(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(
            db, tenant_a, "starter", provider=BillingProviderType.STRIPE,
            external_subscription_id="sub_FAKE123",
        )
        now = int(time.time())
        meta = {"voxdesk_tenant_id": str(tenant_a.id)}

        await self._event(db, stripe_event(
            "customer.subscription.updated",
            stripe_sub(status="active", metadata=meta),
            event_id="evt_live", created=now,
        ))
        await self._event(db, stripe_event(
            "customer.subscription.deleted",
            stripe_sub(status="canceled", metadata=meta),
            event_id="evt_stale_delete", created=now - 7200,
        ))

        subscription = await service.get_subscription(db, tenant_a.id)
        assert subscription.status is SubscriptionStatus.ACTIVE

    async def test_a_payment_failure_marks_past_due_but_keeps_service(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(
            db, tenant_a, "pro", provider=BillingProviderType.STRIPE,
            external_subscription_id="sub_FAKE123",
        )
        body = stripe_event("invoice.payment_failed", {
            "id": "in_FAILED", "object": "invoice", "status": "open",
            "currency": "usd", "amount_due": 49900, "amount_paid": 0,
            "subscription": "sub_FAKE123", "customer": "cus_FAKE123",
        })
        await self._event(db, body)

        subscription = await service.get_subscription(db, tenant_a.id)
        assert subscription.status is SubscriptionStatus.PAST_DUE
        assert subscription.last_payment_failed_at is not None

        # ...and the tenant is still entitled.
        from app.billing.entitlements import load_context

        assert (await load_context(db, tenant_a)).is_entitled

    async def test_a_paid_invoice_clears_past_due(self, db, tenant_a, billing_plans):
        await subscribe(
            db, tenant_a, "pro", provider=BillingProviderType.STRIPE,
            external_subscription_id="sub_FAKE123",
            status=SubscriptionStatus.PAST_DUE,
        )
        await self._event(db, stripe_event("invoice.paid", {
            "id": "in_PAID", "object": "invoice", "status": "paid",
            "currency": "usd", "amount_due": 49900, "amount_paid": 49900,
            "subscription": "sub_FAKE123", "customer": "cus_FAKE123",
        }))

        subscription = await service.get_subscription(db, tenant_a.id)
        assert subscription.status is SubscriptionStatus.ACTIVE
        assert subscription.last_invoice_status is InvoiceStatus.PAID

    async def test_a_paid_invoice_is_mirrored(self, db, tenant_a, billing_plans):
        from app.db.models import BillingInvoice

        await subscribe(
            db, tenant_a, "pro", provider=BillingProviderType.STRIPE,
            external_subscription_id="sub_FAKE123",
        )
        await self._event(db, stripe_event("invoice.paid", {
            "id": "in_MIRROR", "object": "invoice", "status": "paid",
            "currency": "usd", "amount_due": 49900, "amount_paid": 49900,
            "subscription": "sub_FAKE123", "customer": "cus_FAKE123",
            "hosted_invoice_url": "https://invoice.test/x",
        }))

        invoice = (await db.execute(select(BillingInvoice))).scalars().one()
        assert invoice.external_invoice_id == "in_MIRROR"
        assert invoice.amount_paid_cents == 49900

    async def test_an_unattributable_event_is_recorded_and_ignored(
        self, db, billing_plans
    ):
        """
        The receipt is kept so a replay is recognised, and the reason is
        recorded so it is investigable rather than silent.
        """
        body = stripe_event("customer.subscription.updated", stripe_sub(
            customer="cus_UNKNOWN", metadata={},
        ))
        outcome = await self._event(db, body)

        assert not outcome.handled
        assert outcome.ignored_reason == "no_tenant"

        receipt = (
            await db.execute(select(BillingWebhookReceipt))
        ).scalars().one()
        assert receipt.last_error == "no matching tenant"

    async def test_an_unhandled_event_type_is_recorded_and_ignored(
        self, db, tenant_a, billing_plans
    ):
        body = stripe_event("customer.created", {"id": "cus_FAKE123"})
        outcome = await self._event(db, body)
        assert outcome.ignored_reason == "unhandled_type"

    async def test_a_forged_tenant_id_that_does_not_exist_is_ignored(
        self, db, tenant_a, billing_plans
    ):
        """
        The metadata is ours, but the event body is not. A `tenant_id` that
        does not resolve to a real tenant must not select one.
        """
        body = stripe_event("customer.subscription.updated", stripe_sub(
            customer="cus_NOBODY",
            metadata={"voxdesk_tenant_id": str(uuid.uuid4())},
        ))
        outcome = await self._event(db, body)
        assert not outcome.handled

    async def test_checkout_completion_does_not_grant_service_by_itself(
        self, db, tenant_a, billing_plans, monkeypatch
    ):
        """
        **Requirement 8 and 10.** The session says a payment page was
        completed; the subscription object says what state it is in, and those
        can differ (3DS, a failed initial charge).
        """
        async def unavailable(self, external_id):
            return None      # the follow-up read fails

        monkeypatch.setattr(StripeProvider, "get_subscription", unavailable)

        await subscribe(
            db, tenant_a, "pro", provider=BillingProviderType.STRIPE,
            external_subscription_id=None,
            status=SubscriptionStatus.INCOMPLETE,
        )
        await self._event(db, stripe_event("checkout.session.completed", {
            "id": "cs_FAKE", "object": "checkout.session",
            "subscription": "sub_NEW", "customer": "cus_FAKE123",
            "metadata": {"voxdesk_tenant_id": str(tenant_a.id)},
        }))

        subscription = await service.get_subscription(db, tenant_a.id)
        assert subscription.external_subscription_id == "sub_NEW"
        assert subscription.status is SubscriptionStatus.INCOMPLETE


# ============================================================ contract (33) ===

class TestBillingProviderContract:
    """
    Requirement 33, parameterized over the **registry** rather than a
    hand-written list, so a future provider inherits the suite the moment it
    is registered.
    """

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_every_type_has_an_adapter(self, provider):
        assert provider in PROVIDERS

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_the_name_matches_the_enum_value(self, provider):
        assert PROVIDERS[provider].name == provider.value

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_declared_capabilities_are_implemented(self, provider):
        """
        Declaring a capability you have not written routes work to you and
        produces a failure instead of a clean "unsupported".
        """
        adapter = PROVIDERS[provider]
        methods = {
            BillingCapability.CREATE_CUSTOMER: "create_customer",
            BillingCapability.GET_CUSTOMER: "get_customer",
            BillingCapability.CREATE_SUBSCRIPTION: "create_subscription",
            BillingCapability.UPDATE_SUBSCRIPTION: "update_subscription",
            BillingCapability.CANCEL_SUBSCRIPTION: "cancel_subscription",
            BillingCapability.GET_SUBSCRIPTION: "get_subscription",
            BillingCapability.LIST_SUBSCRIPTIONS: "list_subscriptions",
            BillingCapability.LIST_INVOICES: "list_invoices",
            BillingCapability.GET_INVOICE: "get_invoice",
            BillingCapability.CHECKOUT_SESSION: "create_checkout_session",
            BillingCapability.PORTAL_SESSION: "create_portal_session",
            BillingCapability.VERIFY_WEBHOOK: "verify_webhook",
            BillingCapability.HEALTH_CHECK: "health_check",
        }
        for capability, name in methods.items():
            if capability not in capabilities_of(provider):
                continue
            assert getattr(adapter, name) is not getattr(BillingProvider, name), (
                f"{provider.value} declares {capability.value} but does not "
                f"override {name}"
            )

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    async def test_undeclared_capabilities_raise_unsupported(self, provider):
        from app.billing.errors import BillingUnsupportedError

        adapter = make_provider(provider)
        if BillingCapability.CHECKOUT_SESSION not in capabilities_of(provider):
            with pytest.raises(BillingUnsupportedError):
                await adapter.create_checkout_session(
                    external_customer_id="x", price_id="y", tenant_id="z"
                )
        if BillingCapability.LIST_INVOICES not in capabilities_of(provider):
            with pytest.raises(BillingUnsupportedError):
                await adapter.list_invoices("cus_x")

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    @pytest.mark.parametrize("status,expected,retryable", [
        (401, BillingAuthError, False),
        (404, BillingNotFoundError, False),
        (429, BillingRateLimited, True),
        (500, BillingTemporaryError, True),
    ])
    async def test_error_normalization_is_identical(
        self, provider, status, expected, retryable, monkeypatch
    ):
        FakeTransport((status, {"error": "x"})).install(monkeypatch)
        adapter = make_provider(provider)

        with pytest.raises(BillingError) as caught:
            await adapter.request("POST", "https://provider.test/x")
        assert isinstance(caught.value, expected)
        assert caught.value.retryable is retryable

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    async def test_timeouts_are_normalized(self, provider, monkeypatch):
        FakeTransport(httpx.ReadTimeout("slow")).install(monkeypatch)
        with pytest.raises(BillingTimeout):
            await make_provider(provider).request("GET", "https://provider.test/x")

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    async def test_no_httpx_exception_escapes(self, provider, monkeypatch):
        for failure in (
            httpx.ConnectError("x"), httpx.ReadTimeout("x"),
            httpx.PoolTimeout("x"), httpx.RemoteProtocolError("x"),
        ):
            FakeTransport(failure).install(monkeypatch)
            try:
                await make_provider(provider).request("GET", "https://provider.test/x")
            except BillingError:
                pass
            except httpx.HTTPError as exc:
                pytest.fail(f"{provider.value} leaked {type(exc).__name__}")

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_the_config_repr_hides_secrets(self, provider):
        """A config in a traceback must not print an API key."""
        printed = repr(make_provider(provider).config)
        for secret in secrets_for(provider):
            assert secret not in printed
        assert "has_secret" in printed

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    async def test_secrets_never_appear_in_a_raised_error(self, provider, monkeypatch):
        secrets = secrets_for(provider)
        if not secrets:
            pytest.skip(f"{provider.value} holds no secrets")

        secret = secrets[0]
        FakeTransport(
            (400, {"error": {"message": f"key {secret} rejected"}, "key": secret})
        ).install(monkeypatch)

        with pytest.raises(BillingError) as caught:
            await make_provider(provider).request("POST", "https://provider.test/x")
        assert secret not in str(caught.value)
        assert secret not in caught.value.safe_message

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    async def test_a_401_body_is_never_echoed(self, provider, monkeypatch):
        FakeTransport((401, {"detail": "sk_live_LEAKED_KEY_12345 is invalid"})).install(
            monkeypatch
        )
        with pytest.raises(BillingAuthError) as caught:
            await make_provider(provider).request("GET", "https://provider.test/x")
        assert "sk_live_LEAKED_KEY_12345" not in str(caught.value)

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    async def test_a_health_check_never_raises_and_never_leaks(
        self, provider, monkeypatch
    ):
        secrets = secrets_for(provider)
        FakeTransport((401, {"key": secrets[0] if secrets else "x"})).install(
            monkeypatch
        )
        result = await make_provider(provider).health_check()

        assert result.provider == provider.value
        assert result.latency_ms >= 0
        for secret in secrets:
            assert secret not in result.safe_message

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_an_adapter_holds_no_database_handle(self, provider):
        """
        An adapter is constructed with a config and nothing else, so it cannot
        reach another tenant -- a guarantee by construction rather than by
        discipline.
        """
        adapter = make_provider(provider)
        assert set(vars(adapter)) == {"config"}

    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    async def test_customer_creation_is_deterministic_for_reconciliation(
        self, provider, monkeypatch
    ):
        """
        Every adapter must let the service answer "did my earlier attempt
        land?" — either by carrying an idempotency key or by being able to
        look the customer up.
        """
        adapter = make_provider(provider)
        can_find = (
            getattr(type(adapter), "find_customer_by_tenant")
            is not BillingProvider.find_customer_by_tenant
        )
        assert can_find, (
            f"{provider.value} cannot reconcile an ambiguous customer creation"
        )
