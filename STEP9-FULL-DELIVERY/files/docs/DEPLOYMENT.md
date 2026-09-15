# VoxDesk — Deployment (staging & production)

## What runs in production

Three containers (see `docker-compose.prod.yml`):

| Service | Job | Notes |
|---|---|---|
| `db` | PostgreSQL 16 | Persistent volume `pgdata` |
| `api` | FastAPI + built dashboard | Entrypoint runs `alembic upgrade head` first |
| `scheduler` | Background worker | Reminders, campaigns, CRM sync, knowledge ingestion, billing reconciliation |

## Prerequisites

1. A `.env` next to the compose file (start from `.env.example`). In production you MUST set:
   - `APP_ENV=production`
   - `SECRET_KEY`, `JWT_SECRET` (≥32 chars, `openssl rand -hex 32`)
   - `PUBLIC_BASE_URL=https://…` (used for Twilio `wss://` and webhooks)
   - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`
   - `DEEPGRAM_API_KEY`; at least one of `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY`
   - `ELEVENLABS_API_KEY`
   - `CRM_ENCRYPTION_KEYS` (application-level encryption for stored CRM credentials)
   - `KNOWLEDGE_EMBEDDING_PROVIDER` set to a real provider (not `hashing`)
   - `SENTRY_DSN` (recommended) — the app boots without it, but you get no error reporting
2. `./secrets/google_service_account.json` for Google Calendar (path from `GOOGLE_CREDENTIALS_JSON`).
3. The app **refuses to start** in production with insecure configuration
   (`Settings.validate_security()`), and **never** creates schema itself — Alembic is the sole
   schema owner.

## Start

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f api
```

## Health checks

- `/health` — liveness (process up)
- `/health/ready` — readiness (database reachable); returns **503** when the DB is down

## One-time bootstrap

```bash
# after migrations have run, create the first owner for a tenant
docker compose -f docker-compose.prod.yml exec -T api python -m scripts.create_owner \
  --tenant-id <uuid> --email you@example.com
# (password is read from VOXDESK_OWNER_PASSWORD or prompted)
```

## TLS / reverse proxy

The API trusts `X-Forwarded-*` from its proxy (`--proxy-headers`, see `scripts/entrypoint.sh`).
Put a TLS terminator (nginx/Caddy/ALB) in front; restrict `FORWARDED_ALLOW_IPS` to the proxy in
hardened setups. Without `https://` on `PUBLIC_BASE_URL`, Twilio Media Streams (`wss://`) and
signature validation will be wrong and production boot will refuse the config.

## CI

`.github/workflows/ci.yml` runs on every push to `main` and every PR:
backend lint + tests · frontend tests + build · a **PostgreSQL migration smoke test**
(`alembic upgrade head` + `downgrade base` round trip) so SQLite-only DDL bugs surface before deploy.
