"""
TwiML generation for a warm transfer.

Twilio's live-call-update API rewrites the TwiML of a call that is already in
progress, which hangs up the media stream and dials the human instead.

This module is now the *TwiML builder* only. Orchestration -- validation,
idempotency, state transitions, callbacks -- lives in
`app.telephony.transfer_service`, and the provider call itself lives behind
`app.telephony.provider`. Keeping the three apart is what lets the transfer
logic be tested against a fake provider without mocking the application.

`execute_transfer` below is retained as a thin, low-level escape hatch. It does
NOT update call state; anything user-facing must go through the service.
"""
from __future__ import annotations

import structlog
from twilio.twiml.voice_response import Dial, VoiceResponse

from app.core.config import settings

log = structlog.get_logger()


def transfer_action_url() -> str:
    """Where Twilio reports the outcome of the <Dial> leg."""
    return f"{settings.public_base_url.rstrip('/')}/telephony/transfer-status"


def transfer_twiml(
    to_number: str,
    *,
    whisper: str = "",
    caller_id: str | None = None,
    timeout: int = 25,
    record: bool = False,
) -> str:
    """
    TwiML that dials the human, with an optional whisper to the human only.

    The `action` URL is what makes the transfer verifiable: Twilio posts the
    dial outcome there, and only that callback can move the call to
    "transfer connected". Without it we would be guessing.
    """
    response = VoiceResponse()
    if whisper:
        response.say(whisper, voice="Polly.Joanna")

    dial = Dial(
        timeout=timeout,
        caller_id=caller_id,
        answer_on_bridge=True,          # caller keeps hearing ringback, not silence
        record="record-from-answer-dual" if record else "do-not-record",
        action=transfer_action_url(),
        method="POST",
    )
    dial.number(to_number)
    response.append(dial)

    # If the human does not pick up, do not just drop the customer.
    response.say(
        "Sorry, no one is available right now. Please leave your name and number "
        "after the tone and we'll call you back."
    )
    response.record(max_length=90, play_beep=True, transcribe=False)
    response.hangup()
    return str(response)


def voicemail_twiml(prompt: str = "") -> str:
    response = VoiceResponse()
    response.say(prompt or "Please leave a message after the tone.")
    response.record(max_length=120, play_beep=True)
    response.hangup()
    return str(response)


def _client():
    from twilio.rest import Client
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


async def execute_transfer(
    call_sid: str,
    to_number: str,
    *,
    caller_id: str | None = None,
    whisper: str = "",
    record: bool = False,
) -> dict:
    """
    Low-level redirect. Returns a result dict, never raises.

    Prefer `transfer_service.request_transfer`, which also validates the
    destination, enforces idempotency and records call state. This function
    updates nothing in the database.
    """
    if not to_number:
        return {"ok": False, "reason": "no_escalation_number"}

    twiml = transfer_twiml(
        to_number, whisper=whisper, caller_id=caller_id, record=record
    )
    try:
        _client().calls(call_sid).update(twiml=twiml)
    except Exception as exc:
        log.error("transfer.failed", call_sid=call_sid, error=str(exc))
        return {"ok": False, "reason": "twilio_error", "error": str(exc)}

    log.info("transfer.executed", call_sid=call_sid, to=to_number)
    return {"ok": True, "to": to_number}
