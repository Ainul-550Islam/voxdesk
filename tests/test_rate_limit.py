"""Step 9 — rate limiting: in-process backend, middleware, fail-closed."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core import rate_limit
from app.core.rate_limit import _SlidingWindow, add_rate_limit_middleware, client_ip


def test_sliding_window_allows_up_to_limit():
    w = _SlidingWindow()
    for i in range(5):
        assert w.allow("k", limit=5, window=60, now=float(i)) is True
    assert w.allow("k", limit=5, window=60, now=5.0) is False


def test_sliding_window_slides():
    w = _SlidingWindow()
    assert w.allow("k", limit=1, window=10, now=0.0) is True
    assert w.allow("k", limit=1, window=10, now=5.0) is False
    assert w.allow("k", limit=1, window=10, now=11.0) is True


@pytest.mark.asyncio
async def test_middleware_rejects_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_burst", 2)
    monkeypatch.setattr(settings, "rate_limit_login_per_minute", 2)
    monkeypatch.setattr(settings, "redis_url", "")
    # The limiter is a process-global singleton; reset it so this test gets a
    # fresh in-process backend regardless of what earlier tests initialised.
    monkeypatch.setattr(rate_limit, "_limiter", None)

    app = FastAPI()
    add_rate_limit_middleware(app)

    @app.get("/api/tenants")
    async def tenants():
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r1 = await ac.get("/api/tenants")
        r2 = await ac.get("/api/tenants")
        r3 = await ac.get("/api/tenants")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert r3.json()["error"]["code"] == "rate_limited"
    assert r3.headers.get("Retry-After")


@pytest.mark.asyncio
async def test_middleware_passes_through_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "rate_limit_burst", 1)

    app = FastAPI()
    add_rate_limit_middleware(app)

    @app.get("/api/tenants")
    async def tenants():
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        for _ in range(5):
            assert (await ac.get("/api/tenants")).status_code == 200


def test_client_ip_uses_forwarded_header():
    from starlette.datastructures import Headers

    class FakeRequest:
        headers = Headers({"x-forwarded-for": "1.2.3.4, 10.0.0.1"})
        client = None

    assert client_ip(FakeRequest()) == "1.2.3.4"
