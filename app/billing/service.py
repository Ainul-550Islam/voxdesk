"""
Billing orchestration.

The only module that holds a tenant, a plan, a subscription, a provider
adapter and the database at the same time. Routes call this; nothing calls a
provider adapter directly.

Four rules, each of which the brief states and the audit found violated:

**A subscription is never ACTIVE because we asked for it.** Only a verified
webhook or a direct provider read may set state. `create_checkout_session`
returns a URL and writes nothing but `INCOMPLETE`.

**A price never comes from a request.** The client names a plan code; the
price id is resolved from the catalogue. `POST {"price": "price_one_cent"}`
is inexpressible.

**An ambiguous timeout is reconciled, never blind-retried.** A timeout on
`create_customer` or `create_subscription` means the provider may have
succeeded, so we ask before acting.

**Upgrades are immediate, downgrades are scheduled.** Requirement 18 asks for
an unambiguous policy and forbids ambiguous proration. Ours is stated in
`change_plan` and implemented there only.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import metering
from app.billing import plans as plan_catalogue
from app.billing.base import (
    BillingCapability,
    BillingProvider,
    RemoteInvoice,
    RemoteSubscription,
)
from app.billing.errors import (
    BillingConfigurationError,
    BillingError,
    BillingTimeout,
    BillingUnsupportedError,
    safe_message,
)
from app.billing.periods import now_utc
from app.billing.registry import build, config_from_settings, configured_provider
from app.core.logging import log
from app.db.models import (
    BillingInterval,
    BillingInvoice,
    BillingPlan,
    BillingProviderType,
    Subscription,
    SubscriptionStatus,
    Tenant,
)


@dataclass(frozen=True)
class CheckoutResult:
    url: str
    session_id: str
    plan_code: str


def adapter() -> BillingProvider:
    """A fresh provider instance. Never cached — it holds the secret key."""
    return build(configured_provider(), config_from_settings())


# ------------------------------------------------------------------ lookups ---

async def get_subscription(
    session: AsyncSession, tenant_id: uuid.UUID
) -> Subscription | None:
    """
    The tenant's subscription.

    Scoped by `tenant_id` only, because `UNIQUE (tenant_id, provider)` plus a
    single configured provider makes that a key. Never looked up by external
    id from a request — an id is not authorization.
    """
    return (
        await session.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()


async def get_plan(session: AsyncSession, subscription: Subscription | None):
    if subscription is None or subscription.plan_id is None:
        return None
    return await session.get(BillingPlan, subscription.plan_id)


async def _ensure_row(
    session: AsyncSession, tenant: Tenant, provider: BillingProviderType
) -> Subscription:
    """
    The local subscription row, created if absent.

    Uses a SAVEPOINT so losing a race to a concurrent request undoes only this
    insert, not the caller's transaction — and then re-reads, because the
    winner's row is the one both requests should use.
    """
    existing = await get_subscription(session, tenant.id)
    if existing is not None:
        return existing

    row = Subscription(
        tenant_id=tenant.id, provider=provider,
        status=SubscriptionStatus.INCOMPLETE,
    )
    try:
        # `add` goes *inside* the savepoint. Outside, the object stays pending
        # after the rollback and poisons the next statement with
        # `PendingRollbackError`, so the loser of the race cannot even read
        # back who won. Same trap as `metering.record_usage`.
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # A concurrent checkout created it first. Requirement 31: no
        # uncontrolled duplicate.
        found = await get_subscription(session, tenant.id)
        if found is None:      # pragma: no cover - the constraint guarantees one
            raise
        return found
    return row


# ----------------------------------------------------------------- customer ---

async def ensure_customer(
    session: AsyncSession, tenant: Tenant
) -> tuple[Subscription, str]:
    """
    Exactly one billing customer per tenant. Idempotent.

    Requirement 7. The order matters:

    1. Already linked locally → return it. No provider call at all.
    2. Ask the provider whether a customer for this tenant already exists.
       Covers the case where a previous attempt created one and then failed
       before we stored the id — otherwise every retry makes another.
    3. Only then create.

    On a **timeout** during creation the provider is asked again rather than
    retried, because a timeout means it may well have succeeded.
    """
    provider_type = configured_provider()
    subscription = await _ensure_row(session, tenant, provider_type)

    if subscription.external_customer_id:
        return subscription, subscription.external_customer_id

    client = adapter()
    tenant_id = str(tenant.id)

    found = await client.find_customer_by_tenant(tenant_id)
    if found is not None:
        subscription.external_customer_id = found.external_id
        log.info(
            "billing.customer_reused", tenant_id=tenant_id,
            provider=provider_type.value,
        )
        return subscription, found.external_id

    # Derived from the tenant, so a retry of *this* creation is deduplicated
    # by the provider itself. A random key per attempt would defeat that.
    key = f"customer:{tenant.id}"

    try:
        created = await client.create_customer(
            tenant_id=tenant_id, email=None, name=tenant.name, idempotency_key=key,
        )
    except BillingTimeout:
        log.warning("billing.customer_create_timeout", tenant_id=tenant_id)
        recovered = await client.find_customer_by_tenant(tenant_id)
        if recovered is None:
            # "I could not check" is not evidence of absence. Creating anyway
            # is how a tenant ends up with two customer records.
            raise
        created = recovered

    subscription.external_customer_id = created.external_id
    log.info(
        "billing.customer_created", tenant_id=tenant_id,
        provider=provider_type.value,
    )
    return subscription, created.external_id


# ----------------------------------------------------------------- checkout ---

async def start_checkout(
    session: AsyncSession,
    tenant: Tenant,
    *,
    plan_code: str,
    interval: BillingInterval,
) -> CheckoutResult:
    """
    Begin a subscription purchase.

    The client supplies `plan_code` and `interval` and nothing else — no
    amount, no currency, no price id (requirement 8). `resolve_price_id` is
    the trust boundary, and an inactive plan raises the same error as a
    nonexistent one so the endpoint cannot be used to enumerate them.

    **Creating a session establishes nothing.** The local row stays
    INCOMPLETE until a verified webhook says otherwise. A browser that reaches
    the success URL has proved only that it reached a URL.
    """
    plan = await plan_catalogue.require_plan(session, plan_code)
    price_id = plan_catalogue.resolve_price_id(plan, interval)

    client = adapter()
    if not client.supports(BillingCapability.CHECKOUT_SESSION):
        raise BillingUnsupportedError(
            f"{client.name} has no hosted checkout", provider=client.name
        )

    subscription, customer_id = await ensure_customer(session, tenant)

    session_result = await client.create_checkout_session(
        external_customer_id=customer_id,
        price_id=price_id,
        tenant_id=str(tenant.id),
        trial_days=plan.trial_days,
        idempotency_key=f"checkout:{tenant.id}:{plan.code}:{interval.value}",
    )

    # Recorded so the webhook can match the intent, and so a support question
    # about "which plan were they buying" is answerable. Emphatically NOT a
    # grant of service.
    subscription.pending_plan_id = plan.id
    subscription.pending_interval = interval
    subscription.updated_at = now_utc()
    await session.commit()

    log.info(
        "billing.checkout_started", tenant_id=str(tenant.id), plan=plan.code,
        interval=interval.value, provider=client.name,
    )
    return CheckoutResult(
        url=session_result.url, session_id=session_result.session_id,
        plan_code=plan.code,
    )


async def start_portal(session: AsyncSession, tenant: Tenant) -> str:
    """
    A customer portal session for **the authenticated tenant's** customer.

    Requirement 9: the external customer id is read from the tenant's own row,
    never accepted from the caller. There is no parameter through which
    another tenant's id could be supplied.
    """
    client = adapter()
    if not client.supports(BillingCapability.PORTAL_SESSION):
        raise BillingUnsupportedError(
            f"{client.name} has no customer portal", provider=client.name
        )

    subscription = await get_subscription(session, tenant.id)
    if subscription is None or not subscription.external_customer_id:
        raise BillingConfigurationError(
            "this account has no billing customer yet"
        )

    result = await client.create_portal_session(
        external_customer_id=subscription.external_customer_id
    )
    log.info("billing.portal_opened", tenant_id=str(tenant.id))
    return result.url


# ------------------------------------------------------------- plan changes ---

async def change_plan(
    session: AsyncSession,
    tenant: Tenant,
    *,
    plan_code: str,
    interval: BillingInterval | None = None,
) -> Subscription:
    """
    Move to another plan.

    **The policy, stated once (requirement 18):**

    * **Upgrade** (higher monthly price) — takes effect **immediately**, with
      `proration_behavior=create_prorations`. The customer gets the larger
      allowance now and Stripe issues a prorated line for the difference.
      Making someone wait for a renewal after they have asked to pay more is
      the wrong side to err on.
    * **Downgrade** (same or lower price) — **scheduled for the period end**,
      recorded in `pending_plan_id`, with no proration and no refund. Applying
      it immediately would strand a customer who has already used more than
      the smaller plan includes, and refunding mid-period is an accounting
      system this step deliberately does not build.

    A downgrade is reversible before it lands: asking for the current plan
    again clears the pending change.
    """
    target = await plan_catalogue.require_plan(session, plan_code)
    subscription = await get_subscription(session, tenant.id)
    if subscription is None:
        raise BillingConfigurationError("this account has no subscription")

    current = await get_plan(session, subscription)
    target_interval = interval or subscription.interval
    # Validate the price exists before touching anything, so a misconfigured
    # plan fails before the provider call rather than half-way through it.
    price_id = plan_catalogue.resolve_price_id(target, target_interval)

    if current is not None and current.id == target.id and (
        target_interval == subscription.interval
    ):
        # Same plan: cancel any pending downgrade. This is how a customer
        # changes their mind.
        if subscription.pending_plan_id is not None:
            subscription.pending_plan_id = None
            subscription.pending_interval = None
            await session.commit()
            log.info(
                "billing.downgrade_cancelled", tenant_id=str(tenant.id),
                plan=target.code,
            )
        return subscription

    upgrading = plan_catalogue.is_upgrade(current, target)

    if not upgrading:
        subscription.pending_plan_id = target.id
        subscription.pending_interval = target_interval
        subscription.updated_at = now_utc()
        await session.commit()
        log.info(
            "billing.downgrade_scheduled", tenant_id=str(tenant.id),
            from_plan=current.code if current else None, to_plan=target.code,
            effective=(
                subscription.current_period_end.isoformat()
                if subscription.current_period_end else "period_end"
            ),
        )
        return subscription

    if not subscription.external_subscription_id:
        # Nothing to upgrade at the provider yet. Record the intent; checkout
        # will pick it up.
        subscription.pending_plan_id = target.id
        subscription.pending_interval = target_interval
        await session.commit()
        return subscription

    client = adapter()
    remote = await client.update_subscription(
        subscription.external_subscription_id,
        price_id=price_id,
        proration="create_prorations",
        idempotency_key=(
            f"upgrade:{tenant.id}:{target.code}:{target_interval.value}"
        ),
    )
    _apply_remote(subscription, remote, plan=target, interval=target_interval)
    subscription.pending_plan_id = None
    subscription.pending_interval = None
    await session.commit()

    log.info(
        "billing.plan_upgraded", tenant_id=str(tenant.id),
        from_plan=current.code if current else None, to_plan=target.code,
    )
    return subscription


async def cancel(
    session: AsyncSession, tenant: Tenant, *, immediately: bool = False
) -> Subscription:
    """
    Cancel. Idempotent, and at period end by default.

    Defaulting to period end is the honest choice: the customer has paid for
    the month and cutting them off the moment they click cancel takes money
    for service not delivered.
    """
    subscription = await get_subscription(session, tenant.id)
    if subscription is None:
        raise BillingConfigurationError("this account has no subscription")

    if subscription.status is SubscriptionStatus.CANCELED:
        return subscription
    if subscription.cancel_at_period_end and not immediately:
        # Already winding down. A second click must not hit the provider
        # again -- Stripe accepts it, but the audit trail then shows two
        # cancellations for one decision.
        return subscription

    if subscription.external_subscription_id:
        client = adapter()
        try:
            remote = await client.cancel_subscription(
                subscription.external_subscription_id,
                at_period_end=not immediately,
                idempotency_key=f"cancel:{tenant.id}:{immediately}",
            )
            _apply_remote(subscription, remote)
        except BillingError as exc:
            # The local intent is still recorded. The customer asked to
            # cancel; refusing to remember that because a vendor API was slow
            # would mean they ask again and get charged again.
            log.warning(
                "billing.cancel_provider_failed", tenant_id=str(tenant.id),
                error_code=exc.code, error=exc.safe_message,
            )
            subscription.last_error = safe_message(str(exc))[:500]
    if immediately:
        subscription.status = SubscriptionStatus.CANCELED
        subscription.canceled_at = now_utc()
    else:
        subscription.cancel_at_period_end = True
        if subscription.status is SubscriptionStatus.ACTIVE:
            subscription.status = SubscriptionStatus.CANCELING

    subscription.updated_at = now_utc()
    await session.commit()

    log.info(
        "billing.cancellation_requested", tenant_id=str(tenant.id),
        immediately=immediately, status=subscription.status.value,
    )
    return subscription


# --------------------------------------------------------- state application ---

def _apply_remote(
    subscription: Subscription,
    remote: RemoteSubscription,
    *,
    plan: BillingPlan | None = None,
    interval: BillingInterval | None = None,
    event_time: datetime | None = None,
) -> bool:
    """
    Apply provider state to the local row, respecting ordering.

    Requirement 23. Provider webhooks arrive out of order routinely:
    `customer.subscription.updated` can land before
    `checkout.session.completed`, and a retry of an old event can arrive after
    a newer one. Applying a stale event would downgrade a customer who just
    upgraded, or resurrect a cancelled subscription.

    So every write compares timestamps first and returns `False` when the
    incoming state is older than what we already applied. That makes the final
    state deterministic regardless of delivery order.
    """
    stamp = event_time or remote.updated_at
    if (
        stamp is not None
        and subscription.provider_updated_at is not None
        and _aware(stamp) < _aware(subscription.provider_updated_at)
    ):
        log.info(
            "billing.stale_event_ignored",
            tenant_id=str(subscription.tenant_id),
            incoming=stamp.isoformat(),
            current=subscription.provider_updated_at.isoformat(),
        )
        return False

    subscription.status = remote.status
    # `RemoteSubscription.external_id` is the provider's subscription id; the
    # local column spells it out. Reading `remote.external_subscription_id`
    # here was an AttributeError on every state application.
    if remote.external_id:
        subscription.external_subscription_id = remote.external_id
    if remote.external_customer_id:
        subscription.external_customer_id = remote.external_customer_id
    if remote.external_price_id:
        subscription.external_price_id = remote.external_price_id
    if remote.current_period_start:
        subscription.current_period_start = remote.current_period_start
    if remote.current_period_end:
        subscription.current_period_end = remote.current_period_end
    if remote.trial_start:
        subscription.trial_start = remote.trial_start
    if remote.trial_end:
        subscription.trial_end = remote.trial_end

    subscription.cancel_at_period_end = remote.cancel_at_period_end
    if remote.canceled_at:
        subscription.canceled_at = remote.canceled_at
    if plan is not None:
        subscription.plan_id = plan.id
    if interval is not None:
        subscription.interval = interval
    elif remote.interval:
        subscription.interval = remote.interval

    if stamp is not None:
        subscription.provider_updated_at = _aware(stamp)
    subscription.updated_at = now_utc()
    return True


def _aware(value: datetime) -> datetime:
    from app.billing.periods import UTC

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def apply_pending_downgrade(
    session: AsyncSession, subscription: Subscription
) -> bool:
    """
    Land a scheduled downgrade at a renewal.

    Called when a renewal is observed (a period boundary moving), not on a
    timer — the provider's boundary is the authority.
    """
    if subscription.pending_plan_id is None:
        return False

    target = await session.get(BillingPlan, subscription.pending_plan_id)
    if target is None:
        subscription.pending_plan_id = None
        return False

    subscription.plan_id = target.id
    if subscription.pending_interval is not None:
        subscription.interval = subscription.pending_interval
    subscription.pending_plan_id = None
    subscription.pending_interval = None

    log.info(
        "billing.downgrade_applied", tenant_id=str(subscription.tenant_id),
        plan=target.code,
    )
    return True


# ------------------------------------------------------------ reconciliation ---

async def reconcile_subscription(
    session: AsyncSession, tenant: Tenant
) -> Subscription | None:
    """
    Re-read the provider and adopt its state.

    The recovery path for requirement 32: a timeout after
    `create_subscription` leaves the provider holding a subscription we never
    recorded. Listing by customer finds it, so the next attempt adopts it
    rather than creating a second.

    Also the manual "something looks wrong" button — the provider is always
    the authority on subscription state, so re-reading is always safe.
    """
    subscription = await get_subscription(session, tenant.id)
    if subscription is None or not subscription.external_customer_id:
        return subscription

    client = adapter()
    if not client.supports(BillingCapability.LIST_SUBSCRIPTIONS):
        return subscription

    remotes = await client.list_subscriptions(subscription.external_customer_id)
    if not remotes:
        return subscription

    # Prefer a live one; otherwise the most recently updated, so a cancelled
    # subscription does not shadow an active replacement.
    live = [
        r for r in remotes
        if r.status not in (
            SubscriptionStatus.CANCELED, SubscriptionStatus.INCOMPLETE_EXPIRED
        )
    ]
    chosen = live[0] if live else remotes[0]

    plan = await _plan_for_price(session, chosen.external_price_id)
    # `event_time=now` deliberately: a direct read is the freshest possible
    # state and must not lose to a stale stored timestamp.
    _apply_remote(subscription, chosen, plan=plan, event_time=now_utc())
    await session.commit()

    log.info(
        "billing.reconciled", tenant_id=str(tenant.id),
        status=subscription.status.value,
        external_subscription_id=chosen.external_id,
    )
    return subscription


async def _plan_for_price(
    session: AsyncSession, price_id: str | None
) -> BillingPlan | None:
    """
    Which plan a provider price id belongs to.

    A reverse lookup over the catalogue rather than a stored map: the
    catalogue is small, and a second mapping table would be a second thing to
    keep in sync.
    """
    if not price_id:
        return None
    for plan in await plan_catalogue.list_plans(session, active_only=False):
        if price_id in (plan.provider_price_ids or {}).values():
            return plan
    return None


async def record_invoice(
    session: AsyncSession, tenant_id: uuid.UUID, remote: RemoteInvoice
) -> BillingInvoice:
    """Mirror a provider invoice. Idempotent on the external id."""
    existing = (
        await session.execute(
            select(BillingInvoice).where(
                BillingInvoice.provider == configured_provider(),
                BillingInvoice.external_invoice_id == remote.external_id,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        existing = BillingInvoice(
            tenant_id=tenant_id,
            provider=configured_provider(),
            external_invoice_id=remote.external_id,
            status=remote.status,
        )
        session.add(existing)

    existing.status = remote.status
    existing.currency = remote.currency
    existing.amount_due_cents = remote.amount_due_cents
    existing.amount_paid_cents = remote.amount_paid_cents
    existing.period_start = remote.period_start
    existing.period_end = remote.period_end
    existing.hosted_invoice_url = remote.hosted_invoice_url
    existing.updated_at = now_utc()
    return existing


# -------------------------------------------------------------- status view ---

@dataclass(frozen=True)
class BillingStatus:
    """The safe summary. Requirement 19's allowlist, as a type."""

    plan_code: str
    plan_name: str
    subscription_status: str
    interval: str
    current_period_start: datetime | None
    current_period_end: datetime | None
    trial_end: datetime | None
    cancel_at_period_end: bool
    pending_plan_code: str | None
    last_invoice_status: str | None
    usage: dict
    estimated_overage_cents: int
    currency: str


async def billing_status(
    session: AsyncSession, tenant: Tenant
) -> BillingStatus:
    from app.billing.entitlements import load_context

    context = await load_context(session, tenant)
    usage = await metering.period_usage(
        session, tenant_id=tenant.id, period=context.period, plan=context.plan
    )

    total_millicents = sum(u.overage_millicents for u in usage.values())
    subscription = context.subscription
    pending = (
        await session.get(BillingPlan, subscription.pending_plan_id)
        if subscription and subscription.pending_plan_id else None
    )

    return BillingStatus(
        plan_code=context.plan.code if context.plan else "none",
        plan_name=context.plan.name if context.plan else "No plan",
        subscription_status=(
            subscription.status.value if subscription else "none"
        ),
        interval=subscription.interval.value if subscription else "month",
        current_period_start=context.period.start,
        current_period_end=context.period.end,
        trial_end=subscription.trial_end if subscription else None,
        cancel_at_period_end=(
            bool(subscription.cancel_at_period_end) if subscription else False
        ),
        pending_plan_code=pending.code if pending else None,
        last_invoice_status=(
            subscription.last_invoice_status.value
            if subscription and subscription.last_invoice_status else None
        ),
        usage={
            metric.value: {
                "included": entry.display(entry.included),
                "used": entry.display(entry.used),
                "overage": entry.display_overage,
                "unit": entry.display_unit,
                "percent_used": entry.percent_used,
                # Round to whole cents at the display boundary only. The
                # stored figure stays in millicents so a period's total is
                # not the sum of a thousand rounding errors.
                "overage_cents": round(entry.overage_millicents / 100),
            }
            for metric, entry in usage.items()
        },
        estimated_overage_cents=round(total_millicents / 100),
        currency=context.plan.currency if context.plan else "usd",
    )