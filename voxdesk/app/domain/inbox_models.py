"""Unified inbox domain (Batch 01 enterprise expansion).

The inbox is a *view* over the existing conversation storage — voice calls and
text threads live in ``Call``/``Turn`` rows, exactly as they do today — plus a
small layer of inbox-only state (assignment, priority, tags, internal notes,
SLA deadline, read/unread) that has no column yet. This module defines the
value objects; the service layer maps threads/messages onto the existing rows
and keeps the inbox-only state in a tenant-keyed overlay, reporting the schema
gap at the end of the batch.

Hard rules encoded here:

* **Tenant ownership is mandatory.** ``Thread.tenant_id`` is required and
  ``belongs_to`` is the only sanctioned cross-check; a thread can never be
  visible to a second tenant.
* **Message ordering is monotonic.** ``Message.sequence`` must be strictly
  increasing within a thread; ``Thread.append`` enforces it.
* **Reopen is policy-gated.** ``reopen_allowed`` decides whether a ``CLOSED``
  thread may come back to ``OPEN``, based on the age of the closure — a
  months-old thread is answered with a new one instead.
* **Nothing here changes ``/channels/message``.** The webhook keeps writing the
  same rows; the inbox only reads them and overlays presentation state.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from app.domain.agent_models import stable_id

REOPEN_WINDOW_DAYS = 7


class InboxChannel(str, enum.Enum):
    VOICE = "voice"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    WEB = "web"
    CRM = "crm"


class ThreadStatus(str, enum.Enum):
    OPEN = "open"
    PENDING = "pending"
    ASSIGNED = "assigned"
    RESOLVED = "resolved"
    CLOSED = "closed"
    ESCALATED = "escalated"


class MessageDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL_NOTE = "internal_note"


class ThreadPriority(str, enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


@dataclass(frozen=True)
class Message:
    """One message in a thread. ``body`` may contain customer text: never log
    it in full — use ``Thread.safe_repr``-style truncation."""

    id: str
    tenant_id: str
    thread_id: str
    direction: MessageDirection
    channel: InboxChannel
    author_role: str = "customer"
    body: str = ""
    sequence: int = 0
    sent_at: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.id or not self.tenant_id or not self.thread_id:
            problems.append("message requires id, tenant_id and thread_id")
        if len(self.body) > 8_000:
            problems.append("message body must be at most 8000 characters")
        if self.sequence < 1:
            problems.append("message sequence must be >= 1")
        return problems


@dataclass(frozen=True)
class SlaTimer:
    """Deadline tracking for first-response and resolution SLAs."""

    opened_at: str = ""
    deadline_at: str = ""
    breached: bool = False

    def remaining_seconds(self, now: datetime | None = None) -> int | None:
        if not self.deadline_at:
            return None
        deadline = datetime.fromisoformat(self.deadline_at)
        now = now or datetime.now(timezone.utc)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return max(0, int((deadline - now).total_seconds()))


@dataclass(frozen=True)
class Thread:
    """A unified inbox conversation thread."""

    id: str
    tenant_id: str
    channel: InboxChannel
    status: ThreadStatus = ThreadStatus.OPEN
    priority: ThreadPriority = ThreadPriority.NORMAL
    participants: tuple[str, ...] = ()
    assignee_id: str = ""
    tags: tuple[str, ...] = ()
    internal_notes: tuple[str, ...] = ()
    unread_count: int = 0
    sla: SlaTimer = field(default_factory=SlaTimer)
    escalated: bool = False
    messages: tuple[Message, ...] = ()
    last_message_at: str = ""
    created_at: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.id or not self.tenant_id:
            problems.append("thread requires id and tenant_id")
        if self.unread_count < 0:
            problems.append("unread_count must not be negative")
        if len(self.tags) != len(set(self.tags)):
            problems.append("tags must not contain duplicates")
        if len(self.tags) > 100:
            problems.append("at most 100 tags are allowed")
        sequences = [m.sequence for m in self.messages]
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            problems.append("message sequences must be strictly increasing")
        if len(self.internal_notes) > 500:
            problems.append("at most 500 internal notes are allowed")
        for message in self.messages:
            problems += message.validate()
        return problems

    def belongs_to(self, tenant_id: str) -> bool:
        return self.tenant_id == tenant_id

    def next_sequence(self) -> int:
        return max((m.sequence for m in self.messages), default=0) + 1

    def append_message(self, message: Message) -> "Thread":
        """Append a message, enforcing monotonic ordering."""
        if message.sequence <= 0:
            message = replace(message, sequence=self.next_sequence())
        if self.messages and message.sequence <= self.messages[-1].sequence:
            raise ValueError("message sequence must be strictly increasing")
        last_seen = message.sent_at or self.last_message_at
        return replace(
            self,
            messages=self.messages + (message,),
            unread_count=self.unread_count + (1 if message.direction is MessageDirection.INBOUND else 0),
            last_message_at=last_seen,
        )

    def mark_read(self) -> "Thread":
        return replace(self, unread_count=0)

    def transition(self, target: ThreadStatus) -> "Thread":
        if not can_transition(self.status, target):
            raise ValueError(f"invalid thread transition {self.status.value} -> {target.value}")
        return replace(self, status=target)


# ------------------------------------------------------------- transitions ---

_THREAD_TRANSITIONS: dict[ThreadStatus, frozenset[ThreadStatus]] = {
    ThreadStatus.OPEN: frozenset({
        ThreadStatus.PENDING, ThreadStatus.ASSIGNED, ThreadStatus.ESCALATED,
        ThreadStatus.RESOLVED, ThreadStatus.CLOSED,
    }),
    ThreadStatus.PENDING: frozenset({ThreadStatus.OPEN, ThreadStatus.ASSIGNED, ThreadStatus.CLOSED}),
    ThreadStatus.ASSIGNED: frozenset({
        ThreadStatus.OPEN, ThreadStatus.ESCALATED, ThreadStatus.RESOLVED, ThreadStatus.CLOSED,
    }),
    ThreadStatus.RESOLVED: frozenset({ThreadStatus.CLOSED, ThreadStatus.OPEN}),
    ThreadStatus.CLOSED: frozenset({ThreadStatus.OPEN}),     # reopen only, policy-gated
    ThreadStatus.ESCALATED: frozenset({
        ThreadStatus.ASSIGNED, ThreadStatus.RESOLVED, ThreadStatus.CLOSED,
    }),
}


def can_transition(current: ThreadStatus, target: ThreadStatus) -> bool:
    return target in _THREAD_TRANSITIONS.get(current, frozenset())


def reopen_allowed(thread: Thread, *, now: datetime | None = None) -> bool:
    """A closed thread may reopen only within the reopen window."""
    if thread.status is not ThreadStatus.CLOSED:
        return False
    if not thread.last_message_at:
        return True
    closed_at = datetime.fromisoformat(thread.last_message_at)
    if closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=timezone.utc)
    now = (now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now - closed_at <= timedelta(days=REOPEN_WINDOW_DAYS)


def thread_id(tenant_id: str, channel: InboxChannel, source_ref: str) -> str:
    """Deterministic thread identity: same channel + source ⇒ same thread."""
    return stable_id(tenant_id, channel.value, source_ref)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
