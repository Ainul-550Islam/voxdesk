"""Owner notifications. The SMS summary is what makes the client feel the value."""
from __future__ import annotations

import asyncio

from app.core.config import settings
from app.core.logging import log

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

    try:
        await asyncio.to_thread(_call)
        log.info("sms.sent", to=to)
        return True
    except Exception as exc:
        log.error("sms.failed", to=to, error=str(exc))
        return False

