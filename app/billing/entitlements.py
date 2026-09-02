"""
The entitlement service.

Requirement 4: route and agent code asks *this*, never `if tenant.plan ==
"pro"`. Requirement 26: the check happens **before** an expensive provider
action, not after.

The design question that matters here is what to do when a tenant is over
their allowance, and the answer is not one thing:

* **Overage enabled** → continue and meter. The customer agreed to pay for
  what they use.
* **Overage disabled** → refuse *before* the provider call. A trial that can
  run up a bill is not a trial.
* **Inbound calls** → never blocked. This is deliberate and is the one place
  the old code was actively harmful: it hung up on a dentist's patients
  because the dentist owed money. The customer being cut off is not the
  customer who owes us; the correct lever is the outbound and account-level
  features, not the phone line their patients ring.

Development is unlimited by an explicit switch, not by accident.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import metering
from app.billing import plans as plan_catalogue
from app.billing.periods import BillingPeriod, elapsed_fraction, period_for
from app.core.logging import log
from app.db.models import (
    BillingPlan,
    ENTITLED_SUBSCRIPTION_STATUSES,
    Subscription,
    SubscriptionStatus,
    UsageMetric,
)

#: The soft threshold. Crossing it warns; it never blocks.
WARNING_PERCENT = 80

#: Where a metered allowance stops when overage is disabled.
HARD_PERCENT = 100


class Decision(str, Enum):
    ALLOW = "allow"
    #: Allowed, but the tenant should be told. Never blocks a call.
    WARN = "warn"
    #: Allowed and now costing money beyond the plan.
    ALLOW_WITH_OVERAGE = "allow_with_overage"
    DENY = "deny"


@dataclass(frozen=True)
class Entitlement:
    """
    The answer to "may this tenant do this, right now".

    `reason` is operator-facing and safe to log. `message` is what a human
    should be shown — never a plan internal, never a provider detail.
    """

    decision: Decision
    feature: str
    reason: str = ""
    message: str = ""
    limit: int | None = None
    used: int | None = None
    percent_used: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision is not Decision.DENY

    @property
    def should_warn(self) -> bool:
        return self.decision in (Decision.WARN, Decision.ALLOW_WITH_OVERAGE)


@dataclass(frozen=True)
class BillingContext:
    """
    Everything an entitlement decision needs, resolved once.

    Assembled by `load_context` and passed around, so a request that checks
    three entitlements does one set of queries rather than three.
    """

    tenant_id: uuid.UUID
    plan: BillingPlan | None
    subscription: Subscription | None
    period: BillingPeriod
    unlimited: bool = False

    @property
    def plan_code(self) -> str:
        return self.plan.code if self.plan else "none"

    @property
    def subscription_status(self) -> SubscriptionStatus | None:
        return self.subscription.status if self.subscription else None

    @property
    def is_entitled(self) -> bool:
        """
        Does the subscription state permit service at all?

        A tenant with no subscription row is entitled: every tenant created
        before STEP 7 is in that state, and requirement 25 forbids suddenly
        blocking them. They fall back to the default plan.

        `PAST_DUE` is entitled too. Stripe is still retrying the card, and
        cutting a business's phone line off on the first failed charge costs
        far more goodwill than the few days of service it saves.
        """
        if self.subscription is None:
            return True
        return self.subscription.status in ENTITLED_SUBSCRIPTION_STATUSES


async def load_context(
    session: AsyncSession, tenant, *, moment=None
) -> BillingContext:
    """
    Resolve a tenant's billing position.

    Falls back through: the subscription's plan → the tenant's legacy `plan`
    string → the configured default. That chain is what lets a pre-STEP-7
    tenant with `plan="pro"` and no subscription keep the Pro entitlements
    they were sold.
    """
    from app.core.config import settings

    subscription = (
        await session.execute(
            select(Subscription).where(Subscription.tenant_id == tenant.id)
        )
    ).scalar_one_or_none()

    plan: BillingPlan | None = None
    if subscription is not None and subscription.plan_id is not None:
        plan = await session.get(BillingPlan, subscription.plan_id)
    if plan is None:
        legacy_code = (getattr(tenant, "plan", "") or "").strip().lower()
        if legacy_code:
            plan = await plan_catalogue.get_plan_by_code(
                session, legacy_code, active_only=False
            )
    if plan is None:
        plan = await plan_catalogue.default_plan(session)

    return BillingContext(
        tenant_id=tenant.id,
        plan=plan,
        subscription=subscription,
        period=period_for(subscription, moment),
        # An explicit switch, not an environment guess. Requirement 25 allows
        # a clearly defined unlimited mode; leaving it implicit is how a
        # staging default reaches production.
        unlimited=bool(settings.billing_unlimited_entitlements),
    )


# ------------------------------------------------------------ metered checks ---

async def check_metric(
    session: AsyncSession,
    context: BillingContext,
    metric: UsageMetric,
    *,
    additional: int = 0,
) -> Entitlement:
    """
    May the tenant consume `additional` more units of `metric`?

    `additional` matters: requirement 26 says the check must happen before the
    expensive action, and "am I already over" is a different question from
    "will this call put me over". For an outbound campaign we can estimate the
    call length; for an inbound call we cannot, so `additional` is zero and
    the check is about the current position.
    """
    feature = metric.value

    if context.unlimited:
        return Entitlement(Decision.ALLOW, feature, reason="unlimited_mode")

    if not context.is_entitled:
        status = context.subscription_status
        return Entitlement(
            Decision.DENY, feature,
            reason=f"subscription_{status.value if status else 'missing'}",
            message="This account's subscription is not active.",
            details={"subscription_status": status.value if status else None},
        )

    if context.plan is None:
        # No plan could be resolved at all -- an unseeded catalogue, or a plan
        # row deleted out from under a subscription. That is *our* failure,
        # and denying service for it punishes the customer for our
        # bookkeeping. Allowed, metered, and logged loudly enough to be seen.
        log.error(
            "billing.no_plan_resolved", tenant_id=str(context.tenant_id),
            metric=metric.value,
        )
        return Entitlement(
            Decision.ALLOW, feature, reason="no_plan_resolved",
            details={"degraded": True},
        )

    usage = await metering.metric_usage(
        session, tenant_id=context.tenant_id, metric=metric,
        period=context.period, plan=context.plan,
    )
    projected = usage.used + max(0, int(additional))
    included = usage.included

    if included <= 0:
        # The plan includes none of this metric. Allowed only if the plan
        # meters overage; otherwise the feature is simply not sold.
        if context.plan is not None and context.plan.overage_enabled:
            return Entitlement(
                Decision.ALLOW_WITH_OVERAGE, feature,
                reason="no_included_allowance_overage_enabled",
                limit=0, used=usage.used,
            )
        return Entitlement(
            Decision.DENY, feature, reason="not_included_in_plan",
            message=f"Your plan does not include {metric.value.replace('_', ' ')}.",
            limit=0, used=usage.used,
        )

    percent = round(projected / included * 100, 1)

    if projected <= included:
        if percent >= WARNING_PERCENT:
            return Entitlement(
                Decision.WARN, feature, reason="approaching_limit",
                message=(
                    f"You've used {percent:.0f}% of this period's "
                    f"{usage.display_unit} allowance."
                ),
                limit=included, used=projected, percent_used=percent,
            )
        return Entitlement(
            Decision.ALLOW, feature, limit=included, used=projected,
            percent_used=percent,
        )

    # Over the included allowance.
    if context.plan is not None and context.plan.overage_enabled:
        return Entitlement(
            Decision.ALLOW_WITH_OVERAGE, feature, reason="over_included_metered",
            message=(
                f"You're past this period's included {usage.display_unit}s; "
                f"further use is billed as overage."
            ),
            limit=included, used=projected, percent_used=percent,
        )

    return Entitlement(
        Decision.DENY, feature, reason="over_included_no_overage",
        message=(
            f"You've used all {usage.display(included)} "
            f"{usage.display_unit}s included in your plan for this period."
        ),
        limit=included, used=projected, percent_used=percent,
    )


async def check_voice_minutes(
    session: AsyncSession, context: BillingContext, *, estimated_seconds: int = 0
) -> Entitlement:
    return await check_metric(
        session, context, UsageMetric.VOICE_MINUTE, additional=estimated_seconds
    )


# ------------------------------------------------------------ feature checks ---

#: What a feature is worth when a plan does not mention it. Permissive on
#: purpose: an unlisted feature must not silently become zero for every
#: pre-STEP-7 tenant. A limit that matters is written into the plan.
_FEATURE_DEFAULTS: dict[str, Any] = {
    "team_members": plan_catalogue.UNLIMITED,
    "rag_documents": plan_catalogue.UNLIMITED,
    "crm_integrations": plan_catalogue.UNLIMITED,
    "calendar_integrations": plan_catalogue.UNLIMITED,
    "concurrent_calls": plan_catalogue.UNLIMITED,
    "outbound_calls_per_day": plan_catalogue.UNLIMITED,
    "whatsapp": True,
    "outbound_campaigns": True,
    "api_access": True,
}


def check_feature(
    context: BillingContext, feature: str, *, current: int = 0, adding: int = 1
) -> Entitlement:
    """
    A non-metered entitlement: seats, documents, integrations.

    Boolean features are on/off. Numeric ones compare `current + adding`
    against the limit, so "can I add one more" is answerable before the row is
    created rather than after.
    """
    if context.unlimited:
        return Entitlement(Decision.ALLOW, feature, reason="unlimited_mode")

    if not context.is_entitled:
        status = context.subscription_status
        return Entitlement(
            Decision.DENY, feature,
            reason=f"subscription_{status.value if status else 'missing'}",
            message="This account's subscription is not active.",
        )

    if context.plan is None:
        # Same reasoning as the metered path: our missing catalogue must not
        # become the customer's outage.
        log.error(
            "billing.no_plan_resolved", tenant_id=str(context.tenant_id),
            feature=feature,
        )
        return Entitlement(
            Decision.ALLOW, feature, reason="no_plan_resolved",
            details={"degraded": True},
        )

    limit = plan_catalogue.feature_limit(context.plan, feature)
    if limit is None:
        limit = _FEATURE_DEFAULTS.get(feature, plan_catalogue.UNLIMITED)

    if isinstance(limit, bool):
        if limit:
            return Entitlement(Decision.ALLOW, feature)
        return Entitlement(
            Decision.DENY, feature, reason="feature_not_in_plan",
            message=(
                f"{feature.replace('_', ' ').capitalize()} is not included in "
                f"the {context.plan_code} plan."
            ),
        )

    limit = int(limit)
    if limit == plan_catalogue.UNLIMITED:
        return Entitlement(Decision.ALLOW, feature, reason="unlimited")

    projected = int(current) + int(adding)
    if projected <= limit:
        percent = round(projected / limit * 100, 1) if limit else 100.0
        decision = Decision.WARN if percent >= WARNING_PERCENT else Decision.ALLOW
        return Entitlement(
            decision, feature, limit=limit, used=projected, percent_used=percent,
            message=(
                f"You're using {projected} of {limit} "
                f"{feature.replace('_', ' ')}."
                if decision is Decision.WARN else ""
            ),
        )

    return Entitlement(
        Decision.DENY, feature, reason="feature_limit_reached",
        message=(
            f"Your {context.plan_code} plan includes {limit} "
            f"{feature.replace('_', ' ')}. Upgrade to add more."
        ),
        limit=limit, used=current,
    )


# ------------------------------------------------------- counting helpers ---

async def count_team_members(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    from app.db.models import User

    return int(
        (
            await session.execute(
                select(func.count(User.id)).where(
                    User.tenant_id == tenant_id, User.is_active.is_(True)
                )
            )
        ).scalar()
        or 0
    )


async def count_documents(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    from app.db.models import DocumentStatus, KnowledgeDocument

    return int(
        (
            await session.execute(
                select(func.count(KnowledgeDocument.id)).where(
                    KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.status != DocumentStatus.ARCHIVED,
                )
            )
        ).scalar()
        or 0
    )


async def count_crm_integrations(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    from app.db.models import CrmIntegration

    return int(
        (
            await session.execute(
                select(func.count(CrmIntegration.id)).where(
                    CrmIntegration.tenant_id == tenant_id,
                    CrmIntegration.is_enabled.is_(True),
                )
            )
        ).scalar()
        or 0
    )


async def count_calendar_integrations(
    session: AsyncSession, tenant_id: uuid.UUID
) -> int:
    from app.db.models import CalendarIntegration

    return int(
        (
            await session.execute(
                select(func.count(CalendarIntegration.id)).where(
                    CalendarIntegration.tenant_id == tenant_id,
                    CalendarIntegration.is_enabled.is_(True),
                )
            )
        ).scalar()
        or 0
    )


_COUNTERS = {
    "team_members": count_team_members,
    "rag_documents": count_documents,
    "crm_integrations": count_crm_integrations,
    "calendar_integrations": count_calendar_integrations,
}


async def check_feature_live(
    session: AsyncSession, context: BillingContext, feature: str, *, adding: int = 1
) -> Entitlement:
    """
    `check_feature` with the current count looked up.

    The convenience form for a route that is about to create a row: it asks
    "may I add one more" without the caller having to know which table holds
    the answer.
    """
    counter = _COUNTERS.get(feature)
    current = await counter(session, context.tenant_id) if counter else 0
    return check_feature(context, feature, current=current, adding=adding)


# ------------------------------------------------------------- thresholds ---

async def threshold_crossed(
    session: AsyncSession, context: BillingContext, metric: UsageMetric
) -> int | None:
    """
    The highest un-announced threshold this tenant has crossed, or `None`.

    Stateful by design: `UsageSummary.warned_at_percent` records what has
    already been announced, so a tenant who passes 80% does not get a warning
    on every subsequent call for the rest of the month.

    Pace matters. 80% on day 2 of a 30-day period is worth flagging; 80% on
    day 27 is a customer using what they paid for. The early-warning branch
    only fires in the first two-thirds of the period.
    """
    from app.db.models import UsageSummary

    usage = await metering.metric_usage(
        session, tenant_id=context.tenant_id, metric=metric,
        period=context.period, plan=context.plan,
    )
    if usage.included <= 0:
        return None

    percent = usage.percent_used
    summary = (
        await session.execute(
            select(UsageSummary).where(
                UsageSummary.tenant_id == context.tenant_id,
                UsageSummary.billing_period == context.period.label,
                UsageSummary.metric == metric,
            )
        )
    ).scalar_one_or_none()
    already = int(summary.warned_at_percent) if summary else 0

    for threshold in (HARD_PERCENT, WARNING_PERCENT):
        if percent < threshold or already >= threshold:
            continue
        if (
            threshold == WARNING_PERCENT
            and elapsed_fraction(context.period) > 0.66
        ):
            # Late in the period this is normal consumption, not a signal.
            continue
        if summary is not None:
            summary.warned_at_percent = threshold
        log.info(
            "billing.usage_threshold", tenant_id=str(context.tenant_id),
            metric=metric.value, threshold=threshold, percent=percent,
            billing_period=context.period.label,
        )
        return threshold
    return None
