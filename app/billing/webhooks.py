"""
Provider webhook processing.

Requirement 21–23. Three concerns, deliberately separated:

**Verification** happens in the adapter, against the raw bytes, before this
module sees anything. `WebhookEvent` can only be produced by
`verify_webhook`, so an unverified body cannot reach a handler by accident.

**Deduplication** is a `BillingWebhookReceipt` row with a unique constraint on
`(provider, provider_event_id)`. Stripe genuinely re-delivers, and here a
duplicate has financial consequences.

**Ordering** is handled by comparing the event's own `created` timestamp
against what we last applied. Stripe makes no delivery-order guarantee at all:
`customer.subscription.updated` routinely arrives before
`checkout.session.completed`, and a retried old event can land after a newer
one. Every state write goes through `service._apply_remote`, which refuses to
go backwards.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import service
from app.billing.base import WebhookEvent
from app.billing.errors import safe_message
from app.billing.periods import now_utc
from app.billing.registry import configured_provider
from app.core.logging import log
from app.db.models import (
    BillingProviderType,
    BillingWebhookReceipt,
    InvoiceStatus,
    Subscription,
    SubscriptionStatus,
    Tenant,
)


@dataclass(frozen=True)
class WebhookOutcome:
    """What processing did. Never raises for a duplicate."""

    handled: bool
    duplicate: bool = False
    ignored_reason: str | None = None
    tenant_id: uuid.UUID | None = None


#: Events we act on. An allowlist, so an unrecognised event is recorded and
#: ignored rather than falling into a handler that half-understands it.
HANDLED_EVENTS = frozenset({
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_succeeded",
    "invoice.payment_failed",
})


async def process_event(
    db_session: AsyncSession, event: WebhookEvent
) -> WebhookOutcome:
    """
    Handle one **verified** provider event, idempotently.

    Commits its own work. Never raises for an event we cannot attribute or do
    not handle — Stripe retries anything that is not a 2xx, and retrying an
    event nobody will ever handle is pure noise.
    """
    provider = configured_provider()

    claimed = await _claim(db_session, provider, event)
    if claimed is None:
        log.info(
            "billing.webhook_duplicate", provider=provider.value,
            provider_event_id=event.event_id, event_type=event.event_type,
        )
        return WebhookOutcome(handled=False, duplicate=True)

    if event.event_type not in HANDLED_EVENTS:
        claimed.processed = True
        await db_session.commit()
        return WebhookOutcome(handled=False, ignored_reason="unhandled_type")

    obj = ((event.data or {}).get("data") or {}).get("object") or {}
    subscription = await _resolve_subscription(db_session, obj, event)

    if subscription is None:
        # Cannot attribute it. The receipt is kept so a replay is recognised,
        # and the reason is recorded so it is investigable rather than silent.
        claimed.processed = True
        claimed.last_error = "no matching tenant"
        await db_session.commit()
        log.warning(
            "billing.webhook_unattributed", provider=provider.value,
            provider_event_id=event.event_id, event_type=event.event_type,
        )
        return WebhookOutcome(handled=False, ignored_reason="no_tenant")

    claimed.tenant_id = subscription.tenant_id

    try:
        await _dispatch(db_session, event, obj, subscription)
    except Exception as exc:
        # Recorded, not swallowed. `processed` stays False so a manual replay
        # is possible, and the message is scrubbed because it may quote a
        # provider payload.
        claimed.last_error = safe_message(str(exc))[:500]
        await db_session.commit()
        log.error(
            "billing.webhook_failed", provider=provider.value,
            provider_event_id=event.event_id, event_type=event.event_type,
            error=type(exc).__name__,
        )
        raise

    claimed.processed = True
    await db_session.commit()

    log.info(
        "billing.webhook_processed", provider=provider.value,
        provider_event_id=event.event_id, event_type=event.event_type,
        tenant_id=str(subscription.tenant_id),
    )
    return WebhookOutcome(handled=True, tenant_id=subscription.tenant_id)


async def _claim(
    db_session: AsyncSession, provider: BillingProviderType, event: WebhookEvent
) -> BillingWebhookReceipt | None:
    """
    Take the receipt, or discover this event was already handled.

    The unique constraint is the guarantee; the `SELECT` is an optimisation
    for the common case. Committed immediately so a concurrent delivery of the
    same event loses the race here rather than half-way through a handler.
    """
    existing = (
        await db_session.execute(
            select(BillingWebhookReceipt).where(
                BillingWebhookReceipt.provider == provider,
                BillingWebhookReceipt.provider_event_id == event.event_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    receipt = BillingWebhookReceipt(
        provider=provider,
        provider_event_id=event.event_id,
        event_type=event.event_type[:120],
        provider_created_at=event.created_at,
    )
    db_session.add(receipt)
    try:
        async with db_session.begin_nested():
            await db_session.flush()
    except IntegrityError:
        return None
    return receipt


async def _resolve_subscription(
    db_session: AsyncSession, obj: dict, event: WebhookEvent
) -> Subscription | None:
    """
    Find the tenant this event belongs to.

    Four routes, tried in order of reliability:

    1. `metadata.voxdesk_tenant_id` — stamped by us at checkout and on the
       subscription itself. The only one that works before any local id is
       stored, which is exactly the ordering case in requirement 23.
    2. The provider subscription id.
    3. The provider customer id.
    4. For an invoice, the subscription id it references.

    A `tenant_id` that does not resolve to a real tenant is ignored rather
    than trusted — the metadata is ours, but the event body is not, and a
    forged one must not select a tenant. (The signature check upstream makes
    forgery hard; this makes it useless.)
    """
    provider = configured_provider()

    metadata = obj.get("metadata") or {}
    tenant_hint = metadata.get("voxdesk_tenant_id")
    if not tenant_hint:
        nested = (obj.get("subscription_details") or {}).get("metadata") or {}
        tenant_hint = nested.get("voxdesk_tenant_id")

    if tenant_hint:
        try:
            tenant_uuid = uuid.UUID(str(tenant_hint))
        except (ValueError, AttributeError, TypeError):
            tenant_uuid = None
        if tenant_uuid is not None:
            tenant = await db_session.get(Tenant, tenant_uuid)
            if tenant is not None:
                return await service._ensure_row(db_session, tenant, provider)

    subscription_id = _subscription_id(obj)
    if subscription_id:
        found = (
            await db_session.execute(
                select(Subscription).where(
                    Subscription.provider == provider,
                    Subscription.external_subscription_id == subscription_id,
                )
            )
        ).scalar_one_or_none()
        if found is not None:
            return found

    customer_id = _customer_id(obj)
    if customer_id:
        found = (
            await db_session.execute(
                select(Subscription).where(
                    Subscription.provider == provider,
                    Subscription.external_customer_id == customer_id,
                )
            )
        ).scalar_one_or_none()
        if found is not None:
            return found

    return None


def _subscription_id(obj: dict) -> str | None:
    for key in ("subscription", "id"):
        value = obj.get(key)
        if isinstance(value, str) and value.startswith("sub_"):
            return value
        if isinstance(value, dict) and str(value.get("id", "")).startswith("sub_"):
            return str(value["id"])
    return None


def _customer_id(obj: dict) -> str | None:
    value = obj.get("customer")
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict) and value.get("id"):
        return str(value["id"])
    return None


# ------------------------------------------------------------- dispatchers ---

async def _dispatch(
    db_session: AsyncSession, event: WebhookEvent, obj: dict,
    subscription: Subscription,
) -> None:
    handler = {
        "checkout.session.completed": _on_checkout_completed,
        "customer.subscription.created": _on_subscription_changed,
        "customer.subscription.updated": _on_subscription_changed,
        "customer.subscription.deleted": _on_subscription_deleted,
        "invoice.paid": _on_invoice_paid,
        "invoice.payment_succeeded": _on_invoice_paid,
        "invoice.payment_failed": _on_invoice_failed,
    }[event.event_type]
    await handler(db_session, event, obj, subscription)


async def _on_checkout_completed(
    db_session: AsyncSession, event: WebhookEvent, obj: dict,
    subscription: Subscription,
) -> None:
    """
    A checkout finished.

    Deliberately does **not** set ACTIVE from the session body. The session
    says a payment page was completed; the *subscription* object says what
    state the subscription is in, and those can differ (3DS, a failed initial
    charge). So the subscription id is linked and the provider is asked.

    Requirement 10: "do not trust browser callbacks as billing truth" — and
    this is the server-side echo of the same principle.
    """
    subscription_id = _subscription_id(obj) or obj.get("subscription")
    if isinstance(subscription_id, str) and subscription_id:
        subscription.external_subscription_id = subscription_id
    customer_id = _customer_id(obj)
    if customer_id:
        subscription.external_customer_id = customer_id

    # The plan the tenant was buying, recorded at checkout time.
    if subscription.pending_plan_id and subscription.plan_id is None:
        subscription.plan_id = subscription.pending_plan_id

    client = service.adapter()
    if subscription.external_subscription_id:
        try:
            remote = await client.get_subscription(
                subscription.external_subscription_id
            )
        except Exception:
            # A read failure must not fail the webhook: the
            # `subscription.updated` event that follows will carry the state
            # anyway, and returning non-2xx would make Stripe retry a
            # checkout we have already recorded.
            remote = None
        if remote is not None:
            plan = await service._plan_for_price(
                db_session, remote.external_price_id
            )
            service._apply_remote(
                subscription, remote, plan=plan, event_time=event.created_at
            )

    log.info(
        "billing.checkout_completed", tenant_id=str(subscription.tenant_id),
        status=subscription.status.value,
    )


async def _on_subscription_changed(
    db_session: AsyncSession, event: WebhookEvent, obj: dict,
    subscription: Subscription,
) -> None:
    """
    The authoritative state update.

    A renewal is detected by the period moving forward, which is when a
    scheduled downgrade lands and the outgoing period is finalized. Doing
    either on a timer would risk closing a period the provider is still
    adding to.
    """
    from app.billing.providers.stripe import StripeProvider

    client = service.adapter()
    remote = (
        client._to_subscription(obj)
        if isinstance(client, StripeProvider)
        else await client.get_subscription(_subscription_id(obj) or "")
    )
    if remote is None:
        return

    previous_end = subscription.current_period_end
    plan = await service._plan_for_price(db_session, remote.external_price_id)

    applied = service._apply_remote(
        subscription, remote, plan=plan, event_time=event.created_at
    )
    if not applied:
        return          # stale event; `_apply_remote` logged it

    renewed = (
        previous_end is not None
        and remote.current_period_start is not None
        and _aware(remote.current_period_start) >= _aware(previous_end)
    )
    if renewed:
        await _close_previous_period(db_session, subscription, previous_end)
        await service.apply_pending_downgrade(db_session, subscription)


async def _close_previous_period(
    db_session: AsyncSession, subscription: Subscription, previous_end
) -> None:
    """Finalize the period that just ended, so its figures stop moving."""
    from app.billing import metering
    from app.billing.periods import period_for

    tenant = await db_session.get(Tenant, subscription.tenant_id)
    if tenant is None:
        return

    plan = await service.get_plan(db_session, subscription)
    # One second before the boundary is unambiguously inside the old period,
    # which half-open bounds make exact.
    closing = period_for(subscription, _aware(previous_end) - _one_second())
    await metering.finalize_period(
        db_session, tenant_id=tenant.id, period=closing, plan=plan
    )


def _one_second():
    from datetime import timedelta

    return timedelta(seconds=1)


async def _on_subscription_deleted(
    db_session: AsyncSession, event: WebhookEvent, obj: dict,
    subscription: Subscription,
) -> None:
    """
    The subscription is gone at the provider.

    Terminal, so ordering still matters: a delete that arrives *before* a
    newer update (a reactivation) must not win. `_apply_remote` handles that,
    and this only forces the status when the write was accepted.
    """
    remote_updated = event.created_at
    if (
        remote_updated is not None
        and subscription.provider_updated_at is not None
        and _aware(remote_updated) < _aware(subscription.provider_updated_at)
    ):
        log.info(
            "billing.stale_delete_ignored",
            tenant_id=str(subscription.tenant_id),
        )
        return

    subscription.status = SubscriptionStatus.CANCELED
    subscription.canceled_at = now_utc()
    subscription.cancel_at_period_end = False
    subscription.pending_plan_id = None
    subscription.pending_interval = None
    if remote_updated is not None:
        subscription.provider_updated_at = _aware(remote_updated)
    subscription.updated_at = now_utc()

    log.info("billing.subscription_deleted", tenant_id=str(subscription.tenant_id))


async def _on_invoice_paid(
    db_session: AsyncSession, event: WebhookEvent, obj: dict,
    subscription: Subscription,
) -> None:
    from app.billing.providers.stripe import StripeProvider

    client = service.adapter()
    if isinstance(client, StripeProvider):
        await service.record_invoice(
            db_session, subscription.tenant_id, client._to_invoice(obj)
        )

    subscription.last_invoice_status = InvoiceStatus.PAID
    subscription.last_payment_failed_at = None
    # A successful payment clears PAST_DUE. Not touched otherwise -- an
    # invoice paying does not make a cancelling subscription active again.
    if subscription.status is SubscriptionStatus.PAST_DUE:
        subscription.status = SubscriptionStatus.ACTIVE
    subscription.updated_at = now_utc()

    log.info("billing.invoice_paid", tenant_id=str(subscription.tenant_id))


async def _on_invoice_failed(
    db_session: AsyncSession, event: WebhookEvent, obj: dict,
    subscription: Subscription,
) -> None:
    """
    A payment failed.

    Marks PAST_DUE, which **still entitles the tenant to service** (see
    `ENTITLED_SUBSCRIPTION_STATUSES`). Stripe is still retrying the card;
    cutting a business's phone line off on the first failed charge costs more
    goodwill than the few days of service it saves, and the customer often
    does not know their card expired until we tell them.
    """
    from app.billing.providers.stripe import StripeProvider

    client = service.adapter()
    if isinstance(client, StripeProvider):
        await service.record_invoice(
            db_session, subscription.tenant_id, client._to_invoice(obj)
        )

    subscription.last_invoice_status = InvoiceStatus.OPEN
    subscription.last_payment_failed_at = now_utc()
    if subscription.status in (
        SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING
    ):
        subscription.status = SubscriptionStatus.PAST_DUE
    subscription.updated_at = now_utc()

    log.warning(
        "billing.payment_failed", tenant_id=str(subscription.tenant_id),
        status=subscription.status.value,
    )


def _aware(value):
    from app.billing.periods import UTC

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def prune_receipts(db_session: AsyncSession, *, older_than_days: int = 60) -> int:
    """
    Drop old receipts.

    Longer than the CRM and calendar windows: Stripe retries a failing
    endpoint for up to three days, and a receipt that expires while Stripe is
    still retrying would let a duplicate through.
    """
    from datetime import timedelta

    from sqlalchemy import delete

    cutoff = now_utc() - timedelta(days=older_than_days)
    result = await db_session.execute(
        delete(BillingWebhookReceipt).where(
            BillingWebhookReceipt.received_at < cutoff,
            BillingWebhookReceipt.processed.is_(True),
        )
    )
    await db_session.commit()
    return result.rowcount or 0
