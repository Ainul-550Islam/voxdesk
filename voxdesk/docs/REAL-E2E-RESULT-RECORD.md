# Real E2E Voice Call — Structured Result Record

> **Final report line: `REAL TELEPHONY E2E: NOT AUTOMATICALLY EXECUTED`**
>
> Change this banner to `REAL TELEPHONY E2E: EXECUTED` **only** if a genuine
> human-triggered call was actually performed and this record is completed and
> committed as evidence. Otherwise it must stay `NOT AUTOMATICALLY EXECUTED`.

---

## Summary

| Field | Value |
|-------|-------|
| Status | NOT_EXECUTED |
| Performed by | — |
| Date (operator timezone) | — |
| Environment (`APP_ENV`) | — |
| Test tenant id | — |
| Test tenant name | — |
| `E2E_TEST_NUMBER` | — |
| Operator caller number | — |
| Twilio `CallSid` | — |
| Twilio `StreamSid` (if captured) | — |
| Link to evidence (recording/log excerpt, if retained) | — |

> Never paste full phone numbers or credentials here; use the tenant id and
> redacted numbers (`+1*******34` style) or a private evidence link.

## Checkpoint results

Mark each checkpoint `PASS`, `FAIL`, `BLOCKED`, or `NOT_TESTED` and add a note.

| # | Checkpoint | Result | Note |
|---|------------|--------|------|
| 1 | Twilio webhook / signature | NOT_TESTED | |
| 2 | Tenant resolution (test tenant) | NOT_TESTED | |
| 3 | E2E guard (`call.e2e_allowed`) | NOT_TESTED | |
| 4 | Call/session creation (exactly one) | NOT_TESTED | |
| 5 | Media stream / handshake | NOT_TESTED | |
| 6 | STT/TTS/LLM initialization | NOT_TESTED | |
| 7 | System prompt + AI disclosure | NOT_TESTED | |
| 8 | First utterance transcribed | NOT_TESTED | |
| 9 | LLM response spoken | NOT_TESTED | |
| 10 | Tool call/result | NOT_TESTED | |
| 11 | TTS voice/language/speed | NOT_TESTED | |
| 12 | Barge-in | NOT_TESTED | |
| 13 | Multi-turn (≥2 exchanges) | NOT_TESTED | |
| 14 | Human transfer | NOT_TESTED | |
| 15 | Booking (test calendar) | NOT_TESTED | |
| 16 | Exactly-one terminal state | NOT_TESTED | |
| 17 | Turn persistence (no dupes) | NOT_TESTED | |
| 18 | Usage accounting exactly-once | NOT_TESTED | |
| 19 | Billing/metering isolation | NOT_TESTED | |
| 20 | Audit logging | NOT_TESTED | |
| 21 | Post-call processing idempotent | NOT_TESTED | |
| 22 | Observability correlation | NOT_TESTED | |

## Negative (guard) checks

| Check | Result | Note |
|-------|--------|------|
| Non-allowlisted caller refused, no call row | NOT_TESTED | |
| Non-test tenant refused while armed | NOT_TESTED | |
| Production boot refused with `E2E_ENABLED=true` | NOT_TESTED | |

## Issues found

_(One line per issue; cross-reference a ticket if filed.)_

1. —

## Sign-off

- Operator: —
- Date: —

---

## Step 11 bridge: machine-readable result for the launch gate

`scripts/release_gate.py` reads `var/release/e2e-result.json` (never this
Markdown) to decide the **REAL TELEPHONY E2E** launch item. The file is the
*result* of the human exercise above, not a replacement for it.

1. Copy the template:
   `cp scripts/release/e2e-result.template.json var/release/e2e-result.json`
2. After a genuinely operator-triggered live call, set `"result": "PASS"` and
   fill **every** required field (`test_date`, `test_tenant_id`, `call_id`,
   `twilio_call_sid`, `operator`) plus the observations you recorded above.
   A `PASS` with any required field missing is recorded by the gate as `FAIL`.
3. Any other `result` value (`NOT_EXECUTED`, `FAIL`, …) keeps the item
   `NOT_RUN` (or `FAIL`), so the gate can never pass on an unperformed call.
4. The gate will **never** dial a phone number itself — this item can only be
   satisfied by a human who actually made the call and recorded it here.
