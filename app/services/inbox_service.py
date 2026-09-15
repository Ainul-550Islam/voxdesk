"""Unified inbox service (Batch 01 enterprise expansion).

The inbox is a tenant-scoped view over the existing conversation storage —
voice calls and text threads remain ``Call`` rows with ``Turn`` children, and
the existing ``/channels/message`` webhook keeps writing them unchanged. This
service overlays the inbox-only state (priority, assignment, tags, internal
notes, read/unread, SLA) on top, and maps messages onto ``Turn`` rows so every
analytics and transcript view keeps working.

Persistence honesty: assignment, priority, tags, notes, read/unread and SLA
timers have no columns yet and live in a per-tenant overlay (schema gap,
reported at the end of the batch). Thread existence, messages, escalation flag
and status are persisted on the real ``Call``/``Turn`` rows.

Hard rules: no cross-tenant thread visibility (every query filters tenant_id
and every access proves ownership), and message ordering is monotonic.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.messaging import get_or_start_thread
from app.core.errors import BadRequestError, NotFoundError
from app.db.models import Call, CallDirection, CallStatus, Speaker, Turn
from app.domain.inbox_models import (
    InboxChannel,
    Message,
    MessageDirection,
    SlaTimer,
    Thread,
    ThreadPriority,
    ThreadStatus,
    can_transition,
    now_iso,
    reopen_allowed,
    thread_id,
)

#: tenant_id -> call_id -> inbox-only state
_OVERLAY: dict[str, dict[str, dict]] = {}

FIRST_RESPONSE_SLA_SECONDS = 300
RESOLUTION_SLA_HOURS = 24


def _slot(store: dict, *keys: str) -> dict:
    node: dict = store
    for key in keys:
        node = node.setdefault(key, {})
    return node


def _overlay(tenant_id, call_id) -> dict:
    return _slot(_OVERLAY, str(tenant_id), str(call_id))


def _ensure_owned(call: Call | None, tenant_id) -> Call:
    if call is None or str(call.tenant_id) != str(tenant_id):
        raise NotFoundError("thread not found")
    return call


def _channel_of(call: Call) -> InboxChannel:
    intent = call.intent or ""
    if intent.startswith("chat:whatsapp"):
        return InboxChannel.WHATSAPP
    if intent.startswith("chat:"):
        return InboxChannel.SMS
    if intent == "chat:web":
        return InboxChannel.WEB
    if intent == "chat:crm":
        return InboxChannel.CRM
    return InboxChannel.VOICE


def _speaker_for(direction: MessageDirection) -> Speaker:
    if direction is MessageDirection.INBOUND:
        return Speaker.USER
    return Speaker.ASSISTANT


def _thread_status(call: Call, overlay: dict) -> ThreadStatus:
    explicit = overlay.get("status")
    if explicit:
        return ThreadStatus(explicit)
    if call.status is CallStatus.COMPLETED:
        return ThreadStatus.CLOSED
    if call.escalated:
        return ThreadStatus.ESCALATED
    if overlay.get("assignee_id"):
        return ThreadStatus.ASSIGNED
    return ThreadStatus.OPEN


# --------------------------------------------------------------- threads ---

async def find_or_create_thread(
    session: AsyncSession,
    tenant,
    *,
    channel: InboxChannel,
    customer: str,
    initial_message: str = "",
) -> Call:
    """Create (or reuse, for text channels) a thread's backing call row."""
    if channel in (InboxChannel.SMS, InboxChannel.WHATSAPP):
        chat = "whatsapp" if channel is InboxChannel.WHATSAPP else "sms"
        thread = await get_or_start_thread(session, tenant, customer, chat)
        if initial_message:
            await append_message(session, tenant, thread, direction=MessageDirection.INBOUND,
                                 body=initial_message)
        return thread
    call = Call(
        tenant_id=tenant.id,
        call_sid=f"inbox-{int(datetime.utcnow().timestamp()*1000)}",
        from_number=customer,
        to_number=tenant.twilio_number,
        status=CallStatus.IN_PROGRESS,
        direction=CallDirection.INBOUND,
        intent=f"chat:{channel.value}",
    )
    session.add(call)
    await session.commit()
    await session.refresh(call)
    _overlay(tenant.id, call.id).update({
        "opened_at": now_iso(),
        "sla_deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=FIRST_RESPONSE_SLA_SECONDS)).isoformat(),
    })
    if initial_message:
        await append_message(session, tenant, call, direction=MessageDirection.INBOUND,
                             body=initial_message)
    return call


async def append_message(
    session: AsyncSession,
    tenant,
    thread: Call,
    *,
    direction: MessageDirection,
    body: str,
    author_role: str = "customer",
) -> Message:
    """Append a message. Notes go to the overlay; messages go to real Turns."""
    _ensure_owned(thread, tenant.id)
    if not body or len(body) > 8_000:
        raise BadRequestError("message body must be 1–8000 characters")
    if direction is MessageDirection.INTERNAL_NOTE:
        notes = _overlay(tenant.id, thread.id).setdefault("notes", [])
        notes.append({"body": body, "at": now_iso(), "author": author_role})
        sequence = await _sequence_of(session, thread)
        return Message(
            id=str(uuid.uuid4()),
            tenant_id=str(tenant.id),
            thread_id=str(thread.id),
            direction=direction,
            channel=_channel_of(thread),
            author_role=author_role,
            body=body,
            sequence=sequence,
            sent_at=now_iso(),
        )
    turn = Turn(call_id=thread.id, speaker=_speaker_for(direction), text=body)
    session.add(turn)
    await session.commit()
    await session.refresh(turn)
    sequence = await _sequence_of(session, thread)
    return Message(
        id=str(turn.id),
        tenant_id=str(tenant.id),
        thread_id=str(thread.id),
        direction=direction,
        channel=_channel_of(thread),
        author_role=author_role,
        body=body,
        sequence=sequence,
        sent_at=turn.created_at.isoformat() if turn.created_at else now_iso(),
    )


async def _sequence_of(session: AsyncSession, thread: Call) -> int:
    """Monotonic message sequence: persisted turns + overlay notes."""
    turns = (await session.execute(
        select(func.count()).select_from(Turn).where(Turn.call_id == thread.id)
    )).scalar() or 0
    notes = len(_overlay(thread.tenant_id, thread.id).get("notes", []))
    return int(turns) + notes


# --------------------------------------------------------------- projection ---

async def project(session: AsyncSession, tenant, thread: Call) -> Thread:
    """Build the domain thread from the persisted call + overlay + turns."""
    _ensure_owned(thread, tenant.id)
    overlay = _overlay(tenant.id, thread.id)
    turns = (await session.execute(
        select(Turn).where(Turn.call_id == thread.id).order_by(Turn.created_at)
    )).scalars().all()
    messages: list[Message] = []
    for index, turn in enumerate(turns, start=1):
        direction = MessageDirection.INBOUND if turn.speaker is Speaker.USER else MessageDirection.OUTBOUND
        messages.append(Message(
            id=str(turn.id),
            tenant_id=str(tenant.id),
            thread_id=str(thread.id),
            direction=direction,
            channel=_channel_of(thread),
            author_role="customer" if direction is MessageDirection.INBOUND else "assistant",
            body=turn.text or "",
            sequence=index,
            sent_at=turn.created_at.isoformat() if turn.created_at else "",
        ))
    for note in overlay.get("notes", []):
        messages.append(Message(
            id=str(uuid.uuid4()),
            tenant_id=str(tenant.id),
            thread_id=str(thread.id),
            direction=MessageDirection.INTERNAL_NOTE,
            channel=_channel_of(thread),
            author_role=note.get("author", "agent"),
            body=note.get("body", ""),
            sequence=len(messages) + 1,
            sent_at=note.get("at", ""),
        ))
    opened_at = overlay.get("opened_at", thread.started_at.isoformat() if thread.started_at else "")
    deadline = overlay.get("sla_deadline_at", "")
    sla = SlaTimer(opened_at=opened_at, deadline_at=deadline,
                   breached=bool(deadline and datetime.fromisoformat(deadline) < datetime.now(timezone.utc)))
    return Thread(
        id=thread_id(str(tenant.id), _channel_of(thread), str(thread.id)),
        tenant_id=str(tenant.id),
        channel=_channel_of(thread),
        status=_thread_status(thread, overlay),
        priority=ThreadPriority(overlay.get("priority", "normal")),
        participants=(thread.from_number, thread.to_number),
        assignee_id=overlay.get("assignee_id", ""),
        tags=tuple(overlay.get("tags", ())),
        internal_notes=tuple(n.get("body", "") for n in overlay.get("notes", [])),
        unread_count=overlay.get("unread", 0),
        sla=sla,
        escalated=bool(thread.escalated),
        messages=tuple(messages),
        last_message_at=messages[-1].sent_at if messages else thread.started_at.isoformat() if thread.started_at else "",
        created_at=thread.started_at.isoformat() if thread.started_at else "",
    )


# ---------------------------------------------------------------- actions ---

def assign(tenant, thread: Call, assignee_id: str) -> Thread:
    _ensure_owned(thread, tenant.id)
    overlay = _overlay(tenant.id, thread.id)
    overlay["assignee_id"] = assignee_id
    if overlay.get("status") not in (None, "open", "assigned"):
        overlay["status"] = "assigned"
    return _project_sync(tenant, thread)


def set_priority(tenant, thread: Call, priority: ThreadPriority) -> Thread:
    _ensure_owned(thread, tenant.id)
    _overlay(tenant.id, thread.id)["priority"] = priority.value
    return _project_sync(tenant, thread)


def add_note(tenant, thread: Call, body: str) -> Thread:
    _ensure_owned(thread, tenant.id)
    if not body or len(body) > 8_000:
        raise BadRequestError("note must be 1–8000 characters")
    notes = _overlay(tenant.id, thread.id).setdefault("notes", [])
    notes.append({"body": body, "at": now_iso(), "author": "agent"})
    return _project_sync(tenant, thread)


def mark_read(tenant, thread: Call) -> Thread:
    _ensure_owned(thread, tenant.id)
    _overlay(tenant.id, thread.id)["unread"] = 0
    return _project_sync(tenant, thread)


def mark_unread(tenant, thread: Call) -> Thread:
    _ensure_owned(thread, tenant.id)
    overlay = _overlay(tenant.id, thread.id)
    overlay["unread"] = overlay.get("unread", 0) + 1
    return _project_sync(tenant, thread)


def tag(tenant, thread: Call, value: str) -> Thread:
    _ensure_owned(thread, tenant.id)
    if not value or len(value) > 64:
        raise BadRequestError("tag must be 1–64 characters")
    overlay = _overlay(tenant.id, thread.id)
    tags = list(overlay.get("tags", ()))
    if value not in tags:
        tags.append(value)
    overlay["tags"] = tags[-100:]
    return _project_sync(tenant, thread)


async def escalate(session: AsyncSession, tenant, thread: Call, *, reason: str) -> Thread:
    _ensure_owned(thread, tenant.id)
    if not reason or len(reason) > 400:
        raise BadRequestError("escalation reason must be 1–400 characters")
    thread.escalated = True
    thread.transfer_reason = reason
    overlay = _overlay(tenant.id, thread.id)
    overlay["status"] = ThreadStatus.ESCALATED.value
    session.add(thread)
    await session.commit()
    return await project(session, tenant, thread)


async def close(session: AsyncSession, tenant, thread: Call) -> Thread:
    _ensure_owned(thread, tenant.id)
    overlay = _overlay(tenant.id, thread.id)
    current = _thread_status(thread, overlay)
    if not can_transition(current, ThreadStatus.CLOSED):
        raise BadRequestError(f"cannot close from {current.value}")
    thread.status = CallStatus.COMPLETED
    overlay["status"] = ThreadStatus.CLOSED.value
    session.add(thread)
    await session.commit()
    return await project(session, tenant, thread)


async def reopen(session: AsyncSession, tenant, thread: Call) -> Thread:
    _ensure_owned(thread, tenant.id)
    current = await project(session, tenant, thread)
    if not reopen_allowed(current):
        raise BadRequestError("thread is outside the reopen window")
    thread.status = CallStatus.IN_PROGRESS
    _overlay(tenant.id, thread.id)["status"] = ThreadStatus.OPEN.value
    session.add(thread)
    await session.commit()
    return await project(session, tenant, thread)


def _project_sync(tenant, thread: Call) -> Thread:
    """Synchronous projection for the overlay-only actions."""
    _ensure_owned(thread, tenant.id)
    overlay = _overlay(tenant.id, thread.id)
    return Thread(
        id=thread_id(str(tenant.id), _channel_of(thread), str(thread.id)),
        tenant_id=str(tenant.id),
        channel=_channel_of(thread),
        status=_thread_status(thread, overlay),
        priority=ThreadPriority(overlay.get("priority", "normal")),
        participants=(thread.from_number, thread.to_number),
        assignee_id=overlay.get("assignee_id", ""),
        tags=tuple(overlay.get("tags", ())),
        internal_notes=tuple(n.get("body", "") for n in overlay.get("notes", [])),
        unread_count=overlay.get("unread", 0),
        escalated=bool(thread.escalated),
    )


# ------------------------------------------------------------------ search ---

async def search(
    session: AsyncSession,
    tenant,
    *,
    channel: InboxChannel | None = None,
    status: ThreadStatus | None = None,
    assignee_id: str | None = None,
    escalated: bool | None = None,
    limit: int = 100,
) -> list[Thread]:
    """Tenant-scoped inbox search/filter."""
    limit = min(max(limit, 1), 500)
    stmt = select(Call).where(Call.tenant_id == tenant.id).order_by(Call.started_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    results = []
    for row in rows:
        thread = await project(session, tenant, row)
        if channel is not None and thread.channel is not channel:
            continue
        if status is not None and thread.status is not status:
            continue
        if assignee_id is not None and thread.assignee_id != assignee_id:
            continue
        if escalated is not None and thread.escalated != escalated:
            continue
        results.append(thread)
    return results


async def unread_counts(session: AsyncSession, tenant) -> dict[str, int]:
    """Per-thread unread counts for the badge view."""
    stmt = select(Call.id).where(Call.tenant_id == tenant.id)
    ids = (await session.execute(stmt)).scalars().all()
    counts = {}
    for call_id in ids:
        overlay = _overlay(tenant.id, call_id)
        if overlay.get("unread"):
            counts[str(call_id)] = overlay["unread"]
    return counts


async def thread_counts(session: AsyncSession, tenant) -> dict[str, int]:
    """Open/assigned/escalated/closed counts, aggregate-safe."""
    stmt = select(Call.status).where(Call.tenant_id == tenant.id)
    statuses = (await session.execute(stmt)).scalars().all()
    counts = {"open": 0, "closed": 0, "escalated": 0, "assigned": 0}
    for status in statuses:
        if status is CallStatus.COMPLETED:
            counts["closed"] += 1
        else:
            counts["open"] += 1
    for call_id, overlay in _OVERLAY.get(str(tenant.id), {}).items():
        if overlay.get("status") == "escalated":
            counts["escalated"] += 1
        if overlay.get("assignee_id"):
            counts["assigned"] += 1
    return counts
