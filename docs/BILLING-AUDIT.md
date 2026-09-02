# STEP 7 pre-work: audit of the existing billing code

Written **before** anything was modified. Every claim below was executed, not
inferred from reading.

## What existed

| Piece | Where |
|---|---|
| Four columns on `Tenant` | `plan` (free string), `included_minutes`, `minutes_used`, `is_active` |
| One increment site | `twilio_handler.py:313` — `tenant.minutes_used += billable / 60.0` |
| One enforcement site | `twilio_handler.py:69` — hang up at `minutes_used >= included_minutes * 1.5` |
| Two permissions | `BILLING_READ`, `BILLING_WRITE` — owner only, **never referenced by any route** |
| Stripe | **nothing** — no SDK, no config, no key, no webhook |
| Plan table | **nothing** — `plan` is a `String(32)` with the comment `# starter / pro` |
| Usage events | **nothing** — a single float counter |
| Invoices, subscriptions, customers | **nothing** |

## Findings

### The one that loses money

**F1 — The same call bills a different amount depending on webhook ordering.**

`billable = max(0.0, duration_seconds - previous_duration)` subtracts whatever
duration a *previous* callback happened to stamp. Intermediate callbacks are
stamped but never billed, so their seconds vanish. Executed against the real
`call_state` module, one 120-second call:

```
completed only                          -> billed 2.00 min   (correct)
in-progress(0s) then completed(120s)    -> billed 2.00 min   (correct)
in-progress(60s) then completed(120s)   -> billed 1.00 min   (50% short)
answered(90s) then completed(120s)      -> billed 0.50 min   (75% short)
completed x10 (duplicate retries)       -> billed 2.00 min   (correct)
```

Duplicate protection works. Ordering does not. Twilio genuinely sends
`in-progress` and `answered` callbacks, and on a transferred call the parent
and dial legs report out of order — which is exactly the case the surrounding
code was written to survive. Direction of the error is **under**-billing, so
nobody complains and it never gets found.

### The one that bricks accounts

**F2 — `minutes_used` is never reset. Anywhere.**

```
grep -rn "minutes_used" app scripts   ->  4 hits:
  models.py:212          the column
  routes.py:69           a response field
  twilio_handler.py:69   the cap check
  twilio_handler.py:313  the increment
```

No scheduler job, no period rollover, no migration. It is a **lifetime**
counter compared against a **monthly** allowance. A tenant on 500 included
minutes gets 750 minutes ever, and then every inbound call is answered with
*"This account has reached its usage limit"* — permanently, with no way back
short of a manual `UPDATE`.

### Structural

| # | Finding |
|---|---|
| F3 | **`minutes_used` is a display counter being used as billing authority.** It is a mutable float on a mutable row with no history. There is no way to answer "why was this tenant charged this much", no way to re-derive a total, and any bug in it is unrecoverable because the evidence is overwritten. |
| F4 | **No billing period exists at all.** No `current_period_start`, no anchor, nothing to attach usage to. |
| F5 | **No plan catalogue.** `plan` is an unvalidated string; `included_minutes` is a per-tenant integer with no relationship to it. A tenant can be `plan="pro"` with `included_minutes=0`. |
| F6 | **Only voice minutes are metered.** SMS segments, LLM tokens and TTS characters are all consumed and none are counted, despite `compliance.py` already computing SMS segment counts for a different purpose. |
| F7 | **The `× 1.5` hard cap is the only entitlement in the product.** No team-member limit, no document limit, no concurrent-call limit, no outbound-campaign limit. `knowledge_max_documents_per_tenant` is a global constant, not a plan entitlement. |
| F8 | **`BILLING_READ` / `BILLING_WRITE` are declared and never used.** No route consults them, so there is no billing surface to protect yet — but also no precedent to follow. |
| F9 | **The enforcement point is inside the voice loop.** `twilio_handler` reads `tenant.minutes_used` directly on every inbound call. Any future plan logic added there runs while a caller waits. |
| F10 | **The cap blocks inbound calls, which are the customer's revenue.** Hanging up on a dentist's patients because the dentist owes $40 is a product decision that was never made deliberately — it fell out of one line. |
| F11 | **`estimated_value_usd = booked * 150`** in the analytics route is commented *"This is the number you put on the invoice."* It is a marketing figure, not a billing figure, and it sits one function away from real usage data. |

### What already works and must be preserved

* `call_state.apply_provider_status` returns `applied=False` for duplicates,
  terminal-protects, and is well tested (STEP 3). **The finalization signal
  this step needs already exists and is trustworthy** — the bug is in what the
  caller does with it, not in the state machine.
* Twilio signature verification on the status callback (STEP 2/3) means an
  attacker cannot forge usage.
* `Permission` + `require_permission` (STEP 2) is the right authorization
  mechanism and needs no change.
* STEP 5's AES-GCM credential envelope and STEP 5/6's webhook-receipt dedupe
  pattern are directly reusable.

## Direct answers to the questions the brief asked

**What plan information already exists?**
A free-text `plan` string and an `included_minutes` integer, unrelated to each
other, with no catalogue behind them.

**Is `minutes_used` authoritative or only a display counter?**
It is *used* as authority and *built* as a display counter. It has no history,
no period, no idempotency key, and no way to be recomputed.

**Where is call duration finalized?**
`call_state.apply_provider_status`, reached from the Twilio status callback.
That part is solid; billing just reads it wrongly.

**Can duplicate callbacks double-charge?**
No — `result.applied` guards that, and ten duplicates bill once. But *ordered*
callbacks under-charge (F1), which is the same class of bug pointing the other
way.

**Can multiple tenants interfere?**
Not in billing, because there is almost no billing. Every touch is on
`tenant.minutes_used` for a tenant already resolved from the verified Twilio
webhook.

**Does any Stripe code exist?**
None.

## What STEP 7 keeps

`Tenant.plan`, `included_minutes`, `minutes_used` and `is_active` all stay.
`minutes_used` is demoted from authority to **cache**, kept in sync so the
existing dashboard field and any external reader keep working, while the
authoritative number is derived from immutable usage events.
