# Real End-to-End Voice Call — Operator Runbook (Step 5)

> **Status: `REAL TELEPHONY E2E: NOT AUTOMATICALLY EXECUTED`**
>
> This runbook documents how a **human operator** performs one genuine
> Twilio media-stream call against a dedicated test tenant. Nothing in the
> codebase places this call automatically, and no automated test will ever dial
> a real number or reach a production customer. The final report for Step 5 must
> keep the `NOT AUTOMATICALLY EXECUTED` banner unless a human-triggered test was
> actually performed *and evidenced* in `docs/REAL-E2E-RESULT-RECORD.md`.

---

## 1. What this exercise proves

The automated test suite can drive the webhooks, the pipeline, the tools, the
billing hooks, and the CRM hooks against an in-memory database — but it cannot
prove that **one real phone call** flows end-to-end through Twilio:

```
caller ──PSTN──▶ Twilio ──webhook──▶ /telephony/voice
                       └─media WS──▶ /telephony/ws ──▶ STT → LLM → TTS
                                                         │
                     tools (calendar / CRM / transfer) ◀─┘
```

This runbook turns that into a repeatable, safe, human-controlled procedure.

## 2. The safety guard (read this first)

The guard lives in `app/telephony/e2e_guard.py` and is wired into
`/telephony/voice`. Its rules, in order:

1. **Never places a call.** It only classifies an *incoming* webhook.
2. **Disarmed = invisible.** With `E2E_ENABLED=false` it is a no-op and the
   product behaves exactly as before. This is also the "E2E flag absent"
   rejection: there is **no** E2E test mode without the flag, so a forgotten
   flag can never silently produce an "E2E-validated" result — checkpoint 3
   (`call.e2e_allowed`) simply never appears and the run is recorded as
   `BLOCKED`, not `PASS`.
3. **Armed = test-only.** With `E2E_ENABLED=true`, the *only* call that may pass
   is one where:
   - the environment is **not** production (`APP_ENV != production`);
   - the dialed number (`To`) equals `E2E_TEST_NUMBER`;
   - the caller (`From`) is in `E2E_ALLOWED_CALLERS`;
   - the resolved tenant exists and has `is_test_tenant = true`;
   - the tenant's `twilio_number` equals `E2E_TEST_NUMBER`.
4. **Fail-closed.** Any violation is refused at the door — *before* a `Call`
   row, session, media stream, usage event, or audit record can exist. If the
   guard is armed but misconfigured, `/telephony/voice` returns `503` rather
   than answering.
5. **Secret-free.** Rejection messages never contain a phone number, tenant
   name, or configuration value.

The app **refuses to boot in production** while `E2E_ENABLED=true`
(`Settings.validate_security()`).

## 3. Prerequisites

- A **non-production** environment (staging/dev) that can reach Twilio.
- A Twilio account with a **dedicated test phone number** (the "test number").
- A phone in your physical control to dial from (the "operator phone").
- Database access to create the test tenant and flip its flag.
- `twilio` CLI (optional) or the Twilio console to confirm webhook URLs.

## 4. One-time setup

### 4.1 Provision the test tenant

Create a tenant that is visibly and permanently a test tenant:

```sql
INSERT INTO tenants (id, name, industry, twilio_number, is_test_tenant, is_active, ...)
VALUES (gen_random_uuid(), 'E2E TEST — do not bill', 'test',
        '<E2E_TEST_NUMBER>', TRUE, TRUE, ...);
```

Or in the app shell:

```python
from app.db.models import Tenant
# ... create a Tenant with is_test_tenant=True and twilio_number=<E2E_TEST_NUMBER>
```

Guidance (these are **test-only** values, never production credentials):

- Calendar: point at a throwaway Google calendar / free-busy sandbox.
- CRM: point `crm_webhook_url` at a request bin (e.g. a local sink), **never** a
  real client CRM.
- Billing: the test tenant is on the normal `starter` plan; its usage is
  isolated to the test tenant by tenant scoping and is never invoiced by a
  human-triggered test as long as the test tenant is the only actor.
- Knowledge/AI config: use whatever presets you need to validate the chain; the
  tenant is marked `is_test_tenant` so it can never be mistaken for a client.

### 4.2 Environment flags (non-production only)

```
APP_ENV=development            # or staging — never "production"
E2E_ENABLED=true
E2E_TEST_NUMBER=<the dedicated test Twilio number, E.164>
E2E_ALLOWED_CALLERS=<operator phone E.164, comma-separated>
```

Boot the app and confirm it starts (it will refuse to start with these set in
production). Optionally confirm:

```
curl -s localhost:8000/health            # -> {"status":"ok"}
```

### 4.3 Point the test number at the voice webhook

In the Twilio console, set the test number's **Voice → A call comes in** webhook
to:

```
https://<your-staging-host>/telephony/voice
```

Method `POST`. Leave signature validation **on**.

## 5. Performing the manual E2E call

> A human dials the number. Do not automate this step.

1. Open the app logs tailed for the call's correlation id:
   `tail -f <log> | grep -E "call.incoming|e2e|turn|tool|finalized"`.
2. From the **operator phone**, dial `<E2E_TEST_NUMBER>`.
3. Walk through, and note the result of, every checkpoint in §6.
4. After you hang up, wait for the status webhook and post-call processing to
   settle (a few seconds), then record everything in
   `docs/REAL-E2E-RESULT-RECORD.md`.

## 6. Checkpoint list (the full live path)

Tick each one as `PASS`, `FAIL`, `BLOCKED`, or `NOT_TESTED` in the result record.

| # | Checkpoint | What to verify |
|---|------------|----------------|
| 1 | Twilio webhook | `/telephony/voice` received the POST; Twilio signature accepted. |
| 2 | Tenant resolution | The call resolved to the **test tenant** (log shows its name). |
| 3 | E2E guard | Log shows `call.e2e_allowed` with `test_tenant=<name>`. |
| 4 | Call/session creation | Exactly one `calls` row for this `call_sid`. |
| 5 | Media stream | Twilio opened `/telephony/ws`; the handshake completed. |
| 6 | STT/TTS/LLM init | No provider error; the configured providers initialized. |
| 7 | System prompt | The AI greeted with the tenant's greeting + AI disclosure. |
| 8 | First utterance | Your speech was transcribed (check transcript rows). |
| 9 | LLM response | A turn was generated and spoken back. |
| 10 | Tool call/result | A tool (e.g. `check_availability`) ran and its result was used. |
| 11 | TTS | Spoken output matched the configured voice/language/speed. |
| 12 | Barge-in | Speaking over the AI interrupts the current utterance. |
| 13 | Multi-turn | At least two exchanges completed without hanging. |
| 14 | Transfer (optional) | Human transfer flow ends in `TRANSFERRED` (or `FAILED`) exactly once. |
| 15 | Booking (optional) | A booking on the **test** calendar was created/cancelled correctly. |
| 16 | Termination | The call reached **exactly one** terminal state. |
| 17 | Turn persistence | All turns persisted; no duplicates. |
| 18 | Usage accounting | Usage events exist exactly once per idempotency key. |
| 19 | Billing/metering | No double charge; test tenant usage isolated. |
| 20 | Audit logging | Audit rows present and tenant-scoped. |
| 21 | Post-call processing | CRM hook / post-call cleanup ran once, idempotently. |
| 22 | Observability | Logs/metrics carry the call's correlation id end-to-end. |

## 7. Negative checks (safety behaviour to verify by hand)

Do these **before** the main call to confirm the guard, then record results:

1. With `E2E_ENABLED=true`, dial the test number from a **non-allowlisted**
   phone → the call must be answered with the "test mode … not authorized"
   message and hang up, and **no** call row may be created.
2. With `E2E_ENABLED=true`, temporarily point the webhook at a second number
   that maps to a normal (non-test) tenant and dial it → the call must be
   refused (armed mode is test-only).
3. Confirm the app **refuses to boot** with `E2E_ENABLED=true` and
   `APP_ENV=production`.

## 8. Teardown (after the test)

1. Set `E2E_ENABLED=false` and restart the app.
2. Keep the test tenant `is_active=false` (or delete it) so it can never answer
   production traffic.
3. Remove the webhook override from the test Twilio number if it was temporary.
4. Confirm the result record is filled in and dated.

## 9. Emergency stop

If anything behaves unexpectedly during a live test:

1. Hang up the operator phone immediately.
2. Set `E2E_ENABLED=false` and restart the app — the guard is the only thing
   allowing test traffic; turning it off restores normal behaviour.
3. Optionally deactivate the test tenant (`is_active=false`) as a second switch.
