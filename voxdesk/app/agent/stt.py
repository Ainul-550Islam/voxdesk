"""Deepgram STT service construction (Step 3 voice-provider lifecycle).

The speech pipeline needs one thing from an STT provider: a pipecat service
that turns raw audio frames into transcription frames. This module owns the
Deepgram specifics — API-key validation, model/language resolution, and the
streaming configuration — so the pipeline stays provider-agnostic and a
misconfigured deployment fails fast with a typed error instead of a confusing
downstream crash.

Two defects fixed here (both were latent, never exercised by the test suite):

* pipecat 0.0.55's ``DeepgramSTTService`` requires ``live_options`` to be a
  ``LiveOptions`` instance (it calls ``live_options.to_dict()``). The old
  pipeline passed a plain dict, which raised ``AttributeError`` the moment a
  live call constructed the service.
* The same service has no ``model`` constructor parameter: the resolved model
  (nova-3 vs nova-2 fallback) was passed as a keyword that pipecat silently
  dropped, so every call used Deepgram's default model regardless of tenant
  language. The model now travels inside ``live_options`` and actually reaches
  Deepgram.

STEP 10 (pipecat 0.0.55 -> 0.0.94) import change: ``LiveOptions`` is no
longer defined by pipecat. pipecat 0.0.94 imports it from ``deepgram-sdk``
(``pipecat/services/deepgram/stt.py`` does ``from deepgram import ...,
LiveOptions, ...``), so this module imports ``DeepgramSTTService`` from the
canonical ``pipecat.services.deepgram.stt`` package module and ``LiveOptions``
from ``deepgram`` directly. The field set used here (encoding, sample_rate,
punctuate, interim_results, endpointing, smart_format, filler_words,
language, model) is identical in deepgram-sdk 4.7's dataclass, so the wire
payload does not change.

Capabilities mirror the contract style introduced for TTS in Step 2
(``app/agent/tts.py``): the pipeline checks ``supports_*`` before forwarding a
setting, so an unsupported setting is never passed and silently dropped.
"""
from __future__ import annotations

from deepgram import LiveOptions
from pipecat.services.deepgram.stt import DeepgramSTTService

from app.agent.errors import ProviderConfigurationError
from app.core.config import settings
from app.core.i18n import deepgram_options

#: What this Deepgram integration can and cannot map from tenant settings.
DEEPGRAM_CAPABILITIES = {
    "supports_streaming": True,        # websocket live transcription
    "supports_language": True,         # per-language model + language code
    "supports_interim_results": True,  # used for fast turn detection
    "supports_model_selection": True,  # nova-3 vs nova-2 fallback
    "supports_punctuation": True,
    "supports_filler_words": True,     # English models only
    "supports_pitch": False,           # not a transcription concept
}


def validate_stt_config() -> None:
    """Refuse to build an STT service with no Deepgram key configured.

    Without a key the websocket connect fails downstream in a way pipecat
    logs but does not surface clearly; failing here turns it into a typed
    ``configuration_error`` before the pipeline starts.
    """
    if not (settings.deepgram_api_key or "").strip():
        raise ProviderConfigurationError(
            "Deepgram STT API key is not configured",
            provider="deepgram",
        )


def build_stt(tenant) -> DeepgramSTTService:
    """Build the STT service for a tenant.

    Model/language resolution reuses ``deepgram_options`` — the same
    abstraction the pipeline used before this change, so a language that
    nova-3 does not cover still falls back to nova-2. Validation happens
    first so a missing key raises ``ProviderConfigurationError`` rather than
    constructing a service that can never connect.
    """
    validate_stt_config()

    # ভাষা অনুযায়ী STT সেটিং। nova-3 সব ভাষা কভার করে না -> nova-2 fallback।
    stt_opts = deepgram_options(tenant.language, settings.deepgram_model)
    return DeepgramSTTService(
        api_key=settings.deepgram_api_key,
        sample_rate=8000,
        live_options=LiveOptions(
            encoding="mulaw",
            sample_rate=8000,
            punctuate=stt_opts["punctuate"],
            interim_results=True,     # দ্রুত turn detection-এর জন্য বাধ্যতামূলক
            endpointing=250,
            smart_format=stt_opts["smart_format"],
            # filler_words শুধু ইংরেজি মডেলে আছে
            filler_words=stt_opts["filler_words"],
            language=stt_opts["language"],
            model=stt_opts["model"],
        ),
    )
