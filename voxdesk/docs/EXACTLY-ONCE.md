# External Side Effects, Exactly-Once & Retry Recovery (Step 6)

Scope: how VoxDesk stays correct when requests, webhooks, providers and
workers all retry — and when processes crash between a local write and an
external one. This is the operating manual for the guarantees the code
enforces; it states precisely what is **effectively-once**, what is
**at-least-once with a bounded duplicate window**, and why.

The three invariants that matter:

1. A duplicate **valid** webhook is harmless.
2. A **retried job** does not repeat its external side effect.
3. An **ambiguous provider outcome** (timeout, crash) is reconciled or left
   visible for a human — never blindly retried into a duplicate.

---

## 1. The idempotency vocabulary

| Primitive | Where | What it guarantees |
|---|---|---|
| Deterministic idempotency key + unique constraint | `appointments.idempotency_key`, `crm_events.idempotency_key`, `usage_events.idempotency_key` | The same *business fact* records once, at the database, regardless of how many times it is submitted. |
| Slot lock | `appointments.slot_key` unique on `(tenant_id, slot_key)` | Two racing bookings for one slot cannot both persist. |
| Webhook receipt | `billing_webhook_receipts`, `crm_webhook_receipts`, `calendar_webhook_receipts`, `message_webhook_receipts` | A replayed inbound event is recognized and answered without re-processing. |
| Delivery claim | `crm_syncs` conditional `PENDING/FAILED → PROCESSING`, `reminders.claimed_at/claimed_by`, `knowledge_documents` `UPLOADED → PROCESSING` | Exactly one worker owns a piece of work at a time. |
| Provider idempotency key | Stripe `Idempotency-Key` on every POST; calendar `idempotency_key` in `EventRequest` | The provider itself dedupes a retried request. |
| Monotonic state application | `billing._apply_remote`, `telephony.call_state.apply_status` | A late or out-of-order event never regresses state. |

## 2. External side-effect inventory

| # | Operation | External resource | Exactly-once strategy | Classification |
|---|---|---|---|---|
| 1 | Inbound call answer (`/telephony/voice`) | Twilio | Signature verified; `call_sid` unique; duplicate delivery re-answers without a second row | SAFE |
| 2 | Media stream (`/telephony/ws`) | Twilio | Signed 120s token bound to `call_sid`; bounded handshake | SAFE |
| 3 | **Outbound dial** (`outbound.place_call`) | Twilio (a real phone call) | **Atomic attempt claim (compare-and-set on `lead.attempts`) committed before dialing** | SAFE (was AT_RISK) |
| 4 | Human transfer | Twilio `<Dial>` | Two-phase; in-flight guard on `transfer_state`; provider-verdict overrides inference | SAFE* |
| 5 | SMS send (reminders / owner notify) | Twilio Messages | **Durable send lease + reaper** | Effectively-once** |
| 6 | Inbound SMS/WhatsApp (`/channels/message`) | Twilio | **`MessageWebhookReceipt` replay protection + race-safe thread creation** | SAFE (was AT_RISK) |
| 7 | Calendar create/reschedule/cancel | Google / Microsoft / Cal.com / internal | DB slot lock; deterministic key; confirm requires `external_event_id`; timeout reconciled via `find_event_by_key` | SAFE |
| 8 | CRM contact/lead/note/appointment | HubSpot / GoHighLevel / Jobber / webhook | `CrmEvent` (deduped) + `CrmSync` per (event, integration) + **atomic claim** + reaper | SAFE (was AT_RISK) |
| 9 | Stripe customer/subscription/checkout | Stripe | Deterministic `Idempotency-Key` on every POST; webhook receipt; `_apply_remote` monotonic | SAFE |
| 10 | Stripe webhooks | Stripe | Signature verified in adapter; receipt dedupe; event-time ordering; allowlisted types | SAFE |
| 11 | Knowledge embedding | OpenAI (paid) | **Atomic `UPLOADED → PROCESSING` claim; delete-then-insert chunks keyed by version** | SAFE (was AT_RISK) |
| 12 | Outbound webhook (CRM push) | tenant's endpoint | Delivered via the CRM sync worker (row 8) | SAFE |
| 13 | Email | — | Not implemented (no email path exists) | — |
| 14 | Analytics / events | internal tables only | No external mutation | SAFE |

\* Transfer assumes one agent pipeline per call; retried tool calls are
sequential and are absorbed by the in-flight guard. Truly *concurrent*
same-call transfers are not produced by the pipeline and are documented as a
boundary.

\** See §6 for the exact wording.

## 3. The job worker guarantees (what Step 6 changed)

The scheduler (`scripts/scheduler.py`) runs six loops in one process. Three of
them previously used **check-then-act** claims — a `SELECT`, then a status
write — which is correct for one process but duplicates work under two
overlapping processes or a crash. Step 6 replaced each with a **single
conditional `UPDATE` whose rowcount arbitrates ownership**:

* **Reminders** — `reminders._claim` leases a row (`sent = false AND
  (claimed_at IS NULL OR claimed_at < now - lease)`) and commits the lease
  *before* sending. `release_stuck_reminders` reclaims leases abandoned by a
  dead worker.
* **Outbound** — `outbound._claim_attempt` is a compare-and-set on
  `lead.attempts`; the attempt and its backoff are committed *before* Twilio
  is asked to dial, so a crash right after Twilio accepts cannot produce a
  second dial.
* **CRM delivery** — `service.process_sync` claims `PENDING/FAILED →
  PROCESSING` with one `UPDATE`; a losing worker returns
  `claimed_by_another` without touching the provider.
* **Knowledge ingestion** — `ingest.claim_uploaded_document` claims
  `UPLOADED → PROCESSING`; a losing worker never runs extraction or pays for a
  second embedding.

The reapers (`reap_stuck_syncs`, `release_stuck_reminders`,
`reap_stuck_documents`) recover rows stranded by a crash; each is itself a
single atomic `UPDATE`, never a load-modify-commit (which a stale session
identity map can silently turn into a no-op).

## 4. Calendar exactly-once

The booking flow in `calendar/service.book` is deliberately ordered:

1. **Idempotency lookup** — same key ⇒ return the earlier appointment.
2. **Policy** — local, cheap, no network.
3. **Reserve the slot** — a `PENDING` row whose unique `slot_key` is the lock,
   inserted (via `begin_nested`) *before* any provider call.
4. **Provider create** — only the reservation winner reaches this.
5. **Confirm** — `CONFIRMED` + `external_event_id` are written together; there
   is no path to "confirmed" without provider acknowledgement.

An ambiguous timeout goes through `_create_with_reconciliation`, which asks
the provider whether our event already exists (`find_event_by_key`). If that
query itself fails, the original timeout is re-raised and the row is left
unconfirmed for a human — "could not check" is never treated as "absent".
Reschedule is provider-atomic where the provider supports it (Cal.com), and
never touches the local row until the provider accepted. Cancel is idempotent
and frees the slot even if the provider call fails (the error is kept for
staff reconciliation).

## 5. CRM exactly-once

A business fact is recorded **once** (`CrmEvent`, unique on
`(tenant_id, idempotency_key)`, key derived from the fact, never a random
uuid). Delivery is a separate `CrmSync` row per `(event, integration)`,
claimed atomically (§3) and finalized only with an `external_id`. Contact
creation is deduplicated by `CrmContactLink` on a salted identity hash, so the
tenth call from a number updates the first contact rather than creating a new
one. Enrichment (tags, notes) is best-effort and logged, never retried into a
duplicate note.

## 6. Billing & usage exactly-once

Stripe mutations carry a deterministic `Idempotency-Key`
(`customer:{tenant}`, `checkout:{tenant}:{plan}:{interval}`,
`upgrade:{tenant}:{plan}:{interval}`, `cancel:{tenant}:{immediately}`), so a
retry of the same *intent* is deduped by Stripe. A timeout during customer
creation re-reads the provider (`find_customer_by_tenant`) before ever
creating again. Webhooks are verified against the raw body, deduped by
`BillingWebhookReceipt` on `(provider, provider_event_id)`, filtered through a
type allowlist, and applied monotonically (`_apply_remote` refuses to move
state backwards), so out-of-order `customer.subscription.updated` vs
`checkout.session.completed` is handled.

Usage is immutable and append-only (`UsageEvent`, unique on
`(tenant_id, idempotency_key)`); call minutes are billed once from the final
absolute duration, keyed on the call id. Reconciliation is **read-only by
design**: it reports discrepancies and only rebuilds the derived summary
cache; correcting events requires a signed human `record_adjustment`.

## 7. Failure-recovery state machines

External mutations follow a bounded progression:

```
PENDING ──claim──▶ PROCESSING ──▶ SUCCEEDED        (calendar, CRM, knowledge)
                     │  └──────▶ RETRYABLE_FAILURE ──▶ (backoff) ──▶ PROCESSING
                     └─────────▶ PERMANENT_FAILURE (misconfigured, unsupported, auth)
```

and for reminder sends:

```
UNSENT ──claim(lease)──▶ LEASED ──send──▶ SENT
              ▲                              │
              └── reaper (lease expired) ────┘ (crash between send and commit)
```

Ambiguous outcomes (timeout after the provider may have accepted) are never
marked permanent: they reconcile (calendar, Stripe customer) or stay
retryable with a recorded error (CRM), so a retry goes through the idempotency
primitive instead of blind repetition.

## 8. Webhook handling

| Webhook | Signature | Replay protection | Ordering |
|---|---|---|---|
| `/telephony/*` | Twilio HMAC (`stream_auth`) | `call_sid` unique + terminal-state guard | `call_state` refuses regressions |
| `/channels/message` | Twilio HMAC | `MessageWebhookReceipt` | thread reuse by idle window |
| `/billing/webhooks/*` | Stripe signature (raw body) | `BillingWebhookReceipt` | `_apply_remote` monotonic + event time |
| `/api/integrations/crm/webhook` | provider HMAC where supported | `CrmWebhookReceipt` | n/a (one state change) |
| `/api/calendar/webhook` | provider HMAC where supported | `CalendarWebhookReceipt` | n/a (one state change) |

Receipts are pruned by age (`prune_receipts` on each channel, wired into the
scheduler's daily retention loop via `core.retention.prune_webhook_receipts`),
never kept forever; the prune window exceeds any plausible redelivery window
(30 days for CRM/calendar/messaging, 60 for billing because Stripe retries a
failing endpoint for up to three days).

## 9. Precisely stated guarantees

* **Effectively-once under the supported provider contract**: calendar
  booking (provider idempotency key + slot lock + reconciliation), CRM
  delivery (event dedupe + atomic claim + contact link), Stripe mutations
  (`Idempotency-Key` + webhook receipt + monotonic apply), usage/billing
  events (unique idempotency key).
* **Effectively-once external mutation with a bounded duplicate window**:
  outbound dials (claim committed before dialing; a crash after Twilio
  accepts leaves the attempt recorded and not re-dialed), reminder SMS
  (lease + reaper; the residual window is the lease duration around a crash
  between "Twilio accepted" and "we recorded `sent`" — Twilio's Messages API
  has no idempotency key, so this is the ceiling, not a choice).
* **At-least-once, human-visible**: anything left `PENDING`/`FAILED` with a
  recorded `last_error` after an ambiguous provider outcome, which staff
  reconcile from the dashboard.

Nothing here claims mathematical exactly-once where the provider only offers
at-least-once delivery.

## 10. Known provider limitations

* **Twilio Messages** has no idempotency key; SMS is at-least-once. Reminder
  sends minimize the duplicate window with a lease (§6, §9).
* **Twilio voice callbacks** redeliver and reorder; `call_state` and
  idempotency-keyed billing make them harmless.
* **GoHighLevel / HubSpot / Jobber** offer no cross-request idempotency;
  contact-level dedupe comes from `CrmContactLink`, and note/tag enrichment is
  best-effort (a note that already landed is not re-created because a sync is
  finalized only once).
* **Stripe** keys are honored for 24h in-process; a retry after that window
  is still safe because the local webhook receipt and `_apply_remote` are the
  authority, not the Stripe key alone.
