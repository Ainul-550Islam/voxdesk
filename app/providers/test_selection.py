"""Unit tests for app.providers.selection.

Co-located with the package; run with: python -m pytest app/providers/ -q
"""

from __future__ import annotations

from app.providers.selection import (
    FALLBACK_ORDER,
    PRESETS,
    SUPPORTED_PROVIDERS,
    resolve_preset,
    resolve_provider,
    resolve_tenant,
)


def test_unknown_provider_is_an_error():
    result = resolve_provider("azure", {"openai"})
    assert not result.ok
    assert result.provider is None
    assert "Unknown LLM provider" in result.error


def test_requested_provider_with_key_wins_without_fallback():
    result = resolve_provider("anthropic", {"openai", "anthropic"}, model="claude-haiku-4-5")
    assert result.ok
    assert result.provider == "anthropic"
    assert not result.fell_back
    assert result.model == "claude-haiku-4-5"


def test_missing_key_falls_back_in_order():
    result = resolve_provider("openai", {"anthropic"})
    assert result.ok
    assert result.provider == "anthropic"
    assert result.fell_back
    assert result.model == "claude-haiku-4-5"  # default model for the fallback


def test_fallback_skips_providers_without_keys():
    result = resolve_provider("openai", {"google"})
    assert result.provider == "google"
    assert result.fell_back


def test_no_keys_is_an_error():
    result = resolve_provider("openai", set())
    assert not result.ok
    assert result.provider is None
    assert "No LLM provider API key" in result.error


def test_fallback_order_is_stable():
    assert FALLBACK_ORDER == ["openai", "anthropic", "google"]
    assert tuple(SUPPORTED_PROVIDERS) == ("openai", "anthropic", "google")


def test_resolve_preset_only_accepts_known_names():
    assert resolve_preset("fast") == ("openai", "gpt-4o-mini")
    assert resolve_preset("smart") == ("anthropic", "claude-sonnet-4-5")
    assert resolve_preset("nonexistent") is None
    assert resolve_preset(None) is None
    assert resolve_preset("") is None


def test_resolve_tenant_priority():
    # preset wins over explicit provider/model
    assert resolve_tenant("fast", "google", "gemini-x") == ("openai", "gpt-4o-mini")
    # explicit provider+model wins when no preset
    assert resolve_tenant(None, "google", "gemini-2.0-flash") == ("google", "gemini-2.0-flash")
    # partial explicit settings do not count; default applies
    assert resolve_tenant(None, "google", None) == PRESETS["natural"]
    assert resolve_tenant(None, None, None) == PRESETS["natural"]
