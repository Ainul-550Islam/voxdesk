# VoxDesk — Step 13 operator runbook (staging certification toolkit)

This runbook is the operator manual for taking the certified artifact to a
real staging host and producing the launch evidence that Steps 11/12 left
`BLOCKED`. It separates AUTOMATED, HUMAN and EXTERNAL work and states the
exact command order and expected statuses.

The toolkit never dials a phone, never charges Stripe, never books a calendar
slot and never writes CRM data. Every dangerous operation stays a HUMAN step.

## Prerequisites on the staging host

- Docker with the compose plugin, PostgreSQL client (`psql`, `pg_dump`,
  `pg_restore`), Redis (`redis-cli`), `curl`, `openssl`, and — only for
  off-site backup — `rclone`.
- The exact certified commit checked out, working tree clean.
- A staging environment: `APP_ENV=staging` with real staging credentials
  (never production credentials).

## Exit codes (all toolkit commands)

| Code | Meaning |
|---|---|
| 0 | success / PASS |
| 1 | failure / FAIL |
| 2 | blocked / BLOCKED |
| 3 | invalid configuration |

## Status meanings

| Status | Meaning |
|---|---|
| `PASS` | verified with evidence |
| `FAIL` | executed and did not pass |
| `BLOCKED` | cannot proceed (missing tool, env, credentials) |
| `NOT_RUN` | not attempted |
| `SKIPPED` | provider check skipped (no opt-in/credentials) — never a PASS |
| `NOT_APPLICABLE` | localhost-only target; TLS check not applicable |

## Command order

### AUTOMATED — run in this order

```bash
# 1. Preflight (read-only; reports runtime/repo/config/security)
python scripts/ops_preflight.py --expected-commit <certified-sha>

# 2. Staging certification sequence
python scripts/staging_certify.py --staging \
  --base-url http://localhost:8000 \
  --url https://staging.example.com \
  --prometheus-url http://localhost:9090 \
  --grafana-url http://localhost:3000 \
  --write-evidence

# 3. Backup (wraps scripts/backup.sh; refuses APP_ENV=production)
python scripts/staging_certify.py --backup --write-evidence

# 4. Restore drill (RESTORE_TARGET_DB is mandatory)
RESTORE_TARGET_DB=voxdesk_restore_drill \
  python scripts/staging_certify.py --restore \
  --dump backups/voxdesk-YYYYMMDD-HHMMSS.dump --write-evidence

# 5. Real-provider health (opt-in, read-only)
VOXDESK_REAL_INTEGRATION=1 \
  python scripts/provider_report.py --write-evidence

# 6. Observability (read-only)
python scripts/verify_observability.py \
  --prometheus-url http://localhost:9090 --grafana-url http://localhost:3000

# 7. TLS certification
python scripts/verify_tls.py --url https://staging.example.com

# 8. Egress verification (from INSIDE the staging container)
VOXDESK_STAGING_NETWORK=1 \
VOXDESK_EGRESS_ALLOW_HOSTS=api.twilio.com,api.deepgram.com,api.elevenlabs.io,api.openai.com,api.anthropic.com,generativelanguage.googleapis.com \
  python scripts/verify_egress.py

# 9. Deployment drill (deploy -> readiness -> rollback; never downgrades the DB)
python scripts/staging_certify.py --deploy-drill \
  --previous-sha <previous-good-sha> --write-evidence

# 10. Cost configuration validation
VOXDESK_PRICE_SOURCE_DATE=2026-09-13 python scripts/verify_cost_config.py

# 11. Re-evaluate the release gate (authoritative)
python scripts/release_gate.py
```

`--all-safe` runs modes 1, 2, 3, 5, 6, 8 and 9 in one invocation.

### HUMAN — never automated

1. **Real telephony E2E** — arm the E2E test tenant, physically dial the test
   number, and record the call in `docs/REAL-E2E-RESULT-RECORD.md` plus
   `var/release/e2e-result.json` (see `scripts/release/e2e-result.template.json`).
2. **Compliance sign-off** — review `docs/SECURITY.md`, `docs/BCP.md`,
   `docs/INCIDENT-RESPONSE.md` and the GDPR/DPA pack; record `compliance-001`.
3. **Dashboard/alert visual sanity check** — confirm Grafana renders and
   alert rules look correct before the alert test fire.
4. **Incident tabletop** — run the `docs/BCP.md` tabletop with responders;
   record `ir-001`.

### EXTERNAL — never self-certified

- **Third-party penetration test** — `pentest-001` remains `NOT_RUN` until an
  independent third party performs it and delivers a report.

## Evidence rules

- The toolkit writes evidence only with `--write-evidence`, merging into
  `scripts/release/evidence.json` and preserving history.
- A `BLOCKED` here is the honest result of a missing prerequisite — never
  hand-edit it into `PASS`.
- The release gate (`scripts/release_gate.py`) is the only authority on
  READY / NOT_READY / BLOCKED.

## Rollback

The deployment drill and `scripts/rollback.sh` roll back *code* only. Database
downgrades are never automatic: see `scripts/rollback.sh` and
`docs/DEPLOYMENT.md`. A restore uses `scripts/restore.sh` with an explicit
`RESTORE_TARGET_DB`.
