"""
Signed stream tokens for the Twilio Media Stream WebSocket.

The WebSocket at /telephony/ws is machine-to-machine: Twilio connects to it,
not a browser, so forcing a user JWT on it would break real calls. But it was
previously authenticated by nothing at all -- any client that guessed or
observed a `callSid` could connect and receive live call audio.

Twilio cannot sign a WebSocket upgrade the way it signs HTTP webhooks (there
is no X-Twilio-Signature on the upgrade request), so the accepted pattern is
to put a short-lived, server-minted token in the stream URL. We generate the
URL ourselves inside <Connect><Stream>, so only whoever already received our
TwiML can hold a valid token.

This is deliberately separate from `app/auth/jwt.py`: different audience,
different lifetime, and it must never be interchangeable with a user token.
"""
from __future__ import annotations

import hashlib
import hmac
import time

import structlog
from fastapi import Request

from app.core.config import settings

log = structlog.get_logger()

# Twilio dials, rings and connects the stream within seconds. Two minutes is
# generous and still leaves almost no replay window.
STREAM_TOKEN_TTL_SECONDS = 120
_PREFIX = "voxdesk-stream-v1"


def _sign(payload: str) -> str:
    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        f"{_PREFIX}:{payload}".encode(),
        hashlib.sha256,
    ).hexdigest()


def create_stream_token(call_sid: str, *, issued_at: int | None = None) -> str:
    """Returns '<issued_at>.<hex signature>' bound to this specific call."""
    issued = int(issued_at if issued_at is not None else time.time())
    return f"{issued}.{_sign(f'{call_sid}:{issued}')}"


def verify_stream_token(
    call_sid: str, token: str | None, *, now: int | None = None
) -> bool:
    """Constant-time verification. Any malformed input is simply a failure."""
    if not token or not call_sid:
        return False

    issued_raw, _, signature = token.partition(".")
    if not signature:
        return False
    try:
        issued = int(issued_raw)
    except ValueError:
        return False

    current = int(now if now is not None else time.time())
    # Reject expired tokens and tokens minted in the future (clock tampering).
    if not (-5 <= current - issued <= STREAM_TOKEN_TTL_SECONDS):
        return False

    return hmac.compare_digest(_sign(f"{call_sid}:{issued}"), signature)


async def verify_twilio_request(request: Request) -> bool:
    """
    Validate Twilio's X-Twilio-Signature HMAC on a webhook POST.

    This lives here, next to the stream token, because both the voice webhooks
    and the messaging webhooks need it and there must be exactly one
    implementation to review.

    Bypassed when APP_ENV=development so webhooks can be exercised with curl.
    Production must never run with the development flag -- the startup gate in
    app/main.py enforces the rest of the production configuration.
    """
    if settings.app_env == "development":
        return True

    from twilio.request_validator import RequestValidator

    validator = RequestValidator(settings.twilio_auth_token)
    form = await request.form()
    signature = request.headers.get("X-Twilio-Signature", "")
    return validator.validate(str(request.url), dict(form), signature)
