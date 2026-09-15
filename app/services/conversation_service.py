"""Conversation service (Batch 01 enterprise expansion).

Wraps the existing ``Call``/``Turn`` persistence with the richer conversation
domain. Two principles govern this module:

* **The persisted state machine stays authoritative.** ``CallStatus`` and the
  telephony layer decide what actually happened on the wire; this service only
  *projects* that state onto the conversation domain and validates requested
  moves against the domain transition table before writing back. It never
  invents a state the provider did not reach, and it never re-finalises a call
  that the telephony layer already finalised.
* **Tenant scoping is unconditional.** Every query filters on ``tenant_id`` and
  every object access proves ownership first; a missing or foreign row is the
  same ``NotFoundError``.

Persistence honesty: sentiment, topic, satisfaction, tags, events, pause state
and assignment have no column yet and live in a per-tenant, in-process overlay.
They are not durable and are reported as a schema gap in the batch report.
``intent``, ``summary``, ``escalated``, transfer fields and status **are**
persisted on the real ``Call`` row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.messaging import get_or_start_thread
from app.core.errors import BadRequestError, NotFoundError
from app.core.logging import log
from app.db.models import Call, CallDirection, CallStatus, Speaker, Turn
from app.domain.conversation_models import (
    Conversation,
    ConversationChannel,
    ConversationEvent,
    ConversationState,
    ConversationSummary,
    EscalationState,
    ResolutionState,
    SatisfactionState,
    Sentiment,
    can_transition,
    now_iso,
)

#: tenant_id -> call_id -> non-column metadata (sentiment, topic, tags, ...).
_META: dict[str, dict[str, dict]] = {}

REOPEN_WINDOW_HOURS = 24


def _meta(tenant_id: str, call_id: str) -> dict:
    node: dict = _META
    return node.setdefault(tenant_id, {}).setdefault(call_id, {})


def _project_state(status: CallStatus, meta: dict) -> ConversationState:
    if meta.get("paused"):
        return ConversationState.PAUSED
    mapping = {
        CallStatus.RINGING: ConversationState.ACTIVE,
        CallStatus.IN_PROGRESS: ConversationState.ACTIVE,
        CallStatus.COMPLETED: ConversationState.COMPLETED,
        CallStatus.FAILED: ConversationState.FAILED,
        CallStatus.NO_ANSWER: ConversationState.ABANDONED,
        CallStatus.TRANSFERRED: ConversationState.TRANSFERRED,
    }
    return mapping.get(status, ConversationState.ACTIVE)


def _channel_from_intent(intent: str | None) -> ConversationChannel:
    value = intent or ""
    if value.startswith("chat:whatsapp"):
        return ConversationChannel.WHATSAPP
    if value.startswith("chat:"):
        return ConversationChannel.SMS
    if value == "web":
        return ConversationChannel.WEB
    return ConversationChannel.VOICE


def _ensure_owned(call: Call | None, tenant_id) -> Call:
    if call is None or str(call.tenant_id) != str(tenant_id):
        raise NotFoundError("conversation not found")
    return call


# ------------------------------------------------------------------- start ---

async def start_conversation(
    session: AsyncSession,
    tenant,
    *,
    channel: ConversationChannel,
    from_number: str,
    intent: str = "",
    direction: CallDirection = CallDirection.INBOUND,
) -> Call:
    """Create (or reuse, for text channels) a conversation's backing row."""
    if channel in (ConversationChannel.SMS, ConversationChannel.WHATSAPP):
        chat_channel = "whatsapp" if channel is ConversationChannel.WHATSAPP else "sms"
        return await get_or_start_thread(session, tenant, from_number, chat_channel)
    call = Call(
        tenant_id=tenant.id,
        call_sid=f"{channel.value}-{int(datetime.utcnow().timestamp()*1000)}",
        from_number=from_number,
        to_number=tenant.twilio_number,
        status=CallStatus.RINGING if direction is CallDirection.INBOUND else CallStatus.RINGING,
        direction=direction,
        intent=intent or channel.value,
    )
    session.add(call)
    await session.commit()
    await session.refresh(call)
    return call


async def append_turn(
    session: AsyncSession,
    tenant,
    call: Call,
    *,
    speaker: Speaker,
    text: str,
    latency_ms: float | None = None,
) -> Turn:
    """Append one transcript turn. Fails on cross-tenant call ids."""
    _ensure_owned(call, tenant.id)
    if not text or len(text) > 8_000:
        raise BadRequestError("turn text must be 1–8000 characters")
    turn = Turn(call_id=call.id, speaker=speaker, text=text, latency_ms=latency_ms)
    session.add(turn)
    await session.commit()
    await session.refresh(turn)
    return turn


# ------------------------------------------------------------------ views ---

def project(tenant, call: Call) -> Conversation:
    """Project a persisted call onto the conversation domain."""
    _ensure_owned(call, tenant.id)
    meta = _meta(str(tenant.id), str(call.id))
    channel = _channel_from_intent(call.intent)
    summary = ConversationSummary(
        short=call.summary or "",
        topics=tuple(meta.get("topics", ())),
        action_items=tuple(meta.get("action_items", ())),
    )
    escalation = _escalation_state(call, meta)
    state = _project_state(call.status, meta)
    return Conversation(
        id=str(call.id),
        tenant_id=str(tenant.id),
        channel=channel,
        state=state,
        sentiment=Sentiment(meta.get("sentiment", "unknown")),
        intent=call.intent or "",
        topic=meta.get("topic", ""),
        resolution=ResolutionState(meta.get("resolution", "unresolved")),
        escalation=escalation,
        satisfaction=SatisfactionState(meta.get("satisfaction", "unknown")),
        summary=summary,
        ai_confidence=meta.get("confidence"),
        tags=tuple(meta.get("tags", ())),
        events=tuple(ConversationEvent(**e) for e in meta.get("events", [])),
        started_at=call.started_at.isoformat() if call.started_at else "",
        ended_at=call.ended_at.isoformat() if call.ended_at else "",
    )


def _escalation_state(call: Call, meta: dict) -> EscalationState:
    if meta.get("escalation_state"):
        return EscalationState(meta["escalation_state"])
    if call.transfer_state is not None and call.transfer_state.value != "none":
        return EscalationState.IN_PROGRESS
    if call.escalated:
        return EscalationState.REQUESTED
    return EscalationState.NONE


# ----------------------------------------------------------------- updates ---

async def classify(
    session: AsyncSession,
    tenant,
    call: Call,
    *,
    intent: str = "",
    topic: str = "",
    sentiment: Sentiment = Sentiment.UNKNOWN,
    confidence: float | None = None,
) -> Conversation:
    """Persist intent (real column) and topic/sentiment (overlay)."""
    _ensure_owned(call, tenant.id)
    if not intent or len(intent) > 80:
        raise BadRequestError("intent must be 1–80 characters")
    call.intent = intent
    meta = _meta(str(tenant.id), str(call.id))
    meta["topic"] = topic[:120]
    meta["sentiment"] = sentiment.value
    meta["confidence"] = confidence
    session.add(call)
    await session.commit()
    return project(tenant, call)


async def update_sentiment(session: AsyncSession, tenant, call: Call, sentiment: Sentiment) -> Conversation:
    _ensure_owned(call, tenant.id)
    _meta(str(tenant.id), str(call.id))["sentiment"] = sentiment.value
    await session.commit()
    return project(tenant, call)


async def update_intent(session: AsyncSession, tenant, call: Call, intent: str) -> Conversation:
    return await classify(session, tenant, call, intent=intent)


def assign_agent(tenant, call: Call, assignee_id: str) -> Conversation:
    """Assign a team member (overlay — no assignee column exists yet)."""
    _ensure_owned(call, tenant.id)
    meta = _meta(str(tenant.id), str(call.id))
    meta["assignee_id"] = assignee_id
    return project(tenant, call)


async def escalate(
    session: AsyncSession,
    tenant,
    call: Call,
    *,
    destination: str,
    reason: str,
) -> Conversation:
    """Record an escalation intent. Does NOT dial — the transfer service owns
    the provider call. Writes the real ``escalated``/transfer columns."""
    _ensure_owned(call, tenant.id)
    if not reason or len(reason) > 400:
        raise BadRequestError("escalation reason must be 1–400 characters")
    call.escalated = True
    call.transfer_reason = reason
    call.transfer_destination = destination[:64]
    meta = _meta(str(tenant.id), str(call.id))
    meta["escalation_state"] = EscalationState.REQUESTED.value
    _record_event(meta, "escalation.requested", reason)
    session.add(call)
    await session.commit()
    log.info("conversation.escalation_requested", tenant_id=str(tenant.id),
             call_id=str(call.id))
    return project(tenant, call)


async def mark_transferred(session: AsyncSession, tenant, call: Call) -> Conversation:
    """Move an in-progress call to transferred (matches the telephony flow)."""
    _ensure_owned(call, tenant.id)
    current = _project_state(call.status, _meta(str(tenant.id), str(call.id)))
    if not can_transition(current, ConversationState.TRANSFERRED):
        raise BadRequestError(f"cannot transfer from {current.value}")
    call.status = CallStatus.TRANSFERRED
    call.escalated = True
    _meta(str(tenant.id), str(call.id))["escalation_state"] = EscalationState.IN_PROGRESS.value
    session.add(call)
    await session.commit()
    return project(tenant, call)


async def pause(session: AsyncSession, tenant, call: Call) -> Conversation:
    _ensure_owned(call, tenant.id)
    meta = _meta(str(tenant.id), str(call.id))
    if meta.get("paused"):
        return project(tenant, call)
    if not can_transition(_project_state(call.status, meta), ConversationState.PAUSED):
        raise BadRequestError("conversation cannot be paused in its current state")
    meta["paused"] = True
    _record_event(meta, "state.paused", "")
    await session.commit()
    return project(tenant, call)


async def resume(session: AsyncSession, tenant, call: Call) -> Conversation:
    _ensure_owned(call, tenant.id)
    meta = _meta(str(tenant.id), str(call.id))
    meta["paused"] = False
    _record_event(meta, "state.resumed", "")
    await session.commit()
    return project(tenant, call)


async def summarize(
    session: AsyncSession,
    tenant,
    call: Call,
    *,
    short: str,
    topics: tuple[str, ...] = (),
    action_items: tuple[str, ...] = (),
) -> Conversation:
    """Persist the summary (real column) plus topics/actions (overlay)."""
    _ensure_owned(call, tenant.id)
    if len(short) > 4_000:
        raise BadRequestError("summary must be at most 4000 characters")
    call.summary = short
    meta = _meta(str(tenant.id), str(call.id))
    meta["topics"] = list(topics)[:50]
    meta["action_items"] = list(action_items)[:50]
    session.add(call)
    await session.commit()
    return project(tenant, call)


async def close(session: AsyncSession, tenant, call: Call) -> Conversation:
    _ensure_owned(call, tenant.id)
    meta = _meta(str(tenant.id), str(call.id))
    current = _project_state(call.status, meta)
    if not can_transition(current, ConversationState.COMPLETED):
        raise BadRequestError(f"cannot close from {current.value}")
    call.status = CallStatus.COMPLETED
    call.ended_at = datetime.now(timezone.utc)
    meta["resolution"] = ResolutionState.RESOLVED.value
    _record_event(meta, "state.completed", "")
    session.add(call)
    await session.commit()
    return project(tenant, call)


async def fail(session: AsyncSession, tenant, call: Call, *, reason: str = "") -> Conversation:
    _ensure_owned(call, tenant.id)
    meta = _meta(str(tenant.id), str(call.id))
    if not can_transition(_project_state(call.status, meta), ConversationState.FAILED):
        raise BadRequestError("conversation cannot be failed in its current state")
    call.status = CallStatus.FAILED
    call.failure_reason = reason[:120]
    call.ended_at = datetime.now(timezone.utc)
    session.add(call)
    await session.commit()
    return project(tenant, call)


async def abandon(session: AsyncSession, tenant, call: Call) -> Conversation:
    _ensure_owned(call, tenant.id)
    meta = _meta(str(tenant.id), str(call.id))
    if not can_transition(_project_state(call.status, meta), ConversationState.ABANDONED):
        raise BadRequestError("conversation cannot be abandoned in its current state")
    call.status = CallStatus.NO_ANSWER
    call.ended_at = datetime.now(timezone.utc)
    session.add(call)
    await session.commit()
    return project(tenant, call)


async def reopen(session: AsyncSession, tenant, call: Call) -> Conversation:
    """Reopen a completed conversation, only within the policy window."""
    _ensure_owned(call, tenant.id)
    if call.status is not CallStatus.COMPLETED:
        raise BadRequestError("only completed conversations may be reopened")
    if call.ended_at is not None:
        elapsed = datetime.now(timezone.utc) - call.ended_at
        if elapsed > timedelta(hours=REOPEN_WINDOW_HOURS):
            raise BadRequestError("conversation is outside the reopen window")
    call.status = CallStatus.IN_PROGRESS
    call.ended_at = None
    session.add(call)
    await session.commit()
    return project(tenant, call)


def add_tag(tenant, call: Call, tag: str) -> Conversation:
    """Add a conversation tag (overlay; deduplicated)."""
    _ensure_owned(call, tenant.id)
    if not tag or len(tag) > 64:
        raise BadRequestError("tag must be 1–64 characters")
    meta = _meta(str(tenant.id), str(call.id))
    tags = list(meta.get("tags", ()))
    if tag not in tags:
        tags.append(tag)
    meta["tags"] = tags[-100:]
    return project(tenant, call)


def mark_resolved(tenant, call: Call) -> Conversation:
    """Mark the conversation resolved (overlay resolution flag)."""
    _ensure_owned(call, tenant.id)
    meta = _meta(str(tenant.id), str(call.id))
    meta["resolution"] = ResolutionState.RESOLVED.value
    _record_event(meta, "resolution.resolved", "")
    return project(tenant, call)


def _record_event(meta: dict, type_: str, note: str) -> None:
    events = meta.setdefault("events", [])
    events.append({"type": type_, "at": now_iso(), "actor": "system", "note": note[:2000]})
    meta["events"] = events[-100:]


# ------------------------------------------------------------------ search ---

async def search(
    session: AsyncSession,
    tenant,
    *,
    intent: str | None = None,
    from_number: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    state: ConversationState | None = None,
    limit: int = 100,
) -> list[Conversation]:
    """Tenant-scoped conversation search. Limits are clamped, never open."""
    limit = min(max(limit, 1), 500)
    stmt = select(Call).where(Call.tenant_id == tenant.id)
    if intent:
        stmt = stmt.where(Call.intent == intent)
    if from_number:
        stmt = stmt.where(Call.from_number == from_number)
    if since:
        stmt = stmt.where(Call.started_at >= since)
    if until:
        stmt = stmt.where(Call.started_at <= until)
    stmt = stmt.order_by(Call.started_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    conversations = [project(tenant, call) for call in rows]
    if state is not None:
        conversations = [c for c in conversations if c.state is state]
    return conversations


async def kpis(
    session: AsyncSession,
    tenant,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict:
    """Aggregate-safe call KPIs over the tenant's own calls."""
    conditions = [Call.tenant_id == tenant.id]
    if since:
        conditions.append(Call.started_at >= since)
    if until:
        conditions.append(Call.started_at <= until)

    total = (await session.execute(select(func.count()).select_from(Call).where(*conditions))).scalar() or 0
    answered = (await session.execute(
        select(func.count()).select_from(Call).where(*conditions, Call.status == CallStatus.COMPLETED)
    )).scalar() or 0
    failed = (await session.execute(
        select(func.count()).select_from(Call).where(*conditions, Call.status == CallStatus.FAILED)
    )).scalar() or 0
    no_answer = (await session.execute(
        select(func.count()).select_from(Call).where(*conditions, Call.status == CallStatus.NO_ANSWER)
    )).scalar() or 0
    transferred = (await session.execute(
        select(func.count()).select_from(Call).where(*conditions, Call.status == CallStatus.TRANSFERRED)
    )).scalar() or 0
    escalated = (await session.execute(
        select(func.count()).select_from(Call).where(*conditions, Call.escalated.is_(True))
    )).scalar() or 0
    avg_duration = (await session.execute(
        select(func.avg(Call.duration_seconds)).where(*conditions)
    )).scalar() or 0.0

    def pct(num: float, den: float) -> float:
        return round(num / den * 100, 1) if den else 0.0

    return {
        "total": total,
        "answered": answered,
        "completed": answered,
        "failed": failed,
        "no_answer": no_answer,
        "transferred": transferred,
        "escalated": escalated,
        "avg_duration_seconds": round(float(avg_duration), 2),
        "rates": {
            "answer_rate": pct(answered, total),
            "completion_rate": pct(answered, total),
            "failure_rate": pct(failed, total),
            "transfer_rate": pct(transferred, answered),
            "escalation_rate": pct(escalated, total),
        },
    }
