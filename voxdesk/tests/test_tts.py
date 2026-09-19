"""Step 2 — voice provider contract: ElevenLabs speech-speed mapping.

These tests pin the behaviour of the pipecat ElevenLabs integration so a
future version bump cannot silently drop tenant speech speed again. They are
deterministic: no network, no real API key — they assert the exact
``voice_settings`` payload the service would send to the provider (pipecat
0.0.94 forwards it in the per-context message; 0.0.55 forwarded it in the
connect URL — the ``_set_voice_settings()`` seam is the same).
"""
from __future__ import annotations

import pytest
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

from app.agent import tts
from app.agent.tts import (
    ELEVENLABS_CAPABILITIES,
    SpeedAwareElevenLabsTTSService,
    build_tts,
)
from app.agent.voice_settings import (
    SPEECH_SPEED_DEFAULT,
    SPEECH_SPEED_MAX,
    SPEECH_SPEED_MIN,
    normalize_speech_speed,
)
from app.core.config import settings
from tests.conftest import make_tenant


class _FakeTenant:
    """A tenant-shaped object with just the fields build_tts reads."""

    def __init__(self, *, speech_speed=1.0, language="en-US", voice_id=None,
                 name="Fake Co"):
        self.name = name
        self.language = language
        self.voice_id = voice_id
        self.speech_speed = speech_speed


@pytest.fixture(autouse=True)
def _elevenlabs_key(monkeypatch):
    """Step 3: `build_tts` now validates configuration before constructing a
    service, so every test needs a present key. The value is a sentinel; the
    secret-safety tests assert it never leaks into logs or payloads."""
    monkeypatch.setattr(settings, "elevenlabs_api_key", "sk-sentinel-secret-key")


# ------------------------------------------------------- capability model ---

def test_capability_model_declares_what_the_integration_can_map():
    assert ELEVENLABS_CAPABILITIES["supports_speed"] is True
    assert ELEVENLABS_CAPABILITIES["supports_stability"] is True
    assert ELEVENLABS_CAPABILITIES["supports_similarity_boost"] is True
    assert ELEVENLABS_CAPABILITIES["supports_style"] is True
    assert ELEVENLABS_CAPABILITIES["supports_use_speaker_boost"] is True
    assert ELEVENLABS_CAPABILITIES["supports_pitch"] is False
    assert ELEVENLABS_CAPABILITIES["supports_interruptions"] is False


# --------------------------------------------------------- normalization ---

@pytest.mark.parametrize(
    "raw,expected",
    [
        (1.0, 1.0),
        (0.7, 0.7),
        (1.2, 1.2),
        (0.85, 0.85),
        (1.15, 1.15),
        (0.5, 0.7),   # below the supported range -> clamped to the floor
        (0.0, 0.7),
        (2.0, 1.2),   # above the supported range -> clamped to the ceiling
        (9.9, 1.2),
    ],
)
def test_normalize_speech_speed_clamps_into_the_provider_range(raw, expected):
    applied, adjusted = normalize_speech_speed(raw)
    assert applied == pytest.approx(expected)
    assert adjusted == (raw != expected)


def test_normalize_speech_speed_reports_whether_it_adjusted():
    applied, adjusted = normalize_speech_speed(1.05)
    assert applied == pytest.approx(1.05)
    assert adjusted is False

    applied, adjusted = normalize_speech_speed(SPEECH_SPEED_DEFAULT)
    assert applied == pytest.approx(1.0)
    assert adjusted is False


def test_range_constants_are_consistent():
    assert SPEECH_SPEED_MIN < SPEECH_SPEED_DEFAULT < SPEECH_SPEED_MAX


# ------------------------------- the contract we must NOT rely on ---------

def test_pipecat_input_params_now_has_a_speed_field():
    """Step 10 (pipecat 0.0.94): ``InputParams`` gained a ``speed`` field
    (0.7–1.2). The subclass still maps tenant speed at the
    ``_set_voice_settings()`` layer so the value is never dropped, but the
    reason is now isolation of the mapping rather than a missing field.

    If a future pipecat release removes or renames ``speed`` again, this test
    fails first.
    """
    params = ElevenLabsTTSService.InputParams(
        stability=0.5, similarity_boost=0.8, speed=1.1
    )
    assert hasattr(params, "speed")
    assert "speed" in params.model_fields
    assert params.speed == pytest.approx(1.1)


def test_pipecat_accepts_stability_and_similarity_independently():
    """Step 10 (pipecat 0.0.94): stability and similarity are independent
    optional fields — the 0.0.55-era rule that they had to be supplied
    together is gone. The VoxDesk builder always supplies both anyway, but
    pinning this documents the provider contract we rely on.
    """
    params = ElevenLabsTTSService.InputParams(stability=0.5)
    assert params.stability == pytest.approx(0.5)
    assert params.similarity_boost is None


def test_the_service_sends_voice_settings_on_the_wire():
    """pipecat forwards ``voice_settings`` verbatim (0.0.55: in the connect
    URL; 0.0.94: in the per-context message) — the seam the subclass uses."""
    svc = build_tts(_FakeTenant(speech_speed=1.1))
    voice_settings = svc._set_voice_settings()
    assert voice_settings["stability"] == pytest.approx(0.45)
    assert voice_settings["similarity_boost"] == pytest.approx(0.8)
    assert voice_settings["speed"] == pytest.approx(1.1)


# ------------------------------------------------ actual mapping behavior ---

def test_default_speed_is_omitted_like_before():
    """speed=1.0 is the provider default; the wire payload stays unchanged."""
    svc = build_tts(_FakeTenant(speech_speed=1.0))
    voice_settings = svc._set_voice_settings()
    assert voice_settings is not None
    assert "speed" not in voice_settings
    assert voice_settings["stability"] == pytest.approx(0.45)
    assert voice_settings["similarity_boost"] == pytest.approx(0.8)


def test_a_faster_tenant_speed_is_mapped():
    svc = build_tts(_FakeTenant(speech_speed=1.1))
    assert svc._set_voice_settings()["speed"] == pytest.approx(1.1)


def test_a_slower_tenant_speed_is_mapped():
    svc = build_tts(_FakeTenant(speech_speed=0.8))
    assert svc._set_voice_settings()["speed"] == pytest.approx(0.8)


def test_the_boundary_values_are_passed_through():
    assert build_tts(_FakeTenant(speech_speed=0.7))._set_voice_settings()[
        "speed"
    ] == pytest.approx(0.7)
    assert build_tts(_FakeTenant(speech_speed=1.2))._set_voice_settings()[
        "speed"
    ] == pytest.approx(1.2)


def test_out_of_range_stored_speed_is_clamped_and_warned(monkeypatch):
    warnings: list[tuple[str, dict]] = []

    class _LogStub:
        def warning(self, event: str, **kw) -> None:
            warnings.append((event, kw))

    monkeypatch.setattr(settings, "elevenlabs_api_key", "sk-sentinel-secret-key")
    monkeypatch.setattr(tts, "log", _LogStub())

    svc = build_tts(_FakeTenant(speech_speed=2.5, name="Range Co"))
    assert svc._set_voice_settings()["speed"] == pytest.approx(SPEECH_SPEED_MAX)

    assert warnings, "an out-of-range stored speed must be reported"
    event, fields = warnings[0]
    assert event == "tts.speech_speed_normalized"
    assert fields["stored"] == pytest.approx(2.5)
    assert fields["applied"] == pytest.approx(1.2)


def test_no_speed_warning_for_in_range_values(monkeypatch):
    warnings: list[str] = []

    class _LogStub:
        def warning(self, event: str, **kw) -> None:
            warnings.append(event)

    monkeypatch.setattr(tts, "log", _LogStub())
    build_tts(_FakeTenant(speech_speed=1.05))
    assert warnings == []


def test_the_warning_leaks_no_secret(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(settings, "elevenlabs_api_key", "sk-sentinel-secret-key")

    class _LogStub:
        def warning(self, event: str, **kw) -> None:
            captured.update(kw)
            captured["event"] = event

    monkeypatch.setattr(tts, "log", _LogStub())
    build_tts(_FakeTenant(speech_speed=3.0))

    assert "sk-sentinel-secret-key" not in str(captured)
    assert "api_key" not in captured


def test_model_and_voice_resolution_reuses_language_options():
    english = build_tts(_FakeTenant(language="en-US"))
    multilingual = build_tts(_FakeTenant(language="bn-BD"))
    assert english.model_name == "eleven_flash_v2"
    assert multilingual.model_name == "eleven_flash_v2_5"


def test_a_custom_tenant_voice_is_used():
    svc = build_tts(_FakeTenant(voice_id="customVoice123"))
    assert svc._voice_id == "customVoice123"


def test_the_default_voice_is_used_when_none_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "defaultVoice")
    svc = build_tts(_FakeTenant(voice_id=None))
    assert svc._voice_id == "defaultVoice"


@pytest.mark.asyncio
async def test_tenant_specific_speed_from_a_real_tenant_row(db):
    tenant = await make_tenant(db, "Speed Co")
    tenant.speech_speed = 1.15
    await db.commit()
    await db.refresh(tenant)

    svc = build_tts(tenant)
    assert svc._set_voice_settings()["speed"] == pytest.approx(1.15)


def test_backward_compatibility_default_is_unchanged_for_existing_tenants():
    """A tenant with the default 1.0 (the value every existing row has) sends
    the same payload as before this change — no `speed` key."""
    svc = build_tts(_FakeTenant(speech_speed=1.0))
    voice_settings = svc._set_voice_settings()
    assert "speed" not in voice_settings
    assert set(voice_settings) == {
        "stability", "similarity_boost", "style", "use_speaker_boost",
    }


def test_the_subclass_is_used_by_the_builder():
    assert isinstance(build_tts(_FakeTenant()), SpeedAwareElevenLabsTTSService)
