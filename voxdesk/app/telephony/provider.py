"""
Telephony provider abstraction.

The transfer service must be testable against *real application code* -- the
same service, the same session, the same rows -- without dialling anyone. The
only thing that genuinely cannot run in a test is the outbound HTTP call to
Twilio, so that is the only thing behind an interface.

This deliberately does NOT abstract the whole telephony layer. Wrapping
everything would let a test pass while the real integration is broken, which is
the failure mode these tests exist to catch.

`FakeTelephonyProvider` lives here rather than in `tests/` so the fake and the
real client are forced to stay the same shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import structlog

from app.core.config import settings

log = structlog.get_logger()


@dataclass(frozen=True)
class RedirectResult:
    """Outcome of asking the provider to rewrite a live call's TwiML."""

    ok: bool
    #: Provider-side error code, e.g. Twilio's 20404 (call not in progress).
    error_code: str | None = None
    #: Raw provider message. Server-side diagnostics only -- never sent to a
    #: caller and never returned to the LLM.
    error_message: str | None = None


class TelephonyProvider(Protocol):
    """The one operation a live transfer needs."""

    async def redirect_call(self, call_sid: str, twiml: str) -> RedirectResult:
        ...


# ------------------------------------------------------------------ twilio ---

class TwilioProvider:
    """
    Real Twilio. Rewrites the TwiML of an in-progress call, which tears down
    our media stream and dials the human instead.

    The SDK client is built lazily and per call: the credentials can change at
    runtime and a module-level client would pin the first values it ever saw.
    """

    async def redirect_call(self, call_sid: str, twiml: str) -> RedirectResult:
        try:
            from twilio.base.exceptions import TwilioRestException
            from twilio.rest import Client
        except ImportError as exc:      # pragma: no cover - deployment issue
            return RedirectResult(False, "sdk_missing", str(exc))

        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        try:
            client.calls(call_sid).update(twiml=twiml)
        except TwilioRestException as exc:
            # 20404 here almost always means the call already ended.
            return RedirectResult(False, str(exc.code), exc.msg)
        except Exception as exc:
            return RedirectResult(False, "provider_error", str(exc))

        return RedirectResult(True)


# -------------------------------------------------------------------- fake ---

@dataclass
class FakeTelephonyProvider:
    """
    In-memory provider for integration tests.

    It records what it was asked to do so a test can assert on the *TwiML we
    actually generated*, and it can be told to fail so the failure paths run
    for real instead of being mocked away.
    """

    #: Set to force every redirect to fail.
    fail_with: tuple[str, str] | None = None
    #: Recorded (call_sid, twiml) pairs, in order.
    redirects: list[tuple[str, str]] = field(default_factory=list)

    async def redirect_call(self, call_sid: str, twiml: str) -> RedirectResult:
        self.redirects.append((call_sid, twiml))
        if self.fail_with is not None:
            code, message = self.fail_with
            return RedirectResult(False, code, message)
        return RedirectResult(True)

    # -- convenience for assertions ---------------------------------------

    @property
    def call_count(self) -> int:
        return len(self.redirects)

    def last_twiml(self) -> str | None:
        return self.redirects[-1][1] if self.redirects else None


# ----------------------------------------------------------------- default ---

_default: TelephonyProvider | None = None


def get_provider() -> TelephonyProvider:
    """The provider used by production code paths."""
    global _default
    if _default is None:
        _default = TwilioProvider()
    return _default


def set_provider(provider: TelephonyProvider | None) -> None:
    """Swap the provider. Used by tests; `None` restores the real one."""
    global _default
    _default = provider