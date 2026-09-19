"""
Locust load test for VoxDesk.

Run against a deployed API (local compose or staging):

    pip install locust
    locust -f loadtest/locustfile.py --host http://localhost:8000

Then open http://localhost:8089 and start a run. The default tasks hit the
unauthenticated liveness/readiness probes; a load test that only exercises
public endpoints proves the process does not fall over under concurrency, which
is the first thing to validate before adding authenticated traffic.

See docs/PERFORMANCE.md for the full runbook and latency budgets.
"""
from __future__ import annotations

from locust import HttpUser, between, task


class VoxDeskUser(HttpUser):
    wait_time = between(0.5, 2.0)

    @task(3)
    def liveness(self):
        # Process is up and answering.
        self.client.get("/health", name="health")

    @task(2)
    def readiness(self):
        # DB answers. A 503 here is the signal to scale/repair, not a crash.
        self.client.get("/health/ready", name="health/ready")

    @task(1)
    def unknown_api_returns_404(self):
        # SPA fallback must never swallow API paths -- cheap invariant check.
        with self.client.get("/api/loadtest-probe", catch_response=True) as resp:
            if resp.status_code != 404:
                resp.failure(f"expected 404, got {resp.status_code}")
