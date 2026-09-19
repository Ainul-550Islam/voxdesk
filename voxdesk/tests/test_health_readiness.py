"""Step 7 — liveness vs readiness semantics (app/core/health.py)."""
from __future__ import annotations

import pytest

from app.core import health
from app.core.config import settings


@pytest.mark.asyncio
async def test_readiness_ready_when_dependencies_ok(monkeypatch):
    async def _db_ok():
        return True

    async def _redis_none():
        return None

    monkeypatch.setattr(health, "check_database", _db_ok)
    monkeypatch.setattr(health, "check_redis", _redis_none)

    result = await health.readiness()
    assert result["ready"] is True
    body = result["body"]
    assert body["status"] == "ready"
    assert body["checks"]["database"]["ok"] is True
    # Redis not configured -> reported as configured=False, never a failure.
    assert body["checks"]["redis"] == {"ok": True, "configured": False}


@pytest.mark.asyncio
async def test_readiness_fails_when_database_is_down(monkeypatch):
    async def _db_down():
        return False

    async def _redis_none():
        return None

    monkeypatch.setattr(health, "check_database", _db_down)
    monkeypatch.setattr(health, "check_redis", _redis_none)

    result = await health.readiness()
    assert result["ready"] is False
    assert result["body"]["status"] == "unavailable"
    assert result["body"]["checks"]["database"]["ok"] is False


@pytest.mark.asyncio
async def test_readiness_fails_when_redis_is_configured_and_down(monkeypatch):
    async def _db_ok():
        return True

    async def _redis_down():
        return False

    monkeypatch.setattr(health, "check_database", _db_ok)
    monkeypatch.setattr(health, "check_redis", _redis_down)

    result = await health.readiness()
    assert result["ready"] is False
    assert result["body"]["checks"]["redis"]["ok"] is False


@pytest.mark.asyncio
async def test_check_redis_returns_none_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "")
    assert await health.check_redis() is None


def test_provider_config_reports_missing_voice_providers(monkeypatch):
    monkeypatch.setattr(settings, "deepgram_api_key", "")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    for provider in ("openai", "anthropic", "google"):
        monkeypatch.setattr(settings, f"{provider}_api_key", "")
    cfg = health.provider_config_ok()
    assert cfg["ok"] is False
    assert set(cfg["missing"]) == {"deepgram", "elevenlabs", "llm"}


def test_provider_config_ok_when_all_keys_present(monkeypatch):
    monkeypatch.setattr(settings, "deepgram_api_key", "dg-key")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "el-key")
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    cfg = health.provider_config_ok()
    assert cfg["ok"] is True
    assert cfg["missing"] == []


@pytest.mark.asyncio
async def test_readiness_requires_providers_in_production(monkeypatch):
    async def _db_ok():
        return True

    async def _redis_none():
        return None

    monkeypatch.setattr(health, "check_database", _db_ok)
    monkeypatch.setattr(health, "check_redis", _redis_none)
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "deepgram_api_key", "")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    for provider in ("openai", "anthropic", "google"):
        monkeypatch.setattr(settings, f"{provider}_api_key", "")

    result = await health.readiness()
    # DB and Redis fine, but production without any voice provider is not ready.
    assert result["ready"] is False
    assert result["body"]["checks"]["providers"]["ok"] is False


@pytest.mark.asyncio
async def test_readiness_does_not_require_providers_outside_production(monkeypatch):
    async def _db_ok():
        return True

    async def _redis_none():
        return None

    monkeypatch.setattr(health, "check_database", _db_ok)
    monkeypatch.setattr(health, "check_redis", _redis_none)
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "deepgram_api_key", "")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    for provider in ("openai", "anthropic", "google"):
        monkeypatch.setattr(settings, f"{provider}_api_key", "")

    result = await health.readiness()
    assert result["ready"] is True
