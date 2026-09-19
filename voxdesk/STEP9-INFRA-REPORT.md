# VoxDesk — Step 9 Production Infrastructure (CI/CD, Docker, Observability) — Report

## ⚠️ GitHub status (honest)
This environment still has **no live connection to your private GitHub repository** — no `gh` CLI, no SSH
key, no credentials, and no completed device flow (every poll returned `authorization_pending`). I therefore
could not create/push the branch on your remote, and I cannot truthfully return a *remote* SHA. **Your repo
is untouched.**

Everything below was implemented and verified in the sandbox against the verbatim Step 8 source (baseline
`736ded1`), layered on top of the already-verified Phase A. Deliverable = two apply-ready patches.

## Branch / commit (sandbox mirror)
- Branch: `step9/production-infrastructure` (layered on `step9/phase-a-production-safety`)
- Commit: `7350b7d30db44f648a90ef8bc07fe71a46999c46` — "Step 9: production infrastructure (CI/CD, Docker, observability)"
- Patches (both verified to `git am`-apply cleanly onto a pristine Step 8 tree, in order):
  1. `/home/user/0001-Step-9-Phase-A-production-safety-hardening.patch` (Phase A — already delivered)
  2. `/home/user/0001-Step-9-production-infrastructure-CI-CD-Docker-observ.patch` (this work)

## What was added (12 files, +560/−13)

| Component | File | What it does |
|---|---|---|
| CI/CD | `.github/workflows/ci.yml` (new) | 3 jobs: backend (ruff fatal-rules + pytest), **PostgreSQL migration smoke test** (upgrade head + downgrade-base round trip on `postgres:16-alpine`), frontend (vitest + vite build, `npm ci`) |
| Docker | `Dockerfile` (rewritten) | Multi-stage: node stage builds dashboard → `dist`; python 3.12 stage ships `app`+`alembic`+`scripts`+`dist`, runs as **unprivileged `appuser`**, has a **HEALTHCHECK** |
| Docker | `scripts/entrypoint.sh` (new) | `alembic upgrade head` → `exec uvicorn --workers --proxy-headers --forwarded-allow-ips` |
| Docker | `docker-compose.prod.yml` (new) | db + api + scheduler; healthchecks, persistent volumes, **no** source mounts / `--reload` |
| Docker | `.dockerignore` (new) | keeps `.env`, `secrets/`, caches out of the image |
| Observability | `app/core/config.py` | `SENTRY_DSN` (optional), `LOG_LEVEL`, `LOG_FORMAT` |
| Observability | `app/core/logging.py` | JSON renderer in production, level from settings; `timed()` preserved |
| Observability | `app/main.py` | guarded `sentry_sdk.init`; `/health/ready` readiness probe (real `SELECT 1`, 503 on DB down); serves built dashboard (StaticFiles + SPA fallback, unknown `/api/*` still 404) |
| Docs | `docs/DEPLOYMENT.md` (new) | staging/prod runbook |
| Deps | `requirements.txt` | added `aiofiles==25.1.0` (StaticFiles) |
| Tests | `tests/test_production_infra.py` (new) | 10 deterministic tests |

## Verification results (fresh runs this turn)

| Check | Result |
|---|---|
| Backend suite | ✅ **1867 passed, 30 skipped** (1857 Phase A + 10 new infra) |
| Focused (infra + Phase A safety) | ✅ **21 passed** |
| Frontend tests | ✅ **360 passed** |
| Frontend build | ✅ clean |
| `compileall app scripts alembic tests` | ✅ OK |
| ruff fatal-rule selector (`E9,F63,F7,F82`) | ✅ passes (18 pre-existing *cosmetic* violations left untouched — preservation rule) |
| `git diff --check` | ✅ clean |
| Live smoke test (dist present) | `/` & `/agent` → 200 SPA shell · `/health` → 200 · `/api/tenants` → 401 · `/api/unknown` → 404 JSON · `/health/ready` → 503 (DB down) |
| YAML valid | `ci.yml`, `docker-compose.prod.yml` parse OK |
| `sh -n entrypoint.sh` | OK |
| Patch integrity | both patches `git am` clean; patched tree byte-identical to verified tree |

## Notes & remaining risks (not skipped — flagged)

1. **`docker build` / `alembic` on real Postgres could not be executed here** — Docker and Postgres are not
   available in this sandbox. The migration smoke test is shipped but its *first* CI run is the real
   proof; any failure there is a genuine migration defect to fix, not a config issue.
2. **ruff**: the full `ruff check` reports 18 pre-existing cosmetic violations (unused imports, an
   ambiguous `l` variable, one duplicated test name). I deliberately did **not** touch unrelated code;
   CI blocks only on fatal rules for now, with a comment naming the cleanup pass as follow-up.
3. **Multi-worker + lifespan seeding**: with `--workers N`, each worker runs the idempotent plan seeding
   at startup; it is safe by design ("never overwrites an operator-edited price"), but worth a quick look
   under load.
4. **`FORWARDED_ALLOW_IPS` defaults to `*`** for single-proxy setups — tighten to the proxy IP in hardened
   deployments (documented in the entrypoint).

## How to apply (your machine, from the repo root)
```bash
git checkout main
git checkout -b step9/phase-a-production-safety
git am 0001-Step-9-Phase-A-production-safety-hardening.patch
git checkout -b step9/production-infrastructure
git am 0001-Step-9-production-infrastructure-CI-CD-Docker-observ.patch
python3 -m pytest tests/ -q && python3 -m compileall -q app scripts alembic tests
cd dashboard && npm test && npm run build && cd ..
git push -u origin step9/phase-a-production-safety step9/production-infrastructure   # then PRs — not main
```

## To let me push directly (and get your real SHAs)
Approve the fresh device code at **https://github.com/login/device** → **`FC89-2ED5`** (~15 min), then say
"done" + owner/repo (e.g. `Ainul-Islam/voxdesk`). I'll clone, apply both patches, run the suite against the
real repo, push both branches, and report the genuine remote SHAs.
