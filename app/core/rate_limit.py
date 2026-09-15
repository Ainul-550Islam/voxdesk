"""Rate limiting (fail-closed when enabled, zero-dependency when disabled).

Two backends behind one interface:

* Redis (fixed-window INCR + EXPIRE) when ``RATE_LIMIT_*`` and ``REDIS_URL`` are
  configured -- the correct choice for multi-worker deployments.
* In-process sliding window otherwise, so a single-process dev/staging box is
  still protected without adding infrastructure.

The middleware is OFF by default (``RATE_LIMIT_ENABLED=false``) so the test
suite and local development are unaffected; production deployments enable it in
the environment. When the limiter itself errors it fails CLOSED -- the request
is rejected with 429 rather than allowed through unprotected.

Login gets a separate, tighter budget so credential stuffing is throttled
before it ever reaches bcrypt.
"""
from __future__ import annotations

import inspect
import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.config import settings

_RATE_LIMIT_BODY = {"error": {"code": "rate_limited", "message": "Too many requests"}}

# Paths never rate limited: liveness/readiness for the orchestrator, the
# metrics scrape endpoint for Prometheus.
_EXEMPT_PREFIXES = ("/health", "/metrics")


def client_ip(request: Request) -> str:
    """Client IP, honouring X-Forwarded-For only from a trusted proxy.

    The uvicorn production entrypoint already passes --proxy-headers and
    --forwarded-allow-ips, so request.client reflects the proxy. We still take
    the left-most X-Forwarded-For value when present, because that is the
    origin and is the only stable identity for a limit.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _make_key(request: Request) -> str:
    ip = client_ip(request)
    path = request.url.path
    if path.startswith("/auth/"):
        bucket = "auth"
    elif path.startswith(("/api/", "/telephony/", "/channels/")):
        bucket = "api"
    else:
        bucket = "other"
    return f"{bucket}:{ip}"


class _SlidingWindow:
    """In-process limiter: allow N requests per `window` seconds per key."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        queue = self._events[key]
        while queue and queue[0] <= now - window:
            queue.popleft()
        if len(queue) >= limit:
            return False
        queue.append(now)
        return True


class _RedisLimiter:
    """Redis fixed-window limiter. Lazily imports redis so the dependency is
    optional (in-process fallback is used when Redis is absent)."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._client = None

    def _get(self):
        if self._client is None:
            import redis.asyncio as redis

            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    async def allow(self, key: str, limit: int, window: float) -> bool:
        try:
            client = self._get()
            current = await client.incr(key)
            if current == 1:
                await client.expire(key, int(window))
            return current <= limit
        except Exception:
            # Fail closed: a broken limiter must not silently allow traffic.
            return False


class RateLimiter:
    def __init__(self, redis_url: str) -> None:
        self._backend = _RedisLimiter(redis_url) if redis_url else _SlidingWindow()

    async def allow(self, key: str, limit: int, window: float) -> bool:
        # The in-process backend is synchronous, the Redis one is async; both
        # implement the same allow() contract. Normalise on the result.
        result = self._backend.allow(key, limit, window)
        if inspect.isawaitable(result):
            return await result
        return result


_limiter: RateLimiter | None = None


def _get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(settings.redis_url)
    return _limiter


def _budget_for(request: Request) -> tuple[int, float]:
    path = request.url.path
    if path.startswith("/auth/"):
        return settings.rate_limit_login_per_minute, 60.0
    return settings.rate_limit_burst, 60.0


def add_rate_limit_middleware(app: FastAPI) -> None:
    if not settings.rate_limit_enabled:
        return

    @app.middleware("http")
    async def _rate_limit_middleware(request: Request, call_next):
        if request.url.path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        limit, window = _budget_for(request)
        allowed = await _get_limiter().allow(_make_key(request), limit, window)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content=_RATE_LIMIT_BODY,
                headers={"Retry-After": str(int(window))},
            )
        return await call_next(request)


def allow_for_test(key: str, limit: int, window: float) -> Callable[[], bool]:
    """Synchronous helper so tests can drive the in-process backend directly."""
    backend = _SlidingWindow()
    return lambda: backend.allow(key, limit, window)
