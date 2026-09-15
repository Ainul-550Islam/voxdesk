"""Provider capability contracts (Phase 4, multi-provider hardening).

A dependency-free mirror of the capability dictionaries the live voice layer
already maintains — ``ELEVENLABS_CAPABILITIES`` in ``app/agent/tts.py``,
``DEEPGRAM_CAPABILITIES`` in ``app/agent/stt.py`` and ``LLM_CAPABILITIES`` in
``app/agent/llm_factory.py`` — turned into a single, testable model.

The rules encoded here are exactly the ones those modules enforce at build
time:

* **"absent" means "cannot".** An unknown provider, or a provider without a
  ``supports_*`` entry, is treated as *not* supporting the feature — never as
  supporting it. This is what stops the pipeline from silently promising
  (say) tool-calling on a provider that cannot do it.
* **Business-critical requirements fail loudly.** Booking/escalation tools
  require tool-calling; a provider that cannot do it is rejected, not
  silently degraded.

Nothing here imports pipecat, the config stack, or the error hierarchy, so
the contracts can be unit-tested and reused by the REST layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ------------------------------------------------------------------ registry --

#: Capabilities for each voice/LLM provider, mirroring the live dicts.
#: ``supports_*`` keys must stay in sync with ``app/agent/tts.py``,
#: ``app/agent/stt.py`` and ``app/agent/llm_factory.py``.
CAPABILITIES: dict[str, dict[str, bool]] = {
    "deepgram": {
        "supports_streaming": True,
        "supports_language": True,
        "supports_interim_results": True,
        "supports_model_selection": True,
        "supports_punctuation": True,
        "supports_filler_words": True,
        "supports_pitch": False,
    },
    "elevenlabs": {
        "supports_speed": True,
        "supports_stability": True,
        "supports_similarity_boost": True,
        "supports_style": True,
        "supports_use_speaker_boost": True,
        "supports_pitch": False,
        "supports_language": True,
        "supports_streaming": True,
        "supports_interruptions": False,
    },
    "openai": {
        "supports_tool_calling": True,
        "supports_streaming": True,
        "supports_interruptions": True,
    },
    "anthropic": {
        "supports_tool_calling": True,
        "supports_streaming": True,
        "supports_interruptions": True,
    },
    "google": {
        "supports_tool_calling": True,
        "supports_streaming": True,
        "supports_interruptions": True,
    },
}

#: Providers this system can actually construct a service for (mirrors
#: ``llm_factory.SUPPORTED_PROVIDERS`` for the LLM family).
LLM_PROVIDERS = frozenset({"openai", "anthropic", "google"})

VOICE_PROVIDERS = frozenset({"deepgram", "elevenlabs"})


# ---------------------------------------------------------------- gating -----

@dataclass(frozen=True)
class Contract:
    """A capability contract: the features a caller requires of a provider."""

    provider: str
    required: frozenset[str] = frozenset()

    def missing_capabilities(self) -> list[str]:
        """Capabilities this contract requires that the provider cannot do.

        An unknown provider has an empty capability table, so *every*
        requirement is missing — "absent" means "cannot".
        """
        capabilities = CAPABILITIES.get(self.provider, {})
        return [cap for cap in sorted(self.required) if not capabilities.get(cap, False)]

    def satisfied(self) -> bool:
        return not self.missing_capabilities()


def supports(provider: str, capability: str) -> bool:
    """True only when ``provider`` is known *and* declares ``capability``.

    Deliberately False for unknown providers: callers treat "absent" the same
    as "cannot do it", never as "can do it".
    """
    return CAPABILITIES.get(provider, {}).get(capability, False)


def validate_contract(provider: str, required: Any) -> list[str]:
    """Human-readable violations for a required feature set, empty when OK.

    ``required`` may be an iterable of capability names.
    """
    contract = Contract(provider=provider, required=frozenset(required))
    missing = contract.missing_capabilities()
    if not missing:
        return []
    return [
        f"provider {provider!r} does not support {capability}"
        for capability in missing
    ]


def contract_for(provider: str, *required: str) -> Contract:
    return Contract(provider=provider, required=frozenset(required))
