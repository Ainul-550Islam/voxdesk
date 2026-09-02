"""
WhatsApp + SMS conversational channels over Twilio.

Mk Solution and Exousia both advertise "WhatsApp & Phone Integration". This is
that feature, and it is genuinely valuable: a caller who hangs up is gone, but
a text thread survives. Booking-by-text converts the callers who would not
have left a voicemail.

Both channels are the same Twilio webhook shape, so they share one handler and
differ only in the `whatsapp:` address prefix and the reply length budget.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import structlog

from app.telephony.stream_auth import verify_twilio_request
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.functions import FunctionHandlers
from app.agent.text_agent import TextAgent
from app.db.models import (
    Call, CallDirection, CallStatus, Lead, LeadStatus, Speaker, Tenant, Turn,
)
from app.db.session import get_session

log = structlog.get_logger()
router = APIRouter(prefix="/channels", tags=["channels"])

WHATSAPP_PREFIX = "whatsapp:"
THREAD_IDLE_MINUTES = 30      # after this, treat the next message as a new thread
MAX_HISTORY_TURNS = 20

# Carrier-level keywords. These are legally significant: STOP must always work,
# and must work before the LLM ever sees the message.
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "revoke", "optout"}
START_WORDS = {"start", "unstop", "yes", "subscribe", "optin"}
HELP_WORDS = {"help", "info"}


# ---------------------------------------------------------------- helpers ---

def strip_channel_prefix(address: str) -> str:
    """'whatsapp:+15551234567' -> '+15551234567'"""
    return address[len(WHATSAPP_PREFIX):] if address.startswith(WHATSAPP_PREFIX) else address


def detect_channel(address: str) -> str:
    return "whatsapp" if address.startswith(WHATSAPP_PREFIX) else "sms"


def normalize_keyword(body: str) -> str:
    return re.sub(r"[^a-z]", "", (body or "").strip().lower())


def twiml_reply(message: str | None) -> str:
    """Empty <Response/> means 'received, say nothing' -- valid and silent."""
    from twilio.twiml.messaging_response import MessagingResponse
    resp = MessagingResponse()
    if message:
        resp.message(message)
    return str(resp)


def opt_out_confirmation(business: str) -> str:
    return f"You've been unsubscribed from {business}. Reply START to opt back in."


def help_text(business: str) -> str:
    return (
        f"{business}: reply with your question and our assistant will help. "
        f"Reply STOP to unsubscribe."
    )


# ------------------------------------------------------------- thread state ---

async def get_or_start_thread(
    session: AsyncSession, tenant: Tenant, customer: str, channel: str
) -> Call:
    """
    Text conversations are stored as Calls with duration 0 so that every
    analytics query, CRM push and transcript view already works unchanged.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=THREAD_IDLE_MINUTES)
    stmt = (
        select(Call)
        .where(
            Call.tenant_id == tenant.id,
            Call.from_number == customer,
            Call.intent == f"chat:{channel}",
            Call.started_at >= cutoff,
        )
        .order_by(Call.started_at.desc())
        .limit(1)
    )
    thread = (await session.execute(stmt)).scalars().first()
    if thread is not None:
        return thread

    thread = Call(
        tenant_id=tenant.id,
        call_sid=f"{channel}-{customer}-{int(datetime.utcnow().timestamp())}",
        from_number=customer,
        to_number=tenant.twilio_number,
        status=CallStatus.IN_PROGRESS,
        direction=CallDirection.INBOUND,
        intent=f"chat:{channel}",
    )
    session.add(thread)
    await session.commit()
    await session.refresh(thread)
    return thread


async def load_history(session: AsyncSession, thread: Call) -> list[dict]:
    stmt = (
        select(Turn)
        .where(Turn.call_id == thread.id)
        .order_by(Turn.created_at)
        .limit(MAX_HISTORY_TURNS)
    )
    turns = (await session.execute(stmt)).scalars().all()
    return [
        {
            "role": "user" if t.speaker is Speaker.USER else "assistant",
            "content": t.text,
        }
        for t in turns
    ]


async def save_turn(session: AsyncSession, thread: Call, speaker: Speaker, text: str) -> None:
    session.add(Turn(call_id=thread.id, speaker=speaker, text=text))
    await session.commit()


async def set_opt_out(session: AsyncSession, tenant: Tenant, phone: str, out: bool) -> None:
    lead = (
        await session.execute(
            select(Lead).where(Lead.tenant_id == tenant.id, Lead.phone == phone)
        )
    ).scalars().first()
    if lead is None:
        lead = Lead(tenant_id=tenant.id, phone=phone, name="")
        session.add(lead)
    lead.status = LeadStatus.DNC if out else LeadStatus.NEW
    await session.commit()
    log.info("channel.opt_change", phone=phone, opted_out=out)


# ------------------------------------------------------------------ webhook ---

@router.post("/message", response_class=PlainTextResponse)
async def inbound_message(
    request: Request,
    From: str = Form(...),
    To: str = Form(...),
    Body: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    """Single webhook for both SMS and WhatsApp. Point both Twilio numbers here."""
    # Without this, anyone could POST a fake inbound message and make the
    # tenant's LLM answer it -- an unauthenticated way to spend their money.
    if not await verify_twilio_request(request):
        return PlainTextResponse("forbidden", status_code=403)

    channel = detect_channel(From)
    customer = strip_channel_prefix(From)
    business_number = strip_channel_prefix(To)

    tenant = (
        await session.execute(select(Tenant).where(Tenant.twilio_number == business_number))
    ).scalar_one_or_none()
    if tenant is None or not tenant.is_active:
        return PlainTextResponse(twiml_reply(None), media_type="application/xml")

    keyword = normalize_keyword(Body)

    # 1) Compliance keywords are handled before the LLM, always.
    if keyword in STOP_WORDS:
        await set_opt_out(session, tenant, customer, True)
        return PlainTextResponse(
            twiml_reply(opt_out_confirmation(tenant.name)), media_type="application/xml"
        )
    if keyword in START_WORDS and len(keyword) > 2:
        await set_opt_out(session, tenant, customer, False)
        return PlainTextResponse(
            twiml_reply(f"You're subscribed to {tenant.name} again."),
            media_type="application/xml",
        )
    if keyword in HELP_WORDS:
        return PlainTextResponse(
            twiml_reply(help_text(tenant.name)), media_type="application/xml"
        )

    # 2) Respect an existing opt-out even if they text something else.
    existing = (
        await session.execute(
            select(Lead).where(Lead.tenant_id == tenant.id, Lead.phone == customer)
        )
    ).scalars().first()
    if existing is not None and existing.status is LeadStatus.DNC:
        return PlainTextResponse(twiml_reply(None), media_type="application/xml")

    # 3) Normal conversation.
    thread = await get_or_start_thread(session, tenant, customer, channel)
    history = await load_history(session, thread)
    await save_turn(session, thread, Speaker.USER, Body)

    handlers = FunctionHandlers(session, tenant, thread)
    agent = TextAgent(tenant, handlers, channel=channel)

    try:
        result = await agent.reply(history, Body)
        answer = result["reply"]
        thread.llm_used = f"{result['provider']}/{result['model']}"
    except Exception as exc:
        log.error("channel.agent_failed", error=str(exc), channel=channel)
        answer = (
            "Sorry, I'm having trouble right now. "
            "Someone from our team will get back to you shortly."
        )

    await save_turn(session, thread, Speaker.ASSISTANT, answer)
    return PlainTextResponse(twiml_reply(answer), media_type="application/xml")


@router.post("/status", response_class=PlainTextResponse)
async def message_status(
    request: Request,
    MessageStatus: str = Form(""),
    MessageSid: str = Form(""),
):
    """Twilio delivery receipts. Undelivered SMS is the #1 silent failure."""
    if not await verify_twilio_request(request):
        return PlainTextResponse("forbidden", status_code=403)

    if MessageStatus in {"failed", "undelivered"}:
        log.warning("message.undelivered", sid=MessageSid, status=MessageStatus)
    return PlainTextResponse("", media_type="application/xml")

