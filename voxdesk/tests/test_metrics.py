"""Step 9 — Prometheus metrics endpoint and middleware."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import metrics
from app.core.config import settings


@pytest.mark.asyncio
async def test_metrics_endpoint_is_disabled_by_default(client):
    r = await client.get("/metrics")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_metrics_requires_the_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", True)
    monkeypatch.setattr(settings, "metrics_token", "secret-token")

    assert (await client.get("/metrics")).status_code == 401
    assert (await client.get("/metrics?token=wrong")).status_code == 401

    bearer = await client.get(
        "/metrics", headers={"Authorization": "Bearer secret-token"}
    )
    assert bearer.status_code == 200
    assert "voxdesk_http_requests_total" in bearer.text

    query = await client.get("/metrics?token=secret-token")
    assert query.status_code == 200


@pytest.mark.asyncio
async def test_metrics_is_open_when_no_token_is_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", True)
    monkeypatch.setattr(settings, "metrics_token", "")
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "voxdesk_db_up" in r.text


@pytest.mark.asyncio
async def test_middleware_counts_requests(monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", True)
    app = FastAPI()
    metrics.add_metrics_middleware(app)

    @app.get("/api/tenants")
    async def tenants():
        return {"ok": True}

    child = metrics.REQUESTS.labels("GET", "/api/tenants", "200")
    before = child._value.get()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        assert (await ac.get("/api/tenants")).status_code == 200
    assert child._value.get() - before >= 1.0


def test_set_db_up_is_reflected_in_the_scrape_output():
    metrics.set_db_up(True)
    assert "voxdesk_db_up 1.0" in metrics.generate_latest().decode()
    metrics.set_db_up(False)
    assert "voxdesk_db_up 0.0" in metrics.generate_latest().decode()
