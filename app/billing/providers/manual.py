"""
The manual/invoiced provider.

Not a stub. It is how a tenant on an invoice-me contract, or a development
instance with no Stripe account, still gets plans, entitlements, metering,
periods and overage calculation. Everything except the payment rail works
identically, which is what lets the whole billing layer be exercised without
a vendor account — including in every test in this repository.

It declares only the capabilities it can honestly serve. There is no checkout
session and no customer portal, because there is no payment page to send a
browser to, and inventing a URL would be worse than saying so.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.billing.base import (
    BillingCapability,
    BillingProvider,
    HealthResult,
    RemoteCustomer,
    RemoteSubscription,
)
from app.billing.periods import add_months
from app.db.models import BillingInterval, SubscriptionStatus


class ManualBillingProvider(BillingProvider):
    name = "manual"
    capabilities = frozenset({
        BillingCapability.CREATE_CUSTOMER,
        BillingCapability.GET_CUSTOMER,
        BillingCapability.CREATE_SUBSCRIPTION,
        BillingCapability.UPDATE_SUBSCRIPTION,
        BillingCapability.CANCEL_SUBSCRIPTION,
        BillingCapability.GET_SUBSCRIPTION,
        BillingCapability.HEALTH_CHECK,
    })
    # Absent, and deliberately: CHECKOUT_SESSION, PORTAL_SESSION,
    # LIST_INVOICES, GET_INVOICE, VERIFY_WEBHOOK. There is no payment
    # provider behind this, so a checkout URL would be fiction.

    async def create_customer(
        self, *, tenant_id: str, email: str | None, name: str | None,
        idempotency_key: str,
    ) -> RemoteCustomer:
        # Deterministic from the tenant, so a retry produces the same id and
        # the reconciliation path behaves exactly as it does for Stripe.
        return RemoteCustomer(
            external_id=f"manual_cus_{tenant_id}", email=email, name=name
        )

    async def get_customer(self, external_id: str) -> RemoteCustomer | None:
        return RemoteCustomer(external_id=external_id)

    async def find_customer_by_tenant(self, tenant_id: str) -> RemoteCustomer | None:
        return RemoteCustomer(
            external_id=f"manual_cus_{tenant_id}", already_existed=True
        )

    async def create_subscription(
        self, *, external_customer_id: str, price_id: str, trial_days: int,
        idempotency_key: str, metadata: dict[str, str] | None = None,
    ) -> RemoteSubscription:
        now = datetime.now(timezone.utc)
        trialing = trial_days > 0
        return RemoteSubscription(
            external_id=f"manual_sub_{idempotency_key[:32]}",
            status=(
                SubscriptionStatus.TRIALING if trialing else SubscriptionStatus.ACTIVE
            ),
            external_customer_id=external_customer_id,
            external_price_id=price_id,
            interval=BillingInterval.MONTH,
            current_period_start=now,
            current_period_end=add_months(now, 1),
            trial_start=now if trialing else None,
            trial_end=add_months(now, 1) if trialing else None,
            updated_at=now,
        )

    async def update_subscription(
        self, external_id: str, *, price_id: str | None = None,
        cancel_at_period_end: bool | None = None, proration: str = "none",
        idempotency_key: str = "",
    ) -> RemoteSubscription:
        now = datetime.now(timezone.utc)
        return RemoteSubscription(
            external_id=external_id,
            status=(
                SubscriptionStatus.CANCELING if cancel_at_period_end
                else SubscriptionStatus.ACTIVE
            ),
            external_price_id=price_id,
            current_period_start=now,
            current_period_end=add_months(now, 1),
            cancel_at_period_end=bool(cancel_at_period_end),
            updated_at=now,
        )

    async def cancel_subscription(
        self, external_id: str, *, at_period_end: bool = True,
        idempotency_key: str = "",
    ) -> RemoteSubscription:
        now = datetime.now(timezone.utc)
        return RemoteSubscription(
            external_id=external_id,
            status=(
                SubscriptionStatus.CANCELING if at_period_end
                else SubscriptionStatus.CANCELED
            ),
            cancel_at_period_end=at_period_end,
            canceled_at=None if at_period_end else now,
            current_period_start=now,
            current_period_end=add_months(now, 1),
            updated_at=now,
        )

    async def get_subscription(self, external_id: str) -> RemoteSubscription | None:
        # There is no remote store. Returning None would make the service
        # think the subscription vanished and cancel the local row, so this
        # says "I have nothing to add" by echoing an active state.
        now = datetime.now(timezone.utc)
        return RemoteSubscription(
            external_id=external_id,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=now,
            current_period_end=add_months(now, 1),
            updated_at=now,
        )

    async def health_check(self) -> HealthResult:
        return HealthResult(
            connected=True, provider=self.name, latency_ms=0.0,
            safe_message="manual billing; no payment provider configured",
        )

