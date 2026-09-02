# VoxDesk — Call Lifecycle & Human Transfer

Scope: how a call moves through its states, and what happens when the AI hands
a caller to a human. Authentication and tenant isolation are covered in
[`AUTH.md`](AUTH.md); this document assumes them.

---

## 1. The problem this replaced

Before this change, `escalate_to_human` did essentially nothing:

```python
async def escalate_to_human(self, reason: str) -> dict:
    self.call.escalated = True
    self.call.intent = "escalation"
    ...
    return {"ok": True, "action": "transfer", ...}
```

A comment claimed *"The pipeline watches for `action == "transfer"` and issues
a real Twilio `<Dial>`"*. It did not. `transfer.execute_transfer` had **zero
callers** in the entire repository. The AI said *"let me put you through"*, the
caller waited, and nothing happened.

Call status was equally loose. The status webhook did:

```python
call.status = _TERMINAL_STATUS.get(CallStatus_, CallStatus.COMPLETED)
```

which meant an unrecognised status string silently became a successful
completion, and a duplicate or late callback happily overwrote `TRANSFERRED`.
That webhook also had **no signature verification**, so anyone who could reach
the URL could end any call and inflate a tenant's billed minutes.

---

## 2. Call lifecycle

```
                 ┌─────────┐
   outbound ────▶│ RINGING │◀──── inbound (queued/initiated/ringing)
                 └────┬────┘
        ┌─────────────┼──────────────┬───────────────┐
        ▼             ▼              ▼               ▼
  ┌───────────┐  ┌──────────┐  ┌──────────┐   ┌───────────┐
  │IN_PROGRESS│  │NO_ANSWER │  │  FAILED  │   │ COMPLETED │
  └─────┬─────┘  └──────────┘  └──────────┘   └───────────┘
        │            terminal      terminal       terminal
        ├──────────────┬───────────────┐
        ▼              ▼               ▼
 ┌─────────────┐ ┌──────────┐   ┌──────────┐
 │ TRANSFERRED │ │COMPLETED │   │  FAILED  │
 └──────┬──────┘ └──────────┘   └──────────┘
        │
        ├────▶ COMPLETED     (the human hung up)
        └────▶ FAILED        (the transfer leg died)
```

### Valid transitions

| From | To | When |
|---|---|---|
| `RINGING` | `IN_PROGRESS` | answered |
| `RINGING` | `NO_ANSWER` | rang out |
| `RINGING` | `FAILED` | busy, failed, canceled |
| `RINGING` | `COMPLETED` | caller hung up during ringback |
| `IN_PROGRESS` | `TRANSFERRED` | provider accepted the redirect |
| `IN_PROGRESS` | `COMPLETED` | normal end |
| `IN_PROGRESS` | `FAILED` | media/provider error |
| `TRANSFERRED` | `COMPLETED` | human hung up |
| `TRANSFERRED` | `FAILED` | transfer leg died |
| `COMPLETED` / `FAILED` / `NO_ANSWER` | — | **terminal, nothing leaves** |

Anything else — `COMPLETED → RINGING`, `FAILED → IN_PROGRESS`,
`TRANSFERRED → IN_PROGRESS` — is rejected, logged as
`call_state.illegal_transition`, and leaves the row untouched. There is no
retry mechanism that re-opens a finished call; a retry creates a **new call
row with a new SID**.

`TRANSFERRED` is deliberately **not** terminal: the call is still up, just with
a human on it.

### Provider status mapping

| Twilio | VoxDesk |
|---|---|
| `queued`, `initiated`, `ringing` | `RINGING` |
| `in-progress`, `answered` | `IN_PROGRESS` |
| `completed` | `COMPLETED` |
| `no-answer` | `NO_ANSWER` |
| `busy`, `failed`, `canceled`, `cancelled` | `FAILED` (+ `failure_reason`) |
| anything else | **ignored** — never guessed |

Ignoring an unknown string is the point: guessing means terminating a live
call on a typo or a Twilio API change.

### Single source of truth

All of this lives in **`app/telephony/call_state.py`**. Nothing else assigns
`call.status`. The module has no HTTP and no SDK imports, so the rules are
directly testable.

```python
result = call_state.apply_provider_status(call, "completed", duration_seconds=143)
if result.applied:
    ...   # a real change, safe to bill / grade the lead / push to CRM
```

`apply_status` never raises and always reports what it did:
`applied` / `duplicate` / `terminal` / `illegal` / `unknown_status`.

Timestamps are protected: `duration_seconds` only ever grows (a retry
reporting 0 cannot erase 143), and `ended_at` is written once.

---

## 3. How a real transfer works

```
AI decides to escalate
   │
   ▼
escalate_to_human(reason)                 app/agent/functions.py
   │
   ▼
transfer_service.request_transfer()       app/telephony/transfer_service.py
   │
   ├─ 0. tenant scoping        call.tenant_id == tenant.id  else refuse
   ├─ 1. idempotency           already in flight? return existing state
   ├─ 2. call transferable?    terminal or non-transferable? refuse
   ├─ 3. destination           tenant.escalation_number, normalized to E.164
   ├─ 4. record intent         transfer_state = REQUESTED, COMMIT
   │                           + SYSTEM turn "Human transfer requested."
   ├─ 5. provider action       provider.redirect_call(call_sid, twiml)
   │        │
   │        ├─ failed ──▶ transfer_state = FAILED, status UNCHANGED
   │        │             + SYSTEM turn "Human transfer failed."
   │        │
   │        └─ accepted ─▶ transfer_state = DIALING
   │                       status = TRANSFERRED          ◀── only here
   │                       + SYSTEM turn "Human transfer started."
   ▼
Twilio dials the human, <Dial action=/telephony/transfer-status>
   │
   ▼
POST /telephony/transfer-status
   ├─ answered / completed ─▶ transfer_state = CONNECTED
   │                          + SYSTEM turn "Human transfer connected."
   └─ busy / no-answer /   ─▶ transfer_state = FAILED
      failed / canceled       + SYSTEM turn "Human transfer failed."
                              (the caller falls through to our voicemail TwiML)
```

**`CallStatus.TRANSFERRED` is set only after the provider accepts the
redirect.** The AI requesting a transfer is a *request*; it never sets the
status by itself. If Twilio rejects the redirect — most commonly `20404`, the
call already ended — the call stays `IN_PROGRESS`, the transfer is recorded as
`FAILED`, and the agent is told `TRANSFER_FAILED`.

### Sub-states

`Call.transfer_state` is tracked separately from `CallStatus` because a call
is still `IN_PROGRESS` while a transfer is mid-flight.

| State | Meaning |
|---|---|
| `NONE` | no escalation requested |
| `REQUESTED` | intent recorded, nothing dialled yet |
| `DIALING` | provider accepted; the human's phone is ringing |
| `CONNECTED` | the human answered (proved by the `<Dial>` callback) |
| `FAILED` | validation, provider error, busy, or no answer |

### Internal result codes

`TRANSFER_STARTED`, `TRANSFER_COMPLETED`, `TRANSFER_FAILED`,
`ALREADY_TRANSFERRED`.

What the **caller hears** is a separate, generic sentence. Raw provider errors
and phone numbers never reach the caller and never go back to the LLM — there
are tests asserting exactly that.

### Layering

| Module | Responsibility |
|---|---|
| `telephony/transfer.py` | TwiML generation only (`<Say>` whisper, `<Dial>`, voicemail fallback) |
| `telephony/provider.py` | The single outbound provider call, behind an interface |
| `telephony/transfer_service.py` | Orchestration: validation, idempotency, state, events |
| `telephony/call_state.py` | The status machine |
| `telephony/phone.py` | E.164 normalization |

Only the outbound HTTP call is behind an interface. Abstracting more would let
a test pass while the real integration is broken.

---

## 4. Duplicate prevention

`Call.transfer_state` **is** the lock. Any request while the state is
`REQUESTED`, `DIALING` or `CONNECTED` returns the existing state and issues no
provider call at all:

```
first  request → TRANSFER_STARTED       provider called once
second request → ALREADY_TRANSFERRED    provider not called
third  request → ALREADY_TRANSFERRED    provider not called
```

This matters because LLMs retry tool calls, and a duplicate here is a second
phone physically ringing on somebody's desk.

A `FAILED` transfer is *not* in flight, so a genuine retry is permitted and
increments `transfer_attempts`.

Transcript events are de-duplicated by exact text, so a retried webhook cannot
write "Human transfer connected." three times.

---

## 5. Callback races

Twilio retries webhooks and does not guarantee ordering between the parent
call's status callback and the `<Dial>` leg's action callback.

| Case | Sequence | Result |
|---|---|---|
| **A** | transfer → stream closes → `completed` → dial callback | `COMPLETED` + `CONNECTED`. The inferred failure is corrected by the authoritative callback. |
| **B** | transfer → dial callback → `completed` | `COMPLETED` + `CONNECTED` |
| **C** | `completed` then a stale `no-answer` | stays `COMPLETED`; the stale event is ignored |
| **D** | duplicate `completed` × N | `COMPLETED`, billed once, one set of transcript events |

**Inference vs evidence.** If the parent call ends while the transfer is still
`DIALING`, we *infer* the transfer failed and mark it with the
`INFERRED_PREFIX` marker. If the real `<Dial>` callback then arrives saying the
human answered, that evidence overrides our guess. A failure the provider
actually reported (`busy`, `no-answer`) carries no prefix and is **never**
overridden — there is a test for that narrowness.

**Billing.** Minutes are billed only on an *applied* terminal transition, and
only for the delta above what was already recorded, so N duplicate callbacks
bill once. Lead grading and the CRM push are likewise gated on
`result.applied`, and the CRM push additionally checks `crm_synced`.

---

## 6. Stream lifecycle

The signed stream-token design from STEP 2 is unchanged: HMAC-SHA256 over
`call_sid:issued_at`, 120-second TTL, bound to one call SID.

What changed is the teardown path. A transfer **deliberately** kills the media
stream — Twilio replaces the TwiML — and the old handler did:

```python
except Exception:
    call.status = CallStatus.FAILED     # unconditional
```

so every successful transfer was recorded as a failed call. The handler now
checks `transfer_state` first and routes through `call_state`, which also
prevents regressing an already-terminal status.

---

## 7. Tenant-scoped configuration

The destination is read from `Tenant.escalation_number` and **cannot be
supplied by anyone** — not by the LLM, not by a request body, not by a URL
parameter. There is no code path that accepts a destination from outside.

`request_transfer` additionally refuses when `call.tenant_id != tenant.id`
(`TENANT_MISMATCH`), as defence in depth.

`GET /api/calls/{call_id}/transfer` returns transfer metadata through the same
`get_owned()` helper as transcripts, so another tenant's call id returns
**404**. The destination is redacted in that response — the dashboard needs to
show that a transfer happened, not become a directory of staff mobile numbers.

---

## 8. Phone numbers

`app/telephony/phone.py`. Normalizes formatting (spaces, dashes, brackets,
dots, `00` international prefix) to E.164.

It **never guesses a country code**. `5550101234` is rejected, not turned into
`+15550101234` — a US default would quietly dial the wrong continent for a UK
client. `client:` and `sip:` endpoints pass through untouched.

`phone.redact("+15550101234")` → `+1*******34` is used in logs and API
responses.

The LLM-supplied `reason` is spoken aloud to staff in the whisper, and it
echoes caller speech, so digit runs are replaced with `[number removed]` and
markup characters are stripped before it reaches TwiML. The full reason is
still stored in `transfer_reason` for the dashboard.

---

## 9. Observability

Structured events, all carrying `call_id`, `call_sid`, `tenant_id` and where
relevant `transfer_attempt`:

`transfer_requested`, `transfer_started`, `transfer_connected`,
`transfer_failed`, `transfer_duplicate`, `transfer_callback`,
`invalid_transfer_state`, plus `call_state.transition`,
`call_state.duplicate`, `call_state.terminal_protected`,
`call_state.illegal_transition`, `call_state.unknown_provider_status`.

Phone numbers are redacted. JWTs, refresh tokens, API keys and the Twilio auth
token are never logged.

---

## 10. Database fields (migration `0004_call_transfer_lifecycle`)

Added to `calls`, all nullable or server-defaulted, purely additive:

| Column | Purpose |
|---|---|
| `failure_reason` | why a call ended badly (`busy`, `no-answer`, …) |
| `transfer_state` | enum `transferstate`, default `NONE` — the idempotency lock |
| `transfer_destination` | normalized E.164 actually dialled |
| `transfer_reason` | full reason from the agent |
| `transfer_attempts` | genuine attempts, default `0` |
| `transfer_error` | diagnostic detail, server-side |
| `transfer_requested_at` / `_started_at` / `_completed_at` / `_failed_at` | timeline |

Index `ix_calls_tenant_transfer_state` on `(tenant_id, transfer_state)`.

The existing `escalated` boolean is **kept and still maintained** — the
dashboard and the CRM payload already read it. Nothing was duplicated.

---

## 11. Configuration

| Setting | Used for |
|---|---|
| `Tenant.escalation_number` | the human destination. Must be E.164. |
| `Tenant.record_calls` | whether the transfer leg is recorded |
| `Tenant.twilio_number` | caller ID presented to the human |
| `PUBLIC_BASE_URL` | builds the `<Dial action>` callback URL |

Twilio setup: point the number's **status callback** at
`/telephony/status`. The transfer action URL is generated automatically.

---

## 12. Testing

```bash
make test                                  # everything
python -m pytest tests/test_call_state.py         -q   # transition rules
python -m pytest tests/test_transfer.py           -q   # service, fake provider
python -m pytest tests/test_transfer_pipeline.py  -q   # tool → service → provider
python -m pytest tests/test_call_callbacks.py     -q   # webhooks, races
python -m pytest tests/test_stream_lifecycle.py   -q   # stream tokens
python -m pytest tests/test_transfer_security.py  -q   # tenant isolation
python -m pytest tests/test_phone.py              -q   # E.164
```

Transfer tests run the real service against a real session and a
`FakeTelephonyProvider` that records the TwiML actually generated and can be
told to fail. No test asserts merely that a function was called; every one
checks resulting database state.

```python
provider = FakeTelephonyProvider()                       # or fail_with=("20404", "...")
await transfer_service.request_transfer(db, tenant, call, provider=provider)
assert provider.call_count == 1
assert "+15557654321" in provider.last_twiml()
```
