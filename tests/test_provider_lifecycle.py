"""Step 3 — voice-provider lifecycle hardening.

The live voice path (STT/TTS/LLM) must fail safely and observably: no
unhandled crashes, no secret leaks, no stuck calls, no double finalisation.
These tests pin the four things Step 3 added:

1. the closed provider-error taxonomy and its retryability rules;
2. fail-fast configuration validation for every provider builder;
3. secret-safe logging across the builders and the crash path;
4. the media-stream handler's response to a startup provider failure
   (accurate, idempotent FAILED finalisation + a categorised log line).

Step 3 also added the provider capability models (LLM tool-calling), the
bounded provider-error counter, and a bounded websocket handshake — all of
which are pinned here too.

Everything is deterministic: no network, no real API keys — sentinel strings
stand in for keys and the secret-safety tests assert the sentinel never
surfaces.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.agent import llm_factory, stt, tts
from app.agent.errors import (
    CATEGORIES,
    ProviderAuthenticationError,
    ProviderAuthorizationError,
    ProviderConfigurationError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitedError,
    ProviderRuntimeError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    UnsupportedProviderFeatureError,
)
from app.agent.provider_observability import record_provider_error
from app.core import metrics
from app.core.config import settings
from app.db.models import CallStatus, TransferState
from app.telephony import call_state
from tests.test_transfer import seed_call, tenant_with_human

SENTINEL = "sk-sentinel-secret-key"


class _TenantStub:
    """Just the fields the builders read."""

    def __init__(self, *, language="en-US", voice_id=None, speech_speed=1.0,
                 name="Stub Co"):
        self.name = name
        self.language = language
        self.voice_id = voice_id
        self.speech_speed = speech_speed


# -------------------------------------------------- 1. error taxonomy ----

@pytest.mark.parametrize(
    "cls,retryable",
    [
        (ProviderConfigurationError, False),
        (ProviderAuthenticationError, False),
        (ProviderAuthorizationError, False),
        (ProviderRateLimitedError, True),
        (ProviderTimeoutError, True),
        (ProviderUnavailableError, True),
        (ProviderInvalidRequestError, False),
        (UnsupportedProviderFeatureError, False),
        (ProviderRuntimeError, False),
    ],
)
def test_retryability_flags_match_the_policy(cls, retryable):
    err = cls("boom", provider="x")
    assert err.retryable is retryable
    assert err.category in CATEGORIES


def test_the_category_set_is_closed():
    """New categories are a deliberate change, not an accident of a typo."""
    concrete = {
        ProviderConfigurationError.category,
        ProviderAuthenticationError.category,
        ProviderAuthorizationError.category,
        ProviderRateLimitedError.category,
        ProviderTimeoutError.category,
        ProviderUnavailableError.category,
        ProviderInvalidRequestError.category,
        UnsupportedProviderFeatureError.category,
        ProviderRuntimeError.category,
    }
    assert concrete == set(CATEGORIES)
    assert len(CATEGORIES) == len(set(CATEGORIES))


def test_rate_limited_error_carries_a_retry_delay():
    err = ProviderRateLimitedError(provider="deepgram", retry_after=2.5)
    assert err.category == "rate_limit"
    assert err.retryable is True
    assert err.retry_after == 2.5


def test_a_provider_error_never_renders_its_detail():
    """`detail` may carry provider context; it must never reach logs/str."""
    err = ProviderAuthenticationError(
        "invalid credentials",
        provider="openai",
        detail={"Authorization": "Bearer sk-live-leak"},
    )
    assert "sk-live-leak" not in str(err)
    assert "sk-live-leak" not in err.safe_message
    assert err.safe_message == "invalid credentials"
    assert "openai/authentication_error" in str(err)


def test_provider_error_is_a_runtime_error():
    """Existing `except Exception` paths keep catching provider failures."""
    assert issubclass(ProviderError, RuntimeError)


# -------------------------------------- 2. fail-fast config validation ----

def test_build_tts_rejects_a_missing_key(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    with pytest.raises(ProviderConfigurationError) as exc:
        tts.build_tts(_TenantStub())
    assert exc.value.category == "configuration_error"
    assert exc.value.retryable is False
    assert exc.value.provider == "elevenlabs"


def test_build_stt_rejects_a_missing_key(monkeypatch):
    monkeypatch.setattr(settings, "deepgram_api_key", "")
    with pytest.raises(ProviderConfigurationError) as exc:
        stt.build_stt(_TenantStub())
    assert exc.value.category == "configuration_error"
    assert exc.value.provider == "deepgram"


def test_build_llm_rejects_an_unknown_provider(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", SENTINEL)
    with pytest.raises(UnsupportedProviderFeatureError) as exc:
        llm_factory.build_llm("banana", "model")
    assert exc.value.category == "unsupported_feature"
    assert exc.value.retryable is False


def test_build_llm_rejects_when_no_provider_has_a_key(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "google_api_key", "")
    with pytest.raises(ProviderConfigurationError) as exc:
        llm_factory.build_llm("openai", "gpt-4o-mini")
    assert exc.value.category == "configuration_error"
    assert exc.value.retryable is False


@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4o-mini"),
        ("anthropic", "claude-haiku-4-5"),
        ("google", "gemini-2.0-flash"),
    ],
)
def test_build_llm_constructs_every_provider(monkeypatch, provider, model):
    monkeypatch.setattr(settings, "openai_api_key", SENTINEL)
    monkeypatch.setattr(settings, "anthropic_api_key", SENTINEL)
    monkeypatch.setattr(settings, "google_api_key", SENTINEL)
    service = llm_factory.build_llm(provider, model)
    assert service is not None


def test_build_stt_resolves_the_model_for_the_language(monkeypatch):
    monkeypatch.setattr(settings, "deepgram_api_key", SENTINEL)
    english = stt.build_stt(_TenantStub(language="en-US"))
    bengali = stt.build_stt(_TenantStub(language="bn-BD"))
    assert english._settings["model"] == "nova-3"
    assert bengali._settings["model"] == "nova-2"
    assert english._settings["filler_words"] is True
    assert bengali._settings["filler_words"] is False


def test_build_stt_hands_pipecat_a_live_options_object(monkeypatch):
    """Regression: pipecat 0.0.55 calls `live_options.to_dict()`, so a plain
    dict crashed the pipeline on the first live call."""
    monkeypatch.setattr(settings, "deepgram_api_key", SENTINEL)
    service = stt.build_stt(_TenantStub())
    assert service._settings["encoding"] == "mulaw"
    assert service._settings["sample_rate"] == 8000


# --------------------------------------------------- 3. secret-safe logs ----

def test_build_llm_fallback_logs_no_key(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", SENTINEL)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "google_api_key", "")

    captured: list[tuple[str, dict]] = []

    class _LogStub:
        def warning(self, event, **kw):
            captured.append((event, kw))

    monkeypatch.setattr(llm_factory, "log", _LogStub())
    llm_factory.build_llm("anthropic", "claude-haiku-4-5")

    assert captured, "a fallback must be reported"
    event, fields = captured[0]
    assert event == "llm.fallback"
    assert SENTINEL not in repr(fields)
    assert "api_key" not in fields


def test_build_stt_never_stores_the_key_in_settings(monkeypatch):
    monkeypatch.setattr(settings, "deepgram_api_key", SENTINEL)
    service = stt.build_stt(_TenantStub())
    assert SENTINEL not in repr(service._settings)


def test_configuration_errors_carry_fixed_safe_messages(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    with pytest.raises(ProviderConfigurationError) as exc:
        tts.build_tts(_TenantStub())
    # The message is a fixed, safe string — no key, no payload, no secret.
    assert exc.value.safe_message == "ElevenLabs TTS API key is not configured"
    assert SENTINEL not in exc.value.safe_message


# ------------------------- 4. startup failure -> accurate finalisation ----

class _FakeWebsocket:
    def __init__(self, call_sid):
        self.query_params = {"token": "signed-token"}
        self._messages = [
            "connected",
            json.dumps({"start": {"streamSid": "SS-1", "callSid": call_sid}}),
        ]
        self.closed_with = None

    async def accept(self):
        return None

    async def receive_text(self):
        return self._messages.pop(0)

    async def close(self, code=1000):
        self.closed_with = code


def _patch_media_stream(monkeypatch, sessionmaker_, boom):
    """Point the handler at the test database and a failing pipeline."""
    from app.telephony import twilio_handler

    monkeypatch.setattr("app.agent.pipeline.run_voice_agent", boom)
    monkeypatch.setattr(
        twilio_handler, "verify_stream_token", lambda sid, token: True
    )
    monkeypatch.setattr(twilio_handler, "get_sessionmaker", lambda: sessionmaker_)

    captured: list[tuple[str, dict]] = []

    class _LogStub:
        def error(self, event, **kw):
            captured.append((event, kw))

        def warning(self, event, **kw):
            pass

        def info(self, event, **kw):
            pass

        def exception(self, event, **kw):
            pass

    monkeypatch.setattr(twilio_handler, "log", _LogStub())
    return captured


@pytest.mark.asyncio
async def test_startup_provider_failure_marks_the_call_failed(db, sessionmaker_, monkeypatch):
    from app.telephony import twilio_handler

    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    call.call_sid = "CA-providerfail"
    await db.commit()
    await db.refresh(call)

    async def _boom(*a, **kw):
        raise ProviderAuthenticationError(
            "invalid ElevenLabs key", provider="elevenlabs"
        )

    captured = _patch_media_stream(monkeypatch, sessionmaker_, _boom)
    await twilio_handler.media_stream(_FakeWebsocket(call.call_sid))

    await db.refresh(call)
    assert call.status is CallStatus.FAILED
    assert call.transfer_state is TransferState.NONE

    assert captured, "a startup failure must be logged"
    event, fields = captured[0]
    assert event == "call.crashed"
    assert fields["tenant_id"] == str(tenant.id)
    assert fields["provider"] == "elevenlabs"
    assert fields["category"] == "authentication_error"
    assert fields["retryable"] is False
    assert fields["error"] == "invalid ElevenLabs key"
    assert SENTINEL not in repr(fields)


@pytest.mark.asyncio
async def test_provider_failure_cannot_regress_a_terminal_call(db, sessionmaker_, monkeypatch):
    from app.telephony import twilio_handler

    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant, status=CallStatus.COMPLETED)
    call.call_sid = "CA-alreadydone"
    await db.commit()
    await db.refresh(call)

    async def _boom(*a, **kw):
        raise ProviderUnavailableError("deepgram down", provider="deepgram")

    _patch_media_stream(monkeypatch, sessionmaker_, _boom)
    await twilio_handler.media_stream(_FakeWebsocket(call.call_sid))

    await db.refresh(call)
    # A late crash must not overwrite an already-terminal call status.
    assert call.status is CallStatus.COMPLETED


@pytest.mark.asyncio
async def test_failed_finalisation_is_exactly_once(db):
    """The precondition for exactly-once billing/usage: the terminal
    transition applies once and only once."""
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)

    first = call_state.apply_status(call, CallStatus.FAILED, source="media_stream")
    second = call_state.apply_status(call, CallStatus.FAILED, source="media_stream")

    assert first.applied is True
    assert second.applied is False
    assert call.status is CallStatus.FAILED


# ----------------------------------------------------- 5. capabilities ----

def test_llm_capability_model_declares_tool_calling_for_every_provider():
    for provider in llm_factory.SUPPORTED_PROVIDERS:
        caps = llm_factory.llm_capabilities(provider)
        assert caps["supports_tool_calling"] is True
        assert caps["supports_streaming"] is True
        assert caps["supports_interruptions"] is True


def test_an_unknown_provider_has_no_capabilities():
    assert llm_factory.llm_capabilities("banana") == {}


def test_build_llm_rejects_a_provider_without_tool_calling(monkeypatch):
    """The agent always registers booking/escalation tools, so a provider
    that cannot tool-call must fail loudly instead of dropping the tools."""
    monkeypatch.setattr(settings, "openai_api_key", SENTINEL)
    stripped = {k: dict(v) for k, v in llm_factory.LLM_CAPABILITIES.items()}
    stripped["openai"] = {**stripped["openai"], "supports_tool_calling": False}
    monkeypatch.setattr(llm_factory, "LLM_CAPABILITIES", stripped)

    with pytest.raises(UnsupportedProviderFeatureError) as exc:
        llm_factory.build_llm("openai", "gpt-4o-mini")
    assert exc.value.category == "unsupported_feature"
    assert exc.value.retryable is False


def test_stt_capability_model_matches_the_tts_contract_style():
    assert stt.DEEPGRAM_CAPABILITIES["supports_streaming"] is True
    assert stt.DEEPGRAM_CAPABILITIES["supports_language"] is True
    assert stt.DEEPGRAM_CAPABILITIES["supports_interim_results"] is True
    assert stt.DEEPGRAM_CAPABILITIES["supports_pitch"] is False


# -------------------------------------------------- 6. observability -----

def test_provider_error_counter_uses_bounded_labels():
    # Tenant-controlled / arbitrary values must collapse to fixed labels,
    # never mint an unbounded series.
    before = metrics.PROVIDER_ERRORS.labels("other", "provider_error")._value.get()
    record_provider_error("some-tenant-controlled-string", "not-a-category")
    after = metrics.PROVIDER_ERRORS.labels("other", "provider_error")._value.get()
    assert after - before >= 1.0

    before = metrics.PROVIDER_ERRORS.labels(
        "elevenlabs", "authentication_error"
    )._value.get()
    record_provider_error("elevenlabs", "authentication_error")
    after = metrics.PROVIDER_ERRORS.labels(
        "elevenlabs", "authentication_error"
    )._value.get()
    assert after - before >= 1.0


# ------------------------------------------ 7. bounded handshake timeout --

class _SilentWebsocket(_FakeWebsocket):
    """Accepts the socket but never sends 'connected' + 'start'."""

    async def receive_text(self):
        await asyncio.sleep(3600)
        raise AssertionError("the handshake must have timed out")


@pytest.mark.asyncio
async def test_a_silent_handshake_is_bounded_and_closed(monkeypatch):
    from app.telephony import twilio_handler

    monkeypatch.setattr(settings, "stream_handshake_timeout_seconds", 0.05)
    ws = _SilentWebsocket("CA-silent")
    await twilio_handler.media_stream(ws)
    assert ws.closed_with == 1008


@pytest.mark.asyncio
async def test_a_normal_handshake_is_not_affected_by_the_bound(db, sessionmaker_, monkeypatch):
    """The timeout must never reject a well-behaved Twilio handshake."""
    from app.telephony import twilio_handler

    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    call.call_sid = "CA-normal"
    await db.commit()
    await db.refresh(call)

    async def _ok(*a, **kw):
        return None

    monkeypatch.setattr(settings, "stream_handshake_timeout_seconds", 30.0)
    _patch_media_stream(monkeypatch, sessionmaker_, _ok)
    ws = _FakeWebsocket(call.call_sid)
    await twilio_handler.media_stream(ws)
    assert ws.closed_with is None
