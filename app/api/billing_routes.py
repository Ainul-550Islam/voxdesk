"""
Billing API.

    GET  /api/billing                 safe status summary
    GET  /api/billing/plans           the catalogue
    GET  /api/billing/usage           this period's metering
    GET  /api/billing/invoices        invoice metadata
    POST /api/billing/checkout        start a purchase
    POST /api/billing/portal          open the customer portal
    POST /api/billing/change-plan     upgrade or schedule a downgrade
    POST /api/billing/cancel          cancel
    POST /api/billing/reconcile       re-read the provider, rebuild summaries

    POST /api/billing/webhook/{provider}    provider events (unauthenticated,
                                            signature-verified)

Three rules run through the file.

**The tenant is never a parameter.** It comes from `ctx.tenant_id`, which
comes from the verified JWT. There is no endpoint that reads a tenant id, a
customer id or a subscription id from a request.

**A price is never a parameter.** `CheckoutIn` accepts a plan *code* and an
interval. `resolve_price_id` turns those into a provider price. A field named
`price`, `amount` or `currency` does not exist on any request model, so
`POST {"price": "price_one_cent"}` is rejected by Pydantic before it reaches
a handler.

**Responses are allowlists.** `BillingStatusOut` names every field that may
reach a client. No secret key, no webhook secret, no payment method, no raw
provider payload.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import TenantContext, record_audit, require_permission
from app.auth.permissions import Permission
from app.billing import plans as plan_catalogue
from app.billing import reconciliation, service, webhooks
from app.billing.entitlements import load_context
from app.billing.errors import (
    BillingError,
    BillingUnsupportedError,
    PlanNotFound,
)
from app.billing.registry import configured_provider
from app.core.logging import log
from app.db.models import (
    AuditAction,
    BillingInterval,
    BillingInvoice,
    BillingProviderType,
)
from app.db.session import get_session

router = APIRouter(prefix="/api/billing", tags=["billing"])


# ----------------------------------------------------------------- schemas ---

class PlanOut(BaseModel):
    """
    A plan as a customer sees it.

    `provider_price_ids` is absent on purpose. They are not secret, but
    publishing them invites a client to send one back, and the entire trust
    model rests on that never being accepted.
    """

    code: str
    name: str
    description: str
    currency: str
    monthly_price_cents: int
    annual_price_cents: int | None
    included_voice_minutes: int
    included_sms_segments: int
    overage_voice_minute_cents: float
    overage_enabled: bool
    trial_days: int
    features: dict = Field(default_factory=dict)
    is_current: bool = False


class UsageMetricOut(BaseModel):
    metric: str
    unit: str
    included: float
    used: float
    remaining: float
    overage: float
    percent_used: float
    overage_cents: int


class UsageOut(BaseModel):
    billing_period: str
    period_start: str
    period_end: str
    plan_code: str
    metrics: list[UsageMetricOut]
    estimated_overage_cents: int
    currency: str


class BillingStatusOut(BaseModel):
    """Requirement 19's allowlist."""

    plan_code: str
    plan_name: str
    subscription_status: str
    interval: str
    provider: str
    current_period_start: str | None
    current_period_end: str | None
    trial_end: str | None
    cancel_at_period_end: bool
    pending_plan_code: str | None
    last_invoice_status: str | None
    estimated_overage_cents: int
    currency: str
    usage: dict = Field(default_factory=dict)


class InvoiceOut(BaseModel):
    id: str
    status: str
    currency: str
    amount_due_cents: int
    amount_paid_cents: int
    period_start: str | None
    period_end: str | None
    #: Stripe's short-lived signed URL. Safe for an authorized user; it
    #: carries no API credential.
    hosted_invoice_url: str | None


class CheckoutIn(BaseModel):
    """
    **The whole input surface for a purchase.**

    A plan code and an interval. No amount, no currency, no price id, no
    tenant id, no quantity. Requirement 8, enforced by the type.
    """

    plan_code: str = Field(min_length=1, max_length=64)
    interval: BillingInterval = BillingInterval.MONTH


class ChangePlanIn(BaseModel):
    plan_code: str = Field(min_length=1, max_length=64)
    interval: BillingInterval | None = None


class CancelIn(BaseModel):
    #: Default False: the customer has paid for the month, and cutting them
    #: off the moment they click cancel takes money for service not delivered.
    immediately: bool = False
    reason: str = Field(default="", max_length=500)


class SessionOut(BaseModel):
    url: str


# ------------------------------------------------------------- serializers ---

def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def plan_out(plan, *, current_code: str | None = None) -> PlanOut:
    return PlanOut(
        code=plan.code,
        name=plan.name,
        description=plan.description or "",
        currency=plan.currency,
        monthly_price_cents=plan.monthly_price_cents,
        annual_price_cents=plan.annual_price_cents,
        included_voice_minutes=plan.included_voice_minutes,
        included_sms_segments=plan.included_sms_segments,
        # Millicents are an internal precision detail; a pricing page shows
        # cents.
        overage_voice_minute_cents=round(
            plan.overage_voice_minute_millicents / 100, 2
        ),
        overage_enabled=plan.overage_enabled,
        trial_days=plan.trial_days,
        features=dict(plan.feature_entitlements or {}),
        is_current=(current_code is not None and plan.code == current_code),
    )


def _handle_billing_error(exc: BillingError) -> HTTPException:
    """
    Map a normalized billing error to a status.

    A provider failure is a **502**, never a 200 with a hopeful message —
    requirement 32: the customer must not be told "payment completed" when the
    provider was unavailable. The detail is `safe_message`, which has been
    scrubbed of anything key-shaped.
    """
    status = {
        "plan_not_found": 404,
        "not_found": 404,
        "validation": 422,
        "misconfigured": 503,
        "unsupported": 501,
        "unauthorized": 502,
        "card_declined": 402,
        "rate_limited": 503,
        "timeout": 504,
        "temporary": 502,
    }.get(exc.code, 502)
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.safe_message},
    )


# ------------------------------------------------------------------- routes ---

@router.get("", response_model=BillingStatusOut)
@router.get("/", response_model=BillingStatusOut)
async def get_billing(
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_READ)),
    session: AsyncSession = Depends(get_session),
) -> BillingStatusOut:
    status = await service.billing_status(session, ctx.tenant)
    return BillingStatusOut(
        plan_code=status.plan_code,
        plan_name=status.plan_name,
        subscription_status=status.subscription_status,
        interval=status.interval,
        provider=configured_provider().value,
        current_period_start=_iso(status.current_period_start),
        current_period_end=_iso(status.current_period_end),
        trial_end=_iso(status.trial_end),
        cancel_at_period_end=status.cancel_at_period_end,
        pending_plan_code=status.pending_plan_code,
        last_invoice_status=status.last_invoice_status,
        estimated_overage_cents=status.estimated_overage_cents,
        currency=status.currency,
        usage=status.usage,
    )


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_READ)),
    session: AsyncSession = Depends(get_session),
) -> list[PlanOut]:
    context = await load_context(session, ctx.tenant)
    current = context.plan.code if context.plan else None
    return [
        plan_out(plan, current_code=current)
        for plan in await plan_catalogue.list_plans(session, active_only=True)
    ]


@router.get("/usage", response_model=UsageOut)
async def get_usage(
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_READ)),
    session: AsyncSession = Depends(get_session),
) -> UsageOut:
    from app.billing import metering

    context = await load_context(session, ctx.tenant)
    usage = await metering.period_usage(
        session, tenant_id=ctx.tenant_id, period=context.period, plan=context.plan
    )

    return UsageOut(
        billing_period=context.period.label,
        period_start=context.period.start.isoformat(),
        period_end=context.period.end.isoformat(),
        plan_code=context.plan_code,
        metrics=[
            UsageMetricOut(
                metric=metric.value,
                unit=entry.display_unit,
                included=entry.display(entry.included),
                used=entry.display(entry.used),
                remaining=entry.display(entry.remaining),
                overage=entry.display_overage,
                percent_used=entry.percent_used,
                overage_cents=round(entry.overage_millicents / 100),
            )
            for metric, entry in usage.items()
        ],
        estimated_overage_cents=round(
            sum(e.overage_millicents for e in usage.values()) / 100
        ),
        currency=context.plan.currency if context.plan else "usd",
    )


@router.get("/invoices", response_model=list[InvoiceOut])
async def list_invoices(
    limit: int = Query(20, ge=1, le=100),
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_READ)),
    session: AsyncSession = Depends(get_session),
) -> list[InvoiceOut]:
    """
    Invoice metadata.

    Served from the local mirror rather than by calling Stripe on every page
    load: it is faster, it works when Stripe is down, and it keeps the API
    key out of a read path that a dashboard polls.
    """
    rows = (
        (
            await session.execute(
                select(BillingInvoice)
                .where(BillingInvoice.tenant_id == ctx.tenant_id)
                .order_by(BillingInvoice.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        InvoiceOut(
            id=row.external_invoice_id,
            status=row.status.value,
            currency=row.currency,
            amount_due_cents=row.amount_due_cents,
            amount_paid_cents=row.amount_paid_cents,
            period_start=_iso(row.period_start),
            period_end=_iso(row.period_end),
            hosted_invoice_url=row.hosted_invoice_url,
        )
        for row in rows
    ]


@router.post("/checkout", response_model=SessionOut)
async def start_checkout(
    body: CheckoutIn,
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> SessionOut:
    """
    Begin a purchase.

    Returns a URL and nothing else. **No subscription state is established
    here** — a browser reaching the success URL has proved only that it
    reached a URL. Only a verified webhook makes a subscription active.
    """
    try:
        result = await service.start_checkout(
            session, ctx.tenant, plan_code=body.plan_code, interval=body.interval
        )
    except PlanNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.safe_message)
    except BillingError as exc:
        raise _handle_billing_error(exc)

    await record_audit(
        session, action=AuditAction.BILLING_CHECKOUT_STARTED,
        tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        # Plan code and interval only. No amount, no session id, no customer
        # id -- requirement 28.
        detail={"plan": result.plan_code, "interval": body.interval.value},
    )
    await session.commit()
    return SessionOut(url=result.url)


@router.post("/portal", response_model=SessionOut)
async def open_portal(
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> SessionOut:
    """
    Open the customer portal for **the authenticated tenant's** customer.

    There is no request body. The external customer id is read from the
    tenant's own row, so there is no parameter through which another tenant's
    id could be supplied (requirement 9).
    """
    try:
        url = await service.start_portal(session, ctx.tenant)
    except BillingError as exc:
        raise _handle_billing_error(exc)
    return SessionOut(url=url)


@router.post("/change-plan", response_model=BillingStatusOut)
async def change_plan(
    body: ChangePlanIn,
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> BillingStatusOut:
    """
    Upgrade immediately, or schedule a downgrade for the period end.

    The policy is implemented once, in `service.change_plan`, and documented
    there and in `docs/BILLING.md`.
    """
    try:
        subscription = await service.change_plan(
            session, ctx.tenant, plan_code=body.plan_code, interval=body.interval
        )
    except PlanNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.safe_message)
    except BillingError as exc:
        raise _handle_billing_error(exc)

    await record_audit(
        session, action=AuditAction.BILLING_PLAN_CHANGED, tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id, actor_email=ctx.user.email,
        detail={
            "plan": body.plan_code,
            "scheduled": subscription.pending_plan_id is not None,
        },
    )
    await session.commit()
    return await get_billing(ctx=ctx, session=session)


@router.post("/cancel", response_model=BillingStatusOut)
async def cancel_subscription(
    body: CancelIn | None = None,
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> BillingStatusOut:
    payload = body or CancelIn()
    try:
        await service.cancel(
            session, ctx.tenant, immediately=payload.immediately
        )
    except BillingError as exc:
        raise _handle_billing_error(exc)

    await record_audit(
        session, action=AuditAction.BILLING_CANCELLATION_REQUESTED,
        tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        detail={
            "immediately": payload.immediately,
            "reason": payload.reason[:200],
        },
    )
    await session.commit()
    return await get_billing(ctx=ctx, session=session)


@router.post("/reconcile", response_model=BillingStatusOut)
async def reconcile(
    ctx: TenantContext = Depends(require_permission(Permission.BILLING_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> BillingStatusOut:
    """
    Re-read the provider and rebuild the usage cache.

    The "something looks wrong" button. Safe by construction: the provider is
    always authoritative on subscription state, and rebuilding a summary only
    touches a cache.
    """
    try:
        await service.reconcile_subscription(session, ctx.tenant)
    except BillingUnsupportedError:
        pass          # the manual provider has nothing to reconcile against
    except BillingError as exc:
        raise _handle_billing_error(exc)

    await reconciliation.rebuild_summaries(session, ctx.tenant)
    return await get_billing(ctx=ctx, session=session)


# ------------------------------------------------------------------ webhook ---

@router.post("/webhook/{provider}")
async def receive_webhook(
    provider: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """
    Provider events.

    Unauthenticated by necessity — Stripe has no VoxDesk credential — and
    therefore **signature-verified before anything else**.

    `await request.body()` gives the exact bytes as received. Nothing parses
    the JSON before verification: requirement 21 is explicit that a
    re-serialized body invalidates the signature, and reading `request.json()`
    here would silently make every signature check meaningless.

    Always returns 2xx once the signature passes. A non-2xx makes Stripe
    retry, and retrying an event we have already recorded, or one nobody will
    ever handle, is pure noise. Genuine handler failures are the exception:
    those return 500 so Stripe *does* retry.
    """
    try:
        provider_type = BillingProviderType(provider.lower())
    except ValueError:
        # Same shape as a signature failure, so the endpoint cannot be probed
        # for which providers are configured.
        raise HTTPException(status_code=400, detail="Invalid signature")

    if provider_type is not configured_provider():
        raise HTTPException(status_code=400, detail="Invalid signature")

    raw_body = await request.body()
    if len(raw_body) > 1_000_000:
        raise HTTPException(status_code=413, detail="Payload too large")

    client = service.adapter()
    try:
        event = client.verify_webhook(
            raw_body=raw_body,
            signature_header=request.headers.get("Stripe-Signature", ""),
        )
    except BillingError as exc:
        # 400, not 401: Stripe treats 4xx as permanent and stops retrying,
        # which is right for a request that will never verify.
        log.warning(
            "billing.webhook_rejected", provider=provider_type.value,
            reason=exc.code,
        )
        raise HTTPException(status_code=400, detail="Invalid signature")

    outcome = await webhooks.process_event(session, event)

    if outcome.tenant_id and event.event_type == "invoice.payment_failed":
        await record_audit(
            session, action=AuditAction.BILLING_PAYMENT_FAILED,
            tenant_id=outcome.tenant_id, actor_email="stripe:webhook",
            detail={"event_type": event.event_type},
        )
        await session.commit()

    return {
        "received": True,
        "handled": outcome.handled,
        "duplicate": outcome.duplicate,
    }

