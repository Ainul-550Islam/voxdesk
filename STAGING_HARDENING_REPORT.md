# VoxDesk — Staging Runtime Configuration Hardening
### Four-file change set + validation report

- **Date:** 2026-09-13 (Asia/Dhaka)
- **Repo:** `/home/user/voxdesk` — branch `step12/execution-verification`, HEAD `4909435`
- **Scope:** harden the staging runtime configuration using **only** these four files, without touching business logic:

| File | Old bytes | New bytes | Status |
|---|---|---|---|
| `docker-compose.staging.yml` | 5859 | 9604 | rewritten (working tree) |
| `Caddyfile` | 1705 | 3148 | rewritten (working tree) |
| `.env.staging.example` | 5170 | 16188 | rewritten (working tree) |
| `scripts/staging_certify.py` | 21413 | 30782 | rewritten (working tree) |

**Changed-file list (complete):** exactly the four files above.
**Confirmation:** `git status --short` shows only ` M .env.staging.example`, ` M Caddyfile`, ` M docker-compose.staging.yml`, ` M scripts/staging_certify.py`. No other tracked file changed; no test file modified; no new source file created. (The report you are reading lives outside the repo at `/home/user/STAGING_HARDENING_REPORT.md` and is not part of the repository.)

The changes are **uncommitted in the working tree**. They were deliberately not committed so the release-gate artifact (`a7bb02e` application code + Step 14 evidence commit `4909435`) remains untouched; commit + re-freeze can be done on request.

---

## 1. What changed, exactly

### A. `docker-compose.staging.yml`
- Explicit `name: voxdesk-staging` (a distinct Compose project; staging containers/volumes/networks can never collide with production).
- Single internal bridge network `staging` (runtime name `voxdesk-staging-internal`); every service joins it.
- Exposure model hardened: **Caddy is the only public entry point** (`8080:80`, `8443:443`). API `127.0.0.1:8001:8000` and Grafana `127.0.0.1:3001:3000` are host-loopback only. PostgreSQL, Redis, Prometheus, scheduler and backup publish **no host ports**.
- Required-secret enforcement: `${POSTGRES_PASSWORD:?...}` and `${GRAFANA_ADMIN_PASSWORD:?...}` — `docker compose` fails loudly if unset.
- Staging-distinct volumes (`pgdata_staging`, `redisdata_staging`, `knowledge_staging`, `promdata_staging`, `grafdata_staging`, `caddy_data_staging`, `caddy_config_staging`) and staging backups dir `./backups-staging`.
- Real healthchecks on db (`pg_isready`), redis (`redis-cli ping`), api (`/health/ready`), scheduler (`:8001/metrics`), prometheus (`/-/healthy`), grafana (`/api/health`); `start_period` sized so the api entrypoint can run migrations first.
- `depends_on` with readiness awareness: api ← db+redis `service_healthy`; scheduler ← db `service_healthy` + api `service_started`; backup ← db `service_healthy`; prometheus ← api `service_healthy`; grafana ← prometheus `service_started`; caddy ← api `service_healthy`.
- Bounded `restart: unless-stopped`, `stop_grace_period: 30s`, `init: true` on api/scheduler; `init: true`+`stop_grace_period` where appropriate.
- Secrets mounted only where needed: `./secrets-staging:/srv/secrets:ro` on **api and scheduler only**.
- E2E passthrough explicit and gated: `E2E_ENABLED: ${E2E_ENABLED:-false}`, `E2E_TEST_NUMBER`, `E2E_ALLOWED_CALLERS` — default **off**; production compose has no such passthrough (verified: zero `E2E` references in `docker-compose.prod.yml`).
- Backup service preserves the production verified-dump loop (dump → `pg_restore --list` → retention) into `./backups-staging`, with the off-site rclone sync line no-op-ing (`|| true`) because the `postgres:16-alpine` image does not ship `rclone` (documented limitation — the fix is a Dockerfile change, out of scope).
- No embedded secrets (scanned: no `sk_live_`/`AKIA`/`BEGIN RSA`/`BEGIN PRIVATE` markers).

### B. `Caddyfile`
- Global `admin off` (disables the Caddy admin API surface).
- Exactly **two** explicit sites — API on `{$DOMAIN:localhost}` → `reverse_proxy api:8000`, and Grafana on `{$GRAFANA_DOMAIN:grafana.localhost}` → `reverse_proxy grafana:3000`. No wildcard/arbitrary upstream.
- Security headers on both sites: `Strict-Transport-Security "max-age=31536000; includeSubDomains"`, `X-Content-Type-Options`, `X-Frame-Options DENY`, `Referrer-Policy`.
- `request_body max_size 25MB` (headroom above the 20 MB knowledge-upload limit).
- WebSocket upgrade for Twilio Media Streams preserved (Caddy v2 default; nothing in the file interferes — no `header_upgrade`, matching `tests/test_deployment.py`).
- Observability/metrics/internal endpoints are **not** proxied (only `api:8000` and `grafana:3000` are upstreams).
- Cert mechanism unchanged: Caddy automatic ACME/Let's Encrypt with `DOMAIN`; HTTP fallback on `:80` when `DOMAIN` is empty. Certificate validation is never disabled.
- Documented staging limitation: Grafana is reachable publicly only when `GRAFANA_DOMAIN` is explicitly set; otherwise its site address is `grafana.localhost` (loopback per RFC 6761) and the operator path is the host-loopback `127.0.0.1:3001`.

### C. `scripts/staging_certify.py`
Step 13 behavior and exit-code contract preserved (0 success / 1 failure / 2 blocked / 3 invalid config). Added, without removing anything:
- New CLI switches `--compose-up` / `--compose-down` (opt-in, guarded `docker compose -f docker-compose.staging.yml up -d --build` / `down`, never `-v`).
- `STAGING_COMPOSE_FILE = "docker-compose.staging.yml"` — the only compose file the orchestrator may touch (never the production one).
- `_staging_identity_ok()`: refuses `APP_ENV=production` (via the existing `validate_staging_target`) **and** refuses a `COMPOSE_PROJECT_NAME` that does not contain `staging`; every mutating/exec compose operation re-checks this guard. A failed guard is a FAIL and the operation is not executed.
- `_compose_ready()` (docker compose v2 detection) and `_compose_config_check()` (read-only `docker compose config --quiet`).
- `_cleanup_staging_compose()`: best-effort `down` (no `-v`) on partial startup failure.
- The `--staging` sequence now runs real, guarded compose probes instead of placeholders: build, `up -d` (cleaned up on failure), `exec api python scripts/migrate.py`, `exec redis redis-cli ping`, `exec db pg_isready`, `ps --status running -q scheduler`; adds a `staging identity guard` step and a `compose config (docker)` step. Every one of these reports BLOCKED with a precise diagnostic when Docker is absent (never PASS).

### D. `.env.staging.example`
- Complete and accurate for **every** variable referenced by `docker-compose.staging.yml`, `scripts/staging_certify.py`, and **all 103 application Settings fields** (audited programmatically — zero missing, zero invented).
- Fixed two stale names: `COST_UNIT_PRICES` → `COST_UNIT_PRICES_JSON`, `CHAOS_RULES` → `CHAOS_RULES_JSON` (matching `app/core/config.py`).
- Added the 45 previously-omitted Settings fields (billing/calendar/CRM/knowledge/auth/rate-limit/reminder/stream tuning) as `[OPTIONAL]` with their **real defaults** copied from the Settings model.
- Added `PGHOST` (`[OPTIONAL]`, default `db`) — read by `scripts/backup.sh`/`scripts/restore.sh`, which the orchestrator invokes.
- `[REQUIRED]` / `[OPTIONAL]` / `[TEST/STAGING]` distinction on every line; `REPLACE_WITH_STAGING_*` placeholders; no real or production credentials (scanned: no secret markers).

---

## 2. Validation — test commands and exact results

| # | Command | Result |
|---|---|---|
| 1 | `/home/user/.venv/bin/python -m pytest -q` (full backend suite) | **2341 passed, 43 skipped, 9069 warnings** (exit 0) |
| 2 | `/home/user/.venv/bin/python -m pytest tests/test_deployment.py tests/test_ops_toolkit.py -q` | **72 passed** (exit 0) |
| 3 | `cd dashboard && npm run build` | **built in 1.46s** (52 modules; vite 6.4.3) |
| 4 | `cd dashboard && npm test` | **362 passed** (14 files) |
| 5 | `/home/user/.venv/bin/python -m ruff check .` | **All checks passed!** (E4/E7/E9/F, per `pyproject.toml`) |
| 6 | `/home/user/.venv/bin/python -m compileall -q app scripts tests` | **exit 0** |
| 7 | PyYAML parse of `docker-compose.staging.yml`, `docker-compose.prod.yml`, `observability/{prometheus,alerts,slos}.yml` | **all parse** (staging compose = 8 services) |
| 8 | Compose structural audit (services/depends_on/volumes/networks/healthcheck refs) | **no problems** — 8 services, 1 network, 7 volumes, 6 services healthchecked |
| 9 | Env-variable consistency audit (compose ↔ `.env.staging.example` ↔ `app/core/config.py` Settings ↔ `staging_certify.py`) | **0 missing, 0 stale** |
| 10 | Release gate `scripts/release_gate.py` | **unchanged: P0 3 / P1 19 / P2 5 / P3 0 → BLOCKED, ARTIFACT FROZEN yes** |

Backend suite matches the Step 13/14 baseline exactly (2341/43). `tests/test_deployment.py` and `tests/test_ops_toolkit.py` were **not modified** and still pass against the rewritten files (Caddyfile, compose, env examples).

---

## 3. Static configuration validation

- **Compose:** PyYAML parse OK; structural audit OK (every `depends_on` service, volume and network reference resolves; `service_healthy` deps all have healthchecks). `docker compose config` could **not** be run (Docker absent) — reported BLOCKED, not assumed.
- **Caddy:** the `caddy` binary is absent, so a real `caddy validate` could not be run — reported BLOCKED. Structural review: single `admin off` block, two explicit sites, no `/metrics` route, no `header_upgrade`, correct `reverse_proxy` upstreams, 25MB body limit present. All assertions in `tests/test_deployment.py::test_caddy_*` pass.
- **Env consistency:** all `${VAR}` / `${VAR:-…}` / `${VAR:?...}` in the compose are defined in `.env.staging.example` (the `DUMP` token in the backup inline script is a container shell variable, not a compose interpolation). All env reads in `staging_certify.py` are documented. All 103 Settings env names are covered.

## 4. Runtime staging result (live local stack, Docker absent)

Environment: PostgreSQL 17.11, Redis 8.0.2, Prometheus 2.53.3, uvicorn API + `scripts.scheduler` already running from Step 14 (`127.0.0.1:5432/6379/8000/8001/9090`). Final clean run: `scripts/staging_certify.py --staging --base-url http://localhost:8000` (no `--write-evidence`):

| Step | Status |
|---|---|
| preflight | **FAIL** — `working tree clean` (the 4 hardening files are intentionally uncommitted; passes once committed) |
| verify artifact | PASS — commit `49094357bce0`, no drift |
| verify staging environment | PASS — non-production (staging) |
| staging identity guard | PASS — `APP_ENV=staging`, project `voxdesk-staging` |
| runtime dependencies | BLOCKED — docker missing |
| build candidate / start staging / run migrations / verify scheduler / verify Redis / verify PostgreSQL / compose config | BLOCKED — docker missing (precise diagnostics; never PASS) |
| wait for readiness | PASS — `/health/ready` → 200 |
| smoke test | PASS — 7 pass / 2 warn / 0 fail |
| verify health | PASS — `/health/ready` → 200 |
| verify metrics | PASS — `/metrics` → 401 (token-gated) |
| verify proxy | PASS — `GET /` → 404 |
| deployment checks (static) | PASS — 6 files present and valid (pyyaml) |
| security smoke | PASS — `settings.validate_security()` clean |
| provider health | SKIPPED — 13 providers, opt-in off |
| backup verification | PASS — real dump `200K`, `pg_restore --list` verified |
| deployment certification (aggregate) | BLOCKED |
| **OVERALL** | **FAIL (exit 1)** — driven solely by the preflight dirty-tree FAIL |

Independent drills (all real, against the live staging Postgres): backup dump `backups/voxdesk-20260913-080314.dump` (200K, verified); `scripts/backup_verify.sh` → OK; restore drill into `voxdesk_restore_drill` → **28 tables restored**; `alembic current` → `0011_side_effect_exactly_once (head)`.

The Docker-gated runtime steps remain honestly **BLOCKED** (no Docker in this environment); provider checks **SKIPPED** (no credentials); nothing blocked was marked PASS.

---

## 5. Cross-file dependencies (reported, not silently modified)

1. `scripts/backup.sh` / `scripts/restore.sh` read `PGHOST` / `POSTGRES_USER` / `POSTGRES_DB` (defaults `db` / `voxdesk` / `voxdesk`). Documented in `.env.staging.example` (`PGHOST` `[OPTIONAL]`).
2. The staging backup service's off-site rclone sync **no-ops** because `postgres:16-alpine` has no `rclone` binary — fixing it requires a Dockerfile change, **outside** the four-file scope (documented in the compose comment).
3. `observability/prometheus.yml` scrapes `api:8000` and `scheduler:8001` **without** a bearer token — so `METRICS_TOKEN` must stay empty in `.env.staging` (or the Prometheus config must gain the header, which is outside the four-file scope). Documented in `.env.staging.example`.
4. The scheduler serves `:8001/metrics` via `prometheus_client.start_http_server` (no auth) — the scheduler healthcheck relies on this (verified in `scripts/scheduler.py`).
5. `scripts/entrypoint.sh` reads `WEB_CONCURRENCY` / `FORWARDED_ALLOW_IPS` (defaults `2` / `*`) and runs `scripts/migrate.py` before uvicorn — documented in `.env.staging.example`.
6. The Dockerfile `HEALTHCHECK` hits `/health`; the compose overrides it with `/health/ready` (same origin, readiness semantics).

## 6. Remaining blockers (unchanged, honest)

- **Docker/Compose absent** → the canonical `docker-compose.staging.yml` runtime path (build/start/migrate/scheduler/compose-config), TLS verification, egress verification (needs `VOXDESK_STAGING_NETWORK=1` inside the staging namespace), and the deploy/rollback drill remain BLOCKED.
- **Providers** (Deepgram/ElevenLabs/LLM/Stripe) remain SKIPPED — no staging credentials; `VOXDESK_REAL_INTEGRATION` off.
- Third-party pentest, compliance sign-off, Grafana/alerts visual review, incident tabletop remain human/external and NOT_RUN.
- Release gate remains **BLOCKED** (P0 3 / P1 19 / P2 5 / P3 0) — unchanged by this work.

---

## Appendix — complete final file contents

### docker-compose.staging.yml

```yaml
# Staging compose — production-shaped, explicitly non-production.
#
#   docker compose -f docker-compose.staging.yml --env-file .env.staging up -d --build
#
# Staging mirrors docker-compose.prod.yml in structure (same services, same
# healthchecks, same depends_on ordering, same Alembic-owned schema) so a
# change that works here works in production. It differs on the things that
# MUST differ:
#
#   * name: voxdesk-staging      -> a distinct Compose project, so staging
#                                   volumes/containers/networks can never be
#                                   mistaken for the production project.
#   * APP_ENV=staging            -> schema still owned by Alembic (no create_all)
#   * separate volumes           -> pgdata_staging / redisdata_staging / ...
#   * separate host ports        -> API :8001, Grafana :3001, Caddy :8080/:8443,
#                                   so staging can live on the same host as prod
#   * its own env file           -> .env.staging (never copy .env from prod)
#   * its own backups dir        -> ./backups-staging
#
# Exposure model (hardened): Caddy is the ONLY public entry point (host ports
# 8080/8443). The API and Grafana are bound to host loopback only. PostgreSQL,
# Redis, Prometheus, the scheduler and the backup worker publish NO host ports
# and are reachable only on the internal staging network.
#
# Secrets: this file contains NO secret values. POSTGRES_PASSWORD and
# GRAFANA_ADMIN_PASSWORD are REQUIRED from .env.staging (the `:?` form makes
# `docker compose` fail loudly when they are unset) and are never printed.
#
# Staging is where the manual real-call E2E (Step 5) and SLO drills (Step 7
# chaos) run. E2E is allowed here (it is refused in production), default off.
name: voxdesk-staging

services:
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    stop_grace_period: 30s     # let a checkpoint finish on SIGTERM
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-voxdesk}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in .env.staging}
      POSTGRES_DB: ${POSTGRES_DB:-voxdesk}
    volumes:
      - pgdata_staging:/var/lib/postgresql/data
    networks: [staging]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-voxdesk}"]
      interval: 5s
      timeout: 5s
      retries: 10
      start_period: 10s

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - redisdata_staging:/data
    networks: [staging]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 10
      start_period: 5s

  api:
    build: .
    restart: unless-stopped
    init: true                 # reap uvicorn worker children (PID 1 hygiene)
    stop_grace_period: 30s     # let in-flight requests drain on SIGTERM
    env_file: .env.staging
    environment:
      APP_ENV: staging
      DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER:-voxdesk}:${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in .env.staging}@db:5432/${POSTGRES_DB:-voxdesk}
      REDIS_URL: redis://redis:6379/0
      METRICS_ENABLED: ${METRICS_ENABLED:-true}
      METRICS_TOKEN: ${METRICS_TOKEN:-}
      LOG_FORMAT: json
      # Step 5 real-call E2E — explicitly configurable for staging, default
      # OFF. Production compose never passes these through, so there is no
      # production-equivalent switch that could be flipped accidentally.
      E2E_ENABLED: ${E2E_ENABLED:-false}
      E2E_TEST_NUMBER: ${E2E_TEST_NUMBER:-}
      E2E_ALLOWED_CALLERS: ${E2E_ALLOWED_CALLERS:-}
    ports:
      - "127.0.0.1:8001:8000"   # host-loopback; distinct from production :8000
    volumes:
      - ./secrets-staging:/srv/secrets:ro
      - knowledge_staging:/srv/var/knowledge
    networks: [staging]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5).status==200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 60s       # entrypoint runs migrations before serving
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy

  scheduler:
    build: .
    restart: unless-stopped
    init: true
    stop_grace_period: 30s
    env_file: .env.staging
    environment:
      APP_ENV: staging
      DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER:-voxdesk}:${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in .env.staging}@db:5432/${POSTGRES_DB:-voxdesk}
      REDIS_URL: redis://redis:6379/0
    entrypoint: ["python", "-m", "scripts.scheduler"]
    volumes:
      - ./secrets-staging:/srv/secrets:ro
    networks: [staging]
    # The scheduler exposes /metrics on SCHEDULER_METRICS_PORT (default 8001);
    # probing it is a liveness signal for the worker process + its metrics
    # server (job-success gauges are exported there too).
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/metrics', timeout=5).status==200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 30s
    depends_on:
      db:
        condition: service_healthy
      api:
        condition: service_started

  backup:
    image: postgres:16-alpine
    restart: unless-stopped
    env_file: .env.staging      # so RCLONE_REMOTE (and any backup env) is visible
    environment:
      PGPASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in .env.staging}
      POSTGRES_USER: ${POSTGRES_USER:-voxdesk}
      POSTGRES_DB: ${POSTGRES_DB:-voxdesk}
    volumes:
      - ./backups-staging:/backups
    networks: [staging]
    entrypoint: ["/bin/sh", "-c"]
    # Same verified-dump loop as production, into the staging backups dir.
    # NOTE: this image ships pg_dump but NOT rclone; the optional off-site
    # sync line therefore no-ops (|| true) until the backup image includes
    # rclone — that change lives in the Dockerfile, outside this file's scope.
    command: |
      while true; do
        sleep 3600
        if [ "$$(date +%H)" = "02" ]; then
          DUMP="/backups/voxdesk-staging-$$(date +%F).dump"
          if pg_dump -h db -U "$${POSTGRES_USER}" -Fc "$${POSTGRES_DB}" > "$${DUMP}" \
              && [ -s "$${DUMP}" ] \
              && pg_restore --list "$${DUMP}" > /dev/null 2>&1; then
            echo "staging backup ok: $${DUMP}"
            ls -1 /backups/*.dump | head -n -14 | xargs -r rm
            if [ -n "$${RCLONE_REMOTE:-}" ]; then
              rclone copy /backups "$${RCLONE_REMOTE}"/voxdesk-backups-staging || true
            fi
          else
            echo "STAGING BACKUP FAILED (dump unreadable or empty); keeping previous dumps" >&2
          fi
        fi
      done
    depends_on:
      db:
        condition: service_healthy

  prometheus:
    image: prom/prometheus:v2.53.0
    restart: unless-stopped
    volumes:
      - ./observability/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./observability/alerts.yml:/etc/prometheus/alerts.yml:ro
      - ./observability/slos.yml:/etc/prometheus/slos.yml:ro
      - promdata_staging:/prometheus
    networks: [staging]
    command: ["--config.file=/etc/prometheus/prometheus.yml"]
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://127.0.0.1:9090/-/healthy || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 15s
    depends_on:
      api:
        condition: service_healthy

  grafana:
    image: grafana/grafana:11.1.0
    restart: unless-stopped
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?GRAFANA_ADMIN_PASSWORD must be set in .env.staging}
      GF_SECURITY_ADMIN_USER: ${GRAFANA_ADMIN_USER:-admin}
      GF_AUTH_ANONYMOUS_ENABLED: "false"
      GF_USERS_ALLOW_SIGN_UP: "false"
      GF_USERS_ALLOW_ORG_CREATE: "false"
    volumes:
      - grafdata_staging:/var/lib/grafana
      - ./observability/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./observability/grafana/dashboards:/var/lib/grafana/dashboards:ro
    networks: [staging]
    ports:
      - "127.0.0.1:3001:3000"   # host-loopback; distinct from production :3000
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://127.0.0.1:3000/api/health || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 30s
    depends_on:
      prometheus:
        condition: service_started

  caddy:
    image: caddy:2.9-alpine
    restart: unless-stopped
    environment:
      DOMAIN: ${DOMAIN:-}
      GRAFANA_DOMAIN: ${GRAFANA_DOMAIN:-}
    ports:
      - "8080:80"               # staging HTTP (distinct from production 80/443)
      - "8443:443"              # staging HTTPS (when DOMAIN is set)
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data_staging:/data
      - caddy_config_staging:/config
    networks: [staging]
    depends_on:
      api:
        condition: service_healthy

networks:
  staging:
    name: voxdesk-staging-internal
    driver: bridge
    # Intentionally NOT `internal: true`: the API/scheduler must reach external
    # providers (Twilio, Deepgram, ElevenLabs, LLM APIs, Stripe) over the
    # public internet. Egress policy is enforced at the infrastructure layer
    # (see docs/STEP10-EGRESS-POLICY.md), not by this network.

volumes:
  pgdata_staging:
  redisdata_staging:
  knowledge_staging:
  promdata_staging:
  grafdata_staging:
  caddy_data_staging:
  caddy_config_staging:
```

### Caddyfile

```
# VoxDesk TLS reverse proxy (Caddy v2).
#
# Automatic HTTPS: when DOMAIN is a real public hostname, Caddy obtains and
# renews Let's Encrypt certificates with zero config and redirects HTTP -> HTTPS
# on the same site. Leave DOMAIN unset to serve plain HTTP on :80 (behind your
# own load balancer, or for a local smoke test on http://localhost:8080).
# Certificate verification is never disabled: Caddy's default ACME + cert
# validation is used unchanged.
#
# WebSockets (wss://) for the live voice stream pass through unchanged -- Caddy
# v2 upgrades WebSocket connections by default, which is what the Twilio Media
# Streams handshake depends on. Nothing in this file may interfere with that
# upgrade. Caddy also sets X-Forwarded-For / X-Forwarded-Proto / X-Forwarded-Host
# automatically, so the API (uvicorn --proxy-headers) sees the real client and
# scheme for PUBLIC_BASE_URL, wss:// and Twilio signature verification.
#
# Observability: the API's metrics endpoint is token-gated at the application
# layer (METRICS_TOKEN) and is scraped by Prometheus directly over the internal
# compose network (observability/prometheus.yml targets api:8000) -- it is NOT
# routed or exposed here. Prometheus, PostgreSQL, Redis and the scheduler have
# no public path at all.
#
# The Caddy admin API is disabled: it is a localhost control plane this
# deployment does not use, and leaving it on is an unnecessary surface.

{
	admin off
}

{$DOMAIN:localhost} {
	encode zstd gzip

	# Security headers. Strict-Transport-Security is ignored by browsers over
	# plain HTTP, so it is safe to set unconditionally; it takes effect the
	# moment the site is served over TLS. The origin (app/main.py security
	# headers middleware) sets the same headers as a second layer.
	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "no-referrer"
	}

	# Request body ceiling. Knowledge-base uploads allow up to 20 MB
	# (KNOWLEDGE_MAX_FILE_MB), so leave headroom above that. WebSocket media
	# streams are unaffected by request_body limits.
	request_body {
		max_size 25MB
	}

	# The API listens on 8000 inside the compose network. This is the ONLY
	# upstream proxied here -- there is no wildcard/arbitrary upstream path.
	reverse_proxy api:8000
}

# Grafana is exposed through Caddy ONLY on its explicit domain. When
# GRAFANA_DOMAIN is unset the site address falls back to `grafana.localhost`,
# which (per RFC 6761) resolves only to loopback -- so no remote client can
# route to it, and the operator's access path is the host-loopback port
# 127.0.0.1:3001. Set GRAFANA_DOMAIN=grafana.staging.example.com to serve it on
# a real hostname; any request that matches the site still requires Grafana
# sign-in (anonymous access and self-sign-up are disabled in compose).
{$GRAFANA_DOMAIN:grafana.localhost} {
	encode zstd gzip

	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "no-referrer"
	}

	request_body {
		max_size 25MB
	}

	reverse_proxy grafana:3000
}
```

### .env.staging.example

```
# =============================================================================
# VoxDesk STAGING configuration template.
#
# Copy to `.env.staging` and fill in. This file is for the dedicated staging
# environment (docker-compose.staging.yml). It is structurally identical to
# production but ISOLATED from it:
#
#   * APP_ENV=staging — never production.
#   * Separate database/Redis/volumes (the compose file names them *_staging).
#   * Separate secrets — never copy values from the production `.env`.
#   * Safe/test Stripe credentials, test Twilio numbers, test tenants.
#
# Do NOT put production secrets in this file, and do NOT run this stack with
# APP_ENV=production (validate_security() would then enforce production rules
# and refuse placeholders).
#
# Value conventions:
#   [REQUIRED]        must be set for the staging stack to run.
#   [OPTIONAL]        has a safe application default (see app/core/config.py).
#   [TEST/STAGING]    only ever valid here, never in production.
#
# Placeholder pattern: every secret below is a clearly fake token of the form
# REPLACE_WITH_STAGING_* . Replace it with the real staging value generated by
# the command shown in the comment. Never paste a production value.
# =============================================================================

# ---------- App ----------
APP_ENV=staging                                    # [REQUIRED] staging (never production)
# The public URL the API advertises (webhooks, wss:// for Twilio Media Streams).
# Use the Caddy/staging hostname when DOMAIN is set; http://localhost:8001 only
# for a host-local smoke test without Caddy.
PUBLIC_BASE_URL=https://staging.example.com         # [OPTIONAL]
# [REQUIRED] generate: openssl rand -hex 32
SECRET_KEY=REPLACE_WITH_STAGING_SECRET_KEY

# ---------- Auth / JWT ----------
# [REQUIRED] generate: openssl rand -hex 32 — a staging-only value, distinct from prod.
JWT_SECRET=REPLACE_WITH_STAGING_JWT_SECRET_MIN_32_CHARS_0123456789abcdef
JWT_ISSUER=voxdesk                                  # [OPTIONAL]
JWT_AUDIENCE=voxdesk-api                            # [OPTIONAL]
ACCESS_TOKEN_MINUTES=15                             # [OPTIONAL]
REFRESH_TOKEN_DAYS=14                               # [OPTIONAL]

# ---------- Browser origins / hosts ----------
# Staging browser origins (staging dashboard/dev).
CORS_ORIGINS=https://staging.example.com,http://localhost:5173   # [OPTIONAL]
# Comma-separated hostnames allowed in the Host header (empty = middleware off,
# which is correct behind Caddy for the single-proxy topology).
TRUSTED_HOSTS=                                      # [OPTIONAL]

# ---------- Database ----------
# NOTE: docker-compose.staging.yml overrides DATABASE_URL in the api/scheduler
# `environment:` block, computed from POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB.
# The value below is the same in-container shape, used only when running the
# app outside compose.
DATABASE_URL=postgresql+asyncpg://voxdesk:REPLACE_WITH_STAGING_DB_PASSWORD@db:5432/voxdesk
DB_POOL_SIZE=10                                     # [OPTIONAL]
DB_MAX_OVERFLOW=20                                  # [OPTIONAL]

# ---------- Redis / cache ----------
# NOTE: overridden by compose (redis://redis:6379/0) for api/scheduler.
REDIS_URL=redis://redis:6379/0                      # [OPTIONAL]
CACHE_TTL_SECONDS=60                                # [OPTIONAL]

# ---------- Twilio (staging/test credentials) ----------
# Use a Twilio TEST account or a dedicated staging subaccount. Never the
# production SID/token.
TWILIO_ACCOUNT_SID=AC_REPLACE_WITH_STAGING_TWILIO_ACCOUNT_SID   # [REQUIRED for voice]
TWILIO_AUTH_TOKEN=REPLACE_WITH_STAGING_TWILIO_AUTH_TOKEN        # [REQUIRED for voice]
TWILIO_PHONE_NUMBER=+15550001111                   # [REQUIRED for voice] test number
TWILIO_SKIP_WEBHOOK_VERIFY=false                   # [TEST/STAGING] keep false

# ---- Real-call E2E (Step 5) — allowed in staging, default off ----
# Staging is the correct place to run the manual real-telephony E2E. When
# armed, only the allowlisted operator number dialing the test number on the
# test tenant may proceed; every other call is refused. Production refuses
# E2E at boot, so these cannot leak into production.
E2E_ENABLED=false                                  # [TEST/STAGING]
E2E_TEST_NUMBER=                                   # [TEST/STAGING] dedicated Twilio test number
E2E_ALLOWED_CALLERS=                               # [TEST/STAGING] comma-separated E.164 operator numbers

# ---------- Speech to text (Deepgram) — staging key ----------
DEEPGRAM_API_KEY=REPLACE_WITH_STAGING_DEEPGRAM_KEY   # [REQUIRED for voice]
DEEPGRAM_MODEL=nova-3                                # [OPTIONAL]

# ---------- LLM — staging keys; at least one required ----------
OPENAI_API_KEY=REPLACE_WITH_STAGING_OPENAI_KEY       # [REQUIRED one of the three]
ANTHROPIC_API_KEY=REPLACE_WITH_STAGING_ANTHROPIC_KEY # [REQUIRED one of the three]
GOOGLE_API_KEY=REPLACE_WITH_STAGING_GOOGLE_API_KEY   # [REQUIRED one of the three]
DEFAULT_LLM_PRESET=natural                           # [OPTIONAL] fast|natural|cheap|smart

# ---------- Text to speech (ElevenLabs) — staging key ----------
ELEVENLABS_API_KEY=REPLACE_WITH_STAGING_ELEVENLABS_KEY  # [REQUIRED for voice]
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM                # [OPTIONAL]
ELEVENLABS_MODEL=eleven_flash_v2_5                      # [OPTIONAL]

# ---------- Channels ----------
WHATSAPP_ENABLED=false                              # [OPTIONAL]
TWILIO_WHATSAPP_NUMBER=                             # [OPTIONAL]
DEFAULT_LANGUAGE=en-US                              # [OPTIONAL]

# ---------- Knowledge base / RAG ----------
KNOWLEDGE_STORAGE_BACKEND=local                     # [OPTIONAL] local | s3
KNOWLEDGE_LOCAL_PATH=./var/knowledge                # [OPTIONAL]
# Staging may use the deterministic hashing stub; set a real provider to test
# embeddings end-to-end (then also set KNOWLEDGE_EMBEDDING_DIMENSIONS to its
# real width — see app/core/config.py).
KNOWLEDGE_EMBEDDING_PROVIDER=hashing                 # [OPTIONAL]
KNOWLEDGE_EMBEDDING_MODEL=hashing-v1                 # [OPTIONAL]
KNOWLEDGE_INGEST_MODE=worker                         # [OPTIONAL] inline | worker

# ---------- CRM integrations ----------
# A staging-only key ring. Generate:
#   python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
CRM_ENCRYPTION_KEYS=                                 # [OPTIONAL for staging]

# ---------- Billing (Stripe TEST mode only) ----------
BILLING_PROVIDER=manual                              # [OPTIONAL] manual | stripe
# When BILLING_PROVIDER=stripe, use TEST-mode keys only (sk_test_*). Never a
# production/live key in staging.
STRIPE_SECRET_KEY=REPLACE_WITH_STAGING_STRIPE_TEST_SECRET_KEY        # [TEST/STAGING]
STRIPE_WEBHOOK_SECRET=REPLACE_WITH_STAGING_STRIPE_TEST_WEBHOOK_SECRET  # [TEST/STAGING]
STRIPE_PUBLISHABLE_KEY=REPLACE_WITH_STAGING_STRIPE_TEST_PUBLISHABLE_KEY  # [TEST/STAGING]
BILLING_UNLIMITED_ENTITLEMENTS=false                 # [OPTIONAL] keep false
BILLING_ENFORCE_ENTITLEMENTS=true                    # [OPTIONAL]

# ---------- Observability ----------
SENTRY_DSN=                                          # [OPTIONAL] error reporting only
LOG_LEVEL=INFO                                       # [OPTIONAL]
LOG_FORMAT=json                                      # [OPTIONAL]

# ---------- Metrics (Prometheus) ----------
METRICS_ENABLED=true                                 # [OPTIONAL] enabled for the staging scrape
# Leave METRICS_TOKEN EMPTY so the internal Prometheus can scrape the API
# unauthenticated (observability/prometheus.yml targets api:8000 without a
# bearer token). Setting it here requires the matching authorization header in
# prometheus.yml — that file is outside this step's allowed-change set, so
# keep them consistent.
METRICS_TOKEN=                                       # [OPTIONAL]

# ---------- Cost awareness ----------
# Operator-provided provider unit prices as JSON (Settings field
# COST_UNIT_PRICES_JSON). Empty = every price is UNKNOWN by design (never
# invented). See docs/COST-AWARENESS.md.
COST_UNIT_PRICES_JSON=                               # [OPTIONAL]

# ---------- Failure injection (allowed in staging) ----------
# SLO drills run here. Deterministic rules only; never set in production.
CHAOS_ENABLED=false                                  # [TEST/STAGING]
CHAOS_RULES_JSON=                                    # [TEST/STAGING]

# ---------- Scheduler metrics ----------
SCHEDULER_METRICS_PORT=8001                          # [OPTIONAL]

# ---------- Data policy ----------
CALL_RETENTION_DAYS=365                              # [OPTIONAL]
AI_DISCLOSURE_REQUIRED=true                          # [OPTIONAL]

# ---------- Rate limiting ----------
RATE_LIMIT_ENABLED=false                             # [OPTIONAL] staging may keep off; production requires true

# ---------- Licensing / security.txt ----------
LICENSE_SECRET=                                      # [OPTIONAL]
SECURITY_CONTACT=                                    # [OPTIONAL] security@example.com or https URL

# ---------- Advanced tuning (all OPTIONAL — defaults shown, from app/core/config.py) ----------
# Only set these to diverge from a tested default. They exist so the template
# covers every Settings field; the application already uses these values when
# the variable is absent.

# -- Billing (Stripe TEST mode only) --
BILLING_REQUEST_TIMEOUT_SECONDS=15.0                 # [OPTIONAL]
BILLING_CHECKOUT_SUCCESS_URL=                        # [OPTIONAL] dashboard success redirect
BILLING_CHECKOUT_CANCEL_URL=                         # [OPTIONAL] dashboard cancel redirect
BILLING_PORTAL_RETURN_URL=                           # [OPTIONAL] billing-portal return redirect

# -- Calendar (Google) --
CALENDAR_REQUEST_TIMEOUT_SECONDS=6.0                 # [OPTIONAL]
CALENDAR_TOKEN_REFRESH_MARGIN_SECONDS=300            # [OPTIONAL]
CALENDAR_VOICE_TIMEOUT_SECONDS=3.0                   # [OPTIONAL]
CALENDAR_WEBHOOK_TOLERANCE_SECONDS=300               # [OPTIONAL]
# Path to the Google service-account JSON. Inside the staging container the
# secrets dir is mounted at /srv/secrets (compose mounts ./secrets-staging),
# so use /srv/secrets/google_service_account.json there; the host-relative
# default ./secrets/... applies only to out-of-compose runs.
GOOGLE_CREDENTIALS_JSON=./secrets/google_service_account.json   # [OPTIONAL]

# -- CRM integrations --
CRM_REQUEST_TIMEOUT_SECONDS=10.0                     # [OPTIONAL]
CRM_RATE_LIMIT_PER_SECOND=5.0                        # [OPTIONAL]
CRM_RATE_LIMIT_BURST=20.0                            # [OPTIONAL]
CRM_RETRY_BASE_SECONDS=2.0                           # [OPTIONAL]
CRM_RETRY_MAX_ATTEMPTS=5                             # [OPTIONAL]
CRM_RETRY_MAX_SECONDS=900.0                          # [OPTIONAL]
CRM_STUCK_SYNC_MINUTES=15                            # [OPTIONAL]
CRM_SYNC_BATCH_SIZE=20                               # [OPTIONAL]
CRM_SYNC_INTERVAL_SECONDS=20                         # [OPTIONAL]
CRM_WEBHOOK_TOLERANCE_SECONDS=300                    # [OPTIONAL]

# -- Knowledge base / RAG --
KNOWLEDGE_CHUNK_CHARS=3200                           # [OPTIONAL]
KNOWLEDGE_CHUNK_OVERLAP_CHARS=400                    # [OPTIONAL]
KNOWLEDGE_CONTEXT_MAX_CHARS=4000                     # [OPTIONAL]
KNOWLEDGE_EMBEDDING_BATCH_SIZE=32                    # [OPTIONAL]
KNOWLEDGE_EMBEDDING_DIMENSIONS=4096                  # [OPTIONAL] match the embedding provider's real width
KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS=20.0             # [OPTIONAL]
KNOWLEDGE_MAX_DOCUMENTS_PER_TENANT=2000              # [OPTIONAL]
KNOWLEDGE_MAX_FILE_MB=20                             # [OPTIONAL] <= Caddy's 25MB request-body ceiling
KNOWLEDGE_MIN_CHUNK_CHARS=120                        # [OPTIONAL]
KNOWLEDGE_MIN_SCORE=0.03                             # [OPTIONAL]
KNOWLEDGE_PROCESSING_TIMEOUT_SECONDS=900             # [OPTIONAL]
KNOWLEDGE_RERANK_CANDIDATES=12                       # [OPTIONAL]
KNOWLEDGE_RERANK_ENABLED=true                        # [OPTIONAL]
KNOWLEDGE_RETRIEVAL_TIMEOUT_SECONDS=1.5              # [OPTIONAL]
KNOWLEDGE_S3_BUCKET=                                 # [OPTIONAL] only when KNOWLEDGE_STORAGE_BACKEND=s3
KNOWLEDGE_S3_ENDPOINT_URL=                           # [OPTIONAL]
KNOWLEDGE_S3_REGION=                                 # [OPTIONAL]
KNOWLEDGE_TOP_K=4                                    # [OPTIONAL]

# -- Auth / login rate limiting --
MAX_FAILED_LOGINS=8                                  # [OPTIONAL]
LOCKOUT_MINUTES=15                                   # [OPTIONAL]
RATE_LIMIT_BURST=300                                 # [OPTIONAL]
RATE_LIMIT_LOGIN_PER_MINUTE=10                       # [OPTIONAL]

# -- Reminder / voice stream --
REMINDER_LEASE_SECONDS=300                           # [OPTIONAL]
STREAM_HANDSHAKE_TIMEOUT_SECONDS=15.0                # [OPTIONAL]

# =============================================================================
# Infrastructure (docker-compose.staging.yml)
# =============================================================================
# Database role/name/password for the staging Postgres. The password is
# REQUIRED (the compose file fails loudly if unset).
POSTGRES_USER=voxdesk                                 # [REQUIRED]
POSTGRES_PASSWORD=REPLACE_WITH_STAGING_DB_PASSWORD    # [REQUIRED] staging-only DB password
POSTGRES_DB=voxdesk                                   # [REQUIRED]
# DB host for scripts/backup.sh and scripts/restore.sh. Inside the compose
# network the default `db` service name is correct; set 127.0.0.1 only when
# running those scripts against a host-local Postgres outside compose.
PGHOST=                                               # [OPTIONAL] default: db (compose service name)

# Grafana admin login (REQUIRED password).
GRAFANA_ADMIN_USER=admin                              # [REQUIRED]
GRAFANA_ADMIN_PASSWORD=REPLACE_WITH_STAGING_GRAFANA_PASSWORD   # [REQUIRED]

# Caddy entry point. Leave DOMAIN empty for localhost HTTP smoke; set it to
# the staging hostname for automatic HTTPS. GRAFANA_DOMAIN empty keeps Grafana
# reachable only on the host loopback (127.0.0.1:3001).
DOMAIN=                                               # [OPTIONAL]
GRAFANA_DOMAIN=                                       # [OPTIONAL]

# uvicorn worker/trust settings consumed by scripts/entrypoint.sh.
WEB_CONCURRENCY=2                                     # [OPTIONAL]
FORWARDED_ALLOW_IPS=*                                 # [OPTIONAL] trust Caddy's X-Forwarded-* headers

# Compose project identity. The staging compose file hard-codes
# `name: voxdesk-staging`; if you set COMPOSE_PROJECT_NAME, it MUST contain
# "staging" or scripts/staging_certify.py refuses to touch the project.
COMPOSE_PROJECT_NAME=voxdesk-staging                  # [OPTIONAL]

# Off-site backup sync target (rclone). NOTE: the staging backup service runs
# the postgres:16-alpine image, which does NOT include the rclone binary, so
# this sync no-ops until the backup image ships rclone (Dockerfile change,
# outside this file's scope). See also docs/STEP10-BACKUP-SYNC.md.
RCLONE_REMOTE=                                        # [OPTIONAL]

# =============================================================================
# Operational toolkit (scripts/staging_certify.py and friends)
# =============================================================================
# Base URL the certification orchestrator probes for readiness/smoke.
SMOKE_BASE_URL=http://localhost:8001                  # [OPTIONAL] host port of the staging API
# Restore-drill target database (scripts/restore.sh guard).
RESTORE_TARGET_DB=voxdesk_restore_drill               # [OPTIONAL]
# Real-provider health sweep opt-in (scripts/validate_providers.py).
VOXDESK_REAL_INTEGRATION=0                            # [TEST/STAGING] 1 = run real read-only checks
# Egress verification must run from inside the staging network namespace.
VOXDESK_STAGING_NETWORK=0                             # [TEST/STAGING]
VOXDESK_EGRESS_ALLOW_HOSTS=                           # [TEST/STAGING] comma-separated allow-list
```

### scripts/staging_certify.py

```python
#!/usr/bin/env python3
"""VoxDesk staging certification orchestrator (Step 13 section B/C).

Orchestrates only safe staging operations. The dangerous operations — a real
phone call, a production charge, a production booking, a production CRM
mutation — are never part of this tool.

Modes (choose one; ``--all-safe`` runs every safe mode):
    --preflight      machine/repo/config/secret preflight (read-only)
    --staging        full staging deployment certification sequence
    --backup         staging database backup (wraps scripts/backup.sh)
    --restore        staging restore drill (wraps scripts/restore.sh)
    --providers      real-provider health sweep (opt-in, read-only)
    --observability  Prometheus/Grafana validation (read-only)
    --egress         egress enforcement verification (inside staging only)
    --deploy-drill   deploy -> rollback drill (wraps deploy.sh/rollback.sh)
    --compose-up     guarded `docker compose up -d --build` for the staging project
    --compose-down   guarded `docker compose down` for the staging project

There are deliberately NO --force-production / --skip-safety / --ignore-gate
switches. The release gate in scripts/release_gate.py remains authoritative.
Every docker-compose operation (build/up/down/exec/ps/config) is gated on a
staging-identity check: APP_ENV must be non-production and COMPOSE_PROJECT_NAME
(if set) must identify staging. A failed guard is a FAIL, and the mutating
operation is never executed.

Usage:
    python scripts/staging_certify.py --preflight [--json] [--write-evidence]

Exit codes: 0 = PASS, 1 = FAIL, 2 = BLOCKED, 3 = invalid configuration.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.integrations.validation import registry  # noqa: E402
from app.integrations.validation.status import real_integration_enabled  # noqa: E402
from app.release import gate, ops  # noqa: E402

EVIDENCE_PATH = Path(ops.REPO_ROOT) / "scripts" / "release" / "evidence.json"
ARTIFACT_PATH = Path(ops.REPO_ROOT) / "var" / "release" / "artifact.json"

#: The one compose file this orchestrator may touch. Never the production one.
STAGING_COMPOSE_FILE = "docker-compose.staging.yml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="staging_certify.py", description="VoxDesk staging certification orchestrator"
    )
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--staging", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--providers", action="store_true")
    parser.add_argument("--observability", action="store_true")
    parser.add_argument("--egress", action="store_true")
    parser.add_argument("--deploy-drill", action="store_true")
    parser.add_argument("--compose-up", action="store_true",
                        help="guarded `docker compose -f docker-compose.staging.yml up -d --build`")
    parser.add_argument("--compose-down", action="store_true",
                        help="guarded `docker compose -f docker-compose.staging.yml down` (never -v)")
    parser.add_argument("--all-safe", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write-evidence", action="store_true",
                        help="merge produced evidence into scripts/release/evidence.json")
    parser.add_argument("--base-url", default=os.environ.get("SMOKE_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--url", default="", help="staging HTTPS URL for TLS verification")
    parser.add_argument("--prometheus-url", default="")
    parser.add_argument("--grafana-url", default="")
    parser.add_argument("--inside-staging", action="store_true")
    parser.add_argument("--dump", default="", help="backup dump path for --restore")
    parser.add_argument("--restore-target-db", default=os.environ.get("RESTORE_TARGET_DB", ""))
    parser.add_argument("--previous-sha", default="", help="previous-good sha for --deploy-drill")
    parser.add_argument("--expected-commit", default=None)
    return parser


def _http_get(url: str, timeout: float = 6.0) -> tuple[int, bool]:
    try:
        with httpx.Client(verify=True, timeout=timeout, follow_redirects=False) as client:
            resp = client.get(url)
            return resp.status_code, True
    except Exception:  # noqa: BLE001
        return 0, False


def _step(name: str, status: str, detail: str = "", record: ops.EvidenceRecord | None = None) -> dict:
    return {"step": name, "status": status, "detail": detail, "record": record}


def _tools() -> dict[str, str | None]:
    return {
        name: ops.which(name)
        for name in ("docker", "psql", "pg_dump", "pg_restore", "redis-cli", "rclone")
    }


def _compose_ready() -> tuple[bool, str]:
    """Whether the docker compose (v2) plugin is usable. Returns (ok, detail)."""
    if not ops.which("docker"):
        return False, "docker not found on PATH"
    try:
        proc = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"docker compose unavailable: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "docker compose version failed").strip()[:200]
    return True, "docker compose (v2) present"


def _staging_identity_ok(app_env: str) -> tuple[bool, str]:
    """Verify the compose target identifies itself as staging.

    Refuses production outright and refuses a COMPOSE_PROJECT_NAME that does
    not identify staging, so a mutating or exec operation can never touch a
    production (or ambiguous) compose project. Returns (ok, reason).
    """
    allowed, _status, reason = ops.validate_staging_target(app_env)
    if not allowed:
        return False, reason
    project = (os.environ.get("COMPOSE_PROJECT_NAME", "") or "").strip()
    if project and "staging" not in project.lower():
        return False, (
            f"COMPOSE_PROJECT_NAME={project!r} does not identify staging; "
            "refusing to touch a possibly-non-staging compose project"
        )
    return True, (
        f"staging identity confirmed (APP_ENV={app_env}, "
        f"project={project or 'voxdesk-staging'})"
    )


def _compose(args_list: list[str], timeout: int = 1800) -> tuple[int, str, str]:
    """Run ``docker compose -f <staging file> ...``. Returns (rc, stdout, stderr)."""
    proc = subprocess.run(
        ["docker", "compose", "-f", STAGING_COMPOSE_FILE, *args_list],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _compose_config_check() -> tuple[str, str]:
    """Validate the staging compose file resolves with `docker compose config`."""
    ok, detail = _compose_ready()
    if not ok:
        return ops.STATUS_BLOCKED, f"compose config validation unavailable: {detail}"
    rc, out, err = _compose(["config", "--quiet"], timeout=120)
    if rc != 0:
        return ops.STATUS_FAIL, f"docker compose config failed: {(err or out).strip()[-400:]}"
    return ops.STATUS_PASS, f"{STAGING_COMPOSE_FILE} resolves via `docker compose config`"


def _cleanup_staging_compose() -> None:
    """Bring down the staging project after a partial startup failure.

    Never passes ``-v`` (volumes/backups are preserved) and never targets any
    file other than STAGING_COMPOSE_FILE, so a failed certification cannot
    destroy data or touch another environment's services.
    """
    try:
        _compose(["down"], timeout=300)
    except Exception:  # noqa: BLE001 - cleanup is best-effort; the FAIL is already recorded
        pass


def _static_compose_checks() -> tuple[str, str]:
    """Static validation of the compose files without Docker or a YAML parser."""
    files = [
        "docker-compose.yml",
        "docker-compose.staging.yml",
        "docker-compose.prod.yml",
        "observability/prometheus.yml",
        "observability/alerts.yml",
        "observability/slos.yml",
    ]
    problems = []
    for name in files:
        p = Path(ops.REPO_ROOT) / name
        if not p.exists():
            problems.append(f"{name}: missing")
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            problems.append(f"{name}: empty")
    try:
        import yaml  # noqa: F401

        for name in files:
            p = Path(ops.REPO_ROOT) / name
            if p.exists():
                with p.open(encoding="utf-8") as fh:
                    yaml.safe_load(fh)
        parsed_with = "pyyaml"
    except Exception:  # noqa: BLE001
        parsed_with = "presence-only (pyyaml unavailable)"
    if problems:
        return ops.STATUS_FAIL, "; ".join(problems)
    return ops.STATUS_PASS, f"{len(files)} files present and valid ({parsed_with})"


def _security_smoke() -> tuple[str, str]:
    try:
        problems = settings.validate_security()
    except Exception as exc:  # noqa: BLE001
        return ops.STATUS_FAIL, f"validate_security raised {type(exc).__name__}"
    if problems:
        return ops.STATUS_FAIL, "; ".join(str(p) for p in problems[:5])
    return ops.STATUS_PASS, "settings.validate_security() clean"


def _run_staging_sequence(args: argparse.Namespace) -> dict:
    app_env = (os.environ.get("APP_ENV") or settings.app_env or "development").strip()
    tools = _tools()
    identity_ok, identity_reason = _staging_identity_ok(app_env)
    steps: list[dict] = []
    records: list[ops.EvidenceRecord] = []

    def add(step: dict) -> None:
        steps.append(step)
        if step.get("record") is not None:
            records.append(step["record"])

    # 1. preflight
    preflight = ops.run_preflight(ops.REPO_ROOT, expected_commit=args.expected_commit)
    add(_step("preflight", preflight["overall"], f"exit {preflight['exit_code']}"))

    # 2. verify artifact (drift vs frozen record, when present)
    live = ops.compute_live_facts()
    recorded: dict = {}
    if ARTIFACT_PATH.exists():
        try:
            recorded = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            recorded = {}
    drift = ops.drift_lines(recorded, live)
    if recorded.get("git_commit") and drift:
        add(_step("verify artifact", ops.STATUS_FAIL, "; ".join(drift)))
    else:
        add(_step("verify artifact", ops.STATUS_PASS,
                  f"commit {live['git_commit'][:12]} (no drift)" if not drift else "no frozen record"))

    # 3. verify staging environment
    allowed, status, reason = ops.validate_staging_target(app_env)
    add(_step("verify staging environment", status, reason))

    # 3b. staging identity guard — every compose operation below re-checks this
    add(_step("staging identity guard",
              ops.STATUS_PASS if identity_ok else ops.STATUS_FAIL, identity_reason))

    # 4. runtime dependencies
    missing = [name for name, path in tools.items() if not path]
    add(_step("runtime dependencies",
              ops.STATUS_PASS if not missing else ops.STATUS_BLOCKED,
              "all present" if not missing else f"missing: {', '.join(missing)}"))

    # 5. build candidate (guarded compose build — staging project only)
    if not tools.get("docker"):
        add(_step("build candidate", ops.STATUS_BLOCKED, "docker missing"))
    elif not identity_ok:
        add(_step("build candidate", ops.STATUS_FAIL, identity_reason))
    else:
        rc, out, err = _compose(["build"], timeout=1800)
        add(_step("build candidate", ops.STATUS_PASS if rc == 0 else ops.STATUS_FAIL,
                  "docker compose build ok" if rc == 0 else (err or out).strip()[-300:]))

    # 6. start staging (guarded compose up; cleaned up on partial failure)
    if not tools.get("docker"):
        add(_step("start staging", ops.STATUS_BLOCKED, "docker missing"))
    elif not identity_ok:
        add(_step("start staging", ops.STATUS_FAIL, identity_reason))
    else:
        rc, out, err = _compose(["up", "-d"], timeout=600)
        if rc == 0:
            add(_step("start staging", ops.STATUS_PASS, "docker compose up -d ok"))
        else:
            add(_step("start staging", ops.STATUS_FAIL, (err or out).strip()[-300:]))
            _cleanup_staging_compose()

    # 7. wait for readiness
    status_code, ok = _http_get(args.base_url.rstrip("/") + "/health/ready")
    add(_step("wait for readiness", ops.STATUS_PASS if ok and status_code == 200 else ops.STATUS_BLOCKED,
              f"{args.base_url}/health/ready -> {status_code}" if ok else "unreachable"))

    # 8. run migrations (guarded; advisory-locked via scripts/migrate.py)
    if not tools.get("docker"):
        add(_step("run migrations", ops.STATUS_BLOCKED, "docker missing"))
    elif not identity_ok:
        add(_step("run migrations", ops.STATUS_FAIL, identity_reason))
    else:
        rc, out, err = _compose(["exec", "-T", "api", "python", "scripts/migrate.py"], timeout=600)
        add(_step("run migrations", ops.STATUS_PASS if rc == 0 else ops.STATUS_FAIL,
                  "advisory-locked migrations applied" if rc == 0 else (err or out).strip()[-300:]))

    # 9. smoke test
    smoke_script = Path(ops.REPO_ROOT) / "scripts" / "smoke_test.py"
    if not smoke_script.exists():
        add(_step("smoke test", ops.STATUS_FAIL, "scripts/smoke_test.py missing"))
    elif status_code == 200:
        try:
            proc = subprocess.run(
                [sys.executable, str(smoke_script)],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "SMOKE_BASE_URL": args.base_url},
            )
            add(_step("smoke test", ops.STATUS_PASS if proc.returncode == 0 else ops.STATUS_FAIL,
                      (proc.stdout or proc.stderr).strip().splitlines()[-1] if (proc.stdout or proc.stderr) else ""))
        except (OSError, subprocess.SubprocessError) as exc:
            add(_step("smoke test", ops.STATUS_FAIL, f"failed: {exc}"))
    else:
        add(_step("smoke test", ops.STATUS_BLOCKED, "API not ready; cannot run smoke test"))

    # 10. health
    add(_step("verify health", ops.STATUS_PASS if ok and status_code == 200 else ops.STATUS_BLOCKED,
              f"/health/ready -> {status_code}" if ok else "unreachable"))

    # 11. metrics
    mcode, mok = _http_get(args.base_url.rstrip("/") + "/metrics")
    if mok and mcode in (200, 401):
        add(_step("verify metrics", ops.STATUS_PASS, f"/metrics -> {mcode} (200 or token-gated 401)"))
    else:
        add(_step("verify metrics", ops.STATUS_BLOCKED, f"/metrics -> {mcode}" if mok else "unreachable"))

    # 12. scheduler (guarded compose ps)
    if not tools.get("docker"):
        add(_step("verify scheduler", ops.STATUS_BLOCKED, "docker missing"))
    elif not identity_ok:
        add(_step("verify scheduler", ops.STATUS_FAIL, identity_reason))
    else:
        rc, out, err = _compose(["ps", "--status", "running", "-q", "scheduler"], timeout=120)
        running = rc == 0 and bool(out.strip())
        add(_step("verify scheduler", ops.STATUS_PASS if running else ops.STATUS_FAIL,
                  "scheduler container running" if running else (err or "scheduler container not running").strip()[-200:]))

    # 13. Redis (guarded compose exec; internal-only — no published port)
    if not tools.get("docker"):
        add(_step("verify Redis", ops.STATUS_BLOCKED,
                  "docker missing; staging Redis is internal-only (no published port)"))
    elif not identity_ok:
        add(_step("verify Redis", ops.STATUS_FAIL, identity_reason))
    else:
        rc, out, err = _compose(["exec", "-T", "redis", "redis-cli", "ping"], timeout=120)
        pong = rc == 0 and "PONG" in (out + err)
        add(_step("verify Redis", ops.STATUS_PASS if pong else ops.STATUS_FAIL,
                  "redis PONG" if pong else (err or out or "no PONG").strip()[-200:]))

    # 14. PostgreSQL (guarded compose exec; internal-only — no published port)
    if not tools.get("docker"):
        add(_step("verify PostgreSQL", ops.STATUS_BLOCKED,
                  "docker missing; staging database is internal-only (no published port)"))
    elif not identity_ok:
        add(_step("verify PostgreSQL", ops.STATUS_FAIL, identity_reason))
    else:
        pg_user = os.environ.get("POSTGRES_USER", "voxdesk")
        rc, out, err = _compose(["exec", "-T", "db", "pg_isready", "-U", pg_user], timeout=120)
        add(_step("verify PostgreSQL", ops.STATUS_PASS if rc == 0 else ops.STATUS_FAIL,
                  "pg_isready ok" if rc == 0 else (err or out or "pg_isready failed").strip()[-200:]))

    # 15. proxy
    pcode, pok = _http_get(args.base_url.rstrip("/") + "/")
    add(_step("verify proxy", ops.STATUS_PASS if pok and pcode < 500 else ops.STATUS_BLOCKED,
              f"GET / -> {pcode}" if pok else "unreachable"))

    # 16. deployment checks (static)
    cstatus, cdetail = _static_compose_checks()
    add(_step("deployment checks (static)", cstatus, cdetail))

    # 16b. compose config validation (docker, read-only)
    cc_status, cc_detail = _compose_config_check()
    add(_step("compose config (docker)", cc_status, cc_detail))

    # 17. security smoke
    sstatus, sdetail = _security_smoke()
    add(_step("security smoke checks", sstatus, sdetail))

    # 18. provider health (opt-in)
    outcomes = asyncio.run(registry.run_all())
    provider_report = ops.build_provider_report([o.as_dict() for o in outcomes])
    add(_step("provider health checks", provider_report["status"],
              f"{len(provider_report['rows'])} providers, opt-in={real_integration_enabled()}"))

    # 19. backup verification
    backup = ops.run_backup_orchestration(app_env, tools=tools)
    add(_step("backup verification", backup["status"], backup["evidence"], backup.get("record")))

    # 20. TLS (when a URL is supplied)
    if args.url:
        tls = ops.verify_tls(args.url)
        add(_step("TLS certification", tls["status"], tls["evidence"],
                  ops.EvidenceRecord(
                      item_id="tls-001", status=tls["status"],
                      classification="INFRASTRUCTURE", severity="P0",
                      command="scripts/staging_certify.py --staging --url",
                      timestamp=ops.now_iso(), release_commit=live["git_commit"],
                      environment=app_env, evidence=tls["evidence"])))

    # Observability (when configured)
    if args.prometheus_url:
        obs = ops.verify_observability(args.prometheus_url, args.grafana_url)
        add(_step("observability", obs["status"], obs["evidence"],
                  ops.EvidenceRecord(
                      item_id="obs-001", status=obs["status"],
                      classification="INFRASTRUCTURE", severity="P1",
                      command="scripts/staging_certify.py --staging --prometheus-url",
                      timestamp=ops.now_iso(), release_commit=live["git_commit"],
                      environment=app_env, evidence=obs["evidence"])))

    # Aggregate deploy-001 from the deployment steps
    dep_statuses = [s["status"] for s in steps
                    if s["step"] in ("build candidate", "start staging", "run migrations",
                                     "wait for readiness", "smoke test", "verify health")]
    if ops.STATUS_FAIL in dep_statuses:
        deploy_status = ops.STATUS_FAIL
    elif ops.STATUS_BLOCKED in dep_statuses:
        deploy_status = ops.STATUS_BLOCKED
    elif all(s == ops.STATUS_PASS for s in dep_statuses):
        deploy_status = ops.STATUS_PASS
    else:
        deploy_status = ops.STATUS_NOT_RUN
    add(_step("deployment certification (aggregate)", deploy_status,
              "aggregate of build/start/migrate/readiness/smoke/health"))
    records.append(ops.EvidenceRecord(
        item_id="deploy-001", status=deploy_status,
        classification="INFRASTRUCTURE", severity="P1",
        command="scripts/staging_certify.py --staging",
        timestamp=ops.now_iso(), release_commit=live["git_commit"],
        environment=app_env,
        evidence="aggregate of build/start/migrate/readiness/smoke/health "
                 f"({deploy_status})"))

    codes = [ops.exit_code_for_status(s["status"]) for s in steps]
    if codes and max(codes) == 0:
        overall = "PASS"
    elif ops.EXIT_FAIL in codes:
        overall = "FAIL"
    elif ops.EXIT_BLOCKED in codes:
        overall = "BLOCKED"
    else:
        overall = "NOT_RUN"
    return {
        "mode": "staging",
        "timestamp": ops.now_iso(),
        "release_commit": live["git_commit"],
        "environment": app_env,
        "steps": [{"step": s["step"], "status": s["status"], "detail": s["detail"]}
                  for s in steps],
        "overall": overall,
        "exit_code": ops.worst_exit_code(codes),
        "records": records,
    }


def _run_single_mode(args: argparse.Namespace, mode: str) -> dict:
    app_env = (os.environ.get("APP_ENV") or settings.app_env or "development").strip()
    live = ops.compute_live_facts()
    records: list[ops.EvidenceRecord] = []
    steps: list[dict] = []

    if mode == "preflight":
        report = ops.run_preflight(ops.REPO_ROOT, expected_commit=args.expected_commit)
        return {"mode": mode, "preflight": report, "overall": report["overall"],
                "exit_code": report["exit_code"], "records": records}

    if mode == "backup":
        result = ops.run_backup_orchestration(app_env)
        steps.append(_step("backup", result["status"], result["evidence"], result.get("record")))
        if result.get("record"):
            records.append(result["record"])

    elif mode == "restore":
        result = ops.run_restore_orchestration(app_env, args.dump, args.restore_target_db)
        steps.append(_step("restore", result["status"], result["evidence"], result.get("record")))
        if result.get("record"):
            records.append(result["record"])

    elif mode == "providers":
        outcomes = asyncio.run(registry.run_all())
        report = ops.build_provider_report([o.as_dict() for o in outcomes])
        steps.append(_step("providers", report["status"],
                           f"{len(report['rows'])} providers, opt-in={real_integration_enabled()}"))
        if real_integration_enabled():
            ingested = gate.evidence_for_provider_results(
                [o.as_dict() for o in outcomes], ops.today_iso(), "staging_certify.py --providers"
            )
            for ev in ingested.values():
                records.append(ops.EvidenceRecord(
                    item_id=ev.item_id, status=ev.status.value,
                    classification="EXTERNAL", severity="P1",
                    command="scripts/staging_certify.py --providers",
                    timestamp=ops.now_iso(), release_commit=live["git_commit"],
                    environment=app_env, evidence=ev.evidence))

    elif mode == "observability":
        result = ops.verify_observability(args.prometheus_url, args.grafana_url)
        steps.append(_step("observability", result["status"], result["evidence"],
                           ops.EvidenceRecord(
                               item_id="obs-001", status=result["status"],
                               classification="INFRASTRUCTURE", severity="P1",
                               command="scripts/staging_certify.py --observability",
                               timestamp=ops.now_iso(), release_commit=live["git_commit"],
                               environment=app_env, evidence=result["evidence"])))
        if steps[-1].get("record"):
            records.append(steps[-1]["record"])

    elif mode == "egress":
        inside = args.inside_staging or (
            os.environ.get("VOXDESK_STAGING_NETWORK", "").strip().lower() in {"1", "true", "yes", "on"}
        )
        allow_hosts = [
            h.strip() for h in (os.environ.get("VOXDESK_EGRESS_ALLOW_HOSTS", "") or "").split(",")
            if h.strip()
        ]
        result = ops.verify_egress(inside, allow_hosts=allow_hosts)
        steps.append(_step("egress", result["status"], result["evidence"],
                           ops.EvidenceRecord(
                               item_id="egress-001", status=result["status"],
                               classification="INFRASTRUCTURE", severity="P1",
                               command="scripts/staging_certify.py --egress",
                               timestamp=ops.now_iso(), release_commit=live["git_commit"],
                               environment=app_env, evidence=result["evidence"])))
        if steps[-1].get("record"):
            records.append(steps[-1]["record"])

    elif mode == "deploy-drill":
        result = ops.run_deploy_drill_orchestration(app_env, args.previous_sha)
        steps.append(_step("deploy-drill", result["status"], result["evidence"],
                           result.get("record")))
        if result.get("record"):
            records.append(result["record"])

    codes = [ops.exit_code_for_status(s["status"]) for s in steps]
    return {
        "mode": mode,
        "timestamp": ops.now_iso(),
        "release_commit": live["git_commit"],
        "environment": app_env,
        "steps": [{"step": s["step"], "status": s["status"], "detail": s["detail"]}
                  for s in steps],
        "overall": ("PASS" if codes and max(codes) == 0 else
                    ("FAIL" if ops.EXIT_FAIL in codes else
                     ("BLOCKED" if ops.EXIT_BLOCKED in codes else "NOT_RUN"))),
        "exit_code": ops.worst_exit_code(codes),
        "records": records,
    }


def _render(report: dict) -> str:
    lines = [f"staging_certify — mode={report.get('mode')}", "=" * 78]
    if report.get("preflight"):
        lines.append(ops.render_preflight(report["preflight"]))
        return "\n".join(lines)
    for s in report.get("steps", []):
        lines.append(f"  [{s['status']:<11}] {s['step']:<34} {s['detail']}")
    lines.append("-" * 78)
    lines.append(f"OVERALL: {report['overall']}   (exit {report['exit_code']})")
    return "\n".join(lines)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    modes = [m for m, on in (
        ("preflight", args.preflight), ("staging", args.staging),
        ("backup", args.backup), ("restore", args.restore),
        ("providers", args.providers), ("observability", args.observability),
        ("egress", args.egress), ("deploy-drill", args.deploy_drill),
    ) if on]
    if args.all_safe or not modes:
        modes = ["preflight", "staging", "backup", "providers", "observability", "egress", "deploy-drill"]

    reports = []
    for mode in modes:
        if mode == "staging":
            reports.append(_run_staging_sequence(args))
        else:
            reports.append(_run_single_mode(args, mode))

    # Guarded compose lifecycle (opt-in, staging project only). These run as
    # their own reports and are never part of the default mode list.
    if args.compose_up or args.compose_down:
        app_env = (os.environ.get("APP_ENV") or settings.app_env or "development").strip()
        live = ops.compute_live_facts()
        identity_ok, identity_reason = _staging_identity_ok(app_env)
        for action in ([m for m, on in (("compose-up", args.compose_up),
                                        ("compose-down", args.compose_down)) if on]):
            if not identity_ok:
                reports.append({
                    "mode": action,
                    "timestamp": ops.now_iso(),
                    "release_commit": live["git_commit"],
                    "environment": app_env,
                    "steps": [{"step": "staging identity guard", "status": ops.STATUS_FAIL,
                               "detail": identity_reason}],
                    "overall": "FAIL",
                    "exit_code": ops.EXIT_FAIL,
                    "records": [],
                })
                continue
            ok, detail = _compose_ready()
            if not ok:
                reports.append({
                    "mode": action,
                    "timestamp": ops.now_iso(),
                    "release_commit": live["git_commit"],
                    "environment": app_env,
                    "steps": [{"step": "docker compose", "status": ops.STATUS_BLOCKED,
                               "detail": detail}],
                    "overall": "BLOCKED",
                    "exit_code": ops.EXIT_BLOCKED,
                    "records": [],
                })
                continue
            if action == "compose-up":
                rc, out, err = _compose(["up", "-d", "--build"], timeout=1800)
                if rc == 0:
                    step_status, step_detail = ops.STATUS_PASS, "docker compose up -d --build ok"
                else:
                    step_status, step_detail = ops.STATUS_FAIL, (err or out).strip()[-300:]
                    _cleanup_staging_compose()
            else:
                rc, out, err = _compose(["down"], timeout=600)
                step_status, step_detail = (
                    (ops.STATUS_PASS, "docker compose down ok") if rc == 0
                    else (ops.STATUS_FAIL, (err or out).strip()[-300:])
                )
            reports.append({
                "mode": action,
                "timestamp": ops.now_iso(),
                "release_commit": live["git_commit"],
                "environment": app_env,
                "steps": [{"step": "docker compose", "status": step_status, "detail": step_detail}],
                "overall": ("PASS" if step_status == ops.STATUS_PASS else "FAIL"),
                "exit_code": ops.exit_code_for_status(step_status),
                "records": [],
            })

    if args.json:
        print(json.dumps(reports, indent=2, default=str))
    else:
        for report in reports:
            print(_render(report))
            print()

    if args.write_evidence:
        total = 0
        for report in reports:
            total += len(report.get("records", []))
            if report.get("records"):
                ops.merge_evidence_file(EVIDENCE_PATH, report["records"])
        print(f"evidence: merged {total} record(s) into {EVIDENCE_PATH}")

    codes = [r["exit_code"] for r in reports]
    return ops.worst_exit_code(codes)


if __name__ == "__main__":
    sys.exit(main())
```
