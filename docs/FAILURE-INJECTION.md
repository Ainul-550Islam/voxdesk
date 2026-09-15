# VoxDesk — Failure injection (Step 7)

Failure injection exists to prove that the SLO dashboards, alerts and incident
runbooks actually fire — *before* a real provider outage. It is deterministic
by construction and hard-disabled in production.

---

## The contract

1. **No randomness.** A rule is an exact path match with a fixed effect. The
   same request always gets the same treatment. There is no probability knob.
2. **Production-disabled, twice.** `add_chaos_middleware` is a no-op unless
   `CHAOS_ENABLED=true`, and `validate_security()` refuses to boot production
   with it enabled. The middleware also checks `is_production` itself.
3. **No secrets, no side effects.** Injected responses are the fixed body
   `{"detail": "injected failure"}`; injected latency is a fixed
   `asyncio.sleep`. Injected traffic is still counted by the metrics
   middleware, which is the point (the drill must show up on the dashboard).

Rules live in `CHAOS_RULES` — a JSON array:

```bash
CHAOS_ENABLED=true
CHAOS_RULES='[
  {"path": "/api/analytics", "effect": "error", "value": 503},
  {"path": "/api/tenants", "effect": "latency_ms", "value": 250}
]'
```

| Effect | `value` | Behaviour |
|---|---|---|
| `error` | HTTP status | the matching path returns that status with a fixed body |
| `latency_ms` | milliseconds | the matching path sleeps that long, then proceeds |

Anything malformed (unknown effect, bad JSON, wrong types) is ignored, never
guessed. Paths are **exact** — `/api/tenants` matches only itself, not
`/api/tenants/1`.

---

## Running a drill

```bash
# a non-production environment only:
CHAOS_ENABLED=true \
CHAOS_RULES='[{"path": "/api/analytics", "effect": "error", "value": 503}]' \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then:

1. Watch `voxdesk_http_requests_total{status="503"}` climb and
   `voxdesk:slo:availability:5m` drop.
2. Confirm `VoxDeskHighErrorRate` enters Pending → Firing on the alert page.
3. Follow `docs/SLO-ALERTS.md` runbooks against the *drill*, not a real outage.
4. Turn it off and confirm the dashboard recovers.

Never point a drill at a real tenant's traffic path in a way that reaches
customers; a drill target should be a sandbox deployment.

---

## Why deterministic, not chaotic

Random failure injection (e.g. "5% of requests fail") is how you get flaky
tests, unreproducible incidents and a support queue of "it worked when I tried
it". Fixed rules make every drill reproducible and every assertion
deterministic — the same reason the test suite can cover the injection logic
exactly (`tests/test_chaos.py`).
