"""Load-test safety guards — pure functions, no Locust import.

Kept separate from ``locustfile.py`` so the safety contract is unit-testable
without installing Locust, and so the guard logic cannot be entangled with
task code. The rules are documented in docs/LOAD-TESTING.md:

* Loopback targets are always allowed.
* Any remote target requires the explicit ``LOADTEST_ALLOW_REMOTE=1``
  opt-in, because a load test mis-aimed at a production host is a production
  outage, not a test.
"""
from __future__ import annotations

import os

#: Hosts we always allow: loopback only. Everything else is "remote".
_LOOPBACK_PREFIXES = (
    "http://localhost",
    "http://127.0.0.1",
    "http://[::1]",
    "https://localhost",
    "https://127.0.0.1",
    "https://[::1]",
)

_TRUE_VALUES = {"1", "true", "yes", "on"}


def allow_remote() -> bool:
    """Whether the operator explicitly allowed a non-loopback target."""
    return os.environ.get("LOADTEST_ALLOW_REMOTE", "").strip().lower() in _TRUE_VALUES


def validate_target(host: str | None) -> str | None:
    """Return a human-readable reason the target must NOT be load-tested, or
    ``None`` when the target is acceptable. Deterministic and side-effect
    free."""
    if not host:
        return "no --host given"
    if allow_remote():
        return None
    if host.startswith(_LOOPBACK_PREFIXES):
        return None
    return (
        "refusing to load-test a remote host without LOADTEST_ALLOW_REMOTE=1 "
        f"(got {host!r}); see docs/LOAD-TESTING.md"
    )
