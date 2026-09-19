"""Deterministic failure injection (Step 7 observability).

Purpose: prove that SLO dashboards, alerts and incident runbooks actually
fire, by *deterministically* degrading specific endpoints during a drill or a
load test. This is the opposite of random chaos:

* **No randomness.** A rule is an exact path match with a fixed effect. The
  same request always gets the same treatment; there is no probability knob.
* **Production-disabled.** ``add_chaos_middleware`` is a no-op unless
  ``CHAOS_ENABLED=true``, and ``validate_security()`` refuses to boot
  production with it enabled (belt and braces: the middleware also checks
  ``is_production`` itself).
* **No secrets, no side effects.** Injected responses are a fixed
  ``{"detail": "injected failure"}`` body; latency is a fixed ``asyncio.sleep``.

Rules live in ``CHAOS_RULES`` as a JSON array of ``{"path", "effect",
"value"}`` objects. ``effect`` is ``"latency_ms"`` (``value`` = milliseconds)
or ``"error"`` (``value`` = HTTP status). Anything malformed is ignored.
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.config import settings

#: The only effects understood. Anything else is ignored, never guessed.
_EFFECTS = frozenset({"latency_ms", "error"})


def _matching_rule(path: str) -> dict | None:
    for rule in settings.chaos_rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("effect") not in _EFFECTS:
            continue
        if rule.get("path") == path:
            return rule
    return None


async def _apply(request: Request, call_next, rule: dict):
    effect = rule.get("effect")
    if effect == "latency_ms":
        try:
            ms = max(0.0, float(rule.get("value", 0)))
        except (TypeError, ValueError):
            ms = 0.0
        if ms:
            await asyncio.sleep(ms / 1000.0)
        return await call_next(request)
    if effect == "error":
        try:
            status = int(rule.get("value", 503))
        except (TypeError, ValueError):
            status = 503
        return JSONResponse(
            status_code=status, content={"detail": "injected failure"}
        )
    return await call_next(request)


def add_chaos_middleware(app: FastAPI) -> None:
    """Attach the injection middleware. A no-op outside test environments."""
    if not settings.chaos_enabled or settings.is_production:
        return

    @app.middleware("http")
    async def _chaos_middleware(request: Request, call_next):
        rule = _matching_rule(request.url.path)
        if rule is None:
            return await call_next(request)
        return await _apply(request, call_next, rule)
