"""Deterministic provider-cost accounting for operators (Step 7).

This is **operational analytics**, not billing. It answers "what did this
call / this tenant's usage cost *us* in provider fees", using an
operator-configured price table. It never touches customer invoices, plan
overage, Stripe, or any metering row — those stay the property of
``app.billing.metering`` / ``app.billing.plans``.

The honesty rules, stated once:

* A price comes only from ``COST_UNIT_PRICES`` (settings). A missing or zero
  price means **UNKNOWN**; the volume is still counted, the dollars are not,
  and nothing is ever guessed.
* Revenue (what tenants pay) is a different number owned by the plan
  catalogue. Cost and revenue are reported separately; this module never
  conflates them.
* Consumption is never double-counted: cost is recorded exactly where the
  corresponding usage event is written (voice minutes and SMS in
  ``billing.hooks``), and for LLM/TTS exactly where the bytes/tokens are
  measured (the pipeline usage tracker). Each site keys on the same
  idempotency boundary as the usage itself.

Prices are expressed in **millicents per unit** (1/100,000 of a dollar) so a
$0.013/minute Twilio rate is ``1300`` and a $0.00015/token LLM rate is ``15``
per 1,000 tokens.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core import observability
from app.core.config import settings
from app.core.logging import log

#: The fixed key vocabulary understood in ``COST_UNIT_PRICES``. Anything else
#: is ignored, so a typo degrades to UNKNOWN rather than silently pricing.
_PRICE_KEYS = frozenset({
    "voice_minute",        # millicents per minute (Twilio)
    "sms_segment",         # millicents per SMS segment
    "llm_1k_tokens",       # millicents per 1,000 tokens (any provider)
    "tts_1k_chars",        # millicents per 1,000 characters (ElevenLabs)
})

#: LLM providers that may carry a provider-specific price key suffix.
_LLM_PROVIDERS = frozenset({"openai", "anthropic", "google"})


@dataclass(frozen=True)
class CostLine:
    """The deterministic cost of some measured consumption."""

    resource: str
    units: float
    price_millicents: float | None     # per natural unit, or None if UNKNOWN
    known: bool

    @property
    def cost_usd(self) -> float | None:
        """Dollars, or None when the price is UNKNOWN."""
        if not self.known or self.price_millicents is None:
            return None
        return round(self.units * self.price_millicents / 100_000.0, 6)


def _prices() -> dict[str, int]:
    return settings.cost_unit_prices


def price_millicents(resource: str, *, provider: str | None = None) -> float | None:
    """The configured price for one *natural* unit, in millicents, or None.

    ``resource`` is one of ``voice_minute``, ``sms_segment``, ``llm_token``,
    ``tts_character`` (the counter vocabulary). LLM/TTS prices are configured
    per 1,000 units and divided down here so every caller works in natural
    units.
    """
    prices = _prices()
    if resource == "voice_minute":
        raw = prices.get("voice_minute")
        return float(raw) if raw and raw > 0 else None
    if resource == "sms_segment":
        raw = prices.get("sms_segment")
        return float(raw) if raw and raw > 0 else None
    if resource == "llm_token":
        key = f"llm_1k_tokens:{provider}" if provider in _LLM_PROVIDERS else None
        raw = prices.get(key) if key else None
        if raw is None or raw <= 0:
            raw = prices.get("llm_1k_tokens")
        return (float(raw) / 1000.0) if raw and raw > 0 else None
    if resource == "tts_character":
        raw = prices.get("tts_1k_chars")
        return (float(raw) / 1000.0) if raw and raw > 0 else None
    return None


def cost_line(resource: str, units: float, *, provider: str | None = None) -> CostLine:
    """Compute a deterministic cost line from measured units."""
    price = price_millicents(resource, provider=provider)
    return CostLine(
        resource=resource,
        units=float(units),
        price_millicents=price,
        known=price is not None,
    )


def record_cost(resource: str, units: float, *, provider: str | None = None) -> CostLine:
    """Emit a cost counter update and return the line for logging.

    Idempotency is the *caller's* concern (this is a pure metric emit); call
    it only at the same place the usage event is written so it cannot run
    twice for one fact.
    """
    line = cost_line(resource, units, provider=provider)
    observability.record_cost(
        resource, line.units, unit_price_millicents=line.price_millicents
    )
    if not line.known:
        log.debug(
            "cost.unknown", resource=resource, units=round(line.units, 2),
            provider=provider,
        )
    return line


# ------------------------------------------------- per-call reconstruction ---

async def call_cost_breakdown(session, call) -> dict[str, Any]:
    """Reconstruct one call's provider cost from its usage events.

    Reads only; never writes, never estimates. Voice minutes come from the
    idempotent usage event written at finalization; LLM/TTS consumption is not
    stored as usage events (it is operational-only), so it is reported as
    ``unavailable`` here and is visible in the call's log lines instead.
    Returns a plain dict safe to log and safe to show an operator.
    """
    from sqlalchemy import select

    from app.db.models import UsageEvent, UsageMetric

    rows = (
        await session.execute(
            select(UsageEvent).where(UsageEvent.source_entity_id == call.id)
        )
    ).scalars().all()

    voice_seconds = 0
    for row in rows:
        if row.metric is UsageMetric.VOICE_MINUTE:
            voice_seconds += int(row.quantity or 0)

    minutes = voice_seconds / 60.0
    line = cost_line("voice_minute", minutes)
    return {
        "call_id": str(call.id),
        "voice_minutes": round(minutes, 2),
        "voice_cost_usd": line.cost_usd,
        "voice_cost_known": line.known,
        "llm_tokens": "unavailable",   # operational metric, not a usage event
        "tts_characters": "unavailable",
    }
