"""
Locust load test for VoxDesk — safe by default.

Run against a deployed API (local compose or staging):

    pip install locust
    locust -f loadtest/locustfile.py --host http://localhost:8000

Then open http://localhost:8089 and start a run.

Safety contract (see docs/LOAD-TESTING.md for the full rules):

* The default tasks only touch **unauthenticated, read-only** endpoints
  (liveness, readiness, the API-404 invariant, the metrics scrape). No task
  here logs in, places a call, charges Stripe, books an appointment, sends an
  SMS, or writes a CRM contact — and no provider is ever contacted.
* A **remote host is refused** unless the operator sets
  ``LOADTEST_ALLOW_REMOTE=1``. localhost / 127.0.0.1 / [::1] are always
  allowed, because a loopback target cannot be a production deployment by
  accident.
* The refusal is a hard stop per simulated user (``StopUser``), not a warning:
  a mis-aimed ``--host`` must not turn into a production load test.
"""
from __future__ import annotations

from locust import HttpUser, between, task
from locust.exception import StopUser

# The safety guard is a pure module so it can be unit-tested without Locust.
# Locust runs this file with its own directory on sys.path; tests import it
# as the `loadtest` package. Either way the guard is the same code.
try:
    from .safety import validate_target
except ImportError:  # pragma: no cover - locust script path, not the package
    from safety import validate_target


class VoxDeskUser(HttpUser):
    wait_time = between(0.5, 2.0)

    def on_start(self):
        reason = validate_target(self.host)
        if reason:
            # Hard stop: a mis-aimed host must not become a production test.
            raise StopUser(reason)

    @task(3)
    def liveness(self):
        # Process is up and answering.
        self.client.get("/health", name="health")

    @task(2)
    def readiness(self):
        # DB answers. A 503 here is the signal to scale/repair, not a crash.
        self.client.get("/health/ready", name="health/ready")

    @task(1)
    def metrics_scrape(self):
        # Read-only; 200 (enabled), 401 (token-gated) and 404 (disabled) are
        # all legitimate and must never be a load-test failure.
        with self.client.get("/metrics", name="metrics", catch_response=True) as resp:
            if resp.status_code not in (200, 401, 404):
                resp.failure(f"unexpected metrics status {resp.status_code}")

    @task(1)
    def unknown_api_returns_404(self):
        # SPA fallback must never swallow API paths -- cheap invariant check.
        with self.client.get("/api/loadtest-probe", catch_response=True) as resp:
            if resp.status_code != 404:
                resp.failure(f"expected 404, got {resp.status_code}")
