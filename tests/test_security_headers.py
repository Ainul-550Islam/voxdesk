"""Step 9 — security headers on every HTTP response."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.security_headers import add_security_headers


@pytest.fixture
def app():
    app = FastAPI()
    add_security_headers(app)

    @app.get("/public")
    async def public():
        return {"ok": True}

    @app.get("/api/tenants")
    async def authed():
        return {"ok": True}

    return app


@pytest.mark.asyncio
async def test_baseline_headers_are_always_present(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/public")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    csp = r.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    # Scripts are locked to self; inline scripts are forbidden.
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]


@pytest.mark.asyncio
async def test_authenticated_routes_are_no_store(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/api/tenants")
    assert r.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_hsts_when_served_over_tls(app, monkeypatch):
    # HSTS is scheme-driven (Step 8): any environment served over https gets
    # it, so a TLS-terminated staging box is protected exactly like prod.
    monkeypatch.setattr(settings, "public_base_url", "https://app.example.com")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/public")
    assert "Strict-Transport-Security" in r.headers
    assert "max-age=31536000" in r.headers["Strict-Transport-Security"]


@pytest.mark.asyncio
async def test_no_hsts_over_plain_http(app, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/public")
    assert "Strict-Transport-Security" not in r.headers
