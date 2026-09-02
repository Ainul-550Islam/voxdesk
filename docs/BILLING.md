# Billing, Stripe and usage metering

> **Live verification status.** No Stripe API call was made with real
> credentials — not even test-mode ones. See [§13](#13-what-was-not-live-tested)
> for exactly what that means and what was verified instead.

---

## 1. Architecture

```
  customer
    │
    ▼
  BillingPlan          the catalogue -- server-owned, the trust boundary
    │
    ▼
  Subscription         mirrored from the provider, never authoritative alone
    │
    ▼
  UsageEvent           immutable, idempotency-keyed, append-only
    │
    ▼
  UsageSummary         a derived cache; always rebuildable from events
    │
    ├──▶ entitlements  checked BEFORE the expensive action
    └──▶ invoice       provider-issued, mirrored read-only
```

| Path | Responsibility |
|---|---|
| `billing/plans.py` | Catalogue, seeding, `resolve_price_id` |
| `billing/periods.py` | Billing periods, month arithmetic, UTC |
| `billing/metering.py` | Usage events, idempotency, overage |
| `billing/entitlements.py` | May this tenant do this, right now |
| `billing/service.py` | Customer, checkout, plan change, cancel, reconcile |
| `billing/webhooks.py` | Verified event processing, dedupe, ordering |
| `billing/reconciliation.py` | Discrepancy detection |
| `billing/hooks.py` | Where business events become usage |
| `billing/base.py` | Provider interface, capabilities, HTTP entry point |
| `billing/providers/` | `stripe.py`, `manual.py` |
| `api/billing_routes.py` | The API and the webhook endpoint |

### Adding a provider

1. Write `providers/foo.py` subclassing `BillingProvider`.
2. Add `FOO` to `BillingProviderType` **and to migration `0008`'s enum list**
   (the parity test enforces this).
3. Register it in `registry.py`.

The contract tests in `tests/test_billing_subscriptions.py` are parameterized
over the registry, so a new adapter inherits the suite immediately.

---

## 2. Plans

| Plan | Monthly | Voice min | SMS | Overage/min | Overage? | Trial |
|---|--:|--:|--:|--:|:--:|--:|
| Trial | $0 | 60 | 50 | — | **no** | 14 days |
| Starter | $199 | 500 | 500 | 12¢ | yes | — |
| Pro | $499 | 2,000 | 2,500 | 10¢ | yes | — |
| Enterprise | $1,499 | 10,000 | 10,000 | 8¢ | yes | — |

Overage rates are grounded in the platform pricing researched in STEP 4: an
all-in voice minute costs roughly $0.10–0.30 at the infrastructure layer, so a
rate below that would be sold at a loss. The 12¢→8¢ curve is a normal volume
discount with margin at every tier.

**A trial that can run up an overage bill is not a trial**, so Trial has
`overage_enabled = false` and exceeding the allowance refuses the action
rather than metering it.

### Money representation

* Plan prices: **integer cents**.
* Overage rates: **integer hundredths of a cent** (millicents).

A float `19.99` is not exactly 19.99, and accumulating fractional overage in
binary floating point produces invoices that do not reconcile with themselves.
Millicents exist because token pricing is genuinely below one cent per unit —
$2 per million tokens is 0.0002¢, which cents cannot express, and rounding each
token to a cent would overcharge by four orders of magnitude.

### Seeding

Plans are seeded at startup by `sync_seed_plans()`, **not** by a migration.
Prices are business data that changes; repricing should be an operator action
against a running system, not a schema change followed by a deploy.

Seeding **never overwrites `provider_price_ids`** or money columns on an
existing row. An operator who repriced Pro in production must not have that
undone by the next deploy — that is an outage where every customer's next
invoice is wrong.

---

## 3. Entitlements

Route and agent code asks the entitlement service. There is no
`if tenant.plan == "pro"` anywhere in the repository.

```python
context = await load_context(session, tenant)
entitlement = await check_voice_minutes(session, context, estimated_seconds=120)
if not entitlement.allowed:
    ...   # entitlement.message is written to be shown to a person
```

| Decision | Meaning |
|---|---|
| `ALLOW` | Under the limit |
| `WARN` | Past 80%, still allowed |
| `ALLOW_WITH_OVERAGE` | Past 100%, plan meters overage |
| `DENY` | Refused |

Non-metered entitlements — `team_members`, `rag_documents`,
`crm_integrations`, `calendar_integrations`, `concurrent_calls`,
`outbound_calls_per_day`, `whatsapp`, `outbound_campaigns`, `api_access` —
live in `feature_entitlements` JSON, validated against an allowlist on write.
`-1` means unlimited.

### Three deliberate softenings

**`PAST_DUE` still entitles service.** Stripe is still retrying the card.
Cutting a business's phone line off on the first failed charge costs far more
goodwill than the few days of service it saves, and the customer usually does
not know their card expired until we tell them.

**A tenant with no subscription is not blocked.** Every tenant created before
STEP 7 is in that state (requirement 25). They fall back through
`subscription.plan → tenant.plan (legacy string) → the default plan`, which is
how a pre-STEP-7 tenant marked `plan="pro"` keeps the Pro entitlements they
were sold.

**A missing catalogue degrades open.** If no plan can be resolved at all —
unseeded, or a row deleted under a live subscription — the check *allows* and
logs `billing.no_plan_resolved` at error level. That is our failure, and
denying service for it punishes the customer for our bookkeeping.

### Inbound calls are never blocked

The pre-STEP-7 code hung up on inbound callers when a tenant was over quota.
That was removed, and not as an oversight:

* It compared a **lifetime** counter against a **monthly** allowance, so a
  tenant on 500 minutes got 750 minutes *ever* and was then permanently
  answered with *"This account has reached its usage limit"*
  (`docs/BILLING-AUDIT.md` F2).
* It was the wrong lever. Hanging up on a dentist's patients because the
  dentist owes forty dollars punishes the one party who has no way to fix it.

Entitlement is enforced where the spend originates — outbound calls via
`hooks.may_place_outbound_call`, and account-level features. `tenant.is_active`
remains the deliberate off switch for an account that genuinely must stop.

---

## 4. Usage metering

`UsageEvent` is **immutable, append-only and idempotency-keyed**. It is the
financial record of truth; `UsageSummary` is a cache of it, and
`Tenant.minutes_used` is a cache of that.

```
metric        smallest unit    plan is written in
voice_minute  second           minutes
sms_segment   segment          segments
llm_token     token            tokens
tts_character character        characters
```

Seconds, not minutes. Summing a few thousand floats produces a total that does
not match the sum of its parts, and an invoice that disagrees with its own line
items cannot be defended.

### The idempotency key

```
voice_minute:3f2a1b4c-0000-0000-0000-000000000000
llm_token:3f2a1b4c-...:turn:7
```

Derived from the business fact, never generated per attempt. Requirement 12 is
explicit that a random UUID per callback is not duplicate protection — it is
the opposite, because every retry then looks like new usage. And it is legible
rather than hashed: the person reading it is usually looking at a disputed
invoice.

`UNIQUE (tenant_id, idempotency_key)` is the guarantee. The pre-insert
`SELECT` is an optimisation.

> **Implementation note.** `session.add()` goes **inside** the `begin_nested()`
> savepoint. Outside, the object stays pending after the savepoint rolls back
> and is re-flushed by the next statement, so the loser of a race raises
> `PendingRollbackError` and cannot even read back who won. Measured: with
> `add` outside, two of three concurrent workers ended with an unusable
> session.

---

## 5. Voice-minute billing

**One call = one usage event, keyed on the call id, recorded once at
finalization from the final absolute duration.**

The pre-STEP-7 code billed a *delta*:
`minutes_used += duration - previous_duration`. Duplicates were handled, but
ordering was not. Measured against the real state machine, one 120-second
call:

| Callback sequence | Billed |
|---|--:|
| `completed(120s)` | 2.00 min ✅ |
| `in-progress(0s)`, `completed(120s)` | 2.00 min ✅ |
| `in-progress(60s)`, `completed(120s)` | **1.00 min** ❌ |
| `answered(90s)`, `completed(120s)` | **0.50 min** ❌ |
| `completed(120s)` × 10 | 2.00 min ✅ |

Twilio genuinely sends those intermediate callbacks, and on a transferred call
the legs report out of order. The error was *under*-billing, so nobody
complained and it was never found. `tests/test_billing_voice.py` replays every
sequence and asserts 120 seconds each time.

### Transfer policy

**Total customer-visible call duration, not per provider leg.**

A transferred call is one call from the customer's point of view and one line
on the invoice. Billing the parent leg and the dial leg separately would
roughly double the charge for every escalated call, and the customer has no way
to see why. The event records `metadata.transferred` so the single-charge
policy is auditable.

### Rounding

* Duration is **floored** to whole seconds per call. Rounding up on average is
  a systematic overcharge across thousands of calls that nobody consented to.
* Overage is rounded **up to whole minutes on the period total**, once.

Two hundred 20-second calls is **67 billable minutes, not 200**. Per-call
rounding is legal and common, and it is also the single most frequent cause of
a telephony billing dispute.

---

## 6. Overage

```
included = 500 min      used = 620 min
overage  = 120 min  ->  120 x 10¢ = $12.00
```

Rates come from the plan. Nothing in `app/telephony` or `app/agent` knows a
price.

Adjustments (requirement 35) are **new rows with a negative quantity**, never
edits. The original figure survives next to the correction, which is the only
way an invoice dispute is answerable. A negative period total is clamped at
the reporting boundary but not in storage, so reconciliation can still see that
something went wrong.

---

## 7. Billing periods

**A period comes from the subscription, not the calendar.** A tenant who
subscribed on the 15th is billed the 15th to the 15th; rolling their usage up
by calendar month would split every period across two buckets and make the
invoice unreconcilable with the usage page.

* Everything is **UTC**. The label `2026-03` means "the period that *started*
  in March UTC".
* Periods are **half-open** `[start, end)`, so the instant one ends belongs to
  the next and no call is billed twice.
* Month arithmetic **clamps**: `Jan 31 + 1 month = Feb 28`, not `Mar 3`.
  Getting that wrong moves a customer's renewal date permanently, one month at
  a time.
* DST is a non-issue *because* everything is UTC — an anchor stored as a local
  wall clock would shift twice a year and eventually cross a day boundary.

A tenant with no subscription falls back to the calendar month. Between a
renewal happening and its webhook arriving, the anchor is walked forward so
calls are filed into the new period rather than a closed one.

---

## 8. Stripe

### Setup

```
1. Dashboard -> Developers -> API keys        -> STRIPE_SECRET_KEY
2. Products -> create a product per plan, with monthly and annual prices
3. Copy each price id into the plan's provider_price_ids:
     {"month": "price_FAKE_pro_month", "year": "price_FAKE_pro_year"}
4. Developers -> Webhooks -> add endpoint
     https://your-host/api/billing/webhook/stripe
   Events: checkout.session.completed, customer.subscription.created,
           customer.subscription.updated, customer.subscription.deleted,
           invoice.paid, invoice.payment_failed
5. Copy the signing secret        -> STRIPE_WEBHOOK_SECRET
```

*All ids in this document are fake.*

### Written against REST, not the SDK

The `stripe` package is synchronous, this codebase is async throughout, and
requirement 5 says core billing logic must not depend on Stripe SDK classes —
far easier to guarantee when they are not importable.

### Two version-drift defences

**Period bounds moved.** Stripe relocated `current_period_start`/`_end` from
the subscription onto its items in a 2025 API version. The adapter reads the
root and falls back to the first item, because the API version is set
per-account and we do not control it.

**Unmapped statuses.** A status we do not recognise becomes `INCOMPLETE` — it
grants no service and cancels nobody, which is the safe reading of "Stripe
changed something".

### Status mapping

| Stripe | VoxDesk |
|---|---|
| `trialing` | `TRIALING` |
| `active` | `ACTIVE` |
| `active` + `cancel_at_period_end` | **`CANCELING`** |
| `past_due`, `unpaid` | `PAST_DUE` |
| `canceled` | `CANCELED` |
| `incomplete` / `incomplete_expired` | same |
| `paused` | `PAUSED` |
| anything else | `INCOMPLETE` |

`CANCELING` is ours: Stripe expresses "winding down" as `active` plus a
boolean, which loses the distinction every UI needs. This table is the only
place Stripe status strings appear.

### Idempotency toward Stripe

Every mutating call carries `Idempotency-Key`, derived from the operation:

```
customer:{tenant_id}
checkout:{tenant_id}:{plan}:{interval}
upgrade:{tenant_id}:{plan}:{interval}
```

It is the difference between a timeout costing a retry and a timeout costing a
customer a second subscription.

---

## 9. Subscription lifecycle

### Checkout

```
POST /api/billing/checkout   {"plan_code": "pro", "interval": "month"}
                          -> {"url": "https://checkout.stripe.com/..."}
```

**The whole input surface is a plan code and an interval.** No amount, no
currency, no price id, no tenant id — `CheckoutIn` has exactly two fields, so
`POST {"price": "price_one_cent"}` is rejected by the model before it reaches
a handler.

**Creating a session establishes nothing.** The local row stays `INCOMPLETE`.
A browser reaching the success URL has proved only that it reached a URL.

Even `checkout.session.completed` does not set `ACTIVE` from the session body:
the session says a payment page was completed; the *subscription* object says
what state it is in, and those differ under 3DS or a failed initial charge. So
the handler links the subscription id and asks the provider.

### Plan changes

**Upgrade → immediate**, with `proration_behavior=create_prorations`. The
customer gets the larger allowance now and Stripe issues a prorated line.
Making someone wait after they have asked to pay more is the wrong side to err
on.

**Downgrade → scheduled for the period end**, recorded in `pending_plan_id`,
with **no proration and no refund**. Applying it immediately would strand a
customer who has already used more than the smaller plan includes, and
refunding mid-period is an accounting system this step deliberately does not
build. Reversible: asking for the current plan again clears the pending change.

### Cancellation

Defaults to **period end**. The customer has paid for the month; cutting them
off the moment they click cancel takes money for service not delivered.
Idempotent — a second click does not hit the provider again, because the audit
trail would then show two cancellations for one decision.

If the *provider* cancel fails, the local intent is still recorded. The
customer asked to cancel; forgetting that because a vendor API was slow means
they ask again and get charged again.

---

## 10. Webhooks

```
POST /api/billing/webhook/stripe
```

**Verification happens on the raw bytes, before anything else.** The route
reads `await request.body()` and never `request.json()` — a re-serialized body
changes the bytes and every signature check becomes meaningless. There is a
test asserting `request.json()` does not appear in the file.

The scheme, implemented to the published spec:

* signed payload is `{t}.{raw_body}`;
* HMAC-SHA256, hex, keyed on the `whsec_` secret;
* **every** `v1` value is checked — Stripe sends several during a
  signing-secret rotation, and accepting only the first would drop valid
  events for the whole rotation window;
* unknown schemes (`v0`) are ignored, not failed;
* the timestamp is inside the signed payload and is rejected outside 300s,
  which is what stops a captured request being replayed;
* `hmac.compare_digest`, because `==` leaks the prefix through timing.

A verification failure returns **400, not 401**: Stripe treats 4xx as permanent
and stops retrying, which is right for a request that will never verify.

### Deduplication and ordering

`BillingWebhookReceipt` is unique on `(provider, provider_event_id)`. Here a
duplicate has financial consequences, so the constraint is not an optimisation.

Stripe makes **no delivery-order guarantee**. `customer.subscription.updated`
routinely arrives before `checkout.session.completed`, and a retried old event
can land after a newer one. Every state write goes through `_apply_remote`,
which compares the event's `created` timestamp against
`provider_updated_at` and refuses to go backwards. Applying a stale event would
downgrade a customer who just upgraded, or resurrect a cancelled subscription.

### Tenant attribution

Four routes, tried in order of reliability:

1. `metadata.voxdesk_tenant_id`, stamped by us at checkout **and** on the
   subscription — the only one that works before any local id is stored.
2. The provider subscription id.
3. The provider customer id.
4. For an invoice, the subscription it references.

A `tenant_id` that does not resolve to a real tenant is **ignored**. The
metadata is ours, but the event body is not.

---

## 11. Reconciliation

`reconcile_tenant()` reports; it does not fix. The only mutation offered is
`rebuild_summaries`, which recomputes a **cache** from immutable events.
Correcting the events themselves is a signed `record_adjustment` made by a
human who has read the report — requirement 34's "never silently fix financial
records without an audit trail".

| Discrepancy | Cause | Severity |
|---|---|---|
| `summary_drift` | crash between writing an event and rebuilding | warning (**financial** if the period is finalized) |
| `negative_usage` | a double-issued credit | financial |
| `orphan_usage` | usage referencing a deleted call | financial |
| `cross_tenant_usage` | charging one business for another's call | financial |
| `period_mismatch` | a subscription period changed after usage was recorded | warning |

A **finalized** period is reported but never rebuilt: those figures are what
was invoiced, and quietly changing them would leave our records and the
customer's disagreeing with no trace.

Runs hourly in `scripts/scheduler.py`. It deliberately does **not** contact a
billing provider, so a Stripe outage cannot stall the worker every other loop
shares.

---

## 12. Security

| Rule | How |
|---|---|
| No Stripe secret in a response | `BillingStatusOut`/`PlanOut`/`InvoiceOut` are allowlists; a test asserts no field is even *named* like a secret |
| No secret in logs | `errors.safe_message` scrubs every stored string; a 401 body is excluded entirely rather than scrubbed |
| No secret in the audit trail | `detail` carries plan codes and outcomes only |
| No secret in a traceback | `BillingContextConfig.__repr__` prints booleans |
| No price from a client | `CheckoutIn` has two fields; `resolve_price_id` is the only source |
| No customer id from a client | the portal endpoint takes no body |
| Tenant isolation | every query filters on `tenant_id` from the verified JWT; `UNIQUE (provider, external_subscription_id)` stops a webhook matching the wrong row |

Permissions: `BILLING_READ` (owner, admin), `BILLING_WRITE` (owner only). An
admin needs to know why a limit was hit; only an owner may change what the
company is charged.

### Production startup

`Settings.validate_security()` refuses to boot when:

* `BILLING_PROVIDER=stripe` and `STRIPE_SECRET_KEY` is unset;
* `BILLING_PROVIDER=stripe` and `STRIPE_WEBHOOK_SECRET` is unset — without it
  the webhook cannot verify signatures and anyone could forge a subscription;
* production is running a `sk_test_` key, which would take no real payments;
* `BILLING_UNLIMITED_ENTITLEMENTS` is on in production.

And `main.py` refuses to start if an active priced plan has no provider price
id — a failure that would otherwise surface in front of a customer holding a
credit card.

### Local development

```bash
BILLING_PROVIDER=manual        # the default
```

Manual is a real mode, not a stub: plans, entitlements, metering, periods and
overage all work. Only the payment rail is absent. It is also the correct
setting for an invoice-me contract.

```bash
# To exercise Stripe locally
BILLING_PROVIDER=stripe
STRIPE_SECRET_KEY=sk_test_FAKE...
STRIPE_WEBHOOK_SECRET=whsec_FAKE...
BILLING_CHECKOUT_SUCCESS_URL=http://localhost:5173/billing/ok
BILLING_CHECKOUT_CANCEL_URL=http://localhost:5173/billing/cancel
BILLING_PORTAL_RETURN_URL=http://localhost:5173/billing
stripe listen --forward-to localhost:8000/api/billing/webhook/stripe
```

---

## 13. What was not live-tested

**No Stripe API call was made, with any credentials.** The adapter is written
against the published REST contract and exercised against a scripted HTTP
transport that intercepts `httpx` — so header assembly, form encoding,
idempotency keys, status classification and timeout handling are all real code
paths, but no byte left the machine.

Not verified live:

* **Every Stripe endpoint**: `/customers`, `/customers/search`,
  `/subscriptions` (create, update, cancel, list), `/invoices`,
  `/checkout/sessions`, `/billing_portal/sessions`, `/prices`.
* **The `Idempotency-Key` header's actual behaviour** on a real retry.
* **Real webhook delivery**, retries and out-of-order arrival. The signature
  logic is verified against its own reference implementation — which proves
  internal consistency, not that Stripe produces the same bytes.
* **Proration** on a live upgrade. The parameter is sent; the resulting
  invoice line has not been seen.
* **3DS / `default_incomplete`** payment flows.
* **PostgreSQL.** All tests run on SQLite; `alembic upgrade head` has not been
  executed. Migration/model parity is checked by static AST parsing.

What *was* verified: request construction and payload shape for both
providers, response normalization, status mapping including the
`cancel_at_period_end` special case and the 2025 period-field relocation,
error classification, webhook signature verification (valid, wrong secret,
tampered body, re-serialized body, stale and future timestamps, rotation with
multiple `v1`, unknown schemes, malformed headers), webhook dedupe and
out-of-order handling, usage idempotency under genuine concurrency on separate
database connections, overage arithmetic, period boundaries, entitlements,
tenant isolation, secret containment, and the full API. **281 tests.**

---

## 14. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Boot fails with `STRIPE_WEBHOOK_SECRET is required` | correct — without it anyone could forge a subscription |
| Boot fails with `plan 'pro' has no monthly provider price id` | copy the price id from the Stripe dashboard into `provider_price_ids` |
| Checkout returns 404 | the plan code is unknown *or* inactive — deliberately the same error |
| Checkout returns 502 | Stripe was unavailable. The customer is correctly **not** told the payment completed |
| Subscription stuck `INCOMPLETE` | the webhook is not arriving, or 3DS was not completed. Check Developers → Webhooks |
| Usage looks half what it should | pre-STEP-7 symptom (audit F1). Should be impossible now; if it recurs, check that `on_call_finalized` is reached |
| A customer is billed twice for one call | should be impossible — `uq_usage_event_idempotency` is the guarantee. If it happens, that constraint is missing from the database |
| `billing.no_plan_resolved` in the logs | the catalogue is unseeded or a plan row was deleted. Service is degrading open |
| Reconciliation reports `summary_drift` persistently | the rebuild loop is not running |
| Reconciliation reports `negative_usage` | a credit was issued twice. Financial — investigate before the next invoice |
| Webhook returns 400 for everything | wrong `STRIPE_WEBHOOK_SECRET`, or a proxy re-serializing the body |
| A customer over their limit still makes calls | expected for inbound (§3) and for any plan with overage enabled |

Useful queries:

```sql
-- What a tenant will be billed this period
SELECT metric, used_quantity, included_quantity, overage_quantity,
       estimated_overage_millicents / 100.0 AS overage_cents
FROM usage_summaries
WHERE tenant_id = :tenant AND billing_period = '2026-09';

-- Re-derive the authoritative figure from events
SELECT metric, sum(quantity) FROM usage_events
WHERE tenant_id = :tenant AND billing_period = '2026-09'
GROUP BY metric;

-- Every adjustment ever made, with who and why
SELECT created_at, metric, quantity, event_metadata
FROM usage_events WHERE event_type = 'MANUAL_ADJUSTMENT'
ORDER BY created_at DESC;

-- Subscriptions that never completed checkout
SELECT tenant_id, created_at FROM subscriptions
WHERE status = 'INCOMPLETE' AND created_at < now() - interval '1 hour';

-- Webhook events that failed and can be replayed
SELECT provider_event_id, event_type, last_error FROM billing_webhook_receipts
WHERE processed = false;
```

---

## 15. Limitations

1. **No Stripe metered-billing integration.** Overage is computed locally and
   surfaced in the API; it is not pushed to Stripe as usage records, so an
   overage invoice would have to be raised manually or by a later step.
2. **No proration arithmetic of our own.** Upgrades delegate to Stripe's
   `create_prorations`; downgrades are scheduled with none. Deliberate —
   requirement 18 forbids ambiguous proration rules.
3. **No dunning beyond the provider's.** `PAST_DUE` is recorded and service
   continues; there is no escalation schedule or account suspension.
4. **No tax handling.** Stripe Tax would be a separate integration.
5. **Refunds are references, not operations.** `record_adjustment` corrects
   *usage*; issuing an actual refund is done in the Stripe dashboard.
6. **Period finalization is triggered by an observed renewal**, so a tenant
   whose webhooks stop arriving keeps an open period until reconciliation or a
   manual reconcile runs.
7. **`Tenant.minutes_used` and `included_minutes` still exist** as caches and
   legacy fallbacks. Removing them is a separate announced change.
8. **The reconciliation worker compares local records to themselves.** It
   detects internal inconsistency, not disagreement with Stripe's own totals —
   that needs the metered-billing integration in (1).
