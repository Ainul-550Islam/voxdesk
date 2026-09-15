"""ElevenLabs TTS service construction (Step 2 + Step 3 voice-provider contract).

The speech pipeline needs one thing from a TTS provider: a pipecat service
that turns text frames into audio frames. This module owns the ElevenLabs
specifics — model/voice resolution, voice-settings mapping, the speech-speed
support, and (Step 3) fail-fast configuration validation.

Why a subclass
--------------
The tenant ``speech_speed`` column maps to ElevenLabs' ``voice_settings.speed``
field (documented range 0.7–1.2). pipecat (0.0.55 and 0.0.94 alike) builds
its ``voice_settings`` dict from ``InputParams`` — and in 0.0.55 that class
had no ``speed`` field, so the value was silently dropped by pydantic. We
subclass the service to add ``speed`` at exactly the ``_set_voice_settings()``
layer, which is the single seam pipecat uses to produce the ``voice_settings``
object it puts on the wire. In pipecat 0.0.55 that object went into the
websocket connect URL; in 0.0.94 it goes into the per-context message
(``{"text": " ", "context_id": ..., "voice_settings": {...}}``) — either way
the value reaches the provider instead of being thrown away. A speed of 1.0
(the provider default) is omitted, keeping the wire payload byte-for-byte
identical to the pre-speed behaviour for default tenants.

Failure behaviour
-----------------
A missing ElevenLabs key raises ``ProviderConfigurationError`` before any
service is constructed, so a misconfigured deployment fails fast with a typed
error instead of a silent downstream connect failure.
"""
from __future__ import annotations

from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

from app.agent.errors import ProviderConfigurationError
from app.agent.voice_settings import (
    SPEECH_SPEED_DEFAULT,
    normalize_speech_speed,
)
from app.core.config import settings
from app.core.i18n import elevenlabs_options
from app.core.logging import log

#: What this ElevenLabs integration can and cannot map from tenant settings.
#: ``supports_*`` is the contract the pipeline checks before forwarding a
#: value, so an unsupported setting is never passed and silently ignored.
ELEVENLABS_CAPABILITIES = {
    "supports_speed": True,             # voice_settings.speed (0.7–1.2)
    "supports_stability": True,         # voice_settings.stability (0–1)
    "supports_similarity_boost": True,  # voice_settings.similarity_boost (0–1)
    "supports_style": True,             # voice_settings.style (0–1)
    "supports_use_speaker_boost": True,  # voice_settings.use_speaker_boost
    "supports_pitch": False,            # not exposed by pipecat's ElevenLabs integration
    "supports_language": True,          # multilingual models only
    "supports_streaming": True,
    "supports_interruptions": False,    # no per-utterance interruption control
}


class SpeedAwareElevenLabsTTSService(ElevenLabsTTSService):
    """ElevenLabs TTS that forwards a speech-speed multiplier to the provider.

    pipecat builds its ``voice_settings`` dict (stability, similarity, style,
    speaker boost) from ``InputParams`` and, in 0.0.94, forwards it in the
    per-context message rather than the connect URL. ElevenLabs'
    ``voice_settings.speed`` is a documented field (range 0.7–1.2), so we add
    it at that exact ``_set_voice_settings()`` layer.
    """

    def __init__(self, *, speed: float | None = None, **kwargs):
        self._voxdesk_speed = speed
        super().__init__(**kwargs)

    def _set_voice_settings(self):
        voice_settings = super()._set_voice_settings()
        if voice_settings is None:
            voice_settings = {}
        # 1.0 is the provider default; omitting it keeps the wire payload
        # identical to the pre-speed behaviour for default tenants.
        if (
            self._voxdesk_speed is not None
            and self._voxdesk_speed != SPEECH_SPEED_DEFAULT
        ):
            voice_settings["speed"] = self._voxdesk_speed
        return voice_settings or None


def validate_tts_config() -> None:
    """Refuse to build a TTS service with no ElevenLabs key configured.

    Without a key the websocket connect fails downstream in a way pipecat
    logs but does not surface clearly; failing here turns it into a typed
    ``configuration_error`` before the pipeline starts.
    """
    if not (settings.elevenlabs_api_key or "").strip():
        raise ProviderConfigurationError(
            "ElevenLabs TTS API key is not configured",
            provider="elevenlabs",
        )


def build_tts(tenant) -> SpeedAwareElevenLabsTTSService:
    """Build the TTS service for a tenant.

    Model/voice resolution reuses ``elevenlabs_options`` — the same abstraction
    the pipeline used before this change. Speech speed is normalised to the
    provider's supported range; a stored value outside it is logged as a
    structured warning (tenant name and numbers only, never a secret) and
    clamped rather than sent unsupported. A missing API key raises
    ``ProviderConfigurationError`` first.
    """
    validate_tts_config()

    opts = elevenlabs_options(tenant.language, tenant.voice_id)
    speed, adjusted = normalize_speech_speed(tenant.speech_speed)
    if adjusted:
        log.warning(
            "tts.speech_speed_normalized",
            tenant=tenant.name,
            stored=tenant.speech_speed,
            applied=speed,
        )
    return SpeedAwareElevenLabsTTSService(
        api_key=settings.elevenlabs_api_key,
        voice_id=opts["voice_id"] or settings.elevenlabs_voice_id,
        model=opts["model"],          # flash = lowest latency
        sample_rate=8000,
        params=ElevenLabsTTSService.InputParams(
            # Lower stability = more emotion/expression = more human, but too
            # low and pronunciation becomes unstable.
            stability=0.45,
            similarity_boost=0.8,
            style=0.35,               # a little expressiveness
            use_speaker_boost=True,
        ),
        speed=speed,
    )
