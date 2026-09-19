# VoxDesk — Step 9, Phase 1: Production Infrastructure Readiness Audit

**Mode:** READ-ONLY. No application files were modified. The audit ran against an extracted copy
of the Step 8 source bundle (`/home/user/audit/voxdesk/`), never against the live repo.

**Executed checks in-sandbox:**

| Check | Result |
|---|---|
| Backend test suite | ✅ **1846 passed, 30 skipped** (97s) — matches your reported number exactly |
| Frontend test suite (vitest) | ✅ **360 passed** (14 files) — matches your reported number exactly |
| Frontend production build (`vite build`) | ✅ Clean — 52 modules, `dist/` produced in 1.5s |
| Clean-install of `requirements.txt` | ✅ `pip install -r requirements.txt` in a fresh venv → **exit 0**, all pins resolved (pipecat-ai 0.0.55, httpx 0.27.2, python-docx 1.2.0, twilio 9.4.1, …) |
| `docker build` / `docker compose config` | ⛔ Not executable — Docker is not installed in this sandbox. Dockerfile/compose validated statically (flagged below) |

Environment note: sandbox Python is **3.13.14**; the Dockerfile pins `python:3.12-slim`. The dependency
install was verified on 3.13, which is a stronger clean-install signal, not a weaker one.

---

## 1. Infrastructure readiness verdict

# 🟡 READY WITH CONDITIONS

The **application** is production-grade: tests green, dependencies fully pinned and clean-installable,
a real security validation gate at boot, real auth/RBAC/tenant-isolation, a wired Alembic history.
The **infrastructure** is not yet: there is no CI, the Docker image is API-only, the frontend has no
production serving path, and two fail-open behaviors (`create_all` at boot, the Twilio dev bypass)
plus open `/docs` remain. All of these are fixable in Step 9 **without new feature work** — hence
"with conditions," not "not ready."

---

## 2. CRITICAL / HIGH blockers

### 🔴 C1 — `Base.metadata.create_all` runs at startup (CRITICAL, confirmed defect, fix NOW)
- **File/line:** `app/main.py:42` — `await conn.run_sync(Base.metadata.create_all)   # use Alembic in production`
- **Type:** Confirmed defect (the comment acknowledges it but the code path is **unguarded** — it runs in dev *and* prod).
- **Secondary site:** `scripts/seed_demo_tenant.py:15` (same call).
- **Production impact:** SQLAlchemy creates tables outside Alembic's versioned history. Any schema change that ships
  without a migration is silently masked at boot; conversely, future `alembic upgrade head` runs on an environment
  where `create_all` already created divergent objects → drift, broken upgrades, and a fake sense that "it works."
  This is the single most dangerous infra pattern in the repo.
- **Fix:** make Alembic the sole schema owner — run `create_all` only when `not settings.is_production` (or remove it
  and keep it solely in test fixtures, where `tests/conftest.py:35` already creates its own in-memory schema).
- **When:** **Now** (Step 9, item 1).

### 🔴 H1 — Twilio webhook verification fails open on `APP_ENV=development` (HIGH, confirmed defect, fix NOW)
- **File/line:** `app/telephony/stream_auth.py:86-87` — `if settings.app_env == "development": return True`
- **Type:** Confirmed defect (fail-open).
- **Production impact:** Any environment whose `APP_ENV` is accidentally left/typo'd as `development` silently accepts
  **every** Twilio webhook without signature verification → forged inbound-call, IVR, and messaging webhooks. Note the
  compounding effect: with `APP_ENV=development`, `is_production` is also `False`, so the boot-time `validate_security()`
  gate downgrades from *refuse-to-start* to *warn-only* (`app/main.py:31-38`). One bad env var disables two layers.
- **Fix:** fail **closed** — verify unless an explicit, separately-named flag (e.g. `TWILIO_SKIP_WEBHOOK_VERIFY=true`)
  is set, and refuse that flag in production. Keep the boot gate as a backstop.
- **When:** **Now** (Step 9, item 2).

### 🔴 H2 — `/docs`, `/redoc`, `/openapi.json` are unauthenticated (HIGH, confirmed defect, fix NOW)
- **File/line:** `app/main.py:79` — `FastAPI(title="VoxDesk", version="0.4.0", lifespan=lifespan)` with **no**
  `docs_url`/`redoc_url`/`openapi_url` override → FastAPI defaults expose all three publicly.
- **Type:** Confirmed defect.
- **Production impact:** Full API schema disclosure + interactive "Try it out" against a live API. On a
  multi-tenant voice/billing app this is a reconnaissance and abuse surface that should not exist in prod.
- **Fix:** `docs_url=None, redoc_url=None` (and optionally `openapi_url=None`) when `settings.is_production`,
  or gate them behind an auth dependency.
- **When:** **Now** (Step 9, item 3).

### 🔴 H3 — No CI/CD at all (HIGH, confirmed gap, fix NOW-ish)
- **File/line:** `.github/` absent — no `workflows/` anywhere in the repo.
- **Type:** Confirmed gap (not a defect in code).
- **Production impact:** 1,846 backend + 360 frontend tests exist but nothing runs them automatically. Every future
  change ships on trust. This is the direct cause of the "Postgres migration smoke-test gap" (H5).
- **Fix:** add `.github/workflows/ci.yml` — backend pytest, frontend vitest, frontend build, `ruff check`, plus the
  Postgres migration smoke test below.
- **When:** Step 9, item 5.

### 🔴 H4 — Docker image is API-only; prod startup has no migration step (HIGH, confirmed gap, fix NOW-ish)
- **File/line:** `Dockerfile:10` — `COPY app ./app` only. `alembic/`, `alembic.ini`, `scripts/`, `dashboard/` are never
  copied. `Dockerfile:12` — `CMD ["uvicorn", "app.main:app", ...]` with **no** `alembic upgrade head` before it.
- **Type:** Confirmed gap.
- **Production impact:** The image cannot run migrations (no `alembic/` inside), cannot run the scheduler (no
  `scripts/`), cannot serve the dashboard, runs as root, has no HEALTHCHECK, and uses a single uvicorn worker with no
  `--proxy-headers`. It *boots*, but it is not a production deployment unit.
- **Fix:** multi-stage Dockerfile: stage 1 builds `dashboard/` → `dist`; final stage copies `app`, `alembic`, `alembic.ini`,
  `scripts`, serves `dist` (nginx sidecar or FastAPI `StaticFiles`), sets a non-root `USER`, adds `HEALTHCHECK`, and an
  entrypoint that runs `alembic upgrade head` before exec'ing uvicorn with `--workers` + `--proxy-headers --forwarded-allow-ips`.
- **When:** Step 9, items 4, 7, 8.

### 🔴 H5 — PostgreSQL migration smoke-test gap (HIGH, untested, fix soon)
- **File/line:** no Postgres anywhere in the test harness — `tests/conftest.py:33` uses `sqlite+aiosqlite:///:memory:`.
  Alembic history exists (`alembic/versions/0001…0008`) but is never exercised against Postgres.
- **Type:** Untested.
- **Production impact:** Migrations are only ever run on SQLite in dev. Postgres-specific DDL (ENUMs, JSONB, UUIDs,
  partial indexes, server defaults) is the most common place for "works locally, explodes in staging."
- **Fix:** CI job that starts `postgres:16-alpine`, runs `alembic upgrade head` on an empty DB **and** from scratch
  after `alembic downgrade base`, then boots the app against it.
- **When:** Step 9, item 5 (part of CI).

---

## 3. Full findings register (all items, classified)

| # | Finding | Class | Type | File : line | Impact | Fix now / later |
|---|---|---|---|---|---|---|
| 1 | `create_all` at startup | CRITICAL | Confirmed | `app/main.py:42` (+`scripts/seed_demo_tenant.py:15`) | Schema drift, masked migrations | NOW |
| 2 | Twilio verify bypass on dev env | HIGH | Confirmed | `app/telephony/stream_auth.py:86-87` | Forged webhooks + gate downgrade | NOW |
| 3 | Open `/docs` `/redoc` `/openapi.json` | HIGH | Confirmed | `app/main.py:79` | Schema disclosure, interactive exploit surface | NOW |
| 4 | No CI/CD | HIGH | Confirmed gap | `.github/` absent | No automated gates | NOW-ish |
| 5 | Docker image API-only / no migration step | HIGH | Confirmed gap | `Dockerfile:10,12` | Can't migrate/schedule/serve dashboard in prod | NOW-ish |
| 6 | Postgres migration smoke gap | HIGH | Untested | `tests/conftest.py:33` | Postgres DDL breaks in staging | SOON |
| 7 | Frontend artifact has no prod serving path | HIGH | Confirmed gap | `dashboard/` + no `StaticFiles`/nginx stage | `vite build` works; nothing serves `dist` | SOON |
| 8 | uvicorn: single worker, no `--proxy-headers`/`--forwarded-allow-ips` | MEDIUM | Limitation | `Dockerfile:12`, `docker-compose.yml:24` | Wrong scheme/URL behind TLS proxy → Twilio signature & `wss://` breakage | SOON |
| 9 | `/health` is liveness-only; no readiness/DB probe; no container HEALTHCHECK (api/scheduler) | MEDIUM | Confirmed gap | `app/main.py:104-106` | LB can't detect a DB-less "healthy" node | SOON |
| 10 | Sentry declared but never initialized; no global exception handler | MEDIUM | Confirmed gap | `requirements.txt:71` only (no `SENTRY_DSN` in `config.py`, no `sentry.init`) | No production error visibility | SOON |
| 11 | Logging renderer is dev-oriented | MEDIUM | Limitation | `app/core/logging.py:8-16` (`ConsoleRenderer`, no JSON, no env level) | Logs hard to aggregate in prod | LATER |
| 12 | DB connection has no SSL options | MEDIUM | Limitation | `app/db/session.py:24-30` | Managed Postgres requires TLS; defaults are cleartext | LATER |
| 13 | `.env.example` `DATABASE_URL` uses `localhost`, but compose API container needs `db` | MEDIUM | Confirmed (dev) | `.env.example:29` vs `docker-compose.yml` (service `db`) | `docker compose up` can't reach DB with default env | NOW (dev fix) |
| 14 | `is_production` only matches `production|prod`; "staging" gets warning-only gate | MEDIUM | Limitation | `app/core/config.py:127-128` | Staging never exercises the refuse-to-start path | SOON |
| 15 | `docker-compose.yml` is dev-only (`--reload`, bind mounts, `npm run dev`, exposed 5432) | MEDIUM | Confirmed (dev) | `docker-compose.yml:8,22-24,39-43` | No prod compose variant exists | LATER |
| 16 | Live Stripe/Twilio/Calendar never sandbox-tested | MEDIUM | Untested | (no harness) | Money/auth/calendar paths unexercised | LATER |
| 17 | `pipecat-ai==0.0.55` + `httpx==0.27.2` coupled pin | MEDIUM | Limitation (documented) | `requirements.txt:17,40-45` | Blocks independent httpx security updates | LATER |
| 18 | No `.dockerignore` | LOW | Confirmed gap | (absent) | Build context ships `node_modules`, caches | SOON |
| 19 | `datetime.utcnow()` deprecations (py 3.13 warns; removal in future) | LOW | Limitation | e.g. `app/integrations/crm/legacy.py:41`, tests | Future Python upgrade breaks | LATER |
| 20 | No `TRUSTED_HOSTS`/host allow-list enforcement | LOW | Limitation | `app/main.py` (middleware) | Minor host-header risk | LATER |

**Step 8 final-audit findings — disposition:**

| Step 8 finding | Status now | Evidence |
|---|---|---|
| `Base.metadata.create_all` at startup | ❌ Still open (CRITICAL) | `app/main.py:42` |
| PostgreSQL migration smoke-test gap | ❌ Still open (HIGH) | no Postgres in harness / no CI |
| `APP_ENV=development` Twilio bypass | ❌ Still open (HIGH) | `app/telephony/stream_auth.py:86-87` |
| Unauthenticated `/docs` endpoints | ❌ Still open (HIGH) | `app/main.py:79` |
| Live Stripe/Twilio/Calendar not sandbox-tested | ❌ Still open (MEDIUM) | no integration harness |
| Pipecat/httpx pins | ⚠️ Documented limitation, clean-install verified | `requirements.txt:40-45`; install exit 0 |
| python-docx dependency | ✅ Fixed | `requirements.txt:67` `python-docx==1.2.0` |

---

## 4. Answers to A–H

**A. Can the repo build a production Docker image?**
It can build *an* image (Dockerfile is syntactically valid; `pip install` step verified). It cannot build a
*production* image today: no migrations, no scheduler, no dashboard, root user, no healthcheck, one worker.
Static verdict only — Docker not executable in this sandbox.

**B. Can a clean environment install all dependencies?**
✅ **Yes — verified.** Fresh venv, `pip install -r requirements.txt` → exit 0, every pin resolved with no conflict
(the Step 8 requirements-conflict fix holds). Installed on Python 3.13; Dockerfile targets 3.12.

**C. Is there a production startup command?**
Partial. `Dockerfile:12` runs single-worker uvicorn. Missing: `alembic upgrade head` before start, worker count,
proxy-headers, scheduler process, frontend serving.

**D. Is Alembic the actual production migration path?**
Intended: **yes** — `alembic/env.py` correctly reads the URL from app settings (`:13`) with `compare_type=True`,
and 8 revisions exist (`0001_baseline` → `0008_billing`). Enforced: **no** — `create_all` at boot (C1) bypasses it
and no prod step runs migrations.

**E. Is CI configured?**
**No.** `.github/` is absent.

**F. Are frontend and backend deployment artifacts defined?**
Backend: one API-only Dockerfile (partial). Frontend: `vite build` → `dist/` is defined **and verified working**,
but no serving/deployment target consumes it.

**G. Secrets / environment variables required (from `config.py` + `.env.example`):**
Core: `APP_ENV`, `SECRET_KEY`, `PUBLIC_BASE_URL`, `JWT_SECRET`, `CORS_ORIGINS`, `DATABASE_URL`.
Voice/telephony: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `DEEPGRAM_API_KEY`.
AI: one of `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY`; `ELEVENLABS_API_KEY`.
Calendar: `GOOGLE_CREDENTIALS_JSON` (path to a service-account file under `./secrets/`).
Billing: `BILLING_PROVIDER`, and if `stripe`: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PUBLISHABLE_KEY`, checkout/portal URLs.
CRM: `CRM_ENCRYPTION_KEYS` (required in prod). RAG: `KNOWLEDGE_EMBEDDING_PROVIDER` (non-`hashing` in prod), S3 vars if `s3`.

**H. What is missing for a safe staging deployment?**
1. CI with the Postgres migration smoke test (H3/H5). 2. Migration step at startup + `create_all` gated off (C1).
3. Twilio verify fail-closed (H1). 4. `/docs` closed in prod (H2). 5. Prod Docker image incl. scheduler + migrations
+ non-root + healthcheck (H4). 6. Dashboard `dist` serving path (H4). 7. Readiness endpoint + JSON logs + Sentry.
8. Postgres with TLS + correct `DATABASE_URL`. 9. A prod compose/deploy manifest (no dev mounts, no `--reload`).

---

## 5. Existing infrastructure that is already correct ✅

- **Boot-time security gate** `Settings.validate_security()` (`app/core/config.py:217-295`) — refuses prod start on
  default JWT secret, short secret, default `SECRET_KEY`, `"*"` CORS, non-https `PUBLIC_BASE_URL`, missing Twilio token,
  `hashing` embedder, missing CRM encryption keys, Stripe test keys in prod, unlimited entitlements in prod. Excellent.
- **Alembic plumbing** (`alembic/env.py`) — reads URL from settings, `compare_type=True`, async engine, 8 clean revisions.
- **Requirements hygiene** (`requirements.txt`) — fully pinned with explanatory comments; clean-install verified.
- **Auth** — bcrypt (direct, not passlib), PyJWT pinned on decode, scoped RBAC, tenant-isolation tests all pass.
- **Stream-token auth** (`app/telephony/stream_auth.py`) — HMAC, 120s TTL, constant-time compare, replay/future-skew rejection.
- **Secret handling** — env-var driven, `.env` + `secrets/` gitignored, `scripts/create_owner.py` reads password from
  env/prompt (never argv), no default credentials anywhere.
- **CORS** — explicit origin list, `allow_credentials=True`, `"*"` refused by the security gate.
- **Scheduler process isolation** (`scripts/scheduler.py`) — separate process, per-loop try/except so one broken loop
  can't kill webhooks; graceful SIGTERM handling.
- **Frontend** — XSS-boundary tests (`meeting-url-xss`, `transcript-xss`), no-fake-revenue guard, vitest green, build clean.

---

## 6. Exact recommended implementation order for Step 9

**Phase A — safety fixes (do first, small diffs, no feature work):**
1. Gate `create_all` to non-production in `app/main.py:42` (Alembic becomes sole schema owner). *(C1)*
2. Make Twilio verification fail-closed with an explicit skip flag refused in prod. *(H1)*
3. Disable `/docs` + `/redoc` (+ `/openapi.json`) in production. *(H2)*
4. Fix `.env.example` `DATABASE_URL` → `db:5432` so `docker compose up` works out of the box. *(#13)*

**Phase B — CI/CD (unblocks everything else):**
5. Add `.github/workflows/ci.yml`: backend `pytest`, frontend `vitest run`, `vite build`, `ruff check`, and a
   Postgres service job running `alembic upgrade head` on empty + `downgrade base` round-trip. *(H3, H5)*
6. Add `.dockerignore` (node_modules, dist, .env, secrets, .git). *(#18)*

**Phase C — production image & deploy artifact:**
7. Multi-stage Dockerfile: build `dashboard` → `dist`; final image carries `app`, `alembic/`, `alembic.ini`, `scripts/`,
   serves `dist`; non-root `USER`; `HEALTHCHECK`; entrypoint = `alembic upgrade head` → exec uvicorn with
   `--workers N --proxy-headers --forwarded-allow-ips …`. *(H4, #8)*
8. Readiness endpoint (DB ping) + keep `/health` as liveness. *(#9)*
9. Prod deploy manifest (compose/helm) without dev mounts or `--reload`, managed Postgres with TLS. *(#12, #15)*

**Phase D — observability:**
10. Wire Sentry (`SENTRY_DSN` in config + `sentry_sdk.init`) and a JSON structlog renderer; add a global exception handler. *(#10, #11)*

**Phase E — integration coverage (later):**
11. Stripe test-mode, Twilio test credentials, and Google Calendar sandbox end-to-end tests. *(#16)*

---

*Audit performed read-only. No files edited, no packages upgraded, no CI created, no Docker/migration/application
changes made. This report stops at the audit boundary per instructions.*
