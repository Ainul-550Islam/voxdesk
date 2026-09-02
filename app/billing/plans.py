"""
The plan catalogue and the trust boundary around prices.

The single most important function here is `resolve_price_id`. Requirement 8
forbids a browser submitting an amount, a currency or a Stripe price id; the
client names a **plan code** and an **interval**, and the price comes from the
database. Everything else in this module exists to make that lookup safe:
plans are validated on write, inactive plans are refused, and a plan with no
configured price id fails loudly at startup rather than at checkout.

Seed plans are defined here rather than in a migration. A migration is the
wrong home for business data that changes: repricing should be an operator
action against a running system, not a schema change. `sync_seed_plans()` is
idempotent and never overwrites a price an operator has edited.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.errors import BillingConfigurationError, PlanNotFound
from app.db.models import BillingInterval, BillingPlan, UsageMetric

#: Feature entitlements the product actually enforces. An allowlist, so a
#: typo in a plan's JSON is caught on write rather than silently granting
#: nothing (or, worse, silently granting everything because the key was never
#: read).
KNOWN_FEATURES: frozenset[str] = frozenset({
    "team_members",
    "rag_documents",
    "crm_integrations",
    "calendar_integrations",
    "concurrent_calls",
    "outbound_calls_per_day",
    "whatsapp",
    "outbound_campaigns",
    "api_access",
})

#: `-1` means unlimited. Chosen over `None` because these values are compared
#: numerically all over the entitlement service, and `None` would need a
#: guard at every comparison — which is exactly the guard someone forgets.
UNLIMITED = -1


@dataclass(frozen=True)
class SeedPlan:
    """A built-in plan. Prices in minor units; overage in millicents."""

    code: str
    name: str
    description: str
    monthly_price_cents: int
    annual_price_cents: int | None
    included_voice_minutes: int
    included_sms_segments: int
    included_llm_tokens: int
    included_tts_characters: int
    overage_voice_minute_millicents: int
    overage_sms_millicents: int
    overage_llm_token_millicents: int
    overage_tts_character_millicents: int
    overage_enabled: bool
    trial_days: int
    display_order: int
    feature_entitlements: dict[str, Any] = field(default_factory=dict)


#: The default catalogue.
#:
#: Rates are grounded in the platform pricing researched in STEP 4's market
#: work: an all-in voice minute costs roughly $0.10–0.30 at the infrastructure
#: layer, so an overage rate below that would be sold at a loss. 12c/minute on
#: Starter falling to 8c on Enterprise is a normal volume curve and leaves
#: margin at every tier.
SEED_PLANS: tuple[SeedPlan, ...] = (
    SeedPlan(
        code="trial",
        name="Trial",
        description="14 days to try the product. No card required.",
        monthly_price_cents=0,
        annual_price_cents=None,
        included_voice_minutes=60,
        included_sms_segments=50,
        included_llm_tokens=200_000,
        included_tts_characters=100_000,
        overage_voice_minute_millicents=0,
        overage_sms_millicents=0,
        overage_llm_token_millicents=0,
        overage_tts_character_millicents=0,
        # A trial that can run up an overage bill is not a trial. Exceeding
        # the allowance stops the action rather than metering it.
        overage_enabled=False,
        trial_days=14,
        display_order=0,
        feature_entitlements={
            "team_members": 2,
            "rag_documents": 10,
            "crm_integrations": 1,
            "calendar_integrations": 1,
            "concurrent_calls": 1,
            "outbound_calls_per_day": 20,
            "whatsapp": False,
            "outbound_campaigns": False,
            "api_access": False,
        },
    ),
    SeedPlan(
        code="starter",
        name="Starter",
        description="One receptionist, one calendar, one CRM.",
        monthly_price_cents=19_900,
        annual_price_cents=199_000,          # two months free
        included_voice_minutes=500,
        included_sms_segments=500,
        included_llm_tokens=2_000_000,
        included_tts_characters=1_000_000,
        overage_voice_minute_millicents=1_200,      # 12c
        overage_sms_millicents=200,                 # 2c
        overage_llm_token_millicents=0,             # not billed at this tier
        overage_tts_character_millicents=0,
        overage_enabled=True,
        trial_days=0,
        display_order=1,
        feature_entitlements={
            "team_members": 3,
            "rag_documents": 100,
            "crm_integrations": 1,
            "calendar_integrations": 1,
            "concurrent_calls": 2,
            "outbound_calls_per_day": 100,
            "whatsapp": False,
            "outbound_campaigns": True,
            "api_access": False,
        },
    ),
    SeedPlan(
        code="pro",
        name="Pro",
        description="Multi-location, outbound campaigns, full integrations.",
        monthly_price_cents=49_900,
        annual_price_cents=499_000,
        included_voice_minutes=2_000,
        included_sms_segments=2_500,
        included_llm_tokens=10_000_000,
        included_tts_characters=5_000_000,
        overage_voice_minute_millicents=1_000,      # 10c
        overage_sms_millicents=150,
        overage_llm_token_millicents=0,
        overage_tts_character_millicents=0,
        overage_enabled=True,
        trial_days=0,
        display_order=2,
        feature_entitlements={
            "team_members": 10,
            "rag_documents": 1_000,
            "crm_integrations": 3,
            "calendar_integrations": 3,
            "concurrent_calls": 10,
            "outbound_calls_per_day": 1_000,
            "whatsapp": True,
            "outbound_campaigns": True,
            "api_access": True,
        },
    ),
    SeedPlan(
        code="enterprise",
        name="Enterprise",
        description="Unlimited seats and integrations. Invoiced.",
        monthly_price_cents=149_900,
        annual_price_cents=1_499_000,
        included_voice_minutes=10_000,
        included_sms_segments=10_000,
        included_llm_tokens=50_000_000,
        included_tts_characters=25_000_000,
        overage_voice_minute_millicents=800,        # 8c
        overage_sms_millicents=100,
        overage_llm_token_millicents=0,
        overage_tts_character_millicents=0,
        overage_enabled=True,
        trial_days=0,
        display_order=3,
        feature_entitlements={
            "team_members": UNLIMITED,
            "rag_documents": UNLIMITED,
            "crm_integrations": UNLIMITED,
            "calendar_integrations": UNLIMITED,
            "concurrent_calls": 50,
            "outbound_calls_per_day": UNLIMITED,
            "whatsapp": True,
            "outbound_campaigns": True,
            "api_access": True,
        },
    ),
)

#: The plan a tenant with no subscription falls back to.
#:
#: Requirement 25 is explicit that existing development tenants must not
#: suddenly be blocked. Every tenant created before STEP 7 has no
#: subscription row, so without a default they would all hit a zero
#: allowance on their next call.
DEFAULT_PLAN_CODE = "starter"


#: Which plan column holds the included allowance for each metric, and what
#: the smallest unit is. Central so nothing has to remember that voice is
#: stored in seconds while the plan is written in minutes.
METRIC_CONFIG: dict[UsageMetric, dict[str, Any]] = {
    UsageMetric.VOICE_MINUTE: {
        "included_field": "included_voice_minutes",
        "rate_field": "overage_voice_minute_millicents",
        # Events are recorded in seconds; the plan is written in minutes.
        "units_per_included": 60,
        "unit": "second",
        "display_unit": "minute",
    },
    UsageMetric.SMS_SEGMENT: {
        "included_field": "included_sms_segments",
        "rate_field": "overage_sms_millicents",
        "units_per_included": 1,
        "unit": "segment",
        "display_unit": "segment",
    },
    UsageMetric.LLM_TOKEN: {
        "included_field": "included_llm_tokens",
        "rate_field": "overage_llm_token_millicents",
        "units_per_included": 1,
        "unit": "token",
        "display_unit": "token",
    },
    UsageMetric.TTS_CHARACTER: {
        "included_field": "included_tts_characters",
        "rate_field": "overage_tts_character_millicents",
        "units_per_included": 1,
        "unit": "character",
        "display_unit": "character",
    },
}


def included_units(plan: BillingPlan | None, metric: UsageMetric) -> int:
    """
    The included allowance in the metric's smallest unit.

    Returns 0 for an unknown plan rather than infinity: a tenant whose plan
    row vanished should hit a limit and be noticed, not silently get free
    service.
    """
    if plan is None:
        return 0
    config = METRIC_CONFIG[metric]
    raw = int(getattr(plan, config["included_field"], 0) or 0)
    return raw * int(config["units_per_included"])


def overage_rate_millicents(plan: BillingPlan | None, metric: UsageMetric) -> int:
    if plan is None:
        return 0
    return int(getattr(plan, METRIC_CONFIG[metric]["rate_field"], 0) or 0)


def metric_unit(metric: UsageMetric) -> str:
    return METRIC_CONFIG[metric]["unit"]


def feature_limit(plan: BillingPlan | None, feature: str) -> int | bool | None:
    """
    A non-metered entitlement. `None` means the plan does not mention it.

    Callers distinguish "not mentioned" from "zero" deliberately: an unlisted
    feature falls back to the entitlement service's own default, whereas an
    explicit `0` is a deliberate denial.
    """
    if plan is None:
        return None
    return (plan.feature_entitlements or {}).get(feature)


# ------------------------------------------------------------- validation ---

def validate_feature_entitlements(entitlements: Any) -> dict[str, Any]:
    """
    Validate a plan's feature JSON on write.

    An allowlist, because a typo like `"team_memebers": 3` would otherwise
    store cleanly and then grant the service's default forever — a limit that
    exists in the sales conversation and nowhere in the product.
    """
    if entitlements in (None, {}):
        return {}
    if not isinstance(entitlements, dict):
        raise BillingConfigurationError("feature_entitlements must be an object")

    validated: dict[str, Any] = {}
    for key, value in entitlements.items():
        if key not in KNOWN_FEATURES:
            raise BillingConfigurationError(
                f"{key!r} is not a known feature; allowed: "
                f"{', '.join(sorted(KNOWN_FEATURES))}"
            )
        if isinstance(value, bool):
            validated[key] = value
        elif isinstance(value, int):
            if value < UNLIMITED:
                raise BillingConfigurationError(
                    f"{key!r} must be >= {UNLIMITED} ({UNLIMITED} means unlimited)"
                )
            validated[key] = value
        else:
            raise BillingConfigurationError(
                f"{key!r} must be an integer or a boolean, got {type(value).__name__}"
            )
    return validated


# ---------------------------------------------------------------- lookups ---

async def get_plan_by_code(
    session: AsyncSession, code: str, *, active_only: bool = True
) -> BillingPlan | None:
    query = select(BillingPlan).where(BillingPlan.code == (code or "").strip().lower())
    if active_only:
        query = query.where(BillingPlan.is_active.is_(True))
    return (await session.execute(query)).scalar_one_or_none()


async def require_plan(session: AsyncSession, code: str) -> BillingPlan:
    """
    Resolve a plan code or refuse.

    An inactive plan raises the same error as a nonexistent one, so a
    grandfathered plan code cannot be discovered by probing the checkout
    endpoint.
    """
    plan = await get_plan_by_code(session, code, active_only=True)
    if plan is None:
        raise PlanNotFound(f"no active plan with code {code!r}")
    return plan


async def list_plans(
    session: AsyncSession, *, active_only: bool = True
) -> list[BillingPlan]:
    query = select(BillingPlan)
    if active_only:
        query = query.where(BillingPlan.is_active.is_(True))
    return list(
        (await session.execute(query.order_by(BillingPlan.display_order)))
        .scalars()
        .all()
    )


async def default_plan(session: AsyncSession) -> BillingPlan | None:
    """
    The fallback for a tenant with no subscription.

    Deliberately a real row rather than a hard-coded object, so an operator
    can change what un-subscribed tenants get without a deploy.
    """
    return await get_plan_by_code(session, DEFAULT_PLAN_CODE, active_only=False)


def resolve_price_id(plan: BillingPlan, interval: BillingInterval) -> str:
    """
    **The trust boundary.**

    A checkout request names `(plan_code, interval)`. This turns that into the
    provider price id. There is no code path anywhere that takes a price id
    from a request body — requirement 8, and the reason is that a price id is
    an amount: `POST {"price": "price_one_cent"}` against a naive
    implementation buys Enterprise for a penny.

    Raises rather than defaulting. A plan with a missing price id is a
    configuration error someone must fix, and silently falling back to the
    monthly price when a customer asked for annual is a billing dispute.
    """
    prices = plan.provider_price_ids or {}
    price_id = prices.get(interval.value)
    if not price_id:
        raise BillingConfigurationError(
            f"plan {plan.code!r} has no configured price id for "
            f"{interval.value}ly billing"
        )
    return str(price_id)


def price_cents(plan: BillingPlan, interval: BillingInterval) -> int:
    if interval is BillingInterval.YEAR:
        if plan.annual_price_cents is None:
            raise BillingConfigurationError(
                f"plan {plan.code!r} is not offered annually"
            )
        return int(plan.annual_price_cents)
    return int(plan.monthly_price_cents)


def is_upgrade(current: BillingPlan | None, target: BillingPlan) -> bool:
    """
    Is this a move up?

    Compares monthly price, not `display_order`, because price is what the
    customer experiences and what determines whether the change should be
    immediate. An unsubscribed tenant moving to anything is an upgrade.
    """
    if current is None:
        return True
    return int(target.monthly_price_cents) > int(current.monthly_price_cents)


# ------------------------------------------------------------------ seeding ---

async def sync_seed_plans(
    session: AsyncSession, *, overwrite_prices: bool = False
) -> dict[str, int]:
    """
    Ensure the built-in plans exist. Idempotent.

    **Never overwrites `provider_price_ids`** unless explicitly asked, and
    never overwrites money columns on an existing row. An operator who
    repriced Pro in production must not have that undone by the next deploy —
    that is a real outage where every customer's next invoice is wrong.

    Non-financial fields (name, description, ordering) are refreshed, because
    those are safe to own in code.
    """
    created = updated = 0

    for seed in SEED_PLANS:
        existing = await get_plan_by_code(session, seed.code, active_only=False)

        if existing is None:
            session.add(BillingPlan(
                code=seed.code,
                name=seed.name,
                description=seed.description,
                is_active=True,
                display_order=seed.display_order,
                currency="usd",
                monthly_price_cents=seed.monthly_price_cents,
                annual_price_cents=seed.annual_price_cents,
                included_voice_minutes=seed.included_voice_minutes,
                included_sms_segments=seed.included_sms_segments,
                included_llm_tokens=seed.included_llm_tokens,
                included_tts_characters=seed.included_tts_characters,
                overage_voice_minute_millicents=seed.overage_voice_minute_millicents,
                overage_sms_millicents=seed.overage_sms_millicents,
                overage_llm_token_millicents=seed.overage_llm_token_millicents,
                overage_tts_character_millicents=seed.overage_tts_character_millicents,
                overage_enabled=seed.overage_enabled,
                trial_days=seed.trial_days,
                feature_entitlements=validate_feature_entitlements(
                    seed.feature_entitlements
                ),
                provider_price_ids={},
            ))
            created += 1
            continue

        existing.name = seed.name
        existing.description = seed.description
        existing.display_order = seed.display_order
        if overwrite_prices:
            existing.monthly_price_cents = seed.monthly_price_cents
            existing.annual_price_cents = seed.annual_price_cents
        updated += 1

    await session.commit()
    return {"created": created, "updated": updated}


def configuration_problems(plans: list[BillingPlan], *, is_production: bool) -> list[str]:
    """
    Requirement 6: validate at startup that active production plans have
    trusted price ids.

    Returns problem strings rather than raising, matching
    `Settings.validate_security()` — the caller decides whether that is fatal.
    Checked at boot rather than at checkout because the failure otherwise
    surfaces to a customer holding a credit card.
    """
    if not is_production:
        return []

    problems: list[str] = []
    for plan in plans:
        if not plan.is_active or plan.monthly_price_cents == 0:
            # Free and inactive plans need no price id: nothing is charged.
            continue
        prices = plan.provider_price_ids or {}
        if not prices.get(BillingInterval.MONTH.value):
            problems.append(
                f"active plan {plan.code!r} has no monthly provider price id"
            )
        if plan.annual_price_cents and not prices.get(BillingInterval.YEAR.value):
            problems.append(
                f"plan {plan.code!r} is offered annually but has no annual price id"
            )
    return problems

