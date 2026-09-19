# VoxDesk — Launch gate and release certification (Step 11)

This document is the operator's manual for the production launch-control
system. It describes the certification architecture, the evidence model, the
release-gate logic, and every flow an operator must follow to take the
Step 10 release candidate to a declared launch decision. The gate is
implemented in `app/release/` with the command `python scripts/release_gate.py`.

> Launch-control principle: **evidence first, PASS second, READY last.**
> No evidence means `NOT_RUN`, never `PASS`; `PASS` without a recorded
> evidence string, verifier and date is impossible to produce by construction;
> and `READY` additionally requires the certified artifact to match the tree
> you are about to deploy.

## 1. Canonical status model

Every launch requirement has **exactly one** status:

| Status | Meaning |
|---|---|
| `PASS` | Satisfied, with recorded evidence. |
| `FAIL` | Executed and did not pass; must be re-run or fixed. |
| `BLOCKED` | Cannot proceed yet (dependency missing, e.g. no Docker, no credentials, third party not engaged). |
| `NOT_RUN` | Not attempted yet. This is the *conservative default*. |
| `WAIVED` | Accepted as not applicable for a limited time (P2/P3 only). |

Severity:

| Severity | Effect on launch |
|---|---|
| `P0` | Absolutely blocks launch. Never waivable. |
| `P1` | Blocks paid/public production launch. Never waivable. |
| `P2` | Operational follow-up; does not block launch; waivable. |
| `P3` | Future improvement; does not block launch; waivable. |

Every item also carries a **classification** so the gate can never confuse
evidence types:

| Class | Meaning | Example evidence |
|---|---|---|
| `AUTOMATED` | Deterministic, reproducible locally/CI | `pytest`, `ruff`, `pip-audit` |
| `HUMAN` | A person signed off | manual E2E call, compliance review |
| `EXTERNAL` | A third party or external SaaS proved it | provider check, pentest report |
| `INFRASTRUCTURE` | Proven against a real/staging environment | TLS, backup/restore drill, alert fire |

## 2. Evidence model

* **Checklist** (`scripts/release/checklist.json`) — the single source of
  truth: category, requirement, severity, classification.
* **Evidence registry** (`scripts/release/evidence.json`) — one entry per
  item: `status`, `evidence`, `verification_date`, `verifier`, `notes`, and an
  optional `waiver` (reason/approver/date/expiry). An item with no entry is
  `NOT_RUN`.
* **Artifact record** (`var/release/artifact.json`) — the certified identity
  written by `--freeze`: git commit, dependency fingerprint, migration heads,
  secret-excluding configuration fingerprint, environment class, build date.
* **Provider results** (`var/release/provider-results.json`) — produced by
  `scripts/validate_providers.py --json`; consumed by the gate only when the
  opt-in switch `VOXDESK_REAL_INTEGRATION=1` is set.
* **E2E result** (`var/release/e2e-result.json`) — the human-authored result
  of the manual live call (see §5).

The gate **never writes** the evidence registry; it only reads it. Evidence is
what actually happened, recorded by the operator or produced by the tool that
ran.

## 3. Running the gate

```bash
# Plain run — conservative default (all NOT_RUN unless evidence exists):
python scripts/release_gate.py

# Freeze the certified artifact identity (commit + fingerprints):
python scripts/release_gate.py --freeze --release-identifier 2026.09.12-rc1

# Machine-readable:
python scripts/release_gate.py --json

# Ingest real-provider results (opt-in, read-only):
VOXDESK_REAL_INTEGRATION=1 python scripts/release_gate.py \
  --provider-results var/release/provider-results.json
```

The output prints `CATEGORY / REQUIREMENT / STATUS / SEVERITY / EVIDENCE /
BLOCKER` for every item, then the summary:

```
P0 BLOCKERS: N
P1 BLOCKERS: N
P2 OPEN: N
P3 OPEN: N
FINAL RESULT: READY | NOT_READY | BLOCKED
```

Exit code is `0` only for `READY`; otherwise `1`. `--freeze` still writes the
artifact record before the gate evaluates, so a NOT_READY exit during
`--freeze` means "the artifact is frozen but the gate is not ready", not
"the freeze failed".

## 4. Launch-decision logic (the gate)

1. Resolve each item's effective status: registry entry, or `NOT_RUN` if
   absent. A `WAIVED` item with an invalid waiver (P0/P1, missing
   reason/approver, or expired) degrades to `NOT_RUN`.
2. Count blockers: any `P0`/`P1` item in `FAIL`/`BLOCKED`/`NOT_RUN` blocks.
3. Compare the frozen artifact to live facts (git commit, dependency
   fingerprint, migration heads, configuration fingerprint). Any drift means
   the certified artifact is not the deployment artifact.
4. Decide:
   * `BLOCKED` — a P0/P1 blocker exists and at least one is `BLOCKED`.
   * `NOT_READY` — any P0/P1 blocker, or artifact not frozen, or drift.
   * `READY` — zero P0/P1 blockers AND artifact frozen AND no drift.

`P2`/`P3` open items never block launch; they are reported as open items.

## 5. Manual real-telephony E2E (human-controlled, never automated)

1. Arm the E2E test tenant via the Step 5 E2E guard (`E2E_ENABLED`,
   `E2E_TEST_NUMBER`, `E2E_ALLOWED_CALLERS`), which refuses production boot.
2. An operator physically dials the test number (inbound) or triggers the
   call. **The system never dials.**
3. Record pre-call, during-call and post-call observations in
   `docs/REAL-E2E-RESULT-RECORD.md` (22 checkpoints + negative guards).
4. Copy `scripts/release/e2e-result.template.json` to
   `var/release/e2e-result.json`, set `result` to `PASS` and fill all required
   fields (`test_date`, `test_tenant_id`, `call_id`, `twilio_call_sid`,
   `operator`).
5. Re-run the gate. The `REAL TELEPHONY E2E` item becomes `PASS` only from
   this file; anything else leaves it `NOT_RUN`/`FAIL`.

## 6. Real-provider certification (opt-in, read-only)

Reuses the Step 4 framework (`app/integrations/validation/`) for Twilio,
Deepgram, ElevenLabs, OpenAI, Anthropic, Google LLM, Google Calendar,
Microsoft Calendar, Cal.com, HubSpot, GoHighLevel, Jobber, Stripe.

```bash
VOXDESK_REAL_INTEGRATION=1 python scripts/validate_providers.py --json \
  > var/release/provider-results.json
VOXDESK_REAL_INTEGRATION=1 python scripts/release_gate.py \
  --provider-results var/release/provider-results.json
```

All checks are READ_ONLY. The framework's `FORBIDDEN_OPERATIONS` explicitly
prohibits live calls/SMS, Stripe charges, calendar bookings and CRM writes.
Results are ingested per provider: `PASS`→`PASS`, `FAIL`→`FAIL`,
`BLOCKED`→`BLOCKED`, `SKIPPED`→`NOT_RUN`. A `REAL PROVIDER HEALTH` aggregate
item is `PASS` only when **all 13** providers report `PASS`; a partial or
skipped sweep keeps it `NOT_RUN`, one `FAIL` makes it `FAIL`, one `BLOCKED`
(without a `FAIL`) makes it `BLOCKED`.

## 7. Staging, backup/restore, egress, security, incident response

* **Staging** must run the *exact candidate artifact*: freeze it first
  (`--freeze`), deploy the same commit, and re-run the gate — drift detection
  fails the gate if anything moved.
* **Backup / restore** (`BACKUP`, `RESTORE`, `OFF-SITE BACKUP` items) are
  `INFRASTRUCTURE` items satisfied by actual evidence (pg_dump/rclone output,
  row-count + checksum verification after restore). See
  `docs/STEP10-BACKUP-SYNC.md` and `docs/STEP10-DEPLOY-DRILL.md`.
* **Egress** policy is verified under `ABUSE PREVENTION` and documented in
  `docs/STEP10-EGRESS-POLICY.md`.
* **Security** is certified by `tests/test_security_regression.py` + `bandit`,
  `docs/SECURITY.md`, and the dependency audit.
* **Privacy/retention** by `tests/test_gdpr_routes.py` + `tests/test_retention.py`.
* **Incident response** by `docs/INCIDENT-RESPONSE.md` + `docs/BCP.md` drills.

## 8. Waiver policy

* Only `P2`/`P3` may be waived.
* A waiver requires `reason`, `approver`, `date`, and an `expiry` (expiry is
  required in practice; a missing expiry is accepted but discouraged).
* An expired waiver silently degrades the item to `NOT_RUN` and is reported
  as a violation.
* Never waivable: P0/P1 — in particular authentication, tenant isolation,
  billing integrity, data corruption, TLS, and the manual E2E call.

## 9. External pentest

`PENTEST` is `P1`/`EXTERNAL` and remains `NOT_RUN` until a real, independent
third party performs it and delivers a report. No amount of internal tooling
changes this item.

## 10. Release freeze / immutability

`--freeze` records the git commit, dependency fingerprint, migration heads and
a secret-excluding configuration fingerprint. From that moment any change to
the tree, the pins, the migration graph or the settings schema returns
`NOT_READY` with an explicit `DRIFT:` line until you re-freeze and
re-certify.

## 11. After Step 11

The gate cannot become `READY` in a sandbox that lacks Docker, `pg_dump`,
`rclone`, real credentials, and a human-made phone call. The remaining blockers
are exactly the evidence the operator must produce in a real staging/production
environment — the gate is now the checklist that tells them what is missing.
