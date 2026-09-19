"""
Where VoxDesk business events become billable usage.

One module, imported by the telephony and messaging layers, so "what gets
billed" has a single answer rather than several `+=` scattered around.

Every function here obeys the same rules, and each exists because the audit
found the opposite:

**It never raises.** A billing problem must not turn a completed call into a
500 or leave a caller listening to silence.

**It never commits.** The caller owns the transaction, so usage lands in the
same commit as the fact that caused it.

**It never calls a provider.** Requirement 37: no Stripe call inside the live
voice loop. Usage is a database row; invoicing is Stripe's job, later,
elsewhere.

**It derives its idempotency key from the call, not the callback.** Ten
duplicate Twilio callbacks produce one usage event, and — the bug the audit
actually found — an ordered sequence of callbacks produces the *right* number
of minutes rather than a smaller one.
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import cost as cost_tracking
from app.billing import metering
from app.billing.periods import period_for
from app.core.logging import log
from app.db.models import UsageMetric


async def _safe(coro, *, what: str) -> bool:
    """
    Run a metering call, swallowing everything.

    A bare `except Exception` is usually a smell. Here it is the requirement:
    this runs inside the Twilio status webhook, and there is no billing-shaped
    problem that should make VoxDesk return non-2xx to Twilio — which would
    make Twilio retry, which would attempt the same billing again.
    """
    try:
        await coro
        return True
    except Exception as exc:
        log.error("billing.metering_failed", what=what, error=type(exc).__name__)
        return False


async def _subscription_for(session: AsyncSession, tenant_id: uuid.UUID):
    from sqlalchemy import select

    from app.db.models import Subscription

    return (
        await session.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()


# --------------------------------------------------------------- voice ---

async def on_call_finalized(session: AsyncSession, tenant, call) -> bool:
    """
    A call reached a terminal state. Bill it, once.

    **The voice-minute billing policy (requirements 12 and 13), stated here
    because this is the only place it is applied:**

    * **Total customer-visible call duration**, not per provider leg. A call
      that was transferred to a human is *one* call from the customer's point
      of view and appears as one line on the invoice. Billing the parent leg
      and the dial leg separately would roughly double the charge for every
      escalated call, and the customer has no way to see why.
    * The idempotency key is `voice_minute:{call_id}`. Derived from the call,
      so every duplicate callback, worker retry and out-of-order webhook maps
      to the same event.
    * Recorded **once, at finalization**, from `call.duration_seconds` — which
      STEP 3's state machine has already settled. The old code billed a
      *delta* against whatever a previous callback had stamped, so the same
      120-second call billed 2.00, 1.00 or 0.50 minutes depending on the order
      Twilio delivered its webhooks. Billing the final absolute duration once
      removes the ordering dependency entirely.
    * Seconds are stored; rounding to whole minutes happens once, on the
      period total, in `metering.compute_overage`.

    Returns True when a new usage event was written. False means it was
    already billed — which is the normal outcome of a retried callback.
    """
    seconds = metering.billable_seconds(getattr(call, "duration_seconds", 0))
    if seconds <= 0:
        return False

    subscription = await _subscription_for(session, tenant.id)
    period = period_for(subscription)

    key = metering.usage_idempotency_key(UsageMetric.VOICE_MINUTE, call.id)

    result = None

    async def _run():
        nonlocal result
        result = await metering.record_usage(
            session,
            tenant_id=tenant.id,
            metric=UsageMetric.VOICE_MINUTE,
            quantity=seconds,
            idempotency_key=key,
            period=period,
            source_entity_id=call.id,
            source_reference=getattr(call, "call_sid", None),
            metadata={
                "direction": getattr(call.direction, "value", str(call.direction)),
                "status": getattr(call.status, "value", str(call.status)),
                # Recorded so the single-charge-per-call policy is auditable:
                # a transferred call is visibly one event, not two.
                "transferred": bool(getattr(call, "escalated", False)),
            },
        )

    if not await _safe(_run(), what="voice_minutes"):
        return False

    if result is not None and result.created:
        # Keep the pre-STEP-7 float in step. Demoted to a cache; see
        # `docs/BILLING-AUDIT.md`.
        await _safe(
            metering.sync_tenant_cache(session, tenant, period=period),
            what="tenant_cache",
        )
        # Step 7 cost awareness: record the provider cost exactly where the
        # usage event lands, so one finalization is one cost observation. An
        # unconfigured price degrades to UNKNOWN, never a guess.
        try:
            cost_tracking.record_cost("voice_minute", seconds / 60.0)
        except Exception:  # noqa: BLE001 - observability must never fail a call
            pass
        return True
    return False


# ----------------------------------------------------------------- SMS ---

async def on_sms_sent(
    session: AsyncSession, tenant, *, message_id: str, segments: int
) -> bool:
    """
    Outbound SMS segments.

    Keyed on the message id, so a retried send that the provider deduplicated
    does not bill twice. `compliance.py` already computes segment counts for a
    different purpose; this is the billing consumer of the same number.
    """
    if segments <= 0:
        return False

    subscription = await _subscription_for(session, tenant.id)
    period = period_for(subscription)

    recorded = await _safe(
        metering.record_usage(
            session,
            tenant_id=tenant.id,
            metric=UsageMetric.SMS_SEGMENT,
            quantity=int(segments),
            idempotency_key=metering.usage_idempotency_key(
                UsageMetric.SMS_SEGMENT, message_id
            ),
            period=period,
            source_reference=str(message_id),
        ),
        what="sms_segments",
    )
    if recorded:
        try:
            cost_tracking.record_cost("sms_segment", float(segments))
        except Exception:  # noqa: BLE001 - observability must never fail a send
            pass
    return recorded


# ------------------------------------------------------------- LLM / TTS ---

async def on_llm_tokens(
    session: AsyncSession, tenant, *, call_id: uuid.UUID, tokens: int, turn: int
) -> bool:
    """
    LLM tokens for one turn of one call.

    Discriminated by turn number: a call has many turns and each is a distinct
    fact, but the same turn replayed by a retry is not. Without the
    discriminator the second turn would collide with the first and only one
    would ever be billed.
    """
    if tokens <= 0:
        return False

    subscription = await _subscription_for(session, tenant.id)
    period = period_for(subscription)

    return await _safe(
        metering.record_usage(
            session,
            tenant_id=tenant.id,
            metric=UsageMetric.LLM_TOKEN,
            quantity=int(tokens),
            idempotency_key=metering.usage_idempotency_key(
                UsageMetric.LLM_TOKEN, call_id, discriminator=f"turn:{turn}"
            ),
            period=period,
            source_entity_id=call_id,
            metadata={"turn": turn},
        ),
        what="llm_tokens",
    )


async def on_tts_characters(
    session: AsyncSession, tenant, *, call_id: uuid.UUID, characters: int, turn: int
) -> bool:
    if characters <= 0:
        return False

    subscription = await _subscription_for(session, tenant.id)
    period = period_for(subscription)

    return await _safe(
        metering.record_usage(
            session,
            tenant_id=tenant.id,
            metric=UsageMetric.TTS_CHARACTER,
            quantity=int(characters),
            idempotency_key=metering.usage_idempotency_key(
                UsageMetric.TTS_CHARACTER, call_id, discriminator=f"turn:{turn}"
            ),
            period=period,
            source_entity_id=call_id,
            metadata={"turn": turn},
        ),
        what="tts_characters",
    )


# ------------------------------------------------------------ enforcement ---

async def may_place_outbound_call(session: AsyncSession, tenant) -> tuple[bool, str]:
    """
    Requirement 26: check **before** the expensive provider action.

    Returns `(allowed, reason)`. Called by the outbound dialer before it asks
    Twilio to place a call, so a tenant whose plan forbids overage never
    incurs the cost in the first place.

    Inbound calls deliberately do **not** go through this. The audit's F10:
    the old code hung up on a dentist's patients because the dentist owed
    money. The customer being cut off is not the customer who owes us.
    """
    from app.billing.entitlements import Decision, check_voice_minutes, load_context
    from app.core.config import settings

    if not settings.billing_enforce_entitlements:
        return True, "enforcement_disabled"

    try:
        context = await load_context(session, tenant)
        # A conservative estimate of one call. Enough that a tenant one
        # minute from their limit is stopped before spending money, not after.
        entitlement = await check_voice_minutes(
            session, context, estimated_seconds=120
        )
    except Exception as exc:
        # Fail open. A billing lookup failing must not stop a business
        # operating -- the metering still records what happens, so nothing is
        # lost, and the alternative is an outage caused by our own bookkeeping.
        log.error("billing.entitlement_check_failed", error=type(exc).__name__)
        return True, "check_failed_open"

    if entitlement.decision is Decision.DENY:
        log.warning(
            "billing.outbound_blocked", tenant_id=str(tenant.id),
            reason=entitlement.reason, used=entitlement.used,
            limit=entitlement.limit,
        )
        return False, entitlement.reason
    return True, entitlement.decision.value


async def may_add_feature(
    session: AsyncSession, tenant, feature: str, *, adding: int = 1
):
    """
    A non-metered entitlement check for a route about to create a row.

    Returns the `Entitlement` so the caller can surface `message` directly —
    it is written to be shown to a person.
    """
    from app.billing.entitlements import check_feature_live, load_context
    from app.core.config import settings

    context = await load_context(session, tenant)
    if not settings.billing_enforce_entitlements:
        from app.billing.entitlements import Decision, Entitlement

        return Entitlement(Decision.ALLOW, feature, reason="enforcement_disabled")

    return await check_feature_live(session, context, feature, adding=adding)