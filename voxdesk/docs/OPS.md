# VoxDesk — Operations runbook

Target topology: single VM + docker-compose (see `docker-compose.prod.yml`).

## Architecture

| Service | Role | Ports |
|---|---|---|
| db | PostgreSQL 16 | internal |
| redis | cache + rate limiting | internal |
| api | FastAPI + dashboard + migrations | 8000 (host loopback only) |
| scheduler | background worker (reminders, campaigns, CRM, knowledge, billing, retention) | internal |
| backup | nightly pg_dump | internal |
| prometheus | metrics scraper | internal |
| grafana | dashboards + alerts | 3000 (host loopback only) |
| caddy | TLS termination + reverse proxy | 80 / 443 |

## TLS (Caddy)

Caddy is the public entry point and terminates TLS. Set `DOMAIN` in `.env` to
your public hostname to get automatic Let's Encrypt certificates (and
`GRAFANA_DOMAIN` to expose Grafana on a second hostname). Leave `DOMAIN` empty
to serve plain HTTP on :80 behind your own load balancer. WebSocket upgrade
(`wss://` for the live voice stream) passes through with no extra config.

The API and Grafana bind to `127.0.0.1` on the host, so the only public
surface is Caddy on 80/443.

## Deploy

```bash
scripts/deploy.sh          # pull, build, migrate, restart
scripts/deploy.sh --no-build   # skip image rebuild
```

## Rollback

```bash
# Pin the image to the last known-good tag in docker-compose.prod.yml, or:
git checkout <previous-good-sha> && scripts/deploy.sh
# If a migration caused the problem:
docker compose -f docker-compose.prod.yml run --rm --entrypoint sh api \
  -c "alembic downgrade -1"
```

## Backup & restore

Automated: nightly `pg_dump` by the `backup` service (last 14 kept, optional
rclone sync). Manual:

```bash
scripts/backup.sh                      # create a dump
scripts/restore.sh backups/voxdesk-*.dump   # restore (into an empty DB)
```

## Health checks

- `GET /health` — liveness (process up; no dependency checks)
- `GET /health/ready` — readiness (503 when the DB is down, when Redis is
  configured but unreachable, or — in production — when no voice provider is
  configured). See `docs/OBSERVABILITY.md#health-liveness-vs-readiness`.
- `GET /metrics` — Prometheus scrape (token-gated when `METRICS_TOKEN` is set)

## Monitoring

Grafana at `:3000` (default admin / `GRAFANA_ADMIN_PASSWORD`). Pre-provisioned
"VoxDesk Overview" dashboard + alert rules in `observability/alerts.yml`.

## Logs

```bash
docker compose -f docker-compose.prod.yml logs -f api scheduler
```

Production logs are JSON (`LOG_FORMAT=json`) and include the `X-Request-ID` of
each request — grep a request id to reconstruct a failing call.

### Real-call E2E test mode (Step 5)

A human-controlled Twilio test call is the only sanctioned way a *real* phone
call exercises the AI voice pipeline on purpose. It is armed with
`E2E_ENABLED=true` in a **non-production** environment (the app refuses to boot
in production with it set) and is documented in
[`REAL-E2E-RUNBOOK.md`](REAL-E2E-RUNBOOK.md).

Log events emitted by the guard (all secret-free — no full phone numbers):

| Event | Meaning |
|---|---|
| `call.e2e_allowed` | An allowlisted test-tenant call passed the guard (`test_tenant` tag). |
| `call.e2e_rejected` | The guard refused a call (`reason` tag names the violated rule). |
| `call.e2e_rejected_hangup` | The webhook hung up on a refused caller before any row was created. |
| `call.e2e_rejected_production` | E2E mode encountered in production (fatal). |
| `call.incoming_duplicate` | A re-delivered `/voice` for an already-known `call_sid`. |

`call.incoming` / `call.incoming.ivr` now carry `e2e_test=true` for calls the
guard admitted, so test traffic is greppable end-to-end.

## Common incidents

| Symptom | Likely cause | Action |
|---|---|---|
| `/health/ready` 503 | DB down / exhausted pool | `docker compose ps db`; check `DB_POOL_SIZE` |
| 429s everywhere | rate limit misconfig | check `RATE_LIMIT_*`; the limiter fails closed |
| Webhooks 403 | Twilio token/env mismatch | check `TWILIO_AUTH_TOKEN`, `APP_ENV=production` |
| `wss://` failures | proxy/TLS misconfig | `PUBLIC_BASE_URL` must be https; `FORWARDED_ALLOW_IPS` |
| Scheduler stuck | one loop errored | logs; loops are isolated and self-healing |
