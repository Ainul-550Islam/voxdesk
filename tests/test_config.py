"""Step 1 — configuration loading and production-safety validation."""
from __future__ import annotations

import base64
import os

from app.core.config import Settings


def _valid_prod(monkeypatch, **overrides) -> Settings:
    """A production Settings with every required secret set to a safe value.

    Nothing here is a real credential — all values are random/placeholder
    strength strings used only to prove validate_security() accepts a fully
    configured production environment.
    """
    monkeypatch.delenv("APP_ENV", raising=False)
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    values = dict(
        app_env="production",
        public_base_url="https://app.example.com",
        cors_origins="https://app.example.com",
        rate_limit_enabled=True,
        secret_key="a" * 48,
        jwt_secret="b" * 48,
        twilio_account_sid="AC" + "0" * 32,
        twilio_auth_token="c" * 32,
        twilio_phone_number="+15550001111",
        deepgram_api_key="d" * 32,
        openai_api_key="sk-" + "e" * 32,
        elevenlabs_api_key="f" * 32,
        crm_encryption_keys=f"k1:{key}",
        knowledge_embedding_provider="openai",
        knowledge_embedding_model="text-embedding-3-small",
        knowledge_embedding_dimensions=1536,
        billing_provider="manual",
        sentry_dsn="",
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_default_development_settings_load():
    s = Settings(_env_file=None)
    assert s.app_env == "development"
    assert not s.is_production
    assert s.rate_limit_enabled is False
    assert s.redis_url == ""


def test_environment_variable_override_is_honoured(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CALL_RETENTION_DAYS", "30")
    s = Settings()
    assert s.app_env == "production"
    assert s.call_retention_days == 30


def test_development_defaults_report_known_problems():
    s = Settings(_env_file=None)
    problems = s.validate_security()
    # Default JWT secret and SECRET_KEY are the two placeholder flags.
    assert any("JWT_SECRET" in p for p in problems)
    assert any("SECRET_KEY" in p for p in problems)


def test_fully_configured_production_is_clean(monkeypatch):
    s = _valid_prod(monkeypatch)
    assert s.is_production
    assert s.validate_security() == []


def test_production_refuses_default_jwt_secret(monkeypatch):
    s = _valid_prod(monkeypatch, jwt_secret="insecure-development-only-change-me")
    problems = s.validate_security()
    assert any("JWT_SECRET" in p for p in problems)


def test_production_refuses_insecure_base_url(monkeypatch):
    s = _valid_prod(monkeypatch, public_base_url="http://app.example.com")
    problems = s.validate_security()
    assert any("https" in p for p in problems)


def test_production_refuses_missing_twilio_token(monkeypatch):
    s = _valid_prod(monkeypatch, twilio_auth_token="")
    problems = s.validate_security()
    assert any("TWILIO_AUTH_TOKEN" in p for p in problems)


def test_production_refuses_twilio_verify_bypass(monkeypatch):
    s = _valid_prod(monkeypatch, twilio_skip_webhook_verify=True)
    problems = s.validate_security()
    assert any("TWILIO_SKIP_WEBHOOK_VERIFY" in p for p in problems)


def test_production_refuses_missing_crm_encryption_keys(monkeypatch):
    s = _valid_prod(monkeypatch, crm_encryption_keys="")
    problems = s.validate_security()
    assert any("CRM_ENCRYPTION_KEYS" in p for p in problems)


def test_production_refuses_hashing_embedder(monkeypatch):
    s = _valid_prod(monkeypatch, knowledge_embedding_provider="hashing")
    problems = s.validate_security()
    assert any("hashing" in p for p in problems)


def test_production_refuses_test_stripe_key(monkeypatch):
    s = _valid_prod(
        monkeypatch,
        billing_provider="stripe",
        stripe_secret_key="sk_test_1234567890",
        stripe_webhook_secret="whsec_" + "a" * 32,
    )
    problems = s.validate_security()
    assert any("test-mode" in p for p in problems)


def test_stripe_provider_requires_secrets(monkeypatch):
    s = _valid_prod(monkeypatch, billing_provider="stripe")
    problems = s.validate_security()
    assert any("STRIPE_SECRET_KEY" in p for p in problems)
    assert any("STRIPE_WEBHOOK_SECRET" in p for p in problems)


def test_invalid_billing_provider_is_rejected(monkeypatch):
    s = _valid_prod(monkeypatch, billing_provider="paypal")
    problems = s.validate_security()
    assert any("BILLING_PROVIDER" in p for p in problems)


def test_cors_origin_list_is_parsed():
    s = Settings(_env_file=None, cors_origins="http://a.com, http://b.com")
    assert s.cors_origin_list == ["http://a.com", "http://b.com"]


def test_wildcard_cors_is_rejected_when_credentials_allowed():
    s = Settings(_env_file=None, cors_origins="http://a.com, *")
    problems = s.validate_security()
    assert any("CORS_ORIGINS" in p for p in problems)
