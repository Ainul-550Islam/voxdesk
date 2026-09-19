# VoxDesk — Business continuity & disaster recovery runbooks

Ground rules for every runbook below:

* **Never fabricate success.** A restore that was not executed is not "passed";
  a backup that failed its integrity check is not a backup. Every verification
  step here is a real command with a real success criterion.
* **No automatic destructive action.** Code rollback never downgrades the
  database; a restore never overwrites a live database without an explicit
  `RESTORE_ALLOW_OVERWRITE=1`.
* **Read the honest uptime statement** in `docs/DEPLOYMENT.md` §4 before
  claiming any zero-downtime property.

## Recovery objectives (grounded in architecture)

| Metric | Target | Justification |
|---|---|---|
| **RPO** | ≤ 24 h (worst case) | Nightly `pg_dump` (02:00 UTC) + a fresh pre-deploy dump at every deploy. Best case is the moment of the last deploy backup. |
| **RTO** | ≤ 4 h | Restore a dump into a fresh Postgres + `scripts/deploy.sh` on a new VM. Single-VM topology: no quorum or cluster state to rebuild. |
| **MTTR (provider outage)** | minutes | Providers are multi-sourced for LLM (fallback chain); Twilio/Deepgram outages degrade the voice path, not data. |

RPO can only be reduced by continuous WAL archiving to off-site storage —
documented as a known gap, not silently claimed. RTO assumes the off-site dump
is reachable; if it lives in the same failure domain as the primary, RTO is
unbounded (see DEPLOYMENT.md §6 — off-site is mandatory).

---

## DR-1 — VM lost / host failure (full outage)

1. Provision a new host: the repo, Docker, and access to the off-site backups
   (`RCLONE_REMOTE`) and the production `.env`/`secrets/`.
2. Restore secrets: `.env`, `secrets/`, `secrets/google_service_account.json`.
3. `docker compose -f docker-compose.prod.yml up -d db redis`
4. `scripts/restore.sh <latest-dump>` (it verifies the dump, then restores).
5. `scripts/deploy.sh` (applies migrations, waits for readiness).
6. Verify: `curl -fsS localhost:8000/health/ready`, `scripts/smoke_test.py`,
   and a live test call on the production number.

## DR-2 — PostgreSQL corruption

1. Stop writes: `docker compose -f docker-compose.prod.yml stop api scheduler`.
2. Confirm the failure (logs, `pg_isready`, disk) before touching anything.
3. Restore into a **scratch** database first (drill, never the live one):
   `RESTORE_TARGET_DB=voxdesk_restore_drill scripts/restore.sh <dump>`.
4. Verify the scratch: table counts, `alembic current`, tenant/billing/call/
   audit spot checks.
5. Only then restore over the live DB with `RESTORE_ALLOW_OVERWRITE=1`.
6. Restart `api`/`scheduler`; run the smoke test.

## DR-3 — Redis loss

Redis is **cache/rate-limit only** (DEPLOYMENT.md §7). Wipe and restart:

1. `docker compose -f docker-compose.prod.yml up -d redis` (volume or not).
2. Confirm the API is healthy regardless: `/health/ready` must pass even if
   Redis is briefly down (the app degrades to cache-miss and rate-limit
   reset, by design).
3. No data restoration step exists or is needed.

## INC-1 — Failed deploy

1. Deploy output shows which stage failed (backup / migrate / recreate /
   readiness). The script already printed the rollback command.
2. If readiness failed: `docker compose -f docker-compose.prod.yml logs --tail 100 api`
   — is it a migration problem, a config problem (`validate_security`), or a
   dependency problem?
3. Roll back the **code**: `scripts/rollback.sh <previous-good-sha>`. This
   rebuilds and re-runs forward migrations; it never downgrades the DB.
4. If the schema itself is the problem, see DEPLOYMENT.md §5 for the manual,
   operator-only downgrade path — never automatic.

## INC-2 — Database migration failure mid-deploy

1. The advisory lock (`scripts/migrate.py`) means no other starter is racing
   DDL; check `MIGRATE_LOCK_TIMEOUT_SECONDS` if a starter was stuck.
2. Inspect `alembic current` vs the migration files. Do **not** hand-edit the
   schema or `alembic_version`.
3. Fix forward (write the corrective migration) rather than downgrading; if a
   downgrade is unavoidable, do it by hand after a verified backup.

## INC-3 — Redis outage at runtime

1. Symptom: elevated latency, rate-limit counters reset, cache misses.
2. Confirm: `docker compose ... ps redis`; the app's `/health/ready` may still
   pass (Redis is best-effort).
3. Restart Redis (`up -d redis`); the app reconnects automatically on next use
   (no reconnect code path needed — connections are created lazily).

## INC-4 — Provider outage (Twilio / Deepgram / LLM / ElevenLabs)

1. LLM: the fallback chain retries the next configured provider; confirm at
   least one alternative key is configured.
2. Twilio/Deepgram: calls fail or degrade. Alert on the provider-side status
   page; no VoxDesk data is lost. Restore is automatic when the provider
   recovers; optionally fail calls gracefully per `docs/CALL-LIFECYCLE.md`.

## INC-5 — TLS / certificate failure

1. Caddy obtains/renews Let's Encrypt automatically; check
   `docker compose ... logs caddy` for renewal errors.
2. If `DOMAIN` is wrong/unset, Caddy serves plain HTTP — production boot of
   the **API** already refuses that (`PUBLIC_BASE_URL` must be `https://`).
3. Renew/verify: restart Caddy; confirm the browser gets a valid cert and HSTS.

## INC-6 — Backup failing (dump unreadable / empty)

1. The nightly service and `backup.sh` both verify with `pg_restore --list`;
   a failure is logged loudly and **previous dumps are retained**.
2. Run `scripts/backup.sh` manually and read its error. Check disk space
   (`backups/` volume), `PGHOST`/credentials from the environment, and whether
   Postgres is healthy.
3. Fix and re-run; confirm a new dump passes `scripts/backup_verify.sh` and is
   synced off-site.

## INC-7 — Restore drill failed

1. The drill (`RESTORE_TARGET_DB=…`) either refused (target already has
   tables) or landed zero tables.
2. Refusal = the guard working; point `RESTORE_TARGET_DB` at a fresh name.
3. Zero tables = the dump is bad or the restore errored; re-verify the dump
   (`backup_verify.sh`) and re-run with `set -x` for the full error.
4. Record the drill result and timing in the quarterly restore log.

## INC-8 — Runaway cost (provider spend)

1. Trigger: `COST_UNIT_PRICES` metrics show a cost spike (see
   `docs/COST-AWARENESS.md`, Step 7 cost metric with `cost_known`).
2. First cut spend: disable the expensive voice path (tenant greeting checks,
   `BILLING_ENFORCE_ENTITLEMENTS`), throttle the scheduler, or stop the
   scheduler service.
3. Confirm `COST_UNIT_PRICES` is populated and correct — the cost metric
   reports `cost_known=0` (unknown) rather than an invented number when it is
   not (see Step 7 operational follow-up).
4. Review per-tenant usage; alert threshold tuning lives in
   `observability/alerts.yml` and `docs/SLO-ALERTS.md`.

## INC-9 — Scheduler (worker) down / stuck

1. `docker compose ... ps scheduler`; the compose stack has `restart:
   unless-stopped` and self-healing loops (lease reclaim for reminders,
   processing-timeout reset for stuck knowledge documents).
2. Restart: `up -d scheduler`. Idempotency guarantees (Step 6 exactly-once:
   reminder leases, webhook receipts) mean a crash/restart cannot double-send
   a reminder or double-process a webhook.
3. Check the scheduler's own metrics port (`SCHEDULER_METRICS_PORT`) for job
   backlog.

## INC-10 — CRM integration outage / failure

1. See `docs/CRM-INTEGRATIONS.md` and `docs/CRM-AUDIT.md`. CRM webhooks are
   replay-protected (receipt tables), and stored credentials are
   application-level encrypted (`CRM_ENCRYPTION_KEYS`) — a leak is bounded.
2. A tenant's broken CRM token fails that tenant's sync only, never the API.
   Rotate the tenant's credential via the dashboard.

## INC-11 — Calendar outage / double-booking suspicion

1. See `docs/CALENDAR-INTEGRATIONS.md` and `docs/CALENDAR-AUDIT.md`.
2. Calendar webhooks are replay-protected; a suspected double-booking is
   checked against the booking audit trail (booking ID, provider event ID).
3. On provider outage, the scheduler retries with backoff; no silent drops.

## INC-12 — Billing backlog / reconciliation drift

1. See `docs/BILLING.md` and `docs/BILLING-AUDIT.md`.
2. Stripe webhooks are signature-verified (`STRIPE_WEBHOOK_SECRET`) and the
   scheduler reconciles periodically; a backlog is visible in the scheduler
   metrics and the billing audit tables.
3. Never hand-edit entitlements; fix forward via reconciliation or a
   documented, versioned repair script.

---

## Test cadence

* **Quarterly:** full restore drill into a scratch database, timed against the
  4 h RTO. Record the result — an untested backup is not a backup.
* **Every deploy:** the pre-deploy backup's integrity check *is* a live
  backup verification.
* **Post-incident:** update the runbook that was followed if any step did not
  match reality.
