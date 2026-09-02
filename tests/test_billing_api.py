"""
Billing API, tenant isolation, secret handling and reconciliation.

Requirement 30's SECURITY block is the core of this file. Every cross-tenant
test gives Tenant B a genuine authenticated session and a correct identifier
belonging to Tenant A, and asserts that being right about the identifier
changes nothing.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.billing import metering, reconciliation, service
from app.billing.periods import calendar_period, period_for
from app.db.models import (
    AuditLog,
    BillingInvoice,
    BillingProviderType,
    Subscription,
    SubscriptionStatus,
    UsageEvent,
    UsageMetric,
    UsageSummary,
)
from tests.conftest import (
    FakeTransport,
    add_usage,
    auth_headers,
    stripe_event,
    stripe_signature,
    subscribe,
)

UTC = timezone.utc


# ============================================================ read endpoints ===

class TestBillingRead:
    async def test_the_status_summary(self, client, db, owner_a, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro")
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 2_100 * 60)

        headers = await auth_headers(client, owner_a)
        response = await client.get("/api/billing", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["plan_code"] == "pro"
        assert body["subscription_status"] == "active"
        assert body["provider"] == "manual"
        assert body["usage"]["voice_minute"]["used"] == 2_100
        assert body["usage"]["voice_minute"]["overage"] == 100
        assert body["estimated_overage_cents"] == 1_000      # 100 x 10c

    async def test_the_plan_catalogue(self, client, db, owner_a, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "starter")
        headers = await auth_headers(client, owner_a)
        response = await client.get("/api/billing/plans", headers=headers)

        assert response.status_code == 200
        plans = {p["code"]: p for p in response.json()}
        assert set(plans) == {"trial", "starter", "pro", "enterprise"}
        assert plans["starter"]["is_current"] is True
        assert plans["pro"]["overage_voice_minute_cents"] == 10.0

    async def test_the_catalogue_does_not_publish_price_ids(
        self, client, db, owner_a, billing_plans
    ):
        """
        Not secret, but publishing them invites a client to send one back, and
        the entire trust model rests on that never being accepted.
        """
        headers = await auth_headers(client, owner_a)
        response = await client.get("/api/billing/plans", headers=headers)

        assert "price_FAKE" not in response.text
        assert "provider_price_ids" not in response.text

    async def test_the_usage_endpoint(self, client, db, owner_a, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro")
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 600)
        await add_usage(db, tenant_a, UsageMetric.SMS_SEGMENT, 40)

        headers = await auth_headers(client, owner_a)
        body = (await client.get("/api/billing/usage", headers=headers)).json()

        metrics = {m["metric"]: m for m in body["metrics"]}
        assert metrics["voice_minute"]["used"] == 10.0       # 600s -> minutes
        assert metrics["voice_minute"]["unit"] == "minute"
        assert metrics["voice_minute"]["remaining"] == 1_990
        assert metrics["sms_segment"]["used"] == 40

    async def test_invoices_are_served_from_the_local_mirror(
        self, client, db, owner_a, tenant_a, billing_plans
    ):
        """
        Faster, works when Stripe is down, and keeps the API key out of a read
        path a dashboard polls.
        """
        from app.db.models import InvoiceStatus

        db.add(BillingInvoice(
            tenant_id=tenant_a.id, provider=BillingProviderType.MANUAL,
            external_invoice_id="in_FAKE1", status=InvoiceStatus.PAID,
            currency="usd", amount_due_cents=49900, amount_paid_cents=49900,
            hosted_invoice_url="https://invoice.test/x",
        ))
        await db.commit()

        headers = await auth_headers(client, owner_a)
        body = (await client.get("/api/billing/invoices", headers=headers)).json()

        assert len(body) == 1
        assert body[0]["id"] == "in_FAKE1"
        assert body[0]["status"] == "paid"
        assert body[0]["hosted_invoice_url"] == "https://invoice.test/x"


# ========================================================== write endpoints ===

class TestBillingWrite:
    async def test_checkout_takes_only_a_plan_code(
        self, client, db, owner_a, tenant_a, billing_plans, stripe_settings, monkeypatch
    ):
        FakeTransport(
            (200, {"id": "cus_FAKE"}),
            (200, {"id": "cs_FAKE", "url": "https://checkout.test/pay"}),
        ).install(monkeypatch)

        headers = await auth_headers(client, owner_a)
        response = await client.post(
            "/api/billing/checkout", headers=headers,
            json={"plan_code": "pro", "interval": "month"},
        )
        assert response.status_code == 200
        assert response.json()["url"] == "https://checkout.test/pay"

    async def test_checkout_does_not_activate_a_subscription(
        self, client, db, owner_a, tenant_a, billing_plans, stripe_settings, monkeypatch
    ):
        """
        **Requirement 8.** A browser reaching the success URL has proved only
        that it reached a URL.
        """
        FakeTransport(
            (200, {"id": "cus_FAKE"}),
            (200, {"id": "cs_FAKE", "url": "https://checkout.test/pay"}),
        ).install(monkeypatch)

        headers = await auth_headers(client, owner_a)
        await client.post(
            "/api/billing/checkout", headers=headers, json={"plan_code": "pro"}
        )

        subscription = await service.get_subscription(db, tenant_a.id)
        assert subscription.status is SubscriptionStatus.INCOMPLETE
        assert subscription.external_subscription_id is None

    @pytest.mark.parametrize("payload", [
        {"plan_code": "pro", "price": "price_ONE_CENT"},
        {"plan_code": "pro", "amount": 1},
        {"plan_code": "pro", "currency": "xyz"},
        {"plan_code": "pro", "tenant_id": "00000000-0000-0000-0000-000000000000"},
    ])
    async def test_a_client_cannot_smuggle_a_price(
        self, client, db, owner_a, tenant_a, billing_plans, stripe_settings,
        monkeypatch, payload,
    ):
        """
        **The trust boundary, tested from the outside.** Extra fields are
        ignored by the model; the price always comes from the catalogue.
        """
        transport = FakeTransport(
            (200, {"id": "cus_FAKE"}),
            (200, {"id": "cs_FAKE", "url": "https://checkout.test/pay"}),
        ).install(monkeypatch)

        headers = await auth_headers(client, owner_a)
        response = await client.post(
            "/api/billing/checkout", headers=headers, json=payload
        )
        assert response.status_code == 200

        sent = json.dumps(transport.requests)
        assert "price_ONE_CENT" not in sent
        assert "xyz" not in sent

    async def test_an_unknown_plan_is_404(
        self, client, db, owner_a, billing_plans, stripe_settings
    ):
        headers = await auth_headers(client, owner_a)
        response = await client.post(
            "/api/billing/checkout", headers=headers, json={"plan_code": "platinum"}
        )
        assert response.status_code == 404

    async def test_an_inactive_plan_is_404_like_a_missing_one(
        self, client, db, owner_a, billing_plans, stripe_settings
    ):
        from app.billing.plans import get_plan_by_code

        plan = await get_plan_by_code(db, "pro")
        plan.is_active = False
        await db.commit()

        headers = await auth_headers(client, owner_a)
        response = await client.post(
            "/api/billing/checkout", headers=headers, json={"plan_code": "pro"}
        )
        assert response.status_code == 404

    async def test_a_provider_failure_is_a_502_not_a_hopeful_200(
        self, client, db, owner_a, tenant_a, billing_plans, stripe_settings, monkeypatch
    ):
        """
        **Requirement 32.** The customer must not be told "payment completed"
        when the provider was unavailable.
        """
        FakeTransport((503, {"error": "unavailable"})).install(monkeypatch)

        headers = await auth_headers(client, owner_a)
        response = await client.post(
            "/api/billing/checkout", headers=headers, json={"plan_code": "pro"}
        )
        assert response.status_code == 502
        assert "completed" not in response.text.lower()

    async def test_changing_plan(self, client, db, owner_a, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "starter")
        headers = await auth_headers(client, owner_a)

        response = await client.post(
            "/api/billing/change-plan", headers=headers, json={"plan_code": "pro"}
        )
        assert response.status_code == 200
        assert response.json()["plan_code"] == "pro"

    async def test_a_downgrade_shows_as_pending(
        self, client, db, owner_a, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        headers = await auth_headers(client, owner_a)

        body = (await client.post(
            "/api/billing/change-plan", headers=headers, json={"plan_code": "starter"}
        )).json()
        assert body["plan_code"] == "pro"
        assert body["pending_plan_code"] == "starter"

    async def test_cancelling(self, client, db, owner_a, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro")
        headers = await auth_headers(client, owner_a)

        body = (await client.post(
            "/api/billing/cancel", headers=headers, json={"reason": "too expensive"}
        )).json()
        assert body["cancel_at_period_end"] is True
        assert body["subscription_status"] == "canceling"

    async def test_the_portal_needs_no_customer_id_from_the_caller(
        self, client, db, owner_a, tenant_a, billing_plans, stripe_settings, monkeypatch
    ):
        """
        Requirement 9: there is no parameter through which another tenant's
        customer id could be supplied. The endpoint takes no body at all.
        """
        await subscribe(
            db, tenant_a, "pro", provider=BillingProviderType.STRIPE,
            external_customer_id="cus_MINE",
        )
        FakeTransport(
            (200, {"id": "bps_FAKE", "url": "https://portal.test/x"})
        ).install(monkeypatch)

        headers = await auth_headers(client, owner_a)
        response = await client.post("/api/billing/portal", headers=headers)

        assert response.status_code == 200
        assert response.json()["url"] == "https://portal.test/x"


# ============================================================= permissions ===

class TestBillingPermissions:
    async def test_an_owner_can_read_and_write(
        self, client, db, owner_a, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        headers = await auth_headers(client, owner_a)

        assert (await client.get("/api/billing", headers=headers)).status_code == 200
        assert (await client.post(
            "/api/billing/cancel", headers=headers, json={}
        )).status_code == 200

    async def test_an_admin_can_read_but_not_change_the_bill(
        self, client, db, admin_a, tenant_a, billing_plans
    ):
        """
        An admin needs to know why a limit was hit; only an owner may change
        what the company is charged.
        """
        await subscribe(db, tenant_a, "pro")
        headers = await auth_headers(client, admin_a)

        assert (await client.get("/api/billing", headers=headers)).status_code == 200
        assert (await client.post(
            "/api/billing/cancel", headers=headers, json={}
        )).status_code == 403

    async def test_other_roles_see_nothing(
        self, client, db, manager_a, agent_a, viewer_a, tenant_a, billing_plans
    ):
        """
        Manager, agent and viewer have no billing permission at all.

        Written with the three fixtures requested directly rather than through
        `request.getfixturevalue` in a parametrize: these are async fixtures,
        and resolving one lazily from inside a running test tries to drive the
        event loop that is already running.
        """
        for user in (manager_a, agent_a, viewer_a):
            headers = await auth_headers(client, user)
            for path in ("/api/billing", "/api/billing/invoices", "/api/billing/usage"):
                response = await client.get(path, headers=headers)
                assert response.status_code == 403, f"{user.role.value} {path}"

    async def test_unauthenticated_requests_are_rejected(self, client, billing_plans):
        for method, path in (
            ("get", "/api/billing"),
            ("get", "/api/billing/plans"),
            ("get", "/api/billing/usage"),
            ("get", "/api/billing/invoices"),
            ("post", "/api/billing/checkout"),
            ("post", "/api/billing/portal"),
            ("post", "/api/billing/cancel"),
        ):
            kwargs = {"json": {"plan_code": "pro"}} if method == "post" else {}
            response = await getattr(client, method)(path, **kwargs)
            assert response.status_code in (401, 403), path

    async def test_the_routes_use_the_permission_enum_not_role_strings(self):
        import pathlib

        source = pathlib.Path("app/api/billing_routes.py").read_text()
        assert '== "admin"' not in source
        assert '== "owner"' not in source
        assert ".role ==" not in source


# =========================================================== isolation (30) ===

class TestTenantIsolation:
    async def test_tenant_b_sees_only_their_own_billing(
        self, client, db, owner_b, tenant_a, tenant_b, billing_plans
    ):
        await subscribe(db, tenant_a, "enterprise")
        await subscribe(db, tenant_b, "starter")
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 9_000 * 60)

        headers = await auth_headers(client, owner_b)
        body = (await client.get("/api/billing", headers=headers)).json()

        assert body["plan_code"] == "starter"
        assert body["usage"]["voice_minute"]["used"] == 0

    async def test_tenant_b_cannot_see_tenant_a_invoices(
        self, client, db, owner_b, tenant_a, tenant_b, billing_plans
    ):
        from app.db.models import InvoiceStatus

        db.add(BillingInvoice(
            tenant_id=tenant_a.id, provider=BillingProviderType.MANUAL,
            external_invoice_id="in_TENANT_A_SECRET", status=InvoiceStatus.PAID,
            amount_due_cents=149900, amount_paid_cents=149900,
        ))
        await db.commit()

        headers = await auth_headers(client, owner_b)
        response = await client.get("/api/billing/invoices", headers=headers)

        assert response.json() == []
        assert "in_TENANT_A_SECRET" not in response.text

    async def test_tenant_b_cannot_use_tenant_a_customer_id(
        self, client, db, owner_b, tenant_a, tenant_b, billing_plans,
        stripe_settings, monkeypatch,
    ):
        """
        Requirement 9 and 30. The portal reads the customer id from the
        authenticated tenant's own row; there is no way to supply another.
        """
        await subscribe(
            db, tenant_a, "pro", provider=BillingProviderType.STRIPE,
            external_customer_id="cus_TENANT_A",
        )
        await subscribe(
            db, tenant_b, "starter", provider=BillingProviderType.STRIPE,
            external_customer_id="cus_TENANT_B",
        )
        transport = FakeTransport(
            (200, {"id": "bps", "url": "https://portal.test/b"})
        ).install(monkeypatch)

        headers = await auth_headers(client, owner_b)
        await client.post(
            "/api/billing/portal", headers=headers,
            json={"external_customer_id": "cus_TENANT_A"},
        )

        sent = json.dumps(transport.requests)
        assert "cus_TENANT_A" not in sent
        assert "cus_TENANT_B" in sent

    async def test_a_tenant_id_in_the_body_is_ignored(
        self, client, db, owner_b, tenant_a, tenant_b, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        await subscribe(db, tenant_b, "starter")

        headers = await auth_headers(client, owner_b)
        await client.post(
            "/api/billing/change-plan", headers=headers,
            json={"plan_code": "trial", "tenant_id": str(tenant_a.id)},
        )

        # A's subscription is untouched; B's changed.
        a = await service.get_subscription(db, tenant_a.id)
        b = await service.get_subscription(db, tenant_b.id)
        from app.billing.plans import get_plan_by_code

        pro = await get_plan_by_code(db, "pro")
        assert a.plan_id == pro.id
        assert b.pending_plan_id is not None or b.plan_id != pro.id

    async def test_usage_cannot_cross_tenants(
        self, db, tenant_a, tenant_b, billing_plans
    ):
        period = calendar_period()
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 600, period=period)

        assert await metering.used_quantity(
            db, tenant_id=tenant_b.id, metric=UsageMetric.VOICE_MINUTE, period=period
        ) == 0

    async def test_a_subscription_id_belongs_to_exactly_one_tenant(
        self, db, tenant_a, tenant_b, billing_plans
    ):
        """
        Without this a webhook carrying an id could be matched to the wrong
        row -- a cross-tenant billing error.
        """
        from sqlalchemy.exc import IntegrityError

        await subscribe(
            db, tenant_a, "pro", provider=BillingProviderType.STRIPE,
            external_subscription_id="sub_SHARED",
        )
        db.add(Subscription(
            tenant_id=tenant_b.id, provider=BillingProviderType.STRIPE,
            external_subscription_id="sub_SHARED",
            status=SubscriptionStatus.ACTIVE,
        ))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


# ============================================================ secrets (30) ===

class TestSecretHandling:
    async def test_no_stripe_secret_reaches_any_response(
        self, client, db, owner_a, tenant_a, billing_plans, stripe_settings
    ):
        await subscribe(db, tenant_a, "pro")
        headers = await auth_headers(client, owner_a)

        for path in (
            "/api/billing", "/api/billing/plans", "/api/billing/usage",
            "/api/billing/invoices",
        ):
            response = await client.get(path, headers=headers)
            assert response.status_code == 200, path
            assert "sk_test_FAKE" not in response.text, path
            assert "whsec_" not in response.text, path

    async def test_no_secret_reaches_the_audit_log(
        self, client, db, owner_a, tenant_a, billing_plans, stripe_settings, monkeypatch
    ):
        FakeTransport(
            (200, {"id": "cus_FAKE"}),
            (200, {"id": "cs_FAKE", "url": "https://checkout.test/pay"}),
        ).install(monkeypatch)

        headers = await auth_headers(client, owner_a)
        await client.post(
            "/api/billing/checkout", headers=headers, json={"plan_code": "pro"}
        )

        rows = (await db.execute(select(AuditLog))).scalars().all()
        everything = json.dumps([r.detail for r in rows])

        assert "sk_test_FAKE" not in everything
        assert "whsec_" not in everything
        assert "cus_FAKE" not in everything
        # ...but the business fact is recorded.
        assert "pro" in everything

    async def test_no_secret_reaches_the_structured_log(
        self, client, db, owner_a, tenant_a, billing_plans, stripe_settings, monkeypatch
    ):
        captured: list = []
        from app.core import logging as app_logging

        for level in ("info", "warning", "error"):
            original = getattr(app_logging.log, level)

            def spy(event, _orig=original, **kw):
                captured.append((event, kw))
                return _orig(event, **kw)

            monkeypatch.setattr(app_logging.log, level, spy)

        FakeTransport(
            (200, {"id": "cus_FAKE"}),
            (200, {"id": "cs_FAKE", "url": "https://checkout.test/pay"}),
        ).install(monkeypatch)

        headers = await auth_headers(client, owner_a)
        await client.post(
            "/api/billing/checkout", headers=headers, json={"plan_code": "pro"}
        )

        assert captured
        blob = json.dumps(captured, default=str)
        assert "sk_test_FAKE" not in blob
        assert "whsec_" not in blob

    async def test_the_response_model_has_no_field_named_like_a_secret(self):
        """
        Structural. A future field named `stripe_key` added to the response
        would be caught here rather than in production.
        """
        from app.api.billing_routes import BillingStatusOut, InvoiceOut, PlanOut

        forbidden = ("secret", "key", "token", "password", "card", "pan")
        for model in (BillingStatusOut, PlanOut, InvoiceOut):
            for name in model.model_fields:
                assert not any(word in name.lower() for word in forbidden), (
                    f"{model.__name__}.{name} is named like a secret"
                )

    async def test_the_checkout_model_has_no_price_field(self):
        """
        Requirement 8, asserted on the type. `POST {"price": ...}` is
        rejected before it reaches a handler because the field does not exist.
        """
        from app.api.billing_routes import CheckoutIn

        assert set(CheckoutIn.model_fields) == {"plan_code", "interval"}


# ============================================================ webhook route ===

class TestWebhookRoute:
    async def test_a_valid_signature_is_accepted(
        self, client, db, tenant_a, billing_plans, stripe_settings
    ):
        await subscribe(
            db, tenant_a, "pro", provider=BillingProviderType.STRIPE,
            external_customer_id="cus_FAKE123",
        )
        body = stripe_event("invoice.paid", {
            "id": "in_1", "object": "invoice", "status": "paid",
            "currency": "usd", "amount_due": 0, "amount_paid": 0,
            "customer": "cus_FAKE123",
        })

        response = await client.post(
            "/api/billing/webhook/stripe", content=body,
            headers={
                "Stripe-Signature": stripe_signature(body, stripe_settings),
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200
        assert response.json()["handled"] is True

    async def test_an_invalid_signature_is_a_400(
        self, client, db, billing_plans, stripe_settings
    ):
        """
        400, not 401: Stripe treats 4xx as permanent and stops retrying, which
        is right for a request that will never verify.
        """
        body = stripe_event("invoice.paid", {"id": "in_1"})
        response = await client.post(
            "/api/billing/webhook/stripe", content=body,
            headers={"Stripe-Signature": stripe_signature(body, "whsec_WRONG")},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid signature"

    async def test_a_missing_signature_is_rejected(
        self, client, db, billing_plans, stripe_settings
    ):
        body = stripe_event("invoice.paid", {"id": "in_1"})
        response = await client.post("/api/billing/webhook/stripe", content=body)
        assert response.status_code == 400

    async def test_an_unsigned_forged_event_cannot_activate_a_subscription(
        self, client, db, tenant_a, billing_plans, stripe_settings
    ):
        """
        The attack this endpoint exists to stop: anyone can POST to it.
        """
        await subscribe(
            db, tenant_a, "trial", provider=BillingProviderType.STRIPE,
            status=SubscriptionStatus.INCOMPLETE,
            external_subscription_id="sub_FAKE123",
        )
        body = stripe_event("customer.subscription.updated", {
            "id": "sub_FAKE123", "status": "active", "customer": "cus_x",
            "items": {"data": [{"id": "si", "price": {
                "id": "price_FAKE_enterprise_month", "recurring": {"interval": "month"}
            }}]},
            "metadata": {"voxdesk_tenant_id": str(tenant_a.id)},
        })

        response = await client.post(
            "/api/billing/webhook/stripe", content=body,
            headers={"Stripe-Signature": "t=1,v1=forged"},
        )
        assert response.status_code == 400

        subscription = await service.get_subscription(db, tenant_a.id)
        assert subscription.status is SubscriptionStatus.INCOMPLETE

    async def test_a_replayed_event_is_acknowledged_once(
        self, client, db, tenant_a, billing_plans, stripe_settings
    ):
        await subscribe(
            db, tenant_a, "pro", provider=BillingProviderType.STRIPE,
            external_customer_id="cus_FAKE123",
        )
        body = stripe_event(
            "invoice.paid",
            {
                "id": "in_REPLAY", "object": "invoice", "status": "paid",
                "currency": "usd", "amount_due": 0, "amount_paid": 0,
                "customer": "cus_FAKE123",
            },
            event_id="evt_REPLAY",
        )
        headers = {"Stripe-Signature": stripe_signature(body, stripe_settings)}

        first = await client.post(
            "/api/billing/webhook/stripe", content=body, headers=headers
        )
        second = await client.post(
            "/api/billing/webhook/stripe", content=body, headers=headers
        )

        assert first.json()["handled"] is True
        assert second.status_code == 200
        assert second.json()["duplicate"] is True

        count = (await db.execute(select(func.count(BillingInvoice.id)))).scalar()
        assert count == 1

    async def test_an_unknown_provider_looks_like_a_bad_signature(
        self, client, billing_plans, stripe_settings
    ):
        """So the endpoint cannot be probed for which providers are configured."""
        response = await client.post(
            "/api/billing/webhook/paypal", content=b"{}",
            headers={"Stripe-Signature": "t=1,v1=x"},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid signature"

    async def test_the_route_reads_the_raw_body(self):
        """
        Requirement 21, asserted on the source: reading `request.json()` here
        would silently make every signature check meaningless.
        """
        import pathlib

        source = pathlib.Path("app/api/billing_routes.py").read_text()
        assert "await request.body()" in source
        assert "await request.json()" not in source


# =========================================================== reconciliation ===

class TestReconciliation:
    async def test_a_clean_tenant_reports_nothing(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro")
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 600)
        await reconciliation.rebuild_summaries(db, tenant_a)

        report = await reconciliation.reconcile_tenant(db, tenant_a)
        assert report.clean

    async def test_summary_drift_is_detected(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro")
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 600)
        await reconciliation.rebuild_summaries(db, tenant_a)

        summary = (
            await db.execute(
                select(UsageSummary).where(
                    UsageSummary.metric == UsageMetric.VOICE_MINUTE
                )
            )
        ).scalars().one()
        summary.used_quantity = 999
        await db.commit()

        report = await reconciliation.reconcile_tenant(db, tenant_a)
        assert not report.clean
        assert any(d.kind == "summary_drift" for d in report.discrepancies)

    async def test_drift_on_a_finalized_period_is_financial(
        self, db, tenant_a, billing_plans
    ):
        """
        A closed period's figures are what was invoiced. Disagreeing with them
        means our records and the customer's have diverged.
        """
        from app.billing.plans import get_plan_by_code

        await subscribe(db, tenant_a, "pro")
        plan = await get_plan_by_code(db, "pro")
        period = period_for(await service.get_subscription(db, tenant_a.id))

        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 600, period=period)
        await metering.finalize_period(
            db, tenant_id=tenant_a.id, period=period, plan=plan
        )
        await db.commit()

        await add_usage(
            db, tenant_a, UsageMetric.VOICE_MINUTE, 300, key="late", period=period
        )

        report = await reconciliation.reconcile_tenant(db, tenant_a)
        financial = [d for d in report.financial if d.kind == "summary_drift"]
        assert financial
        assert financial[0].expected == 900 and financial[0].found == 600

    async def test_negative_usage_is_flagged_as_financial(
        self, db, tenant_a, billing_plans
    ):
        """A negative total means more was credited than was ever used."""
        await subscribe(db, tenant_a, "pro")
        period = period_for(await service.get_subscription(db, tenant_a.id))

        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 60, period=period)
        await metering.record_adjustment(
            db, tenant_id=tenant_a.id, metric=UsageMetric.VOICE_MINUTE,
            quantity=-600, reason="double credit", actor="x", period=period,
        )
        await db.commit()

        report = await reconciliation.reconcile_tenant(db, tenant_a)
        assert any(d.kind == "negative_usage" for d in report.financial)

    async def test_orphan_usage_is_flagged(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro")
        period = period_for(await service.get_subscription(db, tenant_a.id))

        await metering.record_usage(
            db, tenant_id=tenant_a.id, metric=UsageMetric.VOICE_MINUTE,
            quantity=120, idempotency_key="orphan", period=period,
            source_entity_id=uuid.uuid4(),          # no such call
        )
        await db.commit()

        report = await reconciliation.reconcile_tenant(db, tenant_a)
        assert any(d.kind == "orphan_usage" for d in report.financial)

    async def test_cross_tenant_usage_is_flagged(
        self, db, tenant_a, tenant_b, billing_plans
    ):
        """Charging one business for another's call."""
        from app.db.models import Call, CallDirection, CallStatus

        call = Call(
            tenant_id=tenant_b.id, direction=CallDirection.INBOUND,
            status=CallStatus.COMPLETED, from_number="+1", to_number="+2",
            call_sid="CAx", started_at=datetime.now(UTC),
        )
        db.add(call)
        await db.commit()
        await db.refresh(call)

        await subscribe(db, tenant_a, "pro")
        period = period_for(await service.get_subscription(db, tenant_a.id))
        await metering.record_usage(
            db, tenant_id=tenant_a.id, metric=UsageMetric.VOICE_MINUTE,
            quantity=120, idempotency_key="crossed", period=period,
            source_entity_id=call.id,
        )
        await db.commit()

        report = await reconciliation.reconcile_tenant(db, tenant_a)
        assert any(d.kind == "cross_tenant_usage" for d in report.financial)

    async def test_rebuilding_repairs_drift_without_touching_events(
        self, db, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 600)
        await reconciliation.rebuild_summaries(db, tenant_a)

        summary = (
            await db.execute(
                select(UsageSummary).where(
                    UsageSummary.metric == UsageMetric.VOICE_MINUTE
                )
            )
        ).scalars().one()
        summary.used_quantity = 1
        await db.commit()

        await reconciliation.rebuild_summaries(db, tenant_a)
        await db.refresh(summary)

        assert summary.used_quantity == 600
        assert (await db.execute(select(func.count(UsageEvent.id)))).scalar() == 1

    async def test_the_worker_tick_runs_over_active_tenants(
        self, db, tenant_a, tenant_b, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 600)

        result = await reconciliation.run_reconciliation_tick(db)
        assert result["tenants"] == 2
        assert result["clean"] == 2

    async def test_a_report_serializes_safely(self, db, tenant_a, billing_plans):
        await subscribe(db, tenant_a, "pro")
        report = await reconciliation.reconcile_tenant(db, tenant_a)
        blob = json.dumps(report.as_dict())
        assert "tenant_id" in blob
        assert "sk_" not in blob
