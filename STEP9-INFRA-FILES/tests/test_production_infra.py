"""
Step 9 — production infrastructure: readiness probe, observability wiring, and
serving of the built dashboard.

Deterministic by design: no real Postgres is required. Database reachability
is exercised by swapping the engine behind the readiness probe.
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.main import app as voxdesk_app


# ------------------------------------------------------------ readiness ---

@pytest.mark.asyncio
async def test_liveness_endpoint_returns_ok():
    async with AsyncClient(
        transport=ASGITransport(app=voxdesk_app), base_url="http://test"
    ) as ac:
        r = await ac.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_readiness_ok_when_database_is_reachable(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr("app.main.get_engine", lambda: eng)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=voxdesk_app), base_url="http://test"
        ) as ac:
            r = await ac.get("/health/ready")
        assert r.status_code == 200
        assert r.json() == {"status": "ready", "database": "ok"}
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_readiness_unavailable_when_database_is_down(monkeypatch):
    eng = create_async_engine(
        "postgresql+asyncpg://x:x@127.0.0.1:1/x", connect_args={"timeout": 1}
    )
    monkeypatch.setattr("app.main.get_engine", lambda: eng)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=voxdesk_app), base_url="http://test"
        ) as ac:
            r = await ac.get("/health/ready")
        assert r.status_code == 503
        assert r.json()["database"] == "unreachable"
    finally:
        await eng.dispose()


# ------------------------------------------------------- observability wiring ---

def test_sentry_dsn_defaults_to_empty():
    assert Settings().sentry_dsn == ""


def test_logging_defaults():
    assert Settings().log_level == "INFO"
    assert Settings().log_format == "console"


def test_renderer_selection():
    from app.core.logging import _choose_renderer

    import structlog

    assert isinstance(_choose_renderer("console", True), structlog.processors.JSONRenderer)
    assert isinstance(_choose_renderer("json", False), structlog.processors.JSONRenderer)
    assert isinstance(_choose_renderer("console", False), structlog.dev.ConsoleRenderer)


def test_sentry_init_is_guarded_by_dsn():
    """Structural guarantee: Sentry only initialises when SENTRY_DSN is set."""
    source = pathlib.Path("app/main.py").read_text()
    assert "if settings.sentry_dsn:" in source
    assert "sentry_sdk.init(" in source


# ------------------------------------------------------------ dashboard ---

def test_dashboard_mount_is_gated_on_dist_presence():
    """A fresh checkout (no dist) stays API-only; the image (with dist) serves."""
    source = pathlib.Path("app/main.py").read_text()
    assert "_mount_dashboard_if_built" in source
    assert "_mount_dashboard_if_built(app)" in source


def test_dashboard_helper_does_nothing_without_dist(tmp_path):
    from app.main import _mount_dashboard_if_built

    probe = FastAPI()
    missing = tmp_path / "no-dist-here"
    _mount_dashboard_if_built(probe, dist_dir=str(missing))
    assert not any(r.path.startswith("/assets") for r in probe.routes)


@pytest.mark.asyncio
async def test_dashboard_spa_is_served_when_dist_exists(tmp_path):
    from app.main import _mount_dashboard_if_built

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>voxdesk-shell</html>")
    (dist / "assets" / "app.js").write_text("// app")

    probe = FastAPI()
    _mount_dashboard_if_built(probe, dist_dir=str(dist))

    async with AsyncClient(transport=ASGITransport(app=probe), base_url="http://test") as ac:
        root = await ac.get("/")
        assert root.status_code == 200
        assert "voxdesk-shell" in root.text

        # Client-side routes fall back to the SPA shell, not a 404.
        deep = await ac.get("/agent")
        assert deep.status_code == 200
        assert "voxdesk-shell" in deep.text

        # Real static assets are served.
        asset = await ac.get("/assets/app.js")
        assert asset.status_code == 200

        # Unknown API paths 404 instead of being swallowed by the SPA shell.
        bad_api = await ac.get("/api/definitely-not-real")
        assert bad_api.status_code == 404
