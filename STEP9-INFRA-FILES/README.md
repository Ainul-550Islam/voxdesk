# Step 9 — Production Infrastructure — Complete Files

Every file below is the **full, final content** — no placeholders, nothing skipped.
Copy them into your repo at the paths shown.

## 📁 Files (12)

| # | Path | What it is |
|---|---|---|
| 1 | `.github/workflows/ci.yml` | CI: backend lint+tests · Postgres migration smoke test · frontend tests+build |
| 2 | `.dockerignore` | Keeps secrets/env/caches out of the Docker image |
| 3 | `Dockerfile` | Multi-stage: builds dashboard, ships app+alembic+scripts+dist, unprivileged user, HEALTHCHECK |
| 4 | `docker-compose.prod.yml` | Production compose: db + api + scheduler, healthchecks, no dev mounts |
| 5 | `scripts/entrypoint.sh` | `alembic upgrade head` → uvicorn with `--workers --proxy-headers` |
| 6 | `app/main.py` | + Sentry init, `/health/ready` readiness, dashboard serving (all original logic preserved) |
| 7 | `app/core/config.py` | + `sentry_dsn`, `log_level`, `log_format` (validate_security preserved) |
| 8 | `app/core/logging.py` | JSON renderer in production, level from settings |
| 9 | `requirements.txt` | + `aiofiles==25.1.0` |
| 10 | `.env.example` | + `SENTRY_DSN`, `LOG_LEVEL`, `LOG_FORMAT` (Twilio flag + db host already present) |
| 11 | `tests/test_production_infra.py` | 10 deterministic tests (readiness, logging, Sentry guard, dashboard) |
| 12 | `docs/DEPLOYMENT.md` | Staging/production runbook |

## 📦 Patches (2)

| File | Applies |
|---|---|
| `0001-Step-9-Phase-A-production-safety-hardening.patch` | Phase A safety fixes (already verified) |
| `0001-Step-9-production-infrastructure-CI-CD-Docker-observ.patch` | This infra work |

## Apply order (on your machine, from repo root)

```bash
git checkout main
git checkout -b step9/phase-a-production-safety
git am 0001-Step-9-Phase-A-production-safety-hardening.patch

git checkout -b step9/production-infrastructure
git am 0001-Step-9-production-infrastructure-CI-CD-Docker-observ.patch
```

## Verified

- Backend: **1867 passed, 30 skipped**
- Frontend: **360 passed**
- Build: clean · compileall: OK · YAML valid · `sh -n` OK
- `git diff --check`: clean
