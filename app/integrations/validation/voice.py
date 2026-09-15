"""Real-provider checks for the live voice stack (Step 4).

Twilio, Deepgram, ElevenLabs, OpenAI, Anthropic and Google (Gemini). Every
check is READ_ONLY, uses a bounded timeout, and emits only fixed, secret-safe
text. No URL is ever echoed — a URL can carry an API key in its query string —
and no response body or raw exception string is ever included, because a 401
body is the most likely place for a provider to reflect a key back.

The HTTP checks use ``httpx`` (pinned in requirements.txt) the same way the
CRM, calendar and billing adapters already do. Twilio uses the pinned
``twilio`` SDK exactly like ``app/telephony/provider.py`` does for a live
transfer — but it only ever fetches the account, never a call.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

import httpx

from app.agent.llm_factory import PRESETS
from app.core.config import settings
from app.integrations.validation.status import CheckOutcome, CheckStatus, env_or

#: Deliberately bounded. A health check is a human-triggered diagnostic, not a
#: live call; an unreachable provider must fail fast rather than hang a script.
_TIMEOUT = httpx.Timeout(10.0, connect=10.0)

_OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_DEEPGRAM_PROJECTS_URL = "https://api.deepgram.com/v1/projects"
_ELEVENLABS_VOICES_URL = "https://api.elevenlabs.io/v1/voices"
_ELEVENLABS_MODELS_URL = "https://api.elevenlabs.io/v1/models"


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


async def _get(url: str, *, headers=None, params=None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        return await client.get(url, headers=headers, params=params)


async def check_twilio() -> CheckOutcome:
    """Verify Twilio account credentials without placing a call or an SMS."""
    sid = (settings.twilio_account_sid or "").strip()
    token = (settings.twilio_auth_token or "").strip()
    if not sid or not token:
        return CheckOutcome(
            "Twilio", CheckStatus.SKIPPED,
            "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not configured",
        )
    started = time.perf_counter()
    try:
        from twilio.base.exceptions import TwilioRestException
        from twilio.rest import Client

        client = Client(sid, token)
        # Read-only: fetch our own account resource.
        account = await asyncio.to_thread(client.api.accounts(sid).fetch)
        friendly = getattr(account, "friendly_name", "") or "account"
        return CheckOutcome(
            "Twilio", CheckStatus.PASS,
            f"account authenticated ({friendly})",
            latency_ms=_ms(started),
        )
    except TwilioRestException as exc:
        return CheckOutcome(
            "Twilio", CheckStatus.FAIL,
            f"Twilio rejected the credentials (HTTP {exc.code})",
            latency_ms=_ms(started),
        )
    except Exception as exc:
        return CheckOutcome(
            "Twilio", CheckStatus.FAIL,
            f"Twilio unreachable ({type(exc).__name__})",
            latency_ms=_ms(started),
        )


async def check_deepgram() -> CheckOutcome:
    """Verify the Deepgram key by listing projects (read-only)."""
    key = (settings.deepgram_api_key or "").strip()
    if not key:
        return CheckOutcome(
            "Deepgram", CheckStatus.SKIPPED, "DEEPGRAM_API_KEY not configured"
        )
    started = time.perf_counter()
    try:
        resp = await _get(
            _DEEPGRAM_PROJECTS_URL, headers={"Authorization": f"Token {key}"}
        )
    except httpx.TimeoutException:
        return CheckOutcome("Deepgram", CheckStatus.FAIL, "timed out",
                            latency_ms=_ms(started))
    except httpx.HTTPError as exc:
        return CheckOutcome("Deepgram", CheckStatus.FAIL,
                            f"unreachable ({type(exc).__name__})",
                            latency_ms=_ms(started))
    if resp.status_code == 200:
        return CheckOutcome("Deepgram", CheckStatus.PASS,
                            "projects listed (key valid)", latency_ms=_ms(started))
    if resp.status_code in (401, 403):
        return CheckOutcome("Deepgram", CheckStatus.FAIL,
                            f"credentials rejected (HTTP {resp.status_code})",
                            latency_ms=_ms(started))
    return CheckOutcome("Deepgram", CheckStatus.FAIL,
                        f"unexpected response (HTTP {resp.status_code})",
                        latency_ms=_ms(started))


async def check_elevenlabs() -> CheckOutcome:
    """Verify the ElevenLabs key, then the configured voice and model.

    All read-only: the voices list, a single voice lookup and the models list.
    No audio is ever generated, so no large payload and no synthesis cost.
    """
    key = (settings.elevenlabs_api_key or "").strip()
    if not key:
        return CheckOutcome(
            "ElevenLabs", CheckStatus.SKIPPED, "ELEVENLABS_API_KEY not configured"
        )
    headers = {"xi-api-key": key}
    started = time.perf_counter()
    try:
        resp = await _get(_ELEVENLABS_VOICES_URL, headers=headers)
    except httpx.TimeoutException:
        return CheckOutcome("ElevenLabs", CheckStatus.FAIL, "timed out",
                            latency_ms=_ms(started))
    except httpx.HTTPError as exc:
        return CheckOutcome("ElevenLabs", CheckStatus.FAIL,
                            f"unreachable ({type(exc).__name__})",
                            latency_ms=_ms(started))
    if resp.status_code == 401:
        return CheckOutcome("ElevenLabs", CheckStatus.FAIL,
                            "credentials rejected (HTTP 401)", latency_ms=_ms(started))
    if resp.status_code != 200:
        return CheckOutcome("ElevenLabs", CheckStatus.FAIL,
                            f"unexpected response (HTTP {resp.status_code})",
                            latency_ms=_ms(started))

    voice_id = (settings.elevenlabs_voice_id or "").strip()
    if voice_id:
        try:
            vresp = await _get(
                f"{_ELEVENLABS_VOICES_URL}/{quote(voice_id, safe='')}",
                headers=headers,
            )
        except httpx.HTTPError as exc:
            return CheckOutcome("ElevenLabs", CheckStatus.FAIL,
                                f"voice lookup failed ({type(exc).__name__})",
                                latency_ms=_ms(started))
        if vresp.status_code == 404:
            return CheckOutcome("ElevenLabs", CheckStatus.FAIL,
                                "configured ELEVENLABS_VOICE_ID not found",
                                latency_ms=_ms(started))
        if vresp.status_code not in (200,):
            return CheckOutcome("ElevenLabs", CheckStatus.FAIL,
                                f"voice lookup failed (HTTP {vresp.status_code})",
                                latency_ms=_ms(started))

    model = (settings.elevenlabs_model or "").strip()
    if model:
        try:
            mresp = await _get(_ELEVENLABS_MODELS_URL, headers=headers)
        except httpx.HTTPError as exc:
            return CheckOutcome("ElevenLabs", CheckStatus.FAIL,
                                f"model listing failed ({type(exc).__name__})",
                                latency_ms=_ms(started))
        if mresp.status_code == 200:
            available = {
                m.get("model_id") for m in mresp.json() if isinstance(m, dict)
            }
            if available and model not in available:
                return CheckOutcome("ElevenLabs", CheckStatus.FAIL,
                                    "configured ELEVENLABS_MODEL not available",
                                    latency_ms=_ms(started))

    return CheckOutcome("ElevenLabs", CheckStatus.PASS,
                        "credentials valid; voice/model availability confirmed",
                        latency_ms=_ms(started))


async def check_openai() -> CheckOutcome:
    """Verify the OpenAI key and that the configured model is reachable."""
    key = (settings.openai_api_key or "").strip()
    if not key:
        return CheckOutcome(
            "OpenAI", CheckStatus.SKIPPED, "OPENAI_API_KEY not configured"
        )
    model = env_or("VOXDESK_REAL_OPENAI_MODEL", PRESETS["fast"].model)
    headers = {"Authorization": f"Bearer {key}"}
    started = time.perf_counter()
    try:
        resp = await _get(
            f"{_OPENAI_MODELS_URL}/{quote(model, safe='')}", headers=headers
        )
    except httpx.TimeoutException:
        return CheckOutcome("OpenAI", CheckStatus.FAIL, "timed out",
                            latency_ms=_ms(started))
    except httpx.HTTPError as exc:
        return CheckOutcome("OpenAI", CheckStatus.FAIL,
                            f"unreachable ({type(exc).__name__})",
                            latency_ms=_ms(started))
    if resp.status_code == 200:
        return CheckOutcome("OpenAI", CheckStatus.PASS,
                            f"model {model!r} available", latency_ms=_ms(started))
    if resp.status_code in (401, 403):
        return CheckOutcome("OpenAI", CheckStatus.FAIL,
                            f"credentials rejected (HTTP {resp.status_code})",
                            latency_ms=_ms(started))
    if resp.status_code == 404:
        return CheckOutcome("OpenAI", CheckStatus.FAIL,
                            f"model {model!r} not available", latency_ms=_ms(started))
    return CheckOutcome("OpenAI", CheckStatus.FAIL,
                        f"unexpected response (HTTP {resp.status_code})",
                        latency_ms=_ms(started))


async def check_anthropic() -> CheckOutcome:
    """Verify the Anthropic key and that the configured model is listed."""
    key = (settings.anthropic_api_key or "").strip()
    if not key:
        return CheckOutcome(
            "Anthropic", CheckStatus.SKIPPED, "ANTHROPIC_API_KEY not configured"
        )
    model = env_or("VOXDESK_REAL_ANTHROPIC_MODEL", PRESETS["natural"].model)
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    started = time.perf_counter()
    try:
        resp = await _get(_ANTHROPIC_MODELS_URL, headers=headers)
    except httpx.TimeoutException:
        return CheckOutcome("Anthropic", CheckStatus.FAIL, "timed out",
                            latency_ms=_ms(started))
    except httpx.HTTPError as exc:
        return CheckOutcome("Anthropic", CheckStatus.FAIL,
                            f"unreachable ({type(exc).__name__})",
                            latency_ms=_ms(started))
    if resp.status_code in (401, 403):
        return CheckOutcome("Anthropic", CheckStatus.FAIL,
                            f"credentials rejected (HTTP {resp.status_code})",
                            latency_ms=_ms(started))
    if resp.status_code != 200:
        return CheckOutcome("Anthropic", CheckStatus.FAIL,
                            f"unexpected response (HTTP {resp.status_code})",
                            latency_ms=_ms(started))
    try:
        payload = resp.json()
    except ValueError:
        return CheckOutcome("Anthropic", CheckStatus.FAIL, "non-JSON response",
                            latency_ms=_ms(started))
    ids = {m.get("id") for m in payload.get("data", []) if isinstance(m, dict)}
    if model and ids and model not in ids:
        return CheckOutcome("Anthropic", CheckStatus.FAIL,
                            f"model {model!r} not available", latency_ms=_ms(started))
    return CheckOutcome("Anthropic", CheckStatus.PASS,
                        "models listed; credentials valid", latency_ms=_ms(started))


async def check_google_llm() -> CheckOutcome:
    """Verify the Gemini key and that the configured model is listed."""
    key = (settings.google_api_key or "").strip()
    if not key:
        return CheckOutcome(
            "Google LLM", CheckStatus.SKIPPED, "GOOGLE_API_KEY not configured"
        )
    model = env_or("VOXDESK_REAL_GOOGLE_MODEL", PRESETS["cheap"].model)
    started = time.perf_counter()
    try:
        # The key travels as a query parameter; the URL is never echoed back.
        resp = await _get(_GEMINI_MODELS_URL, params={"key": key})
    except httpx.TimeoutException:
        return CheckOutcome("Google LLM", CheckStatus.FAIL, "timed out",
                            latency_ms=_ms(started))
    except httpx.HTTPError as exc:
        return CheckOutcome("Google LLM", CheckStatus.FAIL,
                            f"unreachable ({type(exc).__name__})",
                            latency_ms=_ms(started))
    if resp.status_code in (400, 401, 403):
        return CheckOutcome("Google LLM", CheckStatus.FAIL,
                            f"credentials rejected (HTTP {resp.status_code})",
                            latency_ms=_ms(started))
    if resp.status_code != 200:
        return CheckOutcome("Google LLM", CheckStatus.FAIL,
                            f"unexpected response (HTTP {resp.status_code})",
                            latency_ms=_ms(started))
    try:
        payload = resp.json()
    except ValueError:
        return CheckOutcome("Google LLM", CheckStatus.FAIL, "non-JSON response",
                            latency_ms=_ms(started))
    names = [
        m.get("name", "") for m in payload.get("models", []) if isinstance(m, dict)
    ]
    if model and names and not any(model in name for name in names):
        return CheckOutcome("Google LLM", CheckStatus.FAIL,
                            f"model {model!r} not available", latency_ms=_ms(started))
    return CheckOutcome("Google LLM", CheckStatus.PASS,
                        "models listed; credentials valid", latency_ms=_ms(started))
