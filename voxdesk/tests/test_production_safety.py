"""
Step 9 Phase A — production safety hardening.

These tests pin the three fail-closed behaviours introduced in Phase A:

1. Production startup must NOT create tables with Base.metadata.create_all
   (Alembic is the sole schema owner); development/test may.
2. Twilio webhook verification is enabled unless the explicit dev-only flag
   TWILIO_SKIP_WEBHOOK_VERIFY is set, and production refuses that flag.
3. /docs, /redoc and /openapi.json are disabled in production and retained in
   development.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, settings
from app.main import app as voxdesk_app
from app.main import lifespan


# --------------------------------------------------------- create_all gate ---

class _RecordingEngine:
    """Fake engine that records whether a run_sync (create_all) happened."""

    def __init__(self):
        self.run_sync_calls: list = []

    def begin(self):
        return _RecordingConnection(self)

    async def dispose(self):
        pass


class _RecordingConnection:
    def __init__(self, engine):
        self.engine = engine

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def run_sync(self, fn, *args, **kwargs):
        self.engine.run_sync_calls.append(fn)


class _DummySession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _DummyMaker:
    def __call__(self):
        return _DummySession()


async def _fake_sync_seed_plans(session):
    return None


async def _fake_list_plans(session, active_only=True):
    return []


def _fake_configuration_problems(plans, is_production):
    return []


def _patch_lifespan_dependencies(monkeypatch, engine, app_env):
    """Redirect every external dependency the lifespan touches so it can run
    without a real database, then select the environment mode."""
    monkeypatch.setattr("app.main.get_engine", lambda: engine)
    monkeypatch.setattr("app.db.session.get_sessionmaker", lambda: _DummyMaker())
    monkeypatch.setattr("app.billing.plans.sync_seed_plans", _fake_sync_seed_plans)
    monkeypatch.setattr("app.billing.plans.list_plans", _fake_list_plans)
    monkeypatch.setattr(
        "app.billing.plans.configuration_problems", _fake_configuration_problems
    )
    monkeypatch.setattr(settings, "app_env", app_env)
    monkeypatch.setattr(Settings, "validate_security", lambda self: [])


@pytest.mark.asyncio
async def test_production_startup_does_not_create_tables(monkeypatch):
    """Alembic is the sole schema owner in production."""
    engine = _RecordingEngine()
    _patch_lifespan_dependencies(monkeypatch, engine, "production")

    async with lifespan(voxdesk_app):
        pass

    assert engine.run_sync_calls == []


@pytest.mark.asyncio
async def test_development_startup_creates_tables(monkeypatch):
    """Development/test still bootstrap schema so the app runs unmigrated."""
    engine = _RecordingEngine()
    _patch_lifespan_dependencies(monkeypatch, engine, "development")

    async with lifespan(voxdesk_app):
        pass

    assert len(engine.run_sync_calls) == 1


# ------------------------------------------------------------ docs exposure ---

def test_production_disables_api_docs(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    from app.main import _api_docs_config

    assert _api_docs_config() == {
        "docs_url": None,
        "redoc_url": None,
        "openapi_url": None,
    }


def test_development_keeps_api_docs(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")
    from app.main import _api_docs_config

    assert _api_docs_config() == {}


@pytest.mark.asyncio
async def test_production_docs_endpoints_return_404(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    from app.main import _api_docs_config

    prod_app = FastAPI(**_api_docs_config())
    async with AsyncClient(
        transport=ASGITransport(app=prod_app), base_url="http://test"
    ) as client:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/redoc")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404


@pytest.mark.asyncio
async def test_development_docs_endpoints_are_available(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")
    from app.main import _api_docs_config

    dev_app = FastAPI(**_api_docs_config())
    async with AsyncClient(
        transport=ASGITransport(app=dev_app), base_url="http://test"
    ) as client:
        assert (await client.get("/docs")).status_code == 200
        assert (await client.get("/openapi.json")).status_code == 200


# ------------------------------------------------------- Twilio fail-closed ---

class _FakeTwilioRequest:
    url = "https://example.com/telephony/voice"
    headers = {"X-Twilio-Signature": "obviously-wrong"}

    async def form(self):
        return {"From": "+15550001", "CallSid": "CA123"}


@pytest.mark.asyncio
async def test_development_with_bypass_disabled_requires_signature(monkeypatch):
    from app.telephony.stream_auth import verify_twilio_request

    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "twilio_skip_webhook_verify", False)
    assert await verify_twilio_request(_FakeTwilioRequest()) is False


@pytest.mark.asyncio
async def test_development_with_bypass_enabled_skips_signature(monkeypatch):
    from app.telephony.stream_auth import verify_twilio_request

    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "twilio_skip_webhook_verify", True)
    assert await verify_twilio_request(_FakeTwilioRequest()) is True


@pytest.mark.asyncio
async def test_production_with_bypass_disabled_requires_signature(monkeypatch):
    from app.telephony.stream_auth import verify_twilio_request

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "twilio_skip_webhook_verify", False)
    assert await verify_twilio_request(_FakeTwilioRequest()) is False


def test_production_with_bypass_enabled_is_rejected_by_security_validation():
    from app.core.config import Settings

    prod = Settings(app_env="production", twilio_skip_webhook_verify=True)
    problems = prod.validate_security()
    assert any("TWILIO_SKIP_WEBHOOK_VERIFY" in p for p in problems), problems


def test_bypass_flag_defaults_to_false():
    from app.core.config import Settings

    assert Settings().twilio_skip_webhook_verify is False
