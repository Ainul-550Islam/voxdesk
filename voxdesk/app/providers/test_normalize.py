"""Unit tests for app.providers.normalize.

Co-located with the package; run with: python -m pytest app/providers/ -q
"""

from __future__ import annotations

from app.providers.normalize import (
    KNOWN_CATEGORIES,
    KNOWN_PROVIDERS,
    SPEECH_SPEED_MAX,
    SPEECH_SPEED_MIN,
    bounded_label,
    clamp,
    normalize_category_label,
    normalize_provider_label,
    normalize_speech_speed,
)


def test_bounded_label_keeps_known_values():
    assert bounded_label("OpenAI", KNOWN_PROVIDERS, "other") == "openai"
    assert bounded_label(" rate_limit ", KNOWN_CATEGORIES, "provider_error") == "rate_limit"


def test_bounded_label_collapses_unknown():
    assert bounded_label("totally_unknown", KNOWN_PROVIDERS, "other") == "other"
    assert bounded_label("", KNOWN_PROVIDERS, "other") == "other"
    assert bounded_label(None, KNOWN_CATEGORIES, "provider_error") == "provider_error"


def test_label_helpers():
    assert normalize_provider_label("ELEVENLABS") == "elevenlabs"
    assert normalize_provider_label("acme-telco") == "other"
    assert normalize_category_label("TIMEOUT") == "timeout"
    assert normalize_category_label("nonsense") == "provider_error"


def test_speech_speed_reuse_existing_contract():
    # The single source of truth is app.agent.voice_settings; this module
    # re-exports it and must agree with its range.
    applied, adjusted = normalize_speech_speed(1.0)
    assert applied == 1.0 and not adjusted
    applied, adjusted = normalize_speech_speed(0.3)
    assert applied == SPEECH_SPEED_MIN and adjusted
    applied, adjusted = normalize_speech_speed(2.0)
    assert applied == SPEECH_SPEED_MAX and adjusted


def test_clamp_generic():
    assert clamp(5, 0, 10) == (5, False)
    assert clamp(-1, 0, 10) == (0, True)
    assert clamp(11, 0, 10) == (10, True)


def test_clamp_handles_floats():
    assert clamp(2.5, 0.0, 1.0) == (1.0, True)
    assert clamp(0.5, 0.0, 1.0) == (0.5, False)
