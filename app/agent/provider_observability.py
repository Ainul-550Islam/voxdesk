"""Observability hooks for the live voice-provider path (Step 3).

Reuses the existing Prometheus conventions in ``app/core.metrics`` — no
second metric system. The one addition is a bounded counter that groups
provider failures by provider and category.

Label cardinality is the whole point of this module: raw provider names and
category strings are *normalised* into a fixed, closed set before they touch a
label, so a misbehaving integration cannot mint unbounded label series (the
way an open-ended ``str(exc)`` or a tenant-supplied model string would).
"""

from __future__ import annotations

from app.core import metrics

#: Providers that can appear on the counter's `provider` label. Everything
#: else collapses to ``other``.
KNOWN_PROVIDERS = frozenset(
    {"deepgram", "elevenlabs", "openai", "anthropic", "google", "llm", "unknown"}
)

#: Categories that can appear on the counter's `category` label. Mirrors the
#: closed set in ``app.agent.errors.CATEGORIES``; anything else collapses to
#: ``provider_error``.
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


def _label(value: str | None, known: frozenset, fallback: str) -> str:
    value = (value or "").strip().lower()
    return value if value in known else fallback


def record_provider_error(provider: str | None, category: str | None) -> None:
    """Count one provider failure with bounded labels.

    Safe to call with arbitrary input: the values are normalised before they
    become label values, so cardinality cannot grow past the product of the
    two closed sets.
    """
    metrics.PROVIDER_ERRORS.labels(
        _label(provider, KNOWN_PROVIDERS, "other"),
        _label(category, KNOWN_CATEGORIES, "provider_error"),
    ).inc()
