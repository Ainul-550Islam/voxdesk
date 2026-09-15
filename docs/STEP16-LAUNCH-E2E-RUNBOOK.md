# STEP 16 — LAUNCH REAL-VOICE E2E RUNBOOK

Operator-facing, human-executed procedure for the final live-voice
end-to-end test on the **staging** environment. This document is the only
sanctioned way to exercise a genuine Twilio media-stream call on purpose.

> **The automated tooling never places a call.** `scripts/staging_certify.py`
> validates *preconditions only* (`--e2e-readiness`). The live call in
> Section 10 is always executed by an authorized human operator.

---

## 1. PURPOSE

Prove, on staging and with real (test-only) provider credentials, that a live
voice call flows end to end:

dial → Twilio media stream → API/agent (STT → LLM → TTS) → response audio →
barge-in/interruption → tool/calendar/CRM actions (if configured) → transfer
(if configured) → hang-up → recording/transcript → metering/billing →
reconciliation → audit.

A PASS here satisfies the P0 evidence item `e2e-001` **only when** the result
is recorded by a human in `var/release/e2e-result.json` with every required
field — the release gate reads that file and will not fabricate a PASS.

## 2. RELEASE IDENTIFIER

| Field | Value |
|---|---|
| Release identifier | `2026.09.13-step15-staging-hardening` |
| Git commit | `01bec2c0e27229f01a6aef7072c5bbbb30a7ade9` |
| Migration head | `0011_side_effect_exactly_once` |
| Dependency fingerprint | `7a957fc5…7f11e522` |
| Configuration fingerprint | `4cbf2686…9fd673` |

Update this header whenever Step 16 changes are committed on top.

## 3. PREREQUISITES

1. A Docker-capable **staging** host (not the sandbox): `docker` + `docker compose` v2.
2. Staging `.env.staging` populated from `.env.staging.example` (no production values).
3. A dedicated **test tenant** provisioned and marked `is_test_tenant=true`.
4. A dedicated **test Twilio number** (or staging subaccount) for that tenant.
5. Staging-only provider credentials (Twilio test account, Deepgram, ElevenLabs,
   one LLM key, Stripe **test-mode** `sk_test_*` keys).
6. An operator phone number to place the call from.
7. `E2E_ENABLED=true`, `E2E_TEST_NUMBER=<test number>`,
   `E2E_ALLOWED_CALLERS=<operator number>` in the staging environment.

## 4. SAFETY RULES

**DO NOT:**

- call a production customer or any real phone number that is not the test number;
- use the production tenant;
- use production Stripe (`sk_live_*`);
- create real billing events against a real customer;
- create production appointments or bookings;
- alter production CRM records;
- test with real customer PII.

**DO:**

- use only dedicated test resources (test tenant, test number, test credentials);
- keep `APP_ENV=staging` for the entire run;
- record every observation in the evidence record, not from memory.

## 5. STAGING CHECK

```bash
python scripts/ops_preflight.py
python scripts/staging_certify.py --preflight
python scripts/staging_certify.py --staging --base-url http://localhost:8001
```

Expected: runtime dependencies present; `docker compose config` valid; build /
start / migrate / readiness / smoke green. If Docker is absent the relevant
steps are **BLOCKED** — do not proceed to a live call from a broken stack.

## 6. PROVIDER CHECK

```bash
VOXDESK_REAL_INTEGRATION=1 python scripts/provider_report.py --json
```

Required for a live call: Twilio, Deepgram, ElevenLabs and at least one LLM
(OpenAI/Anthropic/Google) **PASS**. Calendar/CRM providers may be
**SKIPPED** if that test case is not configured. A **FAIL** on any
voice-critical provider is an abort condition.

## 7. TLS CHECK

```bash
python scripts/verify_tls.py --url https://staging.example.com --websocket --json
```

Required: **PASS** (valid chain, hostname match, not expiring, HSTS +
X-Content-Type-Options + X-Frame-Options + Referrer-Policy present, no
plain-HTTP content serving, WebSocket upgrade acknowledged). A plain-HTTP
localhost target reports NOT_APPLICABLE and is only valid for a local,
non-network smoke — never for the recorded E2E.

## 8. MONITORING CHECK

```bash
python scripts/staging_certify.py --observability \
  --prometheus-url http://127.0.0.1:9090 --grafana-url http://127.0.0.1:3001
```

Expected: Prometheus healthy, `up` targets for `api:8000` and
`scheduler:8001`, rules/alerts loaded, api + scheduler series present.
Grafana `/api/health` reachable. Metrics must be reachable **before** the
call so post-call series can be compared.

## 9. PRE-CALL CHECKLIST

Run and record each item:

| # | Check | Source | Required |
|---|---|---|---|
| 1 | E2E enabled, non-production | `staging_certify.py --e2e-readiness` | PASS |
| 2 | Test tenant exists, `is_test_tenant=true` | dashboard / DB | PASS (human) |
| 3 | Tenant `twilio_number` == `E2E_TEST_NUMBER` | dashboard / DB | PASS (human) |
| 4 | Operator number allowlisted | `.env.staging` `E2E_ALLOWED_CALLERS` | PASS |
| 5 | Voice providers PASS | `provider_report.py` | PASS |
| 6 | Monitoring reachable | Section 8 | PASS |
| 7 | Billing test mode (`sk_test_*`, or `manual`) | `.env.staging` | PASS |
| 8 | TLS PASS | Section 7 | PASS |

```bash
python scripts/staging_certify.py --e2e-readiness --base-url http://localhost:8001
```

If any required precondition is missing, E2E readiness is **BLOCKED** (or
**FAIL** for a safety violation such as a live Stripe key or E2E armed in
production). It is never PASS by itself — the call has not happened yet.

## 10. TEST CALL PROCEDURE

1. From the **allowlisted operator phone**, dial the **test number**.
2. Speak naturally; do not script answers into the assistant.
3. After each scenario, note the observation immediately (time, what was
   heard, what the dashboard/API showed).
4. Do **not** infer PASS from "the call sounded okay" — verify system state
   in Section 12 before marking anything PASS.

## 11. TEST CASES

Each case is recorded as **PASS / FAIL / BLOCKED / NOT_TESTED**. A case is
BLOCKED when its prerequisite (e.g. a configured calendar) is absent; it is
NOT_TESTED when it was intentionally skipped. Only PASS and FAIL are outcome
values; never infer either without evidence.

| ID | Case | Evidence to capture |
|---|---|---|
| A | Greeting | assistant greets, mentions the tenant's configured greeting |
| B | Normal question | coherent answer in the configured language |
| C | RAG answer | answer cites a document from the tenant knowledge base |
| D | Multilingual (if configured) | answer language matches request |
| E | Interruption / barge-in | assistant stops mid-sentence and responds to the interruption |
| F | Tool call | a configured tool fires (observable in logs/metrics) |
| G | Calendar availability | availability check returns real read-only data |
| H | Safe booking (if configured) | booking created **only on the test calendar** |
| I | CRM action (if configured) | read/write **only against the test CRM sandbox** |
| J | Human transfer (if configured) | call transfers to the configured destination |
| K | Provider failure behavior | simulate one provider outage; assistant degrades gracefully, no crash, no hang |
| L | Hang-up | call ends cleanly; webhook finalization runs |
| M | Repeated/duplicate webhook | redelivered webhook is idempotent (exactly-once side effects) |
| N | Finalization | recording/transcript present and complete |
| O | Usage | usage rows recorded (minutes, tokens, provider) |
| P | Billing | metering aggregates match the call; reconciliation reports no discrepancy |
| Q | Audit | call appears in audit/history with the correct tenant and test markers |

## 12. POST-CALL VALIDATION

Verify system state — never infer from audio alone:

```bash
curl -H "Authorization: Bearer <operator-token>" \
  http://localhost:8001/api/calls
curl -H "Authorization: Bearer <operator-token>" \
  http://localhost:8001/api/calls/<call_id>/transcript
curl -H "Authorization: Bearer <operator-token>" \
  http://localhost:8001/api/calls/<call_id>/transfer
```

- Call row exists, belongs to the **test tenant**, direction correct.
- Transcript matches what was spoken (STT working).
- No cross-tenant bleed: the call is invisible from another tenant's session.
- Transfer row present iff transfer (J) ran.
- Prometheus: job-run gauges and voice metrics moved as expected
  (`voxdesk_job_runs_total`, scheduler series) — see docs/OBSERVABILITY.md.

## 13. BILLING / METERING VALIDATION

```bash
curl -H "Authorization: Bearer <operator-token>" \
  http://localhost:8001/api/billing/usage
curl -X POST -H "Authorization: Bearer <operator-token>" \
  http://localhost:8001/api/billing/reconcile
```

- Usage rows reflect the call (duration, tokens, provider).
- Metering aggregates match the raw usage.
- Reconciliation reports no unexpected discrepancy (docs/BILLING.md).
- If `BILLING_PROVIDER=stripe`: **no** real charge, invoice or subscription
  was created; only test-mode objects are acceptable.

## 14. CALENDAR VALIDATION

Only if calendar (G/H) was configured: the availability/booking is visible on
the **test calendar** only, with no production calendar touched. See
docs/CALENDAR-INTEGRATIONS.md.

## 15. CRM VALIDATION

Only if CRM (I) was configured: the action landed in the **test CRM
sandbox** only; no production CRM row was created or mutated. See
docs/CRM-INTEGRATIONS.md.

## 16. TRANSFER VALIDATION

Only if transfer (J) ran: the transfer completed to the configured test
destination; the transfer record exists; the call did not drop silently. See
docs/CALL-LIFECYCLE.md.

## 17. INCIDENT / FAILURE OBSERVATION

For every failure, record (never the secret):

| Field | Value |
|---|---|
| provider | which provider/component failed |
| operation | what was attempted |
| call ID | internal call id / `twilio_call_sid` |
| error class | e.g. timeout, 4xx, SDK error |
| timestamp | ISO-8601 |
| result | observed outcome |

**Never expose:** API key, bearer token, webhook secret, full authorization
header, customer PII. For ambiguous external state, do **not** blindly retry:
use the existing reconciliation/idempotency behaviour
(docs/EXACTLY-ONCE.md, docs/INCIDENT-TRIAGE.md).

## 18. EVIDENCE RECORDING

1. Copy the template: `scripts/release/e2e-result.template.json` →
   `var/release/e2e-result.json`.
2. Fill **every** field: result, test_date, test_tenant_id, test_number,
   operator, call_id, twilio_call_sid, direction, agent, pre_call_checks,
   call_observations, post_call_checks, failures, signed_off_by.
3. `result` may be `PASS` or `FAIL`; anything else (including the default
   `NOT_EXECUTED`) is treated as not-run by the gate.
4. Run `python scripts/release_gate.py` to re-evaluate.

Historical evidence in `scripts/release/evidence.json` is never deleted or
overwritten — new records supersede by id and the old record moves to the
`history` array.

## 19. PASS / FAIL / BLOCKED CRITERIA

- **PASS (E2E)**: every configured test case that ran is PASS, post-call state
  verified, metering/reconciliation clean, and the human fills
  `var/release/e2e-result.json` with `result=PASS` and all required fields.
- **FAIL**: any ran case failed, any safety violation, or post-call state does
  not match. Record and fix the root cause before re-running.
- **BLOCKED**: a prerequisite is missing (Docker, TLS endpoint, provider
  credentials, egress namespace) — record exactly what is missing; do not
  reclassify it as PASS.

## 20. ROLLBACK / ABORT CONDITIONS

Abort the run immediately if any of these occur:

1. `APP_ENV` is production, or any production tenant/number/key is involved.
2. A live Stripe key (`sk_live_`) is detected.
3. E2E armed but `E2E_TEST_NUMBER` or `E2E_ALLOWED_CALLERS` missing/invalid.
4. TLS verification FAILs (invalid/expired/mismatched certificate, or
   plain-HTTP content serving).
5. Any voice-critical provider reports FAIL and cannot be fixed on staging.
6. Cross-tenant data bleed or an unexpected write to a production system is
   observed.

On abort: stop the call, disable E2E (`E2E_ENABLED=false`), leave the staging
stack in its previous healthy state, and file the incident per
docs/INCIDENT-RESPONSE.md.

---

## RELEASE IMPACT

- **REAL TELEPHONY E2E PASS is mandatory** before `e2e-001` can be marked PASS.
- **Provider health PASS is not equivalent to voice E2E PASS.**
- **TLS PASS is not equivalent to production readiness.**
- **Staging PASS is not equivalent to production readiness.**
- **External pentest remains EXTERNAL.**
- **Compliance sign-off remains HUMAN.**
- Even with every automated check green, the tooling stops at
  **READY FOR AUTHORIZED PRODUCTION DEPLOYMENT** — it never deploys
  production automatically.
