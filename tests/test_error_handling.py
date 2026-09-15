"""Step 9 — global error handling and request correlation."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.errors import (
    ConflictError,
    NotFoundError,
    RateLimitedError,
    install_error_handling,
)


@pytest.fixture
def app():
    app = FastAPI()
    install_error_handling(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("database password is hunter2")

    @app.get("/missing")
    async def missing():
        raise NotFoundError("That call does not exist")

    @app.get("/limited")
    async def limited():
        raise RateLimitedError(retry_after=45)

    @app.get("/clash")
    async def clash():
        raise ConflictError("Lead exists", detail={"phone": "+1555"})

    return app


def _client(app):
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_unhandled_exception_is_sanitised_in_production(app, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    async with _client(app) as ac:
        r = await ac.get("/boom")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "internal_error"
    assert "hunter2" not in r.text and "RuntimeError" not in r.text
    assert body["error"]["request_id"]
    assert r.headers.get("X-Request-ID") == body["error"]["request_id"]


@pytest.mark.asyncio
async def test_unhandled_exception_keeps_repr_in_dev(app, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")
    async with _client(app) as ac:
        r = await ac.get("/boom")
    assert r.status_code == 500
    assert "RuntimeError" in r.text


@pytest.mark.asyncio
async def test_domain_errors_have_structured_bodies(app):
    async with _client(app) as ac:
        assert (await ac.get("/missing")).status_code == 404
        assert (await ac.get("/missing")).json()["error"]["code"] == "not_found"

        r = await ac.get("/limited")
        assert r.status_code == 429
        assert r.headers.get("Retry-After") == "45"

        r = await ac.get("/clash")
        assert r.status_code == 409
        assert r.json()["error"]["detail"]["phone"] == "+1555"


@pytest.mark.asyncio
async def test_framework_404_keeps_fastapi_shape(app):
    async with _client(app) as ac:
        r = await ac.get("/does-not-exist")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}
    assert r.headers.get("X-Request-ID")


@pytest.mark.asyncio
async def test_request_id_is_honoured_and_sanitised(app):
    async with _client(app) as ac:
        r = await ac.get("/missing", headers={"X-Request-ID": "client-abc-9"})
        assert r.headers.get("X-Request-ID") == "client-abc-9"

        r = await ac.get("/missing", headers={"X-Request-ID": "evil\nvalue"})
        assert r.headers.get("X-Request-ID") != "evil\nvalue"
