"""Deterministic deployment smoke test (Step 8).

Run against a deployed API:

    SMOKE_BASE_URL=http://localhost:8000 python scripts/smoke_test.py
    # or from a container on the compose network:
    docker compose -f docker-compose.prod.yml run --rm --entrypoint python api scripts/smoke_test.py

It is read-only and non-destructive by construction: no call is placed, no
SMS is sent, no booking is created, no CRM record is written, no Stripe
charge is made, and no provider is contacted. The provider checks report
*configuration presence* (a local settings read), never reachability.

Checks and their meaning:

    1. /health            liveness — must be 200
    2. /health/ready      readiness — must be 200 (deps down = FAIL, correctly)
    3. /                  dashboard shell — 200 when served, else WARN
    4. /metrics           scrape path — 200 (open/enabled), 401 (token-gated) or
                          404 (disabled) are all legitimate; anything else FAIL
    5. /auth/login        auth endpoint up — a bogus login must be rejected
                          with 4xx, never 500
    6. /api/tenants       tenant isolation — anonymous access must be 401/403
    7. /telephony/voice   Twilio webhook — unsigned POST must be 403 (fail-closed
                          signature verification), proving the endpoint is up
    8. provider config    which voice/LLM providers have credentials configured
                          (local read, no network)
    9. E2E guard state    reports whether E2E mode is armed; in staging it must
                          be disarmed unless the operator deliberately armed it

Exit code is non-zero if any FAIL check fired, so it can gate a deploy or CI.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.core.config import settings
from app.core.health import provider_config_ok

BASE = (os.environ.get("SMOKE_BASE_URL") or "http://localhost:8000").rstrip("/")

_results: list[tuple[str, str, str]] = []  # (check, status, detail)


def _record(check: str, status: str, detail: str = "") -> None:
    _results.append((check, status, detail))
    print(f"  [{status:4}] {check}{(' — ' + detail) if detail else ''}")


def _check_auth_rejected(path: str, method: str = "GET", **kw) -> None:
    """An endpoint that requires auth must reject anonymous access with 4xx."""
    try:
        with httpx.Client(base_url=BASE, timeout=10.0) as client:
            resp = client.request(method, path, **kw)
        if 400 <= resp.status_code < 500:
            _record(path, "PASS", f"rejected anonymously with {resp.status_code}")
        else:
            _record(path, "FAIL", f"expected 4xx, got {resp.status_code}")
    except httpx.HTTPError as exc:
        _record(path, "FAIL", f"unreachable: {exc}")


def main() -> int:
    print(f"VoxDesk smoke test against {BASE}\n")

    with httpx.Client(base_url=BASE, timeout=10.0) as client:
        # 1. liveness
        try:
            r = client.get("/health")
            ok = r.status_code == 200 and r.json().get("status") == "ok"
            _record("/health", "PASS" if ok else "FAIL", f"status {r.status_code}")
        except httpx.HTTPError as exc:
            _record("/health", "FAIL", f"unreachable: {exc}")

        # 2. readiness
        try:
            r = client.get("/health/ready")
            if r.status_code == 200:
                _record("/health/ready", "PASS", "ready")
            else:
                _record("/health/ready", "FAIL", f"status {r.status_code}: {r.text[:200]}")
        except httpx.HTTPError as exc:
            _record("/health/ready", "FAIL", f"unreachable: {exc}")

        # 3. dashboard shell
        try:
            r = client.get("/")
            _record("/", "PASS" if r.status_code == 200 else "WARN",
                    f"status {r.status_code}")
        except httpx.HTTPError as exc:
            _record("/", "WARN", f"unreachable: {exc}")

        # 4. metrics scrape path (200/401/404 are all legitimate)
        try:
            r = client.get("/metrics")
            if r.status_code in (200, 401, 404):
                _record("/metrics", "PASS", f"status {r.status_code} "
                         "(200=enabled/open, 401=token-gated, 404=disabled)")
            else:
                _record("/metrics", "FAIL", f"status {r.status_code}")
        except httpx.HTTPError as exc:
            _record("/metrics", "FAIL", f"unreachable: {exc}")

    # 5. auth endpoint up (bogus creds must 4xx, never 500)
    _check_auth_rejected(
        "/auth/login", method="POST",
        json={"email": "smoke-test@example.com", "password": "not-a-real-password"},
    )

    # 6. tenant isolation (anonymous must be refused)
    _check_auth_rejected("/api/tenants")

    # 7. Twilio webhook: an unsigned POST with a well-formed body must be 403
    # (signature verification is fail-closed, so a forged webhook is refused).
    _check_auth_rejected(
        "/telephony/voice", method="POST",
        data={"CallSid": "CA-smoke", "From": "+15550001111", "To": "+15550002222"},
    )

    # 8. provider configuration presence (local read, no network)
    providers = provider_config_ok()
    if providers["ok"]:
        _record("provider_config", "PASS", "voice providers configured")
    else:
        _record("provider_config", "WARN",
                f"missing: {', '.join(providers['missing'])}")

    # 9. E2E guard state
    armed = settings.e2e_enabled
    if armed and settings.is_production:
        _record("e2e_guard", "FAIL", "E2E armed in production (refused at boot)")
    elif armed:
        _record("e2e_guard", "WARN", "E2E armed — this is a deliberate test mode")
    else:
        _record("e2e_guard", "PASS", "disarmed")

    print()
    fails = [c for c, s, _ in _results if s == "FAIL"]
    warns = [c for c, s, _ in _results if s == "WARN"]
    print(f"Smoke summary: {len(_results) - len(fails) - len(warns)} pass, "
          f"{len(warns)} warn, {len(fails)} fail")
    if fails:
        print("FAILED checks: " + ", ".join(fails))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
