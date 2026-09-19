# VoxDesk — Provider-cost awareness (Step 7)

This document describes how VoxDesk answers "what does this cost *us*" — the
operator's question — without ever inventing a number, and without touching
customer billing.

---

## The line this never crosses

* **Provider cost** (this feature) = what VoxDesk pays Twilio / Deepgram /
  ElevenLabs / the LLM providers. Reported to operators only.
* **Revenue** (the billing layer) = what tenants pay, from the plan catalogue
  and overage. Owned by `app/billing/plans.py` and `app/billing/metering.py`.

These are **different numbers owned by different layers**. The cost metrics are
operational analytics and are never a billing authority, never feed an
invoice, and never change a tenant's metered usage.

---

## The price table

Prices come from **one place**: the `COST_UNIT_PRICES` environment variable, a
JSON object of millicents per unit (1/100,000 of a dollar):

```bash
COST_UNIT_PRICES='{
  "voice_minute": 1300,             # $0.013 / minute (Twilio)
  "sms_segment": 790,               # $0.0079 / segment
  "llm_1k_tokens": 15,              # $0.00015 / 1,000 tokens (any provider)
  "llm_1k_tokens:anthropic": 80,    # $0.0008 / 1,000 tokens (provider override)
  "tts_1k_chars": 30                # $0.0003 / 1,000 characters
}'
```

Key vocabulary (closed set; anything else is ignored):

| Key | Unit | Default |
|---|---|---|
| `voice_minute` | millicents / minute | UNKNOWN |
| `sms_segment` | millicents / segment | UNKNOWN |
| `llm_1k_tokens` | millicents / 1,000 tokens | UNKNOWN |
| `llm_1k_tokens:<provider>` | provider-specific override | falls back to generic |
| `tts_1k_chars` | millicents / 1,000 characters | UNKNOWN |

Startup validation rejects malformed JSON or negative prices
(`app/core/config.py::validate_security`).

---

## MEASURED vs UNKNOWN

The honesty rule, stated once: **a missing or zero price means UNKNOWN, and
the dollars are simply not reported.** The *volume* is always counted.

| What | When | Representation |
|---|---|---|
| `voxdesk_cost_units_total{resource}` | always | total consumption in resource units |
| `voxdesk_cost_usd_total{resource}` | price configured | dollars of provider cost |
| `voxdesk_cost_unknown_total{resource}` | price missing | volume that could not be priced |

For example: with no `COST_UNIT_PRICES` set, `voxdesk_cost_units_total` grows
and `voxdesk_cost_unknown_total` mirrors it, while `voxdesk_cost_usd_total`
stays flat. The dashboard shows "unpriced consumption" next to "known cost" so
an operator can see both the dollars and the gap.

`app/billing/cost.py` is the single implementation; `price_millicents()`
returns `None` (not `0`) for an unknown price, so "unknown" can never be
misread as "free".

---

## No double counting

Cost is recorded **exactly where the corresponding consumption is recorded**,
on the same idempotency boundary:

| Resource | Recorded at | Idempotency boundary |
|---|---|---|
| voice minutes | `billing.hooks.on_call_finalized`, only when a *new* usage event is written | `voice_minute:{call_id}` |
| SMS segments | `billing.hooks.on_sms_sent`, only when a new event is written | `sms_segment:{message_id}` |
| LLM tokens | the pipeline usage tracker, from provider-reported `total_tokens` | per completion (provider-reported) |
| TTS characters | the pipeline usage tracker, from the TTS service's reported count | per speak |

A retried Twilio callback, a duplicated message webhook, or a replayed
completion therefore cannot double-count cost — the guard is the same one that
prevents double-billing.

---

## Per-call cost

* **Voice cost per call** is reconstructable from the call's usage events via
  `app.billing.cost.call_cost_breakdown` (read-only).
* **AI usage per call** is the `call.usage` log line (STT chars, TTS chars,
  LLM tokens, provider/model) — a log trace, because LLM/TTS consumption is
  operational-only and is deliberately **not** written as billing usage events
  (that would change tenant metering semantics).

---

## Known limitations (be honest)

1. **Prices are operator-provided and may be stale.** The table reflects what
   was configured, not a live price API. Re-price when a provider re-prices.
2. **Telephony legs beyond Twilio are not modelled** — there is a single
   voice price. If a second telephony provider is added, add its price key.
3. **LLM token prices are per-provider overrides only**, not per-model. Model
   granularity is a log field (`llm_used`), not a price key.
4. **No currency beyond USD is modelled.** Prices are dollars.
5. **Reconciliation against the provider invoice is out of scope** — that is
   the billing layer's job (`app/billing/reconciliation.py`), and it remains
   the authority for what tenants are charged. This feature only measures
   operational cost.
