# Load testing VoxDesk

Locust-based load tests. **Safe by default**: read-only tasks, loopback-only
targets unless explicitly opted in. The full safety contract and runbook is
`docs/LOAD-TESTING.md` — read it before extending these files.

```bash
pip install locust
# local / sandbox (always allowed):
locust -f loadtest/locustfile.py --host http://localhost:8000
# any remote target requires an explicit opt-in:
LOADTEST_ALLOW_REMOTE=1 \
  locust -f loadtest/locustfile.py --host https://staging.example.com
```

Then open the web UI (default http://localhost:8089), choose the number of
users and spawn rate, and start.

Headless (CI or quick run):

```bash
LOADTEST_ALLOW_REMOTE=1 \
  locust -f loadtest/locustfile.py --host https://staging.example.com \
  --headless -u 100 -r 10 -t 60s
```

## What it exercises

| Task | Weight | Purpose |
|---|---|---|
| `GET /health` | 3 | Process liveness under concurrency |
| `GET /health/ready` | 2 | DB round trip; 503 = scale/repair signal |
| `GET /metrics` | 1 | Scrape path; 200/401/404 all legitimate |
| `GET /api/loadtest-probe` → 404 | 1 | SPA fallback must never swallow API paths |

## The safety guard

`loadtest/safety.py` (unit-tested) refuses any non-loopback `--host` unless
`LOADTEST_ALLOW_REMOTE=1`. Each simulated user raises `StopUser` with the
reason, so a mis-aimed host stops the test instead of becoming a production
load test. The guard is the contract; the default tasks never log in, call a
provider, charge Stripe, book an appointment, send an SMS or write a CRM
contact.

## Extending (authenticated read-only traffic)

Add a task that logs in once per user and reuses the access token against
read endpoints. Never add a mutating or money-spending task; point any
behavioural test at a sandbox tenant with mock providers instead.

```python
@task
def authenticated_read(self):
    r = self.client.post("/auth/login", json={"email": "...", "password": "..."})
    token = r.json().get("access_token")
    if token:
        self.client.get("/api/tenants", headers={"Authorization": f"Bearer {token}"})
```

See `docs/PERFORMANCE.md` for the latency budgets these runs should validate,
and `docs/FAILURE-INJECTION.md` for deterministic degradation during a drill.
