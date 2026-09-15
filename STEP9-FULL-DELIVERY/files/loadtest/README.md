# Load testing VoxDesk

Locust-based load tests. Run against a deployed API:

```bash
pip install locust
# local compose:
locust -f loadtest/locustfile.py --host http://localhost:8000
# staging (replace with your host):
locust -f loadtest/locustfile.py --host https://staging.example.com
```

Then open the web UI (default http://localhost:8089), choose the number of
users and spawn rate, and start.

Headless (CI or quick run):

```bash
locust -f loadtest/locustfile.py --host http://localhost:8000 \
  --headless -u 100 -r 10 -t 60s
```

## What it exercises

| Task | Weight | Purpose |
|---|---|---|
| `GET /health` | 3 | Process liveness under concurrency |
| `GET /health/ready` | 2 | DB round trip; 503 = scale/repair signal |
| `GET /api/loadtest-probe` → 404 | 1 | SPA fallback must never swallow API paths |

## Extending (authenticated traffic)

Add a task that logs in once per user and reuses the access token:

```python
@task
def authenticated_read(self):
    r = self.client.post("/auth/login", json={"email": "...", "password": "..."})
    token = r.json().get("access_token")
    if token:
        self.client.get("/api/tenants", headers={"Authorization": f"Bearer {token}"})
```

See docs/PERFORMANCE.md for the latency budgets these runs should validate.
