"""Owner notifications. The SMS summary is what makes the client feel the value."""
from __future__ import annotations

import asyncio

from app.core.config import settings
from app.core.logging import log
from app.core.metrics import record_side_effect
from app.telephony import phone

_client = None


def _get_client():
    """lazy import -- twilio ছাড়াই টেস্ট চালানো যায়।"""
    from twilio.rest import Client

    global _client
    if _client is None:
        _client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    return _client


async def send_sms(to: str, body: str) -> bool:
    def _call():
        _get_client().messages.create(
            to=to, from_=settings.twilio_phone_number, body=body[:1500]
        )

    record_side_effect("sms", "attempt")
    try:
        await asyncio.to_thread(_call)
        log.info("sms.sent", to=phone.redact(to))
        record_side_effect("sms", "success")
        return True
    except Exception as exc:
        # `exc` can quote a Twilio payload; it goes nowhere near the log. The
        # destination is redacted, never a full phone number.
        log.error("sms.failed", to=phone.redact(to), error=type(exc).__name__)
        record_side_effect("sms", "failure")
        return False
