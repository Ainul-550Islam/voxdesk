# VoxDesk — Performance & scale

## Latency budgets (voice path)

A caller on the phone will not wait. These budgets are enforced as timeouts in
code — a provider that misses them fails fast instead of hanging the call.

| Operation | Budget | Enforced at |
|---|---|---|
| Knowledge retrieval on a live call | 1.5 s hard ceiling | `KNOWLEDGE_RETRIEVAL_TIMEOUT_SECONDS` (`app/core/config.py`) |
| Calendar availability lookup | 3.0 s | `CALENDAR_VOICE_TIMEOUT_SECONDS` |
| Calendar provider call (general) | 6.0 s | `CALENDAR_REQUEST_TIMEOUT_SECONDS` |
| CRM provider call | 10.0 s | `CRM_REQUEST_TIMEOUT_SECONDS` |
| Embedding call | 20.0 s | `KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS` |
| Billing provider call | 15.0 s | `BILLING_REQUEST_TIMEOUT_SECONDS` |

## Guard-rail tests

- `tests/test_knowledge_performance.py` — extraction/chunking/retrieval ceilings
- `tests/test_performance_smoke.py` — bcrypt cost, stream-token mint/verify,
  SMS segment counting, retention math

These are **complexity tripwires**, not benchmarks: ceilings are several times
the observed median so a busy CI box doesn't cause flaky failures. A failure
means an accidental O(n²) or a cost regression, and should be investigated.

## Load testing

Tooling ships in `loadtest/` (Locust). The canonical runbook **and the safety
contract** (loopback-only unless `LOADTEST_ALLOW_REMOTE=1`, read-only tasks,
no provider calls) live in `docs/LOAD-TESTING.md`. Start the API, then:

```bash
pip install locust
locust -f loadtest/locustfile.py --host http://localhost:8000
# open http://localhost:8089, pick users/spawn rate, start
```

The default tasks hit `/health`, `/health/ready`, `/metrics` and an API-404
invariant. To exercise authenticated traffic, add a task that logs in
(`POST /auth/login`) and replays the `Authorization` header against
`/api/tenants/…` reads — never a mutating or money-spending endpoint.

What to watch:
- p50/p95 latency of `/health/ready` (DB round trip + `SELECT 1`)
- error rate on `/api/*` (should be 401/403/404, never 500)
- worker CPU/memory under load

## Scaling up

| Axis | How |
|---|---|
| API workers | `WEB_CONCURRENCY` env → `--workers` in `scripts/entrypoint.sh` (per-CPU) |
| DB connections | `pool_size`/`max_overflow` in `app/db/session.py`; scale vertically first, then read replicas |
| Scheduler | single process by design (one worker, not a distributed system — see `scripts/scheduler.py` header); run exactly one replica, its loops are idempotent and self-healing |
| Static dashboard | served by the API process (StaticFiles); move behind a CDN for multi-region |
| Voice media | WebSocket terminates on the API pod; a sticky LB (source-IP or call-SID) keeps a call on one worker for its lifetime |

## Known limits (be honest with buyers)

- No horizontal DB sharding; single primary Postgres. Fine to a comfortable
  multi-hundred-concurrent-call scale; beyond that, split the voice/media tier
  from the API tier.
- No multi-region deployment; latency to your primary region is your floor.
- `--workers N` runs the idempotent plan seed per worker at startup — safe by
  design, but avoid very high worker counts until it is profiled.
