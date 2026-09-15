"""Voice-settings validation shared by the REST layer and the TTS provider layer.

ElevenLabs' streaming TTS accepts a speech-speed multiplier through the
`voice_settings.speed` field, with a supported range of **0.7–1.2** (the
ElevenLabs Agents Platform restricts speed to that range; the REST API is wider
at 0.25–4.0, but the voice pipeline streams). Values outside the supported
range are rejected or clamped by the provider, so VoxDesk enforces the range
itself:

* the API rejects out-of-range writes with a 422, and
* the pipeline normalises out-of-range values that were persisted by older
  clients (clamp, never rewrite) so a provider never receives an unsupported
  value.

This module is deliberately dependency-free — no pipecat import — so the REST
layer (`app/api/routes.py`) and the provider layer (`app/agent/tts.py`) share
one source of truth without loading the voice stack.
"""
from __future__ import annotations

SPEECH_SPEED_DEFAULT = 1.0
SPEECH_SPEED_MIN = 0.7
SPEECH_SPEED_MAX = 1.2


def normalize_speech_speed(value: float) -> tuple[float, bool]:
    """Clamp a stored speech speed into the provider-supported range.

    Returns ``(applied, adjusted)`` where ``adjusted`` is True when the stored
    value lay outside ``[SPEECH_SPEED_MIN, SPEECH_SPEED_MAX]``. The stored value
    is never rewritten — only the value sent to the provider is normalised, so
    an existing tenant's configuration is preserved exactly.
    """
    stored = float(value)
    applied = min(max(stored, SPEECH_SPEED_MIN), SPEECH_SPEED_MAX)
    return applied, applied != stored
