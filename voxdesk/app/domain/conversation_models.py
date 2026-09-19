"""Conversation-domain layer (Batch 01 enterprise expansion).

A *conversation* here is a higher-level view over the existing persisted call
and messaging machinery. It does not replace or bypass anything:

* Voice calls and text threads are still stored as ``Call`` rows with ``Turn``
  children — the state machine in ``app/telephony/call_state.py`` and the
  ``CallStatus`` enum remain authoritative for what is actually persisted.
* This module provides the *domain* vocabulary (sentiment, intent, topic,
  resolution, escalation, satisfaction, tags, events) and a validated
  transition table. The service layer synchronises the two: it reads a
  ``Call`` row, projects it onto the objects below, validates the requested
  move against the table, and writes back only the columns that exist.

The transition table is deliberately stricter than the raw enum: e.g. a
conversation that is ``COMPLETED`` may only be reopened through the explicit
``REOPEN`` transition, and an ``ABANDONED`` conversation may never become
``ACTIVE`` again. ``to_call_status`` documents the projection onto
``CallStatus``; the persisted enum is always derived, never overridden here.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

# ------------------------------------------------------------------ enums ---

class ConversationState(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    TRANSFERRED = "transferred"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class ConversationChannel(str, enum.Enum):
    VOICE = "voice"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    WEB = "web"
    CRM = "crm"


class Sentiment(str, enum.Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    UNKNOWN = "unknown"


class ResolutionState(str, enum.Enum):
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"
    REQUIRES_FOLLOWUP = "requires_followup"


class EscalationState(str, enum.Enum):
    NONE = "none"
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    FAILED = "failed"


class SatisfactionState(str, enum.Enum):
    UNKNOWN = "unknown"
    SATISFIED = "satisfied"
    NEUTRAL = "neutral"
    DISSATISFIED = "dissatisfied"


class ParticipantRole(str, enum.Enum):
    CUSTOMER = "customer"
    ASSISTANT = "assistant"
    AGENT = "agent"
    SYSTEM = "system"
    THIRD_PARTY = "third_party"


# ------------------------------------------------------------------ parts ---

@dataclass(frozen=True)
class Participant:
    """A party in the conversation. Deliberately holds no raw PII.

    ``contact`` is the *redacted* endpoint (phone or handle) — the raw value
    never belongs in a domain object that can be serialised.
    """

    role: ParticipantRole
    contact: str = ""
    label: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if len(self.contact) > 64:
            problems.append("participant contact must be at most 64 characters")
        if len(self.label) > 200:
            problems.append("participant label must be at most 200 characters")
        return problems


@dataclass(frozen=True)
class ToolExecution:
    """A single tool invocation inside a turn, with a sanitised summary.

    ``arguments_summary`` and ``error_summary`` are already scrubbed; the raw
    provider payload never reaches this object.
    """

    name: str
    ok: bool
    arguments_summary: str = ""
    error_summary: str = ""


@dataclass(frozen=True)
class TurnMeta:
    """Provider latency and confidence metadata for one turn."""

    latency_ms: float | None = None
    provider: str = ""
    confidence: float | None = None
    tool_calls: tuple[ToolExecution, ...] = ()

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            problems.append("turn confidence must be between 0.0 and 1.0")
        return problems


@dataclass(frozen=True)
class ConversationEvent:
    """A timestamped, actor-attributed event in the conversation history."""

    type: str
    at: str
    actor: str = "system"
    note: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.type.strip() or len(self.type) > 64:
            problems.append("event type must be 1–64 characters")
        if len(self.note) > 2_000:
            problems.append("event note must be at most 2000 characters")
        return problems


@dataclass(frozen=True)
class ConversationSummary:
    """The agent-written summary, kept separate from the raw transcript."""

    short: str = ""
    topics: tuple[str, ...] = ()
    action_items: tuple[str, ...] = ()

    def validate(self) -> list[str]:
        problems: list[str] = []
        if len(self.short) > 4_000:
            problems.append("summary must be at most 4000 characters")
        if len(self.topics) > 50 or len(self.action_items) > 50:
            problems.append("at most 50 topics and 50 action items are allowed")
        return problems


# ------------------------------------------------------------- conversation ---

@dataclass(frozen=True)
class Conversation:
    """The domain view of one conversation session."""

    id: str
    tenant_id: str
    channel: ConversationChannel
    state: ConversationState = ConversationState.ACTIVE
    participants: tuple[Participant, ...] = ()
    sentiment: Sentiment = Sentiment.UNKNOWN
    intent: str = ""
    topic: str = ""
    resolution: ResolutionState = ResolutionState.UNRESOLVED
    escalation: EscalationState = EscalationState.NONE
    satisfaction: SatisfactionState = SatisfactionState.UNKNOWN
    summary: ConversationSummary = field(default_factory=ConversationSummary)
    ai_confidence: float | None = None
    tags: tuple[str, ...] = ()
    events: tuple[ConversationEvent, ...] = ()
    started_at: str = ""
    ended_at: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.id or not self.id.strip():
            problems.append("conversation id is required")
        if not self.tenant_id or not self.tenant_id.strip():
            problems.append("tenant_id is required")
        if not self.intent or len(self.intent) > 80:
            problems.append("intent must be 1–80 characters")
        if len(self.topic) > 120:
            problems.append("topic must be at most 120 characters")
        if self.ai_confidence is not None and not 0.0 <= self.ai_confidence <= 1.0:
            problems.append("ai_confidence must be between 0.0 and 1.0")
        if len(self.tags) != len(set(self.tags)):
            problems.append("tags must not contain duplicates")
        if len(self.tags) > 100:
            problems.append("at most 100 tags are allowed")
        for participant in self.participants:
            problems += participant.validate()
        problems += self.summary.validate()
        for event in self.events:
            problems += event.validate()
        return problems

    def belongs_to(self, tenant_id: str) -> bool:
        return self.tenant_id == tenant_id

    # ----------------------------------------------------------- transitions ---

    def transition(self, target: ConversationState, *, reason: str = "") -> "Conversation":
        """Return a new conversation in ``target``, or raise ``ValueError``.

        ``reason`` is recorded as a system event; it must be free of secrets
        because it may be logged.
        """
        if not can_transition(self.state, target):
            raise ValueError(
                f"invalid conversation transition {self.state.value} -> {target.value}"
            )
        event = ConversationEvent(
            type=f"state.{target.value}", at=now_iso(), note=reason[:2000]
        )
        return replace(
            self,
            state=target,
            events=self.events + (event,),
            ended_at=now_iso() if target in TERMINAL_STATES else self.ended_at,
        )

    def to_call_status(self) -> str:
        """The ``CallStatus`` value this state maps to when persisted.

        The mapping is used by the service to *derive* the persisted enum; it
        never writes a value the call state machine would reject.
        """
        mapping = {
            ConversationState.ACTIVE: "in_progress",
            ConversationState.PAUSED: "in_progress",
            ConversationState.TRANSFERRED: "transferred",
            ConversationState.COMPLETED: "completed",
            ConversationState.FAILED: "failed",
            ConversationState.ABANDONED: "no_answer",
        }
        return mapping[self.state]


# ------------------------------------------------------------- transitions ---

TERMINAL_STATES: frozenset[ConversationState] = frozenset({
    ConversationState.COMPLETED,
    ConversationState.FAILED,
    ConversationState.ABANDONED,
})

_TRANSITIONS: dict[ConversationState, frozenset[ConversationState]] = {
    ConversationState.ACTIVE: frozenset({
        ConversationState.PAUSED,
        ConversationState.TRANSFERRED,
        ConversationState.COMPLETED,
        ConversationState.FAILED,
        ConversationState.ABANDONED,
    }),
    ConversationState.PAUSED: frozenset({
        ConversationState.ACTIVE,
        ConversationState.COMPLETED,
        ConversationState.FAILED,
        ConversationState.ABANDONED,
    }),
    ConversationState.TRANSFERRED: frozenset({
        ConversationState.ACTIVE,          # the human handed it back
        ConversationState.COMPLETED,
        ConversationState.FAILED,
    }),
    ConversationState.COMPLETED: frozenset({ConversationState.ACTIVE}),  # reopen only
    ConversationState.FAILED: frozenset(),
    ConversationState.ABANDONED: frozenset(),
}


def can_transition(current: ConversationState, target: ConversationState) -> bool:
    return target in _TRANSITIONS.get(current, frozenset())


def is_terminal(state: ConversationState) -> bool:
    return state in TERMINAL_STATES


def now_iso() -> str:
    """A UTC ISO-8601 timestamp, timezone-aware, for event attribution."""
    return datetime.now(timezone.utc).isoformat()
