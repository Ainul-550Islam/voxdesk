# VoxDesk — Load testing (Step 7)

Load testing exists to prove the process does not fall over under
concurrency. It must **never** turn into a production incident of its own
making. This document is the safety contract.

---

## Safety contract (the whole point)

1. **Default tasks are read-only and unauthenticated.** They hit `/health`,
   `/health/ready`, `/metrics` and an API-404 invariant. No task logs in,
   places a call, charges Stripe, books an appointment, sends an SMS, or
   writes a CRM contact. No provider is ever contacted.
2. **Loopback-only by default.** A non-loopback target is refused at the start
   of every simulated user unless `LOADTEST_ALLOW_REMOTE=1` is set. A
   mis-aimed `--host` therefore cannot become a production load test — it
   stops dead with a reason string.
3. **The refusal is a hard stop**, not a warning: each simulated user raises
   `StopUser` with the reason, so nothing proceeds against a refused target.

The guard lives in `loadtest/safety.py` (pure, unit-tested) and is wired into
`loadtest/locustfile.py`.

---

## Running

```bash
pip install locust

# local / sandbox (always allowed):
locust -f loadtest/locustfile.py --host http://localhost:8000

# staging (explicit opt-in required):
LOADTEST_ALLOW_REMOTE=1 \
  locust -f loadtest/locustfile.py --host https://staging.example.com
```

Headless:

```bash
LOADTEST_ALLOW_REMOTE=1 \
  locust -f loadtest/locustfile.py --host https://staging.example.com \
  --headless -u 100 -r 10 -t 60s
```

Open http://localhost:8089 for the web UI, choose users and spawn rate, start.

## What it exercises

| Task | Weight | Purpose |
|---|---|---|
| `GET /health` | 3 | process liveness under concurrency |
| `GET /health/ready` | 2 | DB round trip; 503 = scale/repair signal |
| `GET /metrics` | 1 | scrape path; 200/401/404 all legitimate |
| `GET /api/loadtest-probe` → 404 | 1 | SPA fallback must never swallow API paths |

## Extending safely

To exercise authenticated *read* traffic, add a task that logs in once per
user and replays the token against read-only endpoints. **Never** add a task
that mutates state or spends money. If a test needs booking/CRM/SMS behaviour,
point it at a sandbox tenant with mock providers (see
`docs/FAILURE-INJECTION.md` for deterministic degradation) — never at real
providers or real customer data.

```python
@task
def authenticated_read(self):
    r = self.client.post("/auth/login", json={"email": "...", "password": "..."})
    token = r.json().get("access_token")
    if token:
        self.client.get("/api/tenants", headers={"Authorization": f"Bearer {token}"})
```

## What to watch

* p50/p95 of `/health/ready` (DB round trip + `SELECT 1`).
* Error rate on `/api/*` — should be 401/403/404, never 500.
* Worker CPU/memory under load; connection-pool exhaustion
  (`pool_size`/`max_overflow`).
* Whether `/health/ready` starts returning 503 — that is the signal to scale,
  not a crash.

Latency budgets the API enforces are in `docs/PERFORMANCE.md`; this tool
measures whether they hold under load.
