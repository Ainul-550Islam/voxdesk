# VoxDesk — Step 11 certification report (launch control)

**Date:** 2026-09-13
**Candidate:** `step10/release-candidate` @ `3e7fd37c2b421aaeca2ebeee27587c9152392015`
**Launch-control system:** branch `step11/launch-control` (this report is part of it)
**Status:** launch-control system **implemented, verified, and populated with
genuine automated evidence**; production launch **NOT declared** (by design —
see §5).

This report is the evidence-backed account of the Step 11 launch-control
system. It separates three things that must never be conflated: deterministic
automated tests (which passed and are recorded as evidence), real-provider /
external evidence (opt-in, read-only, not executed in this sandbox), and human
live-call evidence (not performed). No requirement is marked PASS without the
evidence that produced it.

## 1. Certification architecture (what was built)

```
scripts/release/checklist.json   -- single source of truth: 45 requirements
                                    across 30 categories, P0..P3, classified
                                    AUTOMATED / HUMAN / EXTERNAL / INFRASTRUCTURE
scripts/release/evidence.json    -- evidence registry (read-only to the gate;
                                    now holds 15 genuine AUTOMATED PASS records)
scripts/release/e2e-result.template.json
                                 -- human-authored manual-call result template
scripts/release_gate.py          -- the command (thin wrapper)
app/release/models.py            -- Status/Severity/Classification/Decision,
                                    ChecklistItem, Evidence, Waiver,
                                    ArtifactRecord, LiveFacts, Row, LaunchReport
app/release/facts.py             -- offline fingerprints: git commit, dependency
                                    pins, migration heads, secret-excluding
                                    configuration fingerprint
app/release/checklist.py         -- checklist loader + validation
app/release/evidence.py          -- evidence registry loader + conservative default
app/release/gate.py              -- pure evaluation + rendering + provider/E2E
                                    ingestion helpers
app/release/cli.py               -- argparse CLI, freeze, drift, --json, exit codes
tests/test_release_gate.py       -- 47 deterministic tests, zero credentials
```

## 2. Release-gate logic (implemented)

1. Every requirement resolves to exactly one of `PASS / FAIL / BLOCKED /
   NOT_RUN / WAIVED`; a missing registry entry is `NOT_RUN`, never `PASS`.
2. `P0`/`P1` items in `FAIL`/`BLOCKED`/`NOT_RUN` block launch; `P2`/`P3` are
   open items only.
3. `WAIVED` is legal only for `P2`/`P3` with reason + approver + expiry; an
   invalid or expired waiver degrades to `NOT_RUN` and is reported.
4. The frozen artifact (git commit, dependency fingerprint, migration heads,
   secret-excluding configuration fingerprint, env class, build date, optional
   image digest) is compared to live facts; any drift ⇒ `NOT_READY`.
5. Decision: `BLOCKED` if a P0/P1 blocker is `BLOCKED`; else `NOT_READY` if
   any P0/P1 blocker, artifact not frozen, or drift; else `READY`.

A `PASS` without evidence is impossible by construction: the gate only reads
evidence that was recorded, and `evaluate`/`render` carry the evidence string,
verifier and date through verbatim.

## 3. Deterministic quality gate — executed this turn

| Check | Command | Result |
|---|---|---|
| Backend full suite | `pytest tests/` | **2301 passed, 43 skipped, 0 failed** |
| Release-gate tests | `pytest tests/test_release_gate.py` | **47 passed** |
| Compile | `python -m compileall -q app scripts tests` | exit 0 |
| Imports (pipecat 0.0.94 contract) | `pytest tests/test_imports.py` | 5 passed |
| Lint (CI-fatal rules) | `ruff check app scripts tests --select E4,E7,E9,F` | clean |
| Lint (new code, full) | `ruff check app/release tests/test_release_gate.py scripts/release_gate.py` | clean |
| SAST | `bandit -q -r app -x app/integrations/validation` | **0 high, 0 medium, 17 low** (all pre-existing: B105 placeholder strings, B110, B311) — no new findings from Step 11 |
| Backend dependency audit | `scripts/audit_dependencies.sh` | OK; accepted residuals `nltk`, `pillow`, `pytest` (documented in `docs/SECURITY.md`) |
| Frontend tests | `npm test` (dashboard) | **362 passed (14 files)** |
| Frontend build | `npm run build` | exit 0 |
| Frontend production audit | `npm audit --omit=dev --audit-level=high` | 0 vulnerabilities |
| Infra config (static) | YAML parse of `docker-compose*.yml`, `observability/*.yml` | valid |
| Migration head | `alembic heads` | `0011_side_effect_exactly_once (head)` — matches the gate's computed head |
| Real-provider checks | — | **not executed** (opt-in `VOXDESK_REAL_INTEGRATION`, no credentials) |
| Real phone call | — | **never executed; never automated** |

## 4. Gate result right now (honest, conservative)

The 15 AUTOMATED items were genuinely executed this turn and recorded in
`scripts/release/evidence.json` with command, result, verifier and date. Run
`python scripts/release_gate.py`:

```
P0 BLOCKERS: 3    (TLS, Stripe, REAL TELEPHONY E2E — need a real environment,
                   credentials, and a human-made call respectively)
P1 BLOCKERS: 21   (deploy, backup, restore, observability, alerting, egress,
                   9 real-provider checks + REAL PROVIDER HEALTH aggregate,
                   rate limiting, compliance review, pentest, incident response,
                   disaster recovery)
P2 OPEN: 6        (off-site backup, Cal.com, GoHighLevel, Jobber, cost model,
                   retention)
P3 OPEN: 0
ARTIFACT FROZEN: yes
FINAL RESULT: NOT_READY
```

PASS items now: code-001, code-002, tests-001, tests-002, tests-003,
security-001, security-002, auth-001, tenant-001, deps-001, db-001,
migrations-001, billing-001, abuse-001, privacy-001.

## 5. Remaining blockers (what must actually happen before READY)

The gate is behaving correctly: it cannot say READY here. The blockers are
the real evidence a production launch needs, and each maps to a checklist item:

* **Human live-call E2E (`e2e-001`, P0)** — an operator must physically dial
  the armed test tenant and record the call in
  `docs/REAL-E2E-RESULT-RECORD.md` + `var/release/e2e-result.json`.
* **Real-provider checks (13 items + `realprov-health-001`)** — run
  `VOXDESK_REAL_INTEGRATION=1 python scripts/validate_providers.py` with real
  credentials in a real environment.
* **Infrastructure items (TLS, deployment, backup, restore, off-site backup,
  egress, observability, alerting, DR, retention, cost)** — require Docker,
  `pg_dump`, `rclone`, a staging environment and a real deploy; not available
  in this sandbox.
* **Rate limiting (`ratelimit-001`, P1)** — the tests pass, but the
  "configured in production" half of the requirement needs the production
  gateway; left `NOT_RUN` rather than half-certified.
* **External pentest (`pentest-001`, P1)** — `NOT_RUN` until a real third
  party performs it; no internal tooling changes this.
* **Human reviews (`compliance-001`, `ir-001`)** — sign-off against
  `docs/SECURITY.md`, `docs/BCP.md`, `docs/INCIDENT-RESPONSE.md`.

## 6. What happens after Step 11

1. In a staging environment: `--freeze` the candidate, deploy the exact
   artifact, produce the infrastructure and provider evidence, record the
   manual E2E call, add those records to `scripts/release/evidence.json`.
2. Re-run `python scripts/release_gate.py`; iterate until P0/P1 blockers are
   zero and the frozen artifact matches the deployment.
3. Only then — and only with the external pentest either completed or a
   documented P2-style business decision in place — is `FINAL RESULT: READY`
   achievable and meaningful.
