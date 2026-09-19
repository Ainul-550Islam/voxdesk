# Step 8 — Deployment & Disaster-Recovery Lifecycle Hardening: Final Report

Branch: `step9/scale-compliance` · Step 8 commits: `1d359de`, `e9f5455` (26 commits ahead of `main`) · Working tree clean.

---

## 1. Summary

Step 8 hardened the deployment lifecycle so VoxDesk can move development →
staging → production while preserving data integrity, tenant isolation, secrets
safety, migrations, backups, restore, observability, rollback, service
ordering, provider config, TLS and scheduler/worker correctness.

Concretely it delivered: a fail-closed production configuration and a dedicated,
isolated staging environment; a controlled, advisory-locked migration step;
a safe deploy sequence with a verified pre-deploy backup and readiness wait;
a code-only rollback that never auto-downgrades the database; verified,
overwrite-guarded backup/restore with drill support; a hardened reverse proxy
and image; CI supply-chain gates; a deterministic 32-test deployment suite; a
read-only smoke test; and full deploy/DR/incident runbooks.

Because the sandbox has **no Docker daemon but a live PostgreSQL 17**, the
migration chain, the locked migration runner, and the full backup → verify →
restore → overwrite-guard path were **actually executed against PostgreSQL 17**
(scratch databases only, production DB untouched), and a staging-mode API was
booted and passed the smoke test end-to-end. Nothing runtime was fabricated;
the one hard blocker (Docker) is recorded below.

---

## 2. Audit findings

Built from the files, not assumptions (item A):

* **Dependency graph** (see `docs/DEPLOYMENT.md` §1): `caddy` (TLS + headers +
  body cap + WebSocket passthrough) → `api` → `db` (PostgreSQL) + `redis`
  (cache/rate-limit) + `prometheus` → `grafana`; `scheduler` (worker) and
  `backup` fan out from `db`. Ordering enforced with `depends_on:
  service_healthy`. `db`/`redis` expose no host ports; `api`/`grafana` bind
  `127.0.0.1`.
* **Backup** was nightly `pg_dump` (custom format), 14 retained, optional
  rclone — but **no integrity check** before retention/sync.
* **Restore** was `pg_restore --clean --if-exists` straight into the configured
  DB — **no overwrite guard, no drill path**.
* **Deploy** was `git pull` → rebuild → throwaway-container migration → `up -d`
  — **no readiness wait, no pre-deploy backup, no rollback script**.
* **Rollback** existed only as prose in BCP.md.
* **Caddy** was WebSocket-friendly but had `admin` API on, no request-body
  ceiling, and no proxy-level security headers.
* **Dockerfile** was already non-root (`appuser`, UID 10001) and multi-stage;
  base was unpinned (`python:3.12-slim`) — now pinned with a documented policy.
* **CI** already ran backend lint/tests, a Postgres migration round trip,
  frontend tests/build, bandit, pip-audit and gitleaks; real-provider tests
  were already a manual `workflow_dispatch`. Gaps: no production-dependency
  audit gate, no Docker image build in CI.

---

## 3. Staging changes (item B)

New `docker-compose.staging.yml` + `.env.staging.example`:

* `APP_ENV=staging` — treated by code as **not production** (E2E/failure
  injection permitted) and **not development** (no `create_all`; Alembic owns
  schema).
* Separate everything: volumes (`pgdata_staging`, …), host ports (`api`
  `127.0.0.1:8001`, Grafana `127.0.0.1:3001`, Caddy `8080/8443`), secrets
  dir `secrets-staging/`, backups in `./backups-staging`, `.env.staging`.
* Documented rule: **never copy production secrets**; use Twilio test account /
  Stripe `sk_test_*` / dedicated staging tenants.
* `KNOWN_APP_ENVS` now includes `staging`; unknown values fail at boot.

## 4. Production hardening (item C)

`Settings.validate_security()` (existing Settings architecture — **no duplicate
config system**) now additionally refuses to boot production on: rate limiting
off, `LOG_LEVEL=DEBUG`, non-`https` or localhost `PUBLIC_BASE_URL`, any
non-`https` CORS origin, placeholder-looking secrets, `TWILIO_SKIP_WEBHOOK_VERIFY`,
`E2E_ENABLED`, `hashing` embedding provider, missing `CRM_ENCRYPTION_KEYS`,
`sk_test_*` Stripe key or unlimited entitlements in prod. `uses_https` is
derived from the URL scheme so Secure cookies and HSTS follow real TLS posture;
`create_all` now runs only in `development`/`test`, so staging mirrors prod's
Alembic ownership.

## 5. Docker hardening (item D)

`Dockerfile`: base pinned to `python:3.12-slim-bookworm` (comment documents the
change policy — no blind upgrades); already non-root with `HEALTHCHECK`.
`docker-compose.prod.yml`: API `init: true` + `stop_grace_period: 30s`, scheduler
grace period, health-gated `depends_on`, Grafana hardened (`GF_SECURITY_ADMIN_USER`,
sign-up/org-create off), backup service now **verifies each dump before
retention/sync**. `.dockerignore`/`.gitignore` exclude `.env`, `secrets/`,
`backups/`, `*.dump`, `*.pem`, `*.key`, `.netrc`. No secrets baked, no build
tools in the runtime stage.

## 6. Migration & rollback (items E/F/G)

* `scripts/migrate.py` is the only sanctioned migration entry in staging/prod:
  `alembic upgrade head` under a session-level PostgreSQL advisory lock
  (`pg_try_advisory_lock`, key `7272_0011`, timeout 600 s). Startup never
  silently skips migrations — the entrypoint exits on failure.
* Chain audited: linear head `0011_side_effect_exactly_once`, no gaps from
  `0001_baseline` (pinned by test).
* `scripts/deploy.sh`: pull → build → **pre-deploy backup + `pg_restore --list`
  verify (abort on failure)** → locked migrate → recreate → readiness wait
  (120 s) → liveness report. Flags `--no-build/--no-backup/--no-pull`.
* `scripts/rollback.sh <sha>`: code-only rollback (checkout + `deploy.sh
  --no-pull`); records the applied migration; **never runs `alembic downgrade`**
  (pinned by test). Downgrade is an explicit, operator-only action.
* **Honest zero-downtime statement** (item F): single-VM/single-Postgres —
  **no zero-downtime guarantee**; only "migrations run before new code serves"
  is claimed. Documented, not pretended.

## 7. Backup & restore (items H/I/J)

* `backup.sh`/nightly service: dump → **verify with `pg_restore --list`** →
  retain 14 → optional rclone; unreadable dump exits non-zero and is never
  retained/synced. `backup_verify.sh` re-verifies any dump standalone.
* `restore.sh`: verify dump **before writing** → **refuse to overwrite a
  non-empty DB unless `RESTORE_ALLOW_OVERWRITE=1`** → `RESTORE_TARGET_DB=<scratch>`
  drills into a separate database → post-restore table-count verification.
* Off-site is mandatory, not optional; credentials never hardcoded (env only).
* Deterministic restore test executed against PostgreSQL 17 (see §15).

## 8. Redis recovery (item K)

Redis is **ephemeral/cache-only** (rate-limit counters + cache). Correctness-
critical state lives in PostgreSQL. Recovery is wipe-and-restart; the app
already degrades gracefully (cache-miss on read, drop on write) — pinned by
`test_redis_cache_degrades_gracefully_when_down`.

## 9. TLS / reverse proxy (item L)

`Caddyfile`: `admin off`, HSTS + `X-Content-Type-Options` + `X-Frame-Options:
DENY` + `Referrer-Policy` at the proxy, `request_body max_size 25MB` (above the
20 MB upload ceiling), WebSocket upgrade preserved for the Twilio media stream.
API trusts `--proxy-headers`; `FORWARDED_ALLOW_IPS` documented to tighten.

## 10. Security findings & fixes (items M/N)

* Monitoring endpoints not public: Prometheus has no host ports; Grafana
  `127.0.0.1` + explicit `GRAFANA_DOMAIN` opt-in; `/metrics` token-gated when
  `METRICS_TOKEN` set; no `/metrics` route in Caddy.
* No secrets in Git/build/logs/bundle: gitignore/dockerignore/CI gitleaks;
  placeholder-only examples; Dockerfile never copies `.env`; Sentry DSN never
  logged/returned (unchanged).
* Dependency/image scanning: bandit, pip-audit, `npm audit --omit=dev`, pinned
  compose images. **Accepted risks documented**: full `npm audit` shows 2
  moderate **dev-only** Vitest tooling advisories (production deps: 0
  vulnerabilities); `FORWARDED_ALLOW_IPS=*` default for single-proxy setups.

## 11. CI/CD findings & fixes (items O/P)

Added to `ci.yml`: frontend **production-dependency audit gate** (`npm audit
--omit=dev --audit-level=high`) and a **Docker image build** job. Real-provider
tests remain manual (`workflow_dispatch`, `real_provider` marker, excluded from
normal CI). Production deployment requires explicit operator authorization —
no auto-deploy-to-prod step exists.

## 12. Staging smoke test (item Q)

`scripts/smoke_test.py` — read-only, deterministic, no writes and no external
calls: liveness, readiness, dashboard shell, `/metrics` (200/401/404 all
legitimate), `/auth/login` (bogus creds → 401), `/api/tenants` (anonymous →
401), `/telephony/voice` (unsigned → 403 fail-closed), provider-config presence,
E2E-guard state. Non-zero exit gates the deploy.

## 13. DR & incident runbooks (items R/S)

`docs/BCP.md` rewritten: RPO/RTO grounded in architecture (§ below); DR-1
(host loss), DR-2 (Postgres corruption), DR-3 (Redis loss); and 12 incident
runbooks: failed deploy, migration failure, Redis outage, provider outage, TLS
failure, backup failure, restore-drill failure, runaway cost, scheduler down,
CRM outage, calendar double-booking, billing backlog. `docs/DEPLOYMENT.md`
rewritten as the deploy runbook.

## 14. Tests (item T)

`tests/test_deployment.py` — 32 deterministic tests over the 16 listed
deployment/infra concerns (config fail-closed, health endpoints,
Caddy/WebSocket, migration chain, backup/restore safety, secret exposure,
compose isolation, CI workflow, metrics gating, Redis degradation, rollback
guard). `tests/test_security_headers.py` updated for scheme-driven HSTS.

## 15. Validation commands & results (items U/W)

| Gate | Result |
|---|---|
| Backend suite | `python -m pytest -q` → **2182 passed, 43 skipped**, 9074 warnings |
| Lint | `ruff check app/ tests/ scripts/ loadtest/` → **All checks passed** |
| Frontend tests | `npm test` → **362 passed** (14 files) |
| Frontend build | `npm run build` → **success** (52 modules) |
| Frontend prod audit | `npm audit --omit=dev --audit-level=high` → **0 vulnerabilities** |
| Config/deploy YAML | `yaml.safe_load` on prod/staging compose + CI workflows → OK |
| Compile | `python -m compileall app scripts` → OK |

**Runtime validation against the available PostgreSQL 17** (scratch DBs only;
the real `voxdesk` DB was never touched):

```
DATABASE_URL=…/voxdesk_s8 python -m alembic upgrade head
  → clean 0001_baseline → 0011_side_effect_exactly_once, 28 tables
  → alembic current: "0011_side_effect_exactly_once (head)"
DATABASE_URL=…/voxdesk_s8 python scripts/migrate.py
  → "lock acquired", idempotent no-op, exit 0
sh scripts/backup.sh /tmp/…            → dump written, pg_restore --list verified
sh scripts/backup_verify.sh <dump>     → "OK: readable custom-format dump"
RESTORE_TARGET_DB=voxdesk_s8_drill sh scripts/restore.sh <dump>
  → restored + verified 28 tables
RESTORE_TARGET_DB=voxdesk_s8_drill sh scripts/restore.sh <dump>   (no overwrite flag)
  → REFUSED, exit 3 ("target database already has 28 tables")
RESTORE_TARGET_DB=voxdesk_s8_drill RESTORE_ALLOW_OVERWRITE=1 …    → restored OK
uvicorn app.main:app (APP_ENV=staging, port 8001) then:
SMOKE_BASE_URL=http://localhost:8001 python scripts/smoke_test.py
  → 8 pass / 0 fail / 1 warn (warn = provider keys absent in the bare staging boot), exit 0
```

All scratch databases and temp dumps were dropped/removed afterwards.

## 16. Environment blockers (item U, honest)

* **No Docker daemon** in this workspace → compose/container runtime and the
  image build were validated **statically** only (YAML parse + invariant
  tests + `docker build` added to CI for the real runner). No container
  behaviour is claimed.
* **No real provider credentials** (Twilio/Deepgram/LLM/ElevenLabs/Stripe) and
  no real phone numbers → the real-telephony E2E remains a manual,
  human-controlled task (Step 5 runbook) and was **not** run or simulated.
* **No off-site object store** → `RCLONE_REMOTE` sync is configured but not
  exercised here.

## 17. RPO / RTO (item R)

| Metric | Target | Basis |
|---|---|---|
| RPO | ≤ 24 h worst case | nightly 02:00 UTC dump + a fresh verified pre-deploy dump at every deploy |
| RTO | ≤ 4 h | restore dump into fresh Postgres + `scripts/deploy.sh` on a new VM (single-VM topology) |

Reducing RPO needs continuous WAL archiving off-site — recorded as a known gap,
not silently claimed.

## 18. Changed files (18)

`app/core/config.py` · `app/main.py` · `app/api/auth_routes.py` ·
`app/core/security_headers.py` · `tests/test_config.py` ·
`tests/test_security_headers.py` · `scripts/entrypoint.sh` ·
`scripts/deploy.sh` · `scripts/backup.sh` · `scripts/restore.sh` ·
`docker-compose.prod.yml` · `Caddyfile` · `Dockerfile` · `.dockerignore` ·
`.gitignore` · `.github/workflows/ci.yml` · `docs/DEPLOYMENT.md` ·
`docs/BCP.md`

## 19. New files (7)

`.env.staging.example` · `docker-compose.staging.yml` · `scripts/migrate.py` ·
`scripts/rollback.sh` · `scripts/backup_verify.sh` · `scripts/smoke_test.py` ·
`tests/test_deployment.py`

All of the above exist in full in the workspace (source of truth).

## 20. What remains before Step 9

1. **Human-controlled real E2E** via the Step 5 runbook (operator dials the
   allowlisted test number on the test tenant) — never automated.
2. **Staging dashboard/alert sanity check** (human, on a real staging host).
3. **Populate `COST_UNIT_PRICES`** (Step 7 operational follow-up) so the cost
   metric stops reporting `cost_known=0`.
4. Provision real staging + production hosts (Docker, real domains/certs,
   real test/live provider credentials) and run `deploy.sh` → `rollback.sh` →
   `restore.sh` drills on that infrastructure.
5. Configure the off-site backup target (`RCLONE_REMOTE`) and run a real sync.
6. Tighten `FORWARDED_ALLOW_IPS` if a second proxy is introduced.
7. Upgrade Vitest on its next major (accepted moderate dev-only advisory).

No real external mutation was performed automatically (item V): every runtime
validation used scratch databases and a loopback staging-mode API, and the
real `voxdesk` database was left untouched.
