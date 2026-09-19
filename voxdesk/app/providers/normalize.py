"""Bounded-label + range normalisation (Phase 4, multi-provider hardening).

A dependency-free mirror of the two normalisation seams the live layer relies
on:

* ``app.agent.provider_observability`` normalises provider/category strings
  into **closed sets** before they touch Prometheus labels, so a misbehaving
  integration cannot mint unbounded label series.
* ``app.agent.voice_settings`` clamps the speech-speed multiplier into the
  provider-supported range and reports whether a value was adjusted.

This module re-exposes both behaviours without importing Prometheus or the
config stack, reusing ``app.agent.voice_settings`` (which is itself
dependency-free) as the single source of truth for the speed range.
"""

from __future__ import annotations

from app.agent.voice_settings import (
    SPEECH_SPEED_DEFAULT,
    SPEECH_SPEED_MAX,
    SPEECH_SPEED_MIN,
    normalize_speech_speed,
)

#: Providers that may appear on a metric label; everything else → ``other``.
#: Mirrors ``provider_observability.KNOWN_PROVIDERS``.
KNOWN_PROVIDERS = frozenset(
    {"deepgram", "elevenlabs", "openai", "anthropic", "google", "llm", "unknown"}
)

#: Failure categories that may appear on a metric label; everything else →
#: ``provider_error``. Mirrors ``provider_observability.KNOWN_CATEGORIES`` and
#: the closed set in ``app.agent.errors.CATEGORIES``.
KNOWN_CATEGORIES = frozenset(
    {
        "configuration_error",
        "authentication_error",
        "authorization_error",
        "rate_limit",
        "timeout",
        "unavailable",
        "invalid_request",
        "unsupported_feature",
        "provider_error",
    }
)


def bounded_label(value: str | None, known: frozenset[str], fallback: str) -> str:
    """Collapse arbitrary input into a closed set.

    Mirrors ``provider_observability._label``: strip, lower-case, and return
    the value only if it is in ``known``; otherwise ``fallback``. Callers get
    a fixed, bounded label space regardless of the input.
    """
    value = (value or "").strip().lower()
    return value if value in known else fallback


def normalize_provider_label(provider: str | None) -> str:
    return bounded_label(provider, KNOWN_PROVIDERS, "other")


def normalize_category_label(category: str | None) -> str:
    return bounded_label(category, KNOWN_CATEGORIES, "provider_error")


def clamp(value: float, low: float, high: float) -> tuple[float, bool]:
    """Clamp ``value`` into ``[low, high]``; second element is True when the
    value was adjusted (out of range). Generic form of the speech-speed
    normalisation, kept for reuse by other bounded settings."""
    clamped = min(max(float(value), low), high)
    return clamped, clamped != float(value)


# Re-export the voice-setting constants so callers have one import site.
SPEECH_SPEED_BOUNDS = (SPEECH_SPEED_MIN, SPEECH_SPEED_MAX)

__all__ = [
    "KNOWN_CATEGORIES",
    "KNOWN_PROVIDERS",
    "SPEECH_SPEED_BOUNDS",
    "SPEECH_SPEED_DEFAULT",
    "SPEECH_SPEED_MAX",
    "SPEECH_SPEED_MIN",
    "bounded_label",
    "clamp",
    "normalize_category_label",
    "normalize_provider_label",
    "normalize_speech_speed",
]
