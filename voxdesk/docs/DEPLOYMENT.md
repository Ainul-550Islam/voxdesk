# VoxDesk — Deployment (staging & production)

This document is the operator runbook for taking VoxDesk from development →
staging → production. It covers the deployment dependency graph, environment
separation, the safe deploy/rollback sequence, migrations, backup/restore, TLS
and the security controls that hold it together. Anything that is *enforced*
here is enforced by code (`Settings.validate_security()`, `scripts/migrate.py`,
`scripts/deploy.sh`, `scripts/restore.sh`) and verified by
`tests/test_deployment.py` — not just prose.

## 1. Deployment dependency graph

Built from the repository, not assumed. Arrows mean "depends on to serve or
run correctly":

```
                     ┌──────────────┐
                     │    caddy     │  TLS termination, security headers,
                     │  (80/443)    │  request-size ceiling, WebSocket passthrough
                     └──────┬───────┘
                            │ reverse_proxy api:8000  (and grafana:3000 when
                            │ GRAFANA_DOMAIN is set)
                ┌───────────▼───────────┐
                │         api           │  FastAPI + built dashboard
                │  (127.0.0.1:8000)     │  entrypoint → migrate.py → uvicorn
                └──┬───────┬────────┬───┘
        ┌──────────▼──┐ ┌──▼──────┐ │ ┌──────────────┐
        │     db      │ │  redis  │ │ │  prometheus  │ scrapes api:8000/metrics
        │  PostgreSQL │ │ cache + │ │ └──────┬───────┘
        │  (internal) │ │ rate    │ │   ┌────▼─────┐
        └──────┬──────┘ │ limit   │ │   │ grafana  │ (127.0.0.1:3000, internal)
               │        └─────────┘ │   └──────────┘
        ┌──────▼──────────────────┐ │
        │  scheduler (worker)     │ │  reminders, campaigns, CRM sync,
        │  depends_on api+db+redis│ │  knowledge ingestion, billing
        └─────────────────────────┘ │  reconciliation, retention
        ┌───────────────────────────▼─┐
        │  backup (nightly pg_dump)   │  depends_on db: service_healthy
        └─────────────────────────────┘
```

Ordering is enforced in `docker-compose.prod.yml`: `api` waits for `db` and
`redis` to be *healthy*; `scheduler` waits for `db` healthy and `api` started;
`backup`, `prometheus` and `caddy` wait for their upstreams. `db` and `redis`
expose **no host ports** — they are reachable only on the compose network —
and `api`/`grafana` bind to `127.0.0.1` so nothing can bypass Caddy.

## 2. Environments

Three environments, strictly separated:

| | development | staging | production |
|---|---|---|---|
| Compose file | `docker-compose.yml` | `docker-compose.staging.yml` | `docker-compose.prod.yml` |
| `APP_ENV` | `development` | `staging` | `production` |
| Env file | `.env` | `.env.staging` | `.env` |
| Schema owner | `create_all` (dev convenience) | **Alembic only** | **Alembic only** |
| DB/Redis/volumes | local | `*_staging` (separate) | `pgdata`/`redisdata` |
| Secrets | dev placeholders | **its own** (`secrets-staging/`) | `secrets/` |
| Stripe | — | test-mode keys only | live keys |
| E2E guard (Step 5) | allowed | allowed (default off) | **refused at boot** |
| Failure injection | allowed | allowed | refused |
| Host ports | `8000`, `3000` | `8001`, `3001`, `8080/8443` | `8000`, `3000`, `80/443` |

Rules:

* **Never copy production secrets into staging.** Staging uses its own
  `.env.staging`, its own `secrets-staging/` directory, its own volumes
  (`pgdata_staging`, …), its own database and its own test-mode provider
  credentials (Twilio test account, Stripe `sk_test_*`, etc.). Template:
  `.env.staging.example`.
* `validate_security()` treats staging as **not production** (E2E and failure
  injection are permitted) but also as **not development** (no `create_all`,
  Alembic owns the schema). Staging therefore exercises the same migration,
  readiness and proxy path production does, without touching production data.
* `KNOWN_APP_ENVS = {development, test, staging, production, prod}` — any other
  `APP_ENV` fails at boot.

### Sandbox limitations (this repository's environment)

This workspace has no Docker daemon, so container-level staging/production
validation cannot run here. PostgreSQL 17 **is** available locally, so
migration and restore logic can be exercised against it. Everywhere else the
validation is static and deterministic (see `tests/test_deployment.py`). Real
runtime claims are never fabricated: when a check needs Docker or a live
stack, that blocker is stated explicitly rather than simulated.

## 3. Production configuration (fail-closed)

`Settings.validate_security()` runs at startup and, in production, **refuses
to boot** on any of:

* `RATE_LIMIT_ENABLED` not `true`
* `LOG_LEVEL=DEBUG`
* `PUBLIC_BASE_URL` not `https://`, or containing `localhost`/`127.0.0.1`
* any `CORS_ORIGINS` entry that is not `https://`
* placeholder-looking secrets (markers like `change-me`, `xxxx`,
  `placeholder`, …) in `SECRET_KEY`, `JWT_SECRET`, `TWILIO_AUTH_TOKEN`,
  `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, the LLM keys, `STRIPE_*`
* `TWILIO_AUTH_TOKEN` empty, or `TWILIO_SKIP_WEBHOOK_VERIFY` enabled
* `E2E_ENABLED` set (real-call E2E is operator-run in a test environment only)
* `KNOWLEDGE_EMBEDDING_PROVIDER=hashing` (development stub)
* `CRM_ENCRYPTION_KEYS` empty (provider credentials would be stored plaintext)
* `BILLING_PROVIDER=stripe` with a `sk_test_*` key, or
  `BILLING_UNLIMITED_ENTITLEMENTS` enabled
* `JWT_SECRET` still the default or shorter than 32 chars; `SECRET_KEY` default

There is **no second configuration system** — everything lives in
`app/core/config.py` on the existing `Settings` architecture. `uses_https` is
derived from the actual `PUBLIC_BASE_URL` scheme, so the Secure-cookie flag
(`auth_routes.py`) and the HSTS header (`security_headers.py`) follow the real
TLS posture instead of guessing from the environment name.

## 4. Deploy sequence (safe, single-VM)

`scripts/deploy.sh` (idempotent; flags `--no-build`, `--no-backup`, `--no-pull`):

1. `git pull --ff-only` (skipped under rollback)
2. build images
3. **pre-deploy backup** of the database, then **verify** the dump with
   `pg_restore --list`. A deploy that cannot back up (or whose backup fails
   verification) aborts before touching the schema — the backup is the
   rollback point.
4. **migrations under an advisory lock** — `scripts/migrate.py` (below)
5. recreate containers onto the new image
6. wait up to 120 s for `/health/ready`
7. verify `/health` and report

On any failure the script prints the rollback command. It **never** downgrades
the database automatically.

### Honest zero-downtime statement

This is a **single-VM, single-Postgres topology**, so it **does not guarantee
zero-downtime**. `docker compose up -d` recreates the `api` container, which
means a brief (seconds) connection drop on the public path while Caddy
retries. There is no blue/green deployment, no load balancer, and no
online/offline schema pattern. Migrations run *before* the new code serves, so
requests are not served against a half-migrated schema; that is the only
uptime guarantee claimed. If true zero-downtime is required, the deployment
must grow a second host, a load balancer and an expand/contract migration
cadence — out of scope for this step and stated here rather than pretended.

## 5. Migrations and rollback

`scripts/migrate.py` is the **only** sanctioned way the API applies migrations
in staging/production. It acquires a session-level PostgreSQL advisory lock
(`pg_try_advisory_lock`, key `7272_0011`) and then runs
`alembic upgrade head` in a subprocess. Concurrent container starts serialize
on the lock instead of racing DDL. If the lock is not acquired within
`MIGRATE_LOCK_TIMEOUT_SECONDS` (default 600 s) it fails loudly. Startup never
silently skips migrations: the entrypoint runs `migrate.py` and exits on
failure, so the container never serves with a stale schema.

Migration chain (`alembic/versions/`): a single linear head at
`0011_side_effect_exactly_once` with no gaps back to `0001_baseline`
(enforced by `tests/test_deployment.py`). `alembic.ini`/`env.py` use the
SQLAlchemy URL from settings.

**Rollback policy:**

* `scripts/rollback.sh <git-sha>` checks out the sha and runs
  `deploy.sh --no-pull`. It records the *applied* migration revision first, so
  the operator can see whether the target sha predates the schema.
* **It never runs `alembic downgrade`.** Forward migrations can be destructive
  in principle (a `downgrade` can drop columns and lose data). Downgrades are
  a separate, explicitly-authorized, operator-run action.
* Migrations are written **additive/backward-compatible wherever practical**,
  so an old app against a newer schema keeps working — code rollback then
  needs no schema rollback. When a change cannot be backward-compatible it is
  released as its own step and documented as "no code rollback past this point
  without a manual downgrade".

## 6. Backup and restore

**Schedule/format/retention:** the `backup` service (and `scripts/backup.sh`)
take a nightly `pg_dump` custom-format (`-Fc`) archive; the last 14 are kept
locally; optional `rclone` sync to an S3-compatible remote when `RCLONE_REMOTE`
is set. **Off-site is required, not optional**: backups must not live only in
the same failure domain as the primary database — keep `RCLONE_REMOTE` (or an
equivalent) pointed somewhere outside the primary VM.

**Verification:** every dump — nightly, pre-deploy, or manual — is verified
with `pg_restore --list` before it is retained or synced; an unreadable dump
is reported and never counted as a backup (`backup_verify.sh` does this
standalone). `backup.sh` exits non-zero on an unreadable dump.

**Restore (`scripts/restore.sh`):**

1. verifies the dump with `pg_restore --list` **before writing anything**
2. refuses to restore over a database that already has tables unless
   `RESTORE_ALLOW_OVERWRITE=1` — a drill can never silently clobber a live DB
3. `RESTORE_TARGET_DB=voxdesk_restore_drill` restores into a **scratch**
   database for safe, non-production drills
4. after restoring, re-counts tables and fails if nothing landed

Post-restore verification (operator checklist): confirm the Alembic revision
(`alembic current`), spot-check tenant rows, billing/subscription rows, call
records and audit rows, then run `/health/ready` and `scripts/smoke_test.py`.
A restore that has not actually been exercised is not trusted.

## 7. Redis recovery classification

Redis is **ephemeral / cache-only** in this architecture: it holds the
per-IP rate-limit counters and the cache. The compose stack gives it an AOF
volume (`redisdata`) for convenient restart, but no correctness-critical state
lives in Redis — call state, tenant data, billing and audit all live in
PostgreSQL. The application already treats Redis as best-effort: on any Redis
error, reads return cache-miss, writes are dropped, and `ping()` reports
unreachable (`app/core/cache.py`). **Recovery is therefore "wipe and restart"**
— a lost cache is repopulated on demand and rate-limit counters reset, which
is an acceptable, documented behavior (not a data-loss event). This is pinned
by `tests/test_deployment.py::test_redis_cache_degrades_gracefully_when_down`.

## 8. TLS / reverse proxy

Caddy is the public entry point (80/443; automatic Let's Encrypt when `DOMAIN`
is set). `Caddyfile`:

* `admin off` — the admin control plane is not exposed
* security headers at the proxy layer (HSTS, `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy`), mirrored by the app middleware
* `request_body max_size 25MB` — above the 20 MB knowledge-upload ceiling
* **WebSocket upgrade passes through unchanged** — the Twilio Media Streams
  handshake depends on it, and nothing in the file interferes
* Grafana is only exposed when `GRAFANA_DOMAIN` is explicitly set; otherwise
  it is reachable only inside the compose network

The API trusts `X-Forwarded-*` via `--proxy-headers` (entrypoint) so
`PUBLIC_BASE_URL`, `wss://` and Twilio signature validation work; tighten
`FORWARDED_ALLOW_IPS` from the default `*` in hardened setups.

## 9. Monitoring exposure

Prometheus and Grafana are **internal**. Prometheus has no host ports at all;
Grafana binds `127.0.0.1:3000` and is only proxied when `GRAFANA_DOMAIN` is
set. The API `/metrics` scrape endpoint is gated by `METRICS_TOKEN` when set
(`app/core/metrics.py::_scrape_authorized`). The Caddyfile contains no
`/metrics` route. `tests/test_deployment.py` pins all of these.

## 10. Secrets and supply chain

* No secrets in Git: `.gitignore` covers `.env`, `secrets/`, `backups/`,
  `*.dump`, `*.pem`, `*.key`, `.netrc`; gitleaks runs in CI; `.env.example`
  and `.env.staging.example` contain placeholders only (asserted by tests).
* No secrets in the image or build context: `.dockerignore` excludes the same
  set, and the Dockerfile never copies `.env`.
* No secrets in logs/errors: Sentry DSN is the only observability secret and
  is never logged or returned by the API (existing behavior, unchanged).
* **Dockerfile**: multi-stage, runs as non-root `appuser` (UID 10001),
  `HEALTHCHECK`, pinned base `python:3.12-slim-bookworm` (see the comment in
  the file — the pin is a deliberate change policy, not a blind upgrade), no
  build tooling in the runtime stage.
* **Dependency/image scanning**: CI runs `bandit` (SAST), `pip-audit`
  (Python dependency CVEs), `npm audit --omit=dev --audit-level=high`
  (shipped frontend dependencies) and gitleaks; compose images are pinned
  (`postgres:16-alpine`, `redis:7-alpine`, `prom/prometheus:v2.53.0`,
  `grafana/grafana:11.1.0`, `caddy:2.9-alpine`).

### Accepted risks (documented, not hidden)

* `npm audit` (full, including dev) reports a **moderate** advisory in the
  Vitest *test tooling* (`@vitest/mocker` redirect mock, fixed only by a
  breaking Vitest major). It does not affect shipped code: `npm audit
  --omit=dev` is clean. Mitigation: upgrade Vitest on its next major in a
  dedicated change; do not `--force` a breaking upgrade mid-step.
* `FORWARDED_ALLOW_IPS` defaults to `*` for single-proxy deployments; document
  and tighten when a second proxy is introduced.

## 11. CI/CD and deploy authorization

* Normal CI (every push/PR): backend lint + tests, Postgres migration round
  trip (`alembic upgrade head` + `downgrade base`), frontend tests + build +
  production audit, Docker image build.
* **Real-provider tests are separate and manual** (`real-integrations.yml`,
  `workflow_dispatch`, gated on `VOXDESK_REAL_INTEGRATION=1` and the
  `real_provider` marker). They are never in the default CI path.
* **Production deployment requires explicit authorization**: there is no
  automatic deploy-to-prod step. Deploys are operator-run via
  `scripts/deploy.sh`; the safe defaults (backup + verified migration +
  readiness wait) are the authorization gate.

## 12. Staging smoke test

`scripts/smoke_test.py` is the deterministic, read-only deployment smoke test:
it exercises `/health`, `/health/ready`, the dashboard shell, `/metrics`
(200/401/404 are all legitimate), `/auth/login` (a bogus login must 4xx,
never 500), `/api/tenants` (anonymous must be refused), the Twilio webhook
(unsigned POST must 403 — proving signature verification is fail-closed), the
provider-config presence and the E2E guard state. It performs **no** writes
and **no** external calls; non-zero exit gates the deploy.

```bash
SMOKE_BASE_URL=http://localhost:8001 python scripts/smoke_test.py   # staging
```
