"""Step 7 — deterministic failure injection (app/core/chaos.py).

The contract under test: no randomness, exact-path matching, no-op in
production, and no-op unless CHAOS_ENABLED. Injected bodies are secret-free.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import chaos
from app.core.config import settings


def test_chaos_middleware_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "chaos_enabled", False)
    app = FastAPI()
    chaos.add_chaos_middleware(app)
    assert len(app.user_middleware) == 0


def test_chaos_middleware_is_noop_in_production(monkeypatch):
    monkeypatch.setattr(settings, "chaos_enabled", True)
    monkeypatch.setattr(settings, "app_env", "production")
    app = FastAPI()
    chaos.add_chaos_middleware(app)
    assert len(app.user_middleware) == 0


def test_matching_is_exact_path_only(monkeypatch):
    monkeypatch.setattr(
        settings, "chaos_rules_json",
        json.dumps([{"path": "/api/tenants", "effect": "error", "value": 500}]),
    )
    assert chaos._matching_rule("/api/tenants")["effect"] == "error"
    assert chaos._matching_rule("/api/tenants/") is None
    assert chaos._matching_rule("/api/tenants/1") is None
    assert chaos._matching_rule("/api/other") is None


@pytest.mark.asyncio
async def test_chaos_injects_error_on_matching_path(monkeypatch):
    monkeypatch.setattr(settings, "chaos_enabled", True)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(
        settings, "chaos_rules_json",
        json.dumps([{"path": "/api/tenants", "effect": "error", "value": 503}]),
    )
    app = FastAPI()

    @app.get("/api/tenants")
    async def tenants():
        return {"ok": True}

    @app.get("/api/untouched")
    async def untouched():
        return {"ok": True}

    chaos.add_chaos_middleware(app)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        injected = await ac.get("/api/tenants")
        assert injected.status_code == 503
        assert injected.json() == {"detail": "injected failure"}
        clean = await ac.get("/api/untouched")
        assert clean.status_code == 200


@pytest.mark.asyncio
async def test_chaos_injects_fixed_latency(monkeypatch):
    monkeypatch.setattr(settings, "chaos_enabled", True)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(
        settings, "chaos_rules_json",
        json.dumps([{"path": "/api/slow", "effect": "latency_ms", "value": 200}]),
    )
    app = FastAPI()

    @app.get("/api/slow")
    async def slow():
        return {"ok": True}

    chaos.add_chaos_middleware(app)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        started = time.perf_counter()
        r = await ac.get("/api/slow")
        elapsed = time.perf_counter() - started
    assert r.status_code == 200
    assert elapsed >= 0.15


@pytest.mark.asyncio
async def test_chaos_ignores_unknown_effect(monkeypatch):
    monkeypatch.setattr(settings, "chaos_enabled", True)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(
        settings, "chaos_rules_json",
        json.dumps([{"path": "/api/tenants", "effect": "explode", "value": 1}]),
    )
    app = FastAPI()

    @app.get("/api/tenants")
    async def tenants():
        return {"ok": True}

    chaos.add_chaos_middleware(app)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        r = await ac.get("/api/tenants")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_chaos_malformed_rules_are_inert(monkeypatch):
    monkeypatch.setattr(settings, "chaos_enabled", True)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "chaos_rules_json", "not json at all")
    assert settings.chaos_rules == []
    app = FastAPI()

    @app.get("/api/tenants")
    async def tenants():
        return {"ok": True}

    chaos.add_chaos_middleware(app)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        r = await ac.get("/api/tenants")
    assert r.status_code == 200
