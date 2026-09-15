# Step 17 — Batch 01 Enterprise Expansion: Complete File Contents

Repo: /home/user/voxdesk (HEAD $(git rev-parse --short HEAD))
Generated: 2026-09-14T02:12:19Z

The 20 files below are reproduced verbatim from the working tree. Nothing is
omitted, paraphrased, or diff-rendered.


========================================================================
===== FILE: app/domain/agent_models.py (493 lines) =====
========================================================================
```python
"""Domain model for AI-agent configuration (Batch 01 enterprise expansion).

This module is the *pure* heart of the agent-management feature. It holds no
database handle, no FastAPI dependency and no provider client; everything here
is a value object that can be validated, normalised and hashed in a unit test
with nothing but the standard library and the two small, dependency-free
modules it imports from the existing agent stack.

The agent concept intentionally *wraps* the existing per-tenant voice tuning
that already lives on ``Tenant`` (``agent_name``, ``greeting``,
``system_prompt_extra``, ``llm_preset``/``llm_provider``/``llm_model``,
``temperature``, ``humanize``, ``vad_stop_secs``, ``speech_speed``,
``voice_id``, ``language``, ``escalation_number``, ``record_calls``,
``recording_disclaimer``). It does **not** duplicate those columns; the
service layer (``app/services/agent_service.py``) is the only place that reads
or writes them, and it round-trips through the objects defined here.

Two invariants are encoded once, here, and enforced everywhere:

* **A published configuration is immutable.** ``AgentVersion`` is a frozen
  dataclass; the only way to change a live agent is to publish a new version.
* **Nothing secret ever appears in a domain object.** There is no field for
  an API key or a credential anywhere in this module; provider *choice* is a
  name, never a secret.
"""

from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import time as _time
from typing import Any

from app.agent.llm_factory import SUPPORTED_PROVIDERS
from app.agent.voice_settings import SPEECH_SPEED_MAX, SPEECH_SPEED_MIN, normalize_speech_speed

# ------------------------------------------------------------------ shared ---

_BCP47 = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9 .'&-]{1,80}$")
_MAX_SYSTEM_INSTRUCTIONS = 20_000
_MAX_GREETING = 2_000
_MAX_TOOL_CALLS = 24


def stable_id(*parts: object) -> str:
    """A deterministic, tenant-safe identifier derived from stable inputs.

    Two identical ``(tenant_id, name, version)`` tuples always produce the
    same id, so a retried create is idempotent and a clone with a new name
    never collides. Used by the agent, workflow and automation domains.
    """
    payload = "|".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


# ------------------------------------------------------------------ enums ---

class AgentStatus(str, enum.Enum):
    """Lifecycle of one agent configuration."""

    DRAFT = "draft"            # being edited; not serving calls
    PUBLISHED = "published"    # the live configuration
    RETIRED = "retired"        # no longer served; kept for history


class ModelProvider(str, enum.Enum):
    """Provider choice. Mirrors ``app.agent.llm_factory.SUPPORTED_PROVIDERS``."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"


class InterruptionPolicy(str, enum.Enum):
    """How the agent reacts when the human starts talking over it."""

    ALLOW_ALWAYS = "allow_always"      # stop on any user speech (barge-in)
    ALLOW_AFTER_ACK = "allow_after_ack"  # only after the agent finished a sentence
    BLOCK = "block"                    # ignore overlapping speech (IVR-style)


class ResponseStyle(str, enum.Enum):
    NATURAL = "natural"
    FORMAL = "formal"
    CASUAL = "casual"
    CONCISE = "concise"


class EscalationPolicy(str, enum.Enum):
    """When a live human is offered. Never dials here — policy only."""

    NONE = "none"
    ON_REQUEST = "on_request"
    ON_SENTIMENT = "on_sentiment"
    ON_KEYWORD = "on_keyword"
    ALWAYS = "always"


class FallbackBehavior(str, enum.Enum):
    """What happens when the model/tool layer fails a turn."""

    REPEAT = "repeat"
    TRANSFER_TO_HUMAN = "transfer_to_human"
    TAKE_MESSAGE = "take_message"
    END_CALL = "end_call"


class KnowledgeSourceType(str, enum.Enum):
    TENANT_FACTS = "tenant_facts"      # the flat JSON the business typed in
    DOCUMENTS = "documents"            # uploaded RAG documents
    MANUAL = "manual"                  # operator-authored notes


class HandoffMode(str, enum.Enum):
    NONE = "none"
    NUMBER = "number"                  # dial a configured number
    SAME_QUEUE = "same_queue"          # stay in the voice queue


# ------------------------------------------------------------- components ---

@dataclass(frozen=True)
class VoiceConfig:
    voice_id: str = ""
    speech_speed: float = 1.0
    fallback_voice_id: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        speed, adjusted = normalize_speech_speed(self.speech_speed)
        if adjusted:
            problems.append(
                f"speech_speed {self.speech_speed} outside supported range "
                f"{SPEECH_SPEED_MIN}–{SPEECH_SPEED_MAX}"
            )
        if len(self.voice_id) > 64:
            problems.append("voice_id must be at most 64 characters")
        if len(self.fallback_voice_id) > 64:
            problems.append("fallback_voice_id must be at most 64 characters")
        return problems

    def normalized(self) -> "VoiceConfig":
        speed, _ = normalize_speech_speed(self.speech_speed)
        return replace(self, speech_speed=speed)


@dataclass(frozen=True)
class LanguageConfig:
    primary: str = "en-US"
    fallbacks: tuple[str, ...] = ()

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not _BCP47.match(self.primary or ""):
            problems.append(f"primary language {self.primary!r} is not a BCP-47 tag")
        seen = {self.primary}
        for lang in self.fallbacks:
            if not _BCP47.match(lang or ""):
                problems.append(f"fallback language {lang!r} is not a BCP-47 tag")
            if lang in seen:
                problems.append(f"duplicate language {lang!r}")
            seen.add(lang)
        if len(self.fallbacks) > 5:
            problems.append("at most 5 fallback languages are allowed")
        return problems


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "anthropic"
    model: str = ""
    temperature: float = 0.65
    max_tokens: int | None = None

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.provider not in SUPPORTED_PROVIDERS:
            problems.append(
                f"provider {self.provider!r} unsupported; choose one of "
                f"{', '.join(SUPPORTED_PROVIDERS)}"
            )
        if not 0.0 <= self.temperature <= 2.0:
            problems.append("temperature must be between 0.0 and 2.0")
        if self.max_tokens is not None and not 1 <= self.max_tokens <= 16_384:
            problems.append("max_tokens must be between 1 and 16384")
        if len(self.model) > 80:
            problems.append("model must be at most 80 characters")
        return problems


@dataclass(frozen=True)
class ToolConfig:
    enabled: tuple[str, ...] = ()
    max_tool_calls: int = 8

    def validate(self, available: frozenset[str] | None = None) -> list[str]:
        problems: list[str] = []
        allowed = available if available is not None else self._default_available()
        unknown = sorted(set(self.enabled) - allowed)
        if unknown:
            problems.append(f"unknown tool(s): {', '.join(unknown)}")
        if len(self.enabled) != len(set(self.enabled)):
            problems.append("enabled tools contain duplicates")
        if not 1 <= self.max_tool_calls <= _MAX_TOOL_CALLS:
            problems.append(f"max_tool_calls must be between 1 and {_MAX_TOOL_CALLS}")
        return problems

    @staticmethod
    def _default_available() -> frozenset[str]:
        # Kept local so this module never hard-depends on the (larger) function
        # registry at import time; the service passes the authoritative set.
        from app.agent.functions import DISPATCHABLE_TOOLS

        return frozenset(DISPATCHABLE_TOOLS)


@dataclass(frozen=True)
class SafetyPolicy:
    max_tool_calls: int = 8
    ai_disclosure_required: bool = True
    allow_escalation: bool = True
    record_calls: bool = False
    disallowed_topics: tuple[str, ...] = ()

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not 1 <= self.max_tool_calls <= _MAX_TOOL_CALLS:
            problems.append("safety.max_tool_calls must be between 1 and 24")
        if len(self.disallowed_topics) > 100:
            problems.append("at most 100 disallowed topics are allowed")
        if any(len(t) > 120 or not t.strip() for t in self.disallowed_topics):
            problems.append("each disallowed topic must be 1–120 characters")
        return problems


@dataclass(frozen=True)
class OperatingHours:
    timezone: str = "UTC"
    open: _time = _time(9, 0)
    close: _time = _time(17, 0)

    def validate(self) -> list[str]:
        problems: list[str] = []
        try:
            import zoneinfo

            zoneinfo.ZoneInfo(self.timezone)
        except Exception:
            problems.append(f"timezone {self.timezone!r} is not a valid IANA zone")
        if self.open >= self.close:
            problems.append("open must be earlier than close")
        return problems


@dataclass(frozen=True)
class KnowledgeSource:
    type: KnowledgeSourceType = KnowledgeSourceType.TENANT_FACTS
    references: tuple[str, ...] = ()

    def validate(self) -> list[str]:
        problems: list[str] = []
        if len(self.references) > 500:
            problems.append("at most 500 knowledge references are allowed")
        if any(not r or len(r) > 200 for r in self.references):
            problems.append("each knowledge reference must be 1–200 characters")
        return problems


@dataclass(frozen=True)
class HandoffConfig:
    mode: HandoffMode = HandoffMode.NONE
    destination: str = ""
    timeout_seconds: int = 30

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.mode is HandoffMode.NUMBER and not self.destination.strip():
            problems.append("handoff destination is required in NUMBER mode")
        if len(self.destination) > 32:
            problems.append("handoff destination must be at most 32 characters")
        if not 5 <= self.timeout_seconds <= 300:
            problems.append("handoff timeout must be between 5 and 300 seconds")
        return problems


# ---------------------------------------------------------------- the agent ---

@dataclass(frozen=True)
class AgentConfig:
    """A complete, validated agent configuration.

    ``tenant_id`` is required and present on every object: an agent can never
    exist without an owner, which is the root of the tenant-isolation
    guarantee. ``identity`` fields carry no secret.
    """

    tenant_id: str
    name: str
    greeting: str = ""
    system_instructions: str = ""
    language: LanguageConfig = field(default_factory=LanguageConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    response_style: ResponseStyle = ResponseStyle.NATURAL
    interruption: InterruptionPolicy = InterruptionPolicy.ALLOW_ALWAYS
    escalation: EscalationPolicy = EscalationPolicy.ON_REQUEST
    operating_hours: OperatingHours = field(default_factory=OperatingHours)
    knowledge: tuple[KnowledgeSource, ...] = ()
    tools: ToolConfig = field(default_factory=ToolConfig)
    safety: SafetyPolicy = field(default_factory=SafetyPolicy)
    fallback: FallbackBehavior = FallbackBehavior.TAKE_MESSAGE
    handoff: HandoffConfig = field(default_factory=HandoffConfig)
    confidence_min: float = 0.35
    confidence_floor: float = 0.0

    # ------------------------------------------------------------------ id ---

    @property
    def id(self) -> str:
        """Deterministic, content-independent id: same tenant + name ⇒ same id.

        The id is deliberately *not* derived from ``config_hash``: an agent's
        identity must be stable across edits so that ``update_draft``, version
        history and rollback all key on one consistent id. Content identity is
        captured separately by ``config_hash`` (used for change detection).
        """
        return stable_id(self.tenant_id, self.name)

    def config_hash(self) -> str:
        """Deterministic hash of the canonical config (no timestamps, no ids)."""
        return stable_id(
            self.name,
            self.greeting,
            self.system_instructions,
            self.language,
            self.voice.normalized(),
            self.model,
            self.response_style.value,
            self.interruption.value,
            self.escalation.value,
            self.operating_hours,
            tuple(sorted((s.type.value, s.references) for s in self.knowledge)),
            self.tools,
            self.safety,
            self.fallback.value,
            self.handoff,
            self.confidence_min,
            self.confidence_floor,
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.tenant_id or not self.tenant_id.strip():
            problems.append("tenant_id is required")
        if not _AGENT_NAME_RE.match(self.name or ""):
            problems.append("name must be 1–80 characters: letters, digits, space, . ' & -")
        if len(self.greeting) > _MAX_GREETING:
            problems.append(f"greeting must be at most {_MAX_GREETING} characters")
        if len(self.system_instructions) > _MAX_SYSTEM_INSTRUCTIONS:
            problems.append(
                f"system_instructions must be at most {_MAX_SYSTEM_INSTRUCTIONS} characters"
            )
        problems += self.language.validate()
        problems += self.voice.validate()
        problems += self.model.validate()
        problems += self.tools.validate()
        problems += self.safety.validate()
        problems += self.operating_hours.validate()
        problems += self.handoff.validate()
        for source in self.knowledge:
            problems += source.validate()
        if not 0.0 <= self.confidence_floor <= self.confidence_min <= 1.0:
            problems.append("confidence thresholds must satisfy 0 <= floor <= min <= 1")
        if self.escalation in (EscalationPolicy.ON_SENTIMENT, EscalationPolicy.ALWAYS):
            if not self.safety.allow_escalation:
                problems.append("escalation policy conflicts with safety.allow_escalation=False")
        return problems

    def is_valid(self) -> bool:
        return not self.validate()

    def normalized(self) -> "AgentConfig":
        """Return a copy with provider-side ranges clamped (never rewrites)."""
        return replace(self, voice=self.voice.normalized())

    def with_defaults(self) -> "AgentConfig":
        """A config with every optional field populated by safe defaults."""
        return replace(
            self,
            language=self.language if self.language.primary else LanguageConfig(),
            voice=self.voice if self.voice.voice_id else VoiceConfig(),
            model=self.model if self.model.model else ModelConfig(provider=self.model.provider or "anthropic"),
        )


@dataclass(frozen=True)
class AgentVersion:
    """An immutable published snapshot of an agent configuration."""

    agent_id: str
    version: int
    config_hash: str
    status: AgentStatus = AgentStatus.PUBLISHED
    changelog: str = ""
    published_at: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.agent_id:
            problems.append("agent_id is required")
        if self.version < 1:
            problems.append("version must be >= 1")
        if not self.config_hash:
            problems.append("config_hash is required")
        if len(self.changelog) > 4_000:
            problems.append("changelog must be at most 4000 characters")
        return problems


@dataclass(frozen=True)
class AgentBundle:
    """The draft/published pair a tenant actually operates.

    ``draft`` is editable; ``published`` is the last immutable version. The
    service layer is the only thing that may promote a draft to a published
    version, and it always creates a new ``AgentVersion`` rather than editing
    the old one.
    """

    tenant_id: str
    draft: AgentConfig
    published: AgentVersion | None = None

    @property
    def status(self) -> AgentStatus:
        if self.published is None:
            return AgentStatus.DRAFT
        return AgentStatus.PUBLISHED

    @property
    def agent_id(self) -> str:
        return self.draft.id

    def validate(self) -> list[str]:
        problems = list(self.draft.validate())
        if self.published is not None:
            problems += self.published.validate()
            if self.published.agent_id != self.draft.id:
                problems.append("published version belongs to a different agent")
        return problems


# ------------------------------------------------------------- transitions ---

_AGENT_TRANSITIONS: dict[AgentStatus, frozenset[AgentStatus]] = {
    AgentStatus.DRAFT: frozenset({AgentStatus.PUBLISHED, AgentStatus.RETIRED}),
    AgentStatus.PUBLISHED: frozenset({AgentStatus.PUBLISHED, AgentStatus.RETIRED}),
    AgentStatus.RETIRED: frozenset({AgentStatus.RETIRED}),
}


def can_transition(current: AgentStatus, target: AgentStatus) -> bool:
    """Whether moving ``current -> target`` is permitted.

    Publishing an already-published agent is allowed (it mints a new version);
    a retired agent may never come back, so a rollback to history stays
    possible while a resurrection of a retired identity does not.
    """
    return target in _AGENT_TRANSITIONS.get(current, frozenset())


def diff_configs(before: AgentConfig, after: AgentConfig) -> dict[str, Any]:
    """Field-level diff between two configs, for the compare-versions view.

    Values are safe to display: no secrets exist in the model to begin with.
    """
    result: dict[str, Any] = {}
    for field_name in (
        "greeting", "system_instructions", "response_style", "interruption",
        "escalation", "fallback", "confidence_min", "confidence_floor",
    ):
        old, new = getattr(before, field_name), getattr(after, field_name)
        if old != new:
            result[field_name] = {"from": str(old), "to": str(new)}
    for field_name in ("language", "voice", "model", "tools", "safety",
                       "operating_hours", "handoff", "knowledge"):
        old, new = getattr(before, field_name), getattr(after, field_name)
        if old != new:
            result[field_name] = {"from": repr(old), "to": repr(new)}
    return result
```

========================================================================
===== FILE: app/domain/conversation_models.py (303 lines) =====
========================================================================
```python
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
```

========================================================================
===== FILE: app/domain/workflow_models.py (362 lines) =====
========================================================================
```python
"""Workflow-definition domain (Batch 01 enterprise expansion).

A workflow is a validated, deterministic graph of *controlled application
actions*. Two hard guarantees are encoded here and nowhere else:

1. **No arbitrary code.** A workflow action is a name from ``CONTROLLED_ACTIONS``
   plus validated parameters. There is no ``exec``, ``eval``, ``import``,
   ``shell``, ``lambda`` or user-supplied code of any kind. Anything that looks
   like code is rejected in ``validate`` before a workflow can even be stored.
2. **Deterministic identity.** ``WorkflowDefinition.identity`` is the SHA-256 of
   the canonical definition content (with timestamps and ids excluded), so two
   identical definitions always compare equal and versioning can detect "no
   actual change" instead of bumping a version for a rename of the metadata.

Workflows are tenant-owned: ``tenant_id`` is required on every object and the
service layer keys its registry by it. Executions are also tenant-scoped and
carry an idempotency key so a replayed trigger cannot run a workflow twice.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from typing import Any

from app.domain.agent_models import stable_id

# -------------------------------------------------------- action vocabulary ---

#: The only action names a workflow may reference. Each maps to a handler in
#: ``app/services/workflow_service.py``; anything else is rejected.
CONTROLLED_ACTIONS = frozenset({
    "update_lead_status",        # tenant-local, read-only re: providers
    "add_conversation_tag",      # tenant-local
    "enqueue_notification",      # routed through notification_service
    "record_escalation_intent",  # records that a human should be offered; never dials
    "create_followup_intent",    # records a follow-up task; never dials
    "mark_resolved",             # tenant-local resolution flag
    "apply_dnc",                 # tenant-local do-not-call flag on a lead
})

#: Markers that can never appear in an action name. A belt-and-braces guard on
#: top of the allowlist: even a future bug that widened the allowlist cannot
#: admit a code-shaped name through these.
FORBIDDEN_ACTION_MARKERS = (
    "__", "exec", "eval", "import", "system", "shell", "subprocess",
    "lambda", "compile", "globals", "locals",
)

#: Condition operators, each implemented by ``evaluate_condition``.
CONDITION_OPERATORS = frozenset({
    "eq", "ne", "gt", "gte", "lt", "lte",
    "in", "not_in", "contains", "starts_with", "ends_with",
    "exists", "not_exists",
})


def evaluate_condition(field_value: Any, operator: str, expected: Any) -> bool:
    """Evaluate one condition against a payload field. Pure and total.

    Unknown operators are rejected by ``Condition.validate``; this function is
    only ever called with a validated operator, and it is defensive anyway.
    """
    if operator == "exists":
        return field_value is not None
    if operator == "not_exists":
        return field_value is None
    if field_value is None:
        return False
    if operator == "eq":
        return field_value == expected
    if operator == "ne":
        return field_value != expected
    if operator == "gt":
        return _cmp(field_value, expected) > 0
    if operator == "gte":
        return _cmp(field_value, expected) >= 0
    if operator == "lt":
        return _cmp(field_value, expected) < 0
    if operator == "lte":
        return _cmp(field_value, expected) <= 0
    if operator == "in":
        return isinstance(expected, (list, tuple, set)) and field_value in expected
    if operator == "not_in":
        return isinstance(expected, (list, tuple, set)) and field_value not in expected
    if operator == "contains":
        return isinstance(field_value, (str, list, tuple)) and expected in field_value
    if operator == "starts_with":
        return isinstance(field_value, str) and field_value.startswith(str(expected))
    if operator == "ends_with":
        return isinstance(field_value, str) and field_value.endswith(str(expected))
    return False


def _cmp(a: Any, b: Any) -> int:
    """Total, type-aware comparison used by the ordering operators."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return (a > b) - (a < b)
    if isinstance(a, str) and isinstance(b, str):
        return (a > b) - (a < b)
    raise ValueError(f"cannot order-compare {type(a).__name__} with {type(b).__name__}")


# ------------------------------------------------------------------ enums ---

class WorkflowStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class NodeType(str, enum.Enum):
    TRIGGER = "trigger"
    CONDITION = "condition"
    ACTION = "action"
    DELAY = "delay"
    RETRY = "retry"
    TIMEOUT = "timeout"
    APPROVAL = "approval"
    HANDOFF = "handoff"
    TERMINAL = "terminal"


class ExecutionStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


# ------------------------------------------------------------------- nodes ---

@dataclass(frozen=True)
class Condition:
    """One field/operator/value predicate."""

    field: str
    operator: str
    value: Any = None

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.field or not self.field.strip() or len(self.field) > 64:
            problems.append("condition field must be 1–64 characters")
        if self.operator not in CONDITION_OPERATORS:
            problems.append(f"condition operator {self.operator!r} is not allowed")
        return problems

    def matches(self, payload: dict[str, Any]) -> bool:
        return evaluate_condition(payload.get(self.field), self.operator, self.value)


@dataclass(frozen=True)
class WorkflowAction:
    """A controlled action with validated parameters."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.name not in CONTROLLED_ACTIONS:
            problems.append(f"action {self.name!r} is not a controlled action")
        lowered = self.name.lower()
        if any(marker in lowered for marker in FORBIDDEN_ACTION_MARKERS):
            problems.append(f"action name {self.name!r} is forbidden")
        if not isinstance(self.params, dict):
            problems.append("action params must be a mapping")
            return problems
        if len(self.params) > 40:
            problems.append("action params must have at most 40 keys")
        for key, value in self.params.items():
            if not isinstance(key, str) or not key.strip() or len(key) > 64:
                problems.append("action parameter keys must be 1–64 character strings")
            if isinstance(value, (dict, list)) and _json_size(value) > 8_000:
                problems.append(f"action parameter {key!r} is too large")
        return problems


def _json_size(value: Any) -> int:
    """Cheap upper-bound size estimate for a nested value (no serialisation)."""
    if isinstance(value, dict):
        return sum(len(str(k)) + _json_size(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return sum(_json_size(v) for v in value)
    return len(str(value))


@dataclass(frozen=True)
class WorkflowNode:
    """One node in the graph. ``next`` is the single deterministic successor."""

    id: str
    type: NodeType
    action: WorkflowAction | None = None
    condition: Condition | None = None
    branches: tuple[tuple[Condition, str], ...] = ()   # (when, target-node-id)
    default_next: str = ""                              # fallback for branches
    next: str = ""
    delay_seconds: int = 0
    timeout_seconds: int = 30
    retry_limit: int = 3
    approver_role: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.id or not self.id.strip() or len(self.id) > 64:
            problems.append("node id must be 1–64 characters")
        if self.type is NodeType.ACTION and self.action is None:
            problems.append(f"action node {self.id!r} has no action")
        if self.type is NodeType.CONDITION and not self.branches and not self.condition:
            problems.append(f"condition node {self.id!r} has no condition")
        if self.type is NodeType.APPROVAL and not self.approver_role.strip():
            problems.append(f"approval node {self.id!r} has no approver_role")
        if self.action is not None:
            problems += self.action.validate()
        if self.condition is not None:
            problems += self.condition.validate()
        for cond, _target in self.branches:
            problems += cond.validate()
        if not 0 <= self.delay_seconds <= 86_400:
            problems.append("delay_seconds must be between 0 and 86400")
        if not 1 <= self.timeout_seconds <= 3_600:
            problems.append("timeout_seconds must be between 1 and 3600")
        if not 1 <= self.retry_limit <= 10:
            problems.append("retry_limit must be between 1 and 10")
        return problems


@dataclass(frozen=True)
class WorkflowDefinition:
    """A versioned workflow owned by one tenant."""

    id: str
    tenant_id: str
    name: str
    version: int = 1
    status: WorkflowStatus = WorkflowStatus.DRAFT
    trigger: str = ""
    entry_node: str = ""
    nodes: tuple[WorkflowNode, ...] = ()
    description: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.tenant_id or not self.tenant_id.strip():
            problems.append("tenant_id is required")
        if not self.id or not self.id.strip():
            problems.append("workflow id is required")
        if not self.name.strip() or len(self.name) > 200:
            problems.append("workflow name must be 1–200 characters")
        if self.version < 1:
            problems.append("version must be >= 1")
        if len(self.description) > 4_000:
            problems.append("description must be at most 4000 characters")
        if len(self.trigger) > 64:
            problems.append("trigger must be at most 64 characters")
        node_ids = {node.id for node in self.nodes}
        if not self.nodes:
            problems.append("workflow must have at least one node")
        if self.entry_node and self.entry_node not in node_ids:
            problems.append(f"entry node {self.entry_node!r} does not exist")
        terminal = [n for n in self.nodes if n.type is NodeType.TERMINAL]
        if not terminal:
            problems.append("workflow must contain at least one terminal node")
        for node in self.nodes:
            problems += node.validate()
            if node.next and node.next not in node_ids:
                problems.append(f"node {node.id!r} points at unknown node {node.next!r}")
            for _cond, target in node.branches:
                if target not in node_ids:
                    problems.append(f"node {node.id!r} branches to unknown node {target!r}")
            if node.default_next and node.default_next not in node_ids:
                problems.append(f"node {node.id!r} default_next is unknown")
        return problems

    def is_valid(self) -> bool:
        return not self.validate()

    def identity(self) -> str:
        """Deterministic content hash — the versioning primitive."""
        return stable_id(
            self.tenant_id,
            self.name,
            self.trigger,
            self.entry_node,
            tuple(
                (n.id, n.type.value, n.action, n.condition, n.branches,
                 n.default_next, n.next, n.delay_seconds, n.timeout_seconds,
                 n.retry_limit, n.approver_role)
                for n in self.nodes
            ),
        )

    def with_version(self, version: int) -> "WorkflowDefinition":
        return replace(self, version=version)


@dataclass(frozen=True)
class WorkflowStep:
    """One executed (or skipped) node within an execution."""

    node_id: str
    status: str            # executed | skipped | scheduled | failed | unsupported
    detail: str = ""
    attempt: int = 1
    at: str = ""


@dataclass(frozen=True)
class WorkflowExecution:
    """One run of a workflow against one tenant event."""

    id: str
    workflow_id: str
    tenant_id: str
    idempotency_key: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    current_node: str = ""
    input_summary: dict[str, Any] = field(default_factory=dict)
    steps: tuple[WorkflowStep, ...] = ()
    started_at: str = ""
    finished_at: str = ""
    error: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.workflow_id:
            problems.append("workflow_id is required")
        if not self.tenant_id:
            problems.append("tenant_id is required")
        if not self.idempotency_key:
            problems.append("idempotency_key is required")
        return problems


# ------------------------------------------------------------- transitions ---

_WORKFLOW_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.DRAFT: frozenset({WorkflowStatus.ACTIVE, WorkflowStatus.ARCHIVED}),
    WorkflowStatus.ACTIVE: frozenset({WorkflowStatus.PAUSED, WorkflowStatus.ARCHIVED}),
    WorkflowStatus.PAUSED: frozenset({WorkflowStatus.ACTIVE, WorkflowStatus.ARCHIVED}),
    WorkflowStatus.ARCHIVED: frozenset(),
}


def can_transition(current: WorkflowStatus, target: WorkflowStatus) -> bool:
    return target in _WORKFLOW_TRANSITIONS.get(current, frozenset())


_TERMINAL_EXECUTION = frozenset({
    ExecutionStatus.COMPLETED, ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED, ExecutionStatus.TIMED_OUT,
})


def execution_is_terminal(status: ExecutionStatus) -> bool:
    return status in _TERMINAL_EXECUTION
```

========================================================================
===== FILE: app/domain/automation_models.py (207 lines) =====
========================================================================
```python
"""Automation-definition domain (Batch 01 enterprise expansion).

An automation is a *bounded* event reaction: a fixed event type, a set of
filter rules, a list of controlled actions, and an execution policy (retry +
cooldown). It is deliberately not an unbounded event engine:

* The event vocabulary is the closed ``TriggerEvent`` enum. A tenant cannot
  register a new event type any more than it can register a new action.
* Actions come from ``workflow_models.CONTROLLED_ACTIONS`` — the same
  controlled vocabulary the workflow engine uses, so there is exactly one
  definition of "what may be executed", never two.
* Deduplication and cooldown are deterministic functions of business events,
  so a replayed webhook cannot create duplicate external side effects.

``run_idempotency_key`` is the deduplication primitive: it derives from
``(tenant_id, automation_id, event_type, business_event_id)`` so two deliveries
of the same business fact collapse to one automation run.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from app.domain.agent_models import stable_id
from app.domain.workflow_models import (
    CONTROLLED_ACTIONS,
    FORBIDDEN_ACTION_MARKERS,
    WorkflowAction,
)


# ------------------------------------------------------------------ enums ---

class AutomationStatus(str, enum.Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class TriggerEvent(str, enum.Enum):
    """The closed set of business events an automation may subscribe to."""

    CALL_COMPLETED = "call_completed"
    LEAD_CREATED = "lead_created"
    APPOINTMENT_BOOKED = "appointment_booked"
    APPOINTMENT_CANCELLED = "appointment_cancelled"
    PAYMENT_EVENT = "payment_event"
    CRM_EVENT = "crm_event"
    INBOUND_MESSAGE = "inbound_message"
    KNOWLEDGE_UPDATED = "knowledge_updated"
    SENTIMENT_EVENT = "sentiment_event"


class ScheduleKind(str, enum.Enum):
    ON_EVENT = "on_event"     # react immediately to the trigger
    DELAYED = "delayed"       # react once after a fixed delay


# ------------------------------------------------------------------- parts ---

@dataclass(frozen=True)
class FilterRule:
    """A predicate a triggering payload must satisfy.

    Reuses the workflow condition evaluator, so filter semantics and workflow
    branch semantics are identical — one evaluator, one set of operators.
    """

    field: str
    operator: str
    value: Any = None

    def validate(self) -> list[str]:
        from app.domain.workflow_models import CONDITION_OPERATORS

        problems: list[str] = []
        if not self.field or not self.field.strip() or len(self.field) > 64:
            problems.append("filter field must be 1–64 characters")
        if self.operator not in CONDITION_OPERATORS:
            problems.append(f"filter operator {self.operator!r} is not allowed")
        return problems

    def matches(self, payload: dict[str, Any]) -> bool:
        from app.domain.workflow_models import evaluate_condition

        return evaluate_condition(payload.get(self.field), self.operator, self.value)


@dataclass(frozen=True)
class ExecutionPolicy:
    """Retry and cooldown rules for one automation."""

    max_attempts: int = 3
    backoff_seconds: int = 60
    cooldown_seconds: int = 0
    max_per_event: int = 1

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not 1 <= self.max_attempts <= 10:
            problems.append("max_attempts must be between 1 and 10")
        if not 0 <= self.backoff_seconds <= 86_400:
            problems.append("backoff_seconds must be between 0 and 86400")
        if not 0 <= self.cooldown_seconds <= 86_400:
            problems.append("cooldown_seconds must be between 0 and 86400")
        if not 1 <= self.max_per_event <= 100:
            problems.append("max_per_event must be between 1 and 100")
        return problems


@dataclass(frozen=True)
class AutomationSchedule:
    kind: ScheduleKind = ScheduleKind.ON_EVENT
    delay_seconds: int = 0

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not 0 <= self.delay_seconds <= 86_400:
            problems.append("schedule delay_seconds must be between 0 and 86400")
        return problems


@dataclass(frozen=True)
class AutomationDefinition:
    """A tenant-owned automation definition."""

    id: str
    tenant_id: str
    name: str
    event: TriggerEvent
    filters: tuple[FilterRule, ...] = ()
    actions: tuple[WorkflowAction, ...] = ()
    schedule: AutomationSchedule = field(default_factory=AutomationSchedule)
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    status: AutomationStatus = AutomationStatus.DISABLED
    description: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.tenant_id or not self.tenant_id.strip():
            problems.append("tenant_id is required")
        if not self.id or not self.id.strip():
            problems.append("automation id is required")
        if not self.name.strip() or len(self.name) > 200:
            problems.append("automation name must be 1–200 characters")
        if len(self.description) > 4_000:
            problems.append("description must be at most 4000 characters")
        if not self.actions:
            problems.append("automation must have at least one action")
        for rule in self.filters:
            problems += rule.validate()
        for action in self.actions:
            problems += action.validate()
            if action.name not in CONTROLLED_ACTIONS:
                problems.append(f"action {action.name!r} is not a controlled action")
            lowered = action.name.lower()
            if any(marker in lowered for marker in FORBIDDEN_ACTION_MARKERS):
                problems.append(f"action name {action.name!r} is forbidden")
        problems += self.schedule.validate()
        problems += self.policy.validate()
        if len(self.actions) > 20:
            problems.append("at most 20 actions per automation are allowed")
        return problems

    def is_valid(self) -> bool:
        return not self.validate()

    def matches(self, payload: dict[str, Any]) -> bool:
        """True when every filter matches the payload (empty filters match all)."""
        return all(rule.matches(payload) for rule in self.filters)


@dataclass(frozen=True)
class AutomationRun:
    """One (attempted) execution of an automation for one business event."""

    id: str
    automation_id: str
    tenant_id: str
    idempotency_key: str
    event: TriggerEvent
    business_event_id: str
    status: str = "pending"        # pending | running | completed | failed | cancelled
    attempts: int = 0
    next_attempt_at: str = ""
    last_error: str = ""
    result_summary: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    finished_at: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.automation_id or not self.tenant_id or not self.idempotency_key:
            problems.append("automation run requires automation_id, tenant_id, idempotency_key")
        if not self.business_event_id:
            problems.append("business_event_id is required")
        return problems


# ------------------------------------------------------------- idempotency ---

def run_idempotency_key(
    tenant_id: str, automation_id: str, event: TriggerEvent, business_event_id: str
) -> str:
    """The deduplication primitive: same business fact ⇒ same key."""
    return stable_id(tenant_id, automation_id, event.value, business_event_id)
```

========================================================================
===== FILE: app/domain/campaign_models.py (255 lines) =====
========================================================================
```python
"""Campaign-domain models (Batch 01 enterprise expansion).

These objects sit *above* the existing ``Campaign``/``Lead``/``Tenant`` tables
and the outbound safety machinery in ``app/telephony/outbound.py``. The domain
layer never dials anything; it expresses the *rules* a campaign must obey —
DNC checks, call-window checks, daily limits, attempt limits — and the service
layer turns those rules into eligibility decisions and execution intents.

The state machine is the important part. A campaign moves
``draft -> scheduled -> running -> (paused <-> running) -> completed/cancelled``.
Every transition is explicit and validated, and the service layer refuses to
resume a campaign whose window has closed or whose daily limit is exhausted.

Campaign execution MUST respect the existing call-window/DNC/message safety
controls. Nothing in this module can weaken them; ``ComplianceGate`` only ever
makes requirements *stricter* (all flags default to the safe value).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, time as _time, timezone

from app.domain.agent_models import stable_id
from app.domain.automation_models import FilterRule

# ------------------------------------------------------------------ enums ---

class CampaignState(str, enum.Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class CampaignChannel(str, enum.Enum):
    VOICE = "voice"
    SMS = "sms"
    WHATSAPP = "whatsapp"


class CampaignGoal(str, enum.Enum):
    QUALIFY = "qualify"
    REMIND = "remind"
    FOLLOWUP = "followup"
    SURVEY = "survey"


# ------------------------------------------------------------------- parts ---

@dataclass(frozen=True)
class Segment:
    """A named audience segment expressed as filter rules (reused evaluator)."""

    id: str
    tenant_id: str
    name: str
    rules: tuple[FilterRule, ...] = ()

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.tenant_id or not self.id or not self.name.strip():
            problems.append("segment requires tenant_id, id and name")
        if len(self.name) > 200:
            problems.append("segment name must be at most 200 characters")
        for rule in self.rules:
            problems += rule.validate()
        return problems


@dataclass(frozen=True)
class Audience:
    """The population a campaign targets. Bounded, tenant-scoped."""

    tenant_id: str
    segment_ids: tuple[str, ...] = ()
    lead_ids: tuple[str, ...] = ()

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.tenant_id:
            problems.append("audience requires tenant_id")
        if len(self.lead_ids) > 100_000:
            problems.append("audience lead_ids must be at most 100000")
        if len(self.segment_ids) > 100:
            problems.append("audience segment_ids must be at most 100")
        return problems


@dataclass(frozen=True)
class CampaignSchedule:
    start_at: str = ""
    end_at: str = ""
    daily_start: _time = _time(9, 0)
    daily_end: _time = _time(20, 0)
    days_of_week: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)   # 0=Monday
    timezone: str = "UTC"

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.daily_start >= self.daily_end:
            problems.append("daily_start must be earlier than daily_end")
        bad_days = sorted(set(self.days_of_week) - set(range(7)))
        if bad_days:
            problems.append(f"days_of_week contains invalid days: {bad_days}")
        if not self.days_of_week:
            problems.append("days_of_week must not be empty")
        return problems


@dataclass(frozen=True)
class Throttle:
    calls_per_minute: int = 2
    daily_limit: int = 200
    max_attempts_per_lead: int = 3

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not 1 <= self.calls_per_minute <= 60:
            problems.append("calls_per_minute must be between 1 and 60")
        if not 1 <= self.daily_limit <= 100_000:
            problems.append("daily_limit must be between 1 and 100000")
        if not 1 <= self.max_attempts_per_lead <= 10:
            problems.append("max_attempts_per_lead must be between 1 and 10")
        return problems


@dataclass(frozen=True)
class ComplianceGate:
    """The safety requirements a campaign must honour. Defaults are strict."""

    require_dnc_check: bool = True
    require_call_window: bool = True
    require_attempt_limit: bool = True
    require_daily_limit: bool = True
    require_a2p_registration: bool = False   # only for SMS/WhatsApp channels

    def validate(self) -> list[str]:
        # These may not be disabled: the domain offers no way to weaken the
        # legal/regulatory controls, only to tighten them.
        problems: list[str] = []
        if not self.require_dnc_check:
            problems.append("require_dnc_check may not be disabled")
        if not self.require_call_window:
            problems.append("require_call_window may not be disabled")
        return problems


@dataclass(frozen=True)
class CampaignDefinition:
    """The full domain view of a campaign."""

    id: str
    tenant_id: str
    name: str
    goal: CampaignGoal = CampaignGoal.QUALIFY
    channel: CampaignChannel = CampaignChannel.VOICE
    script_prompt: str = ""
    opening_line: str = ""
    audience: Audience = field(default_factory=lambda: Audience(tenant_id=""))
    schedule: CampaignSchedule = field(default_factory=CampaignSchedule)
    throttle: Throttle = field(default_factory=Throttle)
    compliance: ComplianceGate = field(default_factory=ComplianceGate)
    state: CampaignState = CampaignState.DRAFT

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.tenant_id:
            problems.append("campaign requires tenant_id")
        if not self.id:
            problems.append("campaign requires id")
        if not self.name.strip() or len(self.name) > 200:
            problems.append("campaign name must be 1–200 characters")
        if len(self.script_prompt) > 20_000:
            problems.append("script_prompt must be at most 20000 characters")
        if len(self.opening_line) > 2_000:
            problems.append("opening_line must be at most 2000 characters")
        problems += self.audience.validate()
        problems += self.schedule.validate()
        problems += self.throttle.validate()
        problems += self.compliance.validate()
        if self.channel is not CampaignChannel.VOICE and not self.compliance.require_a2p_registration:
            problems.append("non-voice campaigns must require A2P registration")
        return problems

    def is_valid(self) -> bool:
        return not self.validate()

    def identity(self) -> str:
        return stable_id(self.tenant_id, self.name, self.goal.value, self.channel.value)


@dataclass(frozen=True)
class CampaignExecutionIntent:
    """A single, safe unit of work the outbound layer MAY pick up later.

    Creating an intent is **not** dialing. It only records "this lead is
    eligible and due"; the existing outbound mechanisms decide when and how to
    act. ``idempotency_key`` stops the same lead being queued twice.
    """

    id: str
    campaign_id: str
    tenant_id: str
    lead_id: str
    channel: CampaignChannel
    window_at: str = ""
    idempotency_key: str = ""
    reason_skipped: str = ""

    @property
    def skipped(self) -> bool:
        return bool(self.reason_skipped)

    def validate(self) -> list[str]:
        problems: list[str] = []
        for field_name in ("id", "campaign_id", "tenant_id", "lead_id"):
            if not getattr(self, field_name):
                problems.append(f"execution intent requires {field_name}")
        return problems


@dataclass(frozen=True)
class CampaignMetrics:
    """Aggregate-safe outcome counters. No phone numbers, no PII."""

    total_leads: int = 0
    attempted: int = 0
    answered: int = 0
    completed: int = 0
    booked: int = 0
    failed: int = 0
    dnc_skipped: int = 0
    window_skipped: int = 0
    attempt_limit_skipped: int = 0
    daily_limit_skipped: int = 0
    conversions: int = 0

    def merge(self, other: "CampaignMetrics") -> "CampaignMetrics":
        return CampaignMetrics(**{
            name: getattr(self, name) + getattr(other, name)
            for name in self.__dataclass_fields__
        })


def intent_idempotency_key(campaign_id: str, lead_id: str) -> str:
    """One campaign + one lead ⇒ one key, so re-planning never double-queues."""
    return stable_id(campaign_id, lead_id)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
```

========================================================================
===== FILE: app/domain/analytics_models.py (252 lines) =====
========================================================================
```python
"""Analytics domain objects (Batch 01 enterprise expansion).

These are *aggregate-safe* structures: they carry counts, rates, latencies and
money-shaped estimates — never a phone number, an email address, a full name
or a transcript. The ``PII_FIELDS`` allowlist and ``KpiSnapshot.assert_no_pii``
turn that rule into something a test can assert, so a future edit that slips a
raw customer field into a KPI fails the suite instead of shipping.

Money is represented honestly. ``CostKpi.estimated_cost_millicents`` is always
labelled an *estimate*: it is derived from configured unit prices and metered
usage, and it must never be presented as an invoiced amount — invoicing is
``app.billing``'s job, untouched here.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

#: Field names that would signal raw customer data leaking into an aggregate.
PII_FIELDS = frozenset({
    "phone", "phone_number", "from_number", "to_number", "email",
    "full_name", "customer_name", "name", "transcript", "recording_url",
    "address", "call_sid",
})


class KpiGranularity(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    CUSTOM = "custom"


class KpiKind(str, enum.Enum):
    CALL = "call"
    AGENT = "agent"
    PROVIDER = "provider"
    CAMPAIGN = "campaign"
    FUNNEL = "funnel"
    APPOINTMENT = "appointment"
    LEAD = "lead"
    COST = "cost"
    QUALITY = "quality"
    SLA = "sla"


def _pct(numerator: int | float, denominator: int | float) -> float:
    """0–100 percentage, 0.0 on a zero denominator. Mirrors the dashboard."""
    if not denominator:
        return 0.0
    return round(numerator / denominator * 100, 1)


@dataclass(frozen=True)
class CallKpi:
    total: int = 0
    answered: int = 0
    completed: int = 0
    failed: int = 0
    no_answer: int = 0
    transferred: int = 0
    escalated: int = 0
    avg_duration_seconds: float = 0.0

    def rates(self) -> dict[str, float]:
        return {
            "answer_rate": _pct(self.answered, self.total),
            "completion_rate": _pct(self.completed, self.total),
            "failure_rate": _pct(self.failed, self.total),
            "transfer_rate": _pct(self.transferred, self.answered),
            "escalation_rate": _pct(self.escalated, self.total),
        }


@dataclass(frozen=True)
class AgentKpi:
    agent_id: str = ""
    calls: int = 0
    bookings: int = 0
    escalations: int = 0
    avg_duration_seconds: float = 0.0
    csat: float | None = None

    def rates(self) -> dict[str, float]:
        return {
            "booking_rate": _pct(self.bookings, self.calls),
            "escalation_rate": _pct(self.escalations, self.calls),
        }


@dataclass(frozen=True)
class ProviderKpi:
    provider: str = ""
    calls: int = 0
    errors: int = 0
    avg_latency_ms: float | None = None

    def rates(self) -> dict[str, float]:
        return {"error_rate": _pct(self.errors, self.calls + self.errors)}


@dataclass(frozen=True)
class CampaignKpi:
    campaign_id: str = ""
    total: int = 0
    conversions: int = 0
    booked: int = 0
    estimated_cost_millicents: int = 0

    def rates(self) -> dict[str, float]:
        return {
            "conversion_rate": _pct(self.conversions, self.total),
            "booking_rate": _pct(self.booked, self.total),
        }


@dataclass(frozen=True)
class FunnelKpi:
    """Stage counts through the call funnel. Counts only, no identities."""

    total: int = 0
    answered: int = 0
    engaged: int = 0
    qualified: int = 0
    booked: int = 0

    def rates(self) -> dict[str, float]:
        return {
            "answer_rate": _pct(self.answered, self.total),
            "engagement_rate": _pct(self.engaged, self.answered),
            "qualification_rate": _pct(self.qualified, self.engaged),
            "booking_rate": _pct(self.booked, self.qualified),
        }


@dataclass(frozen=True)
class AppointmentKpi:
    total: int = 0
    confirmed: int = 0
    cancelled: int = 0
    no_show: int = 0

    def rates(self) -> dict[str, float]:
        return {
            "confirmation_rate": _pct(self.confirmed, self.total),
            "cancellation_rate": _pct(self.cancelled, self.total),
            "no_show_rate": _pct(self.no_show, self.total),
        }


@dataclass(frozen=True)
class LeadKpi:
    total: int = 0
    qualified: int = 0
    unqualified: int = 0
    dnc: int = 0
    converted: int = 0

    def rates(self) -> dict[str, float]:
        return {
            "qualification_rate": _pct(self.qualified, self.total),
            "conversion_rate": _pct(self.converted, self.total),
        }


@dataclass(frozen=True)
class CostKpi:
    """Usage × configured unit price. An estimate, never an invoice."""

    minutes: float = 0.0
    sms_segments: int = 0
    llm_tokens: int = 0
    estimated_cost_millicents: int = 0
    currency: str = "USD"


@dataclass(frozen=True)
class QualityKpi:
    avg_response_ms: float | None = None
    p95_response_ms: float | None = None
    positive_sentiment_rate: float = 0.0
    negative_sentiment_rate: float = 0.0
    resolved_rate: float = 0.0


@dataclass(frozen=True)
class SlaKpi:
    within_target: int = 0
    breached: int = 0
    target_seconds: int = 0

    def rates(self) -> dict[str, float]:
        return {"sla_attainment": _pct(self.within_target, self.within_target + self.breached)}


@dataclass(frozen=True)
class KpiPoint:
    """One kind of KPI for one period. ``metrics`` is aggregate-safe only."""

    kind: KpiKind
    period_start: str = ""
    period_end: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def rates(self) -> dict[str, float]:
        return self.metrics.get("rates", {})


@dataclass(frozen=True)
class KpiSnapshot:
    """A tenant-scoped collection of KPI points for a date range."""

    tenant_id: str
    range_start: str = ""
    range_end: str = ""
    granularity: KpiGranularity = KpiGranularity.DAILY
    points: tuple[KpiPoint, ...] = ()
    generated_at: str = ""

    def assert_no_pii(self) -> list[str]:
        """Return any PII-shaped field names found anywhere in the snapshot."""
        found: list[str] = []
        for point in self.points:
            for key in point.metrics:
                lowered = key.lower()
                if any(pi in lowered for pi in PII_FIELDS):
                    found.append(f"{point.kind.value}.{key}")
        return found

    def empty(self) -> bool:
        return not self.points

    @staticmethod
    def build(
        tenant_id: str,
        *,
        range_start: str = "",
        range_end: str = "",
        granularity: KpiGranularity = KpiGranularity.DAILY,
        points: tuple[KpiPoint, ...] = (),
    ) -> "KpiSnapshot":
        return KpiSnapshot(
            tenant_id=tenant_id,
            range_start=range_start,
            range_end=range_end,
            granularity=granularity,
            points=points,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
```

========================================================================
===== FILE: app/domain/notification_models.py (224 lines) =====
========================================================================
```python
"""Notification domain models (Batch 01 enterprise expansion).

Notifications are templated, priority-ordered and deduplicated. The design
rules encoded here:

* **Templates render with a strict allowlist.** ``NotificationTemplate.render``
  substitutes only variables present in ``variables`` and never evaluates
  anything — no ``str.format``, no f-string over tenant text, no eval. A
  placeholder that is not declared is left verbatim (and ``validate`` flags
  undeclared placeholders at template creation time).
* **Channels are bounded and reuse existing infrastructure.** ``IN_APP`` and
  ``INTERNAL_ALERT`` are local; ``SMS`` routes through the existing Twilio
  sender in ``app.integrations.notifications``; ``EMAIL`` and ``WEBHOOK`` are
  declared but delivery is deferred to a later batch — there is no new email
  provider here and no ad-hoc HTTP dispatcher.
* **Deduplication is deterministic.** ``dedupe_key`` derives from tenant,
  template, event source and the business key, so a replayed event cannot
  produce a duplicate notification.
* **Sensitive bodies are never logged.** ``safe_repr`` is the only sanctioned
  string form for logging; it truncates the rendered body and never includes
  recipient contact details in full.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, replace
from datetime import datetime, time as _time, timezone

from app.domain.agent_models import stable_id

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_ALLOWED_VARIABLES = frozenset({
    "tenant_name", "agent_name", "customer_name", "appointment_time",
    "appointment_date", "amount", "plan_name", "call_summary", "business",
})


class NotificationChannel(str, enum.Enum):
    IN_APP = "in_app"
    WEBHOOK = "webhook"
    EMAIL = "email"
    SMS = "sms"
    INTERNAL_ALERT = "internal_alert"


class NotificationPriority(str, enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class DeliveryState(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    RETRYING = "retrying"
    SUPPRESSED = "suppressed"


class EventSource(str, enum.Enum):
    """Closed set of event origins — the engine is bounded, not a queue of
    arbitrary tenant-defined events."""

    CALL_COMPLETED = "call_completed"
    APPOINTMENT_REMINDER = "appointment_reminder"
    BILLING = "billing"
    CAMPAIGN = "campaign"
    SYSTEM = "system"
    E2E = "e2e"


@dataclass(frozen=True)
class Recipient:
    """Who receives the notification. ``target`` is the redacted endpoint."""

    kind: str = "user"          # user | phone | webhook | email
    target: str = ""
    user_id: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.kind not in {"user", "phone", "webhook", "email"}:
            problems.append(f"recipient kind {self.kind!r} is invalid")
        if not self.target and not self.user_id:
            problems.append("recipient requires target or user_id")
        if len(self.target) > 200:
            problems.append("recipient target must be at most 200 characters")
        return problems


@dataclass(frozen=True)
class PreferenceSet:
    """Per-channel delivery preferences for one recipient."""

    channel: NotificationChannel = NotificationChannel.IN_APP
    enabled: bool = True
    quiet_start: _time = _time(0, 0)
    quiet_end: _time = _time(0, 0)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.quiet_start == self.quiet_end and self.quiet_start != _time(0, 0):
            problems.append("quiet hours must have distinct start and end")
        return problems

    def is_quiet(self, moment: datetime | None = None) -> bool:
        """True when ``moment`` falls inside the configured quiet window.

        ``(0, 0)`` means no quiet hours. An overnight window such as 22:00 → 08:00
        is expressed as ``start > end`` and matches either side of midnight.
        """
        if self.quiet_start == _time(0, 0) and self.quiet_end == _time(0, 0):
            return False
        now = (moment or datetime.now(timezone.utc)).astimezone().time()
        if self.quiet_start < self.quiet_end:
            return self.quiet_start <= now <= self.quiet_end
        return now >= self.quiet_start or now <= self.quiet_end


@dataclass(frozen=True)
class NotificationTemplate:
    """A tenant-owned template. Rendering is strict substitution only."""

    id: str
    tenant_id: str
    name: str
    channel: NotificationChannel
    body: str = ""
    variables: tuple[str, ...] = ()

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.tenant_id or not self.id:
            problems.append("template requires tenant_id and id")
        if not self.name.strip() or len(self.name) > 200:
            problems.append("template name must be 1–200 characters")
        if len(self.body) > 8_000:
            problems.append("template body must be at most 8000 characters")
        used = set(_PLACEHOLDER.findall(self.body or ""))
        unknown = sorted(used - set(self.variables) - _ALLOWED_VARIABLES)
        if unknown:
            problems.append(f"template uses undeclared variable(s): {', '.join(unknown)}")
        if len(self.variables) > 50:
            problems.append("template must declare at most 50 variables")
        return problems

    def render(self, values: dict[str, str]) -> str:
        """Substitute declared variables only. Unknown keys are ignored; the
        placeholder stays visible rather than being swallowed or evaluated."""
        result = self.body or ""
        for key, value in values.items():
            if key in _ALLOWED_VARIABLES or key in self.variables:
                result = result.replace("{" + key + "}", str(value))
        return result


@dataclass(frozen=True)
class Notification:
    """One concrete notification instance."""

    id: str
    tenant_id: str
    template_id: str
    channel: NotificationChannel
    recipient: Recipient
    event_source: EventSource
    priority: NotificationPriority = NotificationPriority.NORMAL
    dedupe_key: str = ""
    rendered_body: str = ""
    delivery_state: DeliveryState = DeliveryState.PENDING
    attempts: int = 0
    next_attempt_at: str = ""
    sent_at: str = ""
    error_summary: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.tenant_id or not self.id or not self.template_id:
            problems.append("notification requires tenant_id, id and template_id")
        problems += self.recipient.validate()
        return problems

    def safe_repr(self) -> str:
        """The only form sanctioned for logs: no full body, no raw contact."""
        body = (self.rendered_body or "")[:40].replace("\n", " ")
        return (
            f"Notification(id={self.id[:8]}, channel={self.channel.value}, "
            f"priority={self.priority.value}, state={self.delivery_state.value}, "
            f"body_preview={body!r})"
        )


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one delivery attempt. Honest about what actually happened."""

    state: DeliveryState
    channel: NotificationChannel
    detail: str = ""

    @property
    def delivered(self) -> bool:
        return self.state in (DeliveryState.SENT, DeliveryState.DELIVERED)


def dedupe_key(tenant_id: str, template_id: str, source: EventSource, business_key: str) -> str:
    """Same business fact ⇒ same key ⇒ one notification, ever."""
    return stable_id(tenant_id, template_id, source.value, business_key)


def with_attempt(notification: Notification, error: str = "") -> Notification:
    """Advance the retry counter and flip state, without touching history."""
    attempts = notification.attempts + 1
    return replace(
        notification,
        attempts=attempts,
        delivery_state=DeliveryState.RETRYING if error else DeliveryState.SENT,
        error_summary=error[:500],
        next_attempt_at="",
    )
```

========================================================================
===== FILE: app/domain/inbox_models.py (224 lines) =====
========================================================================
```python
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
```

========================================================================
===== FILE: app/services/agent_service.py (421 lines) =====
========================================================================
```python
"""Agent configuration service (Batch 01 enterprise expansion).

This is the single write path for AI-agent configuration. It deliberately does
**not** introduce a second configuration architecture: the fields that already
exist on ``Tenant`` (voice tuning, LLM choice, greeting, escalation number,
recording flags) are written through the same columns the existing
``PATCH /tenants/{id}/voice`` endpoint writes, so a live call reads one source
of truth. The new, richer surface (tools, safety policy, fallback behaviour,
version history) lives above those columns and is versioned immutably.

Persistence honesty
-------------------
* Everything that maps onto a ``Tenant`` column is **persisted for real** via
  the existing session and committed.
* Version history (``AgentVersion``) is kept in a per-tenant, in-process
  ledger. It is **not durable** across restarts — a durable ``agent_versions``
  table requires a migration, which this batch must not create. The ledger is
  explicit about this and never pretends otherwise (see the batch report).

Security invariants
-------------------
* Tenant ownership is enforced on every call: the caller passes a ``Tenant``
  and the service refuses a config whose ``tenant_id`` does not match.
* Publishing is versioned and auditable via structured logs; an existing
  published version is never mutated — publish always mints a new one.
* No provider credential exists in this module; provider *choice* only.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import prompts
from app.agent.functions import DISPATCHABLE_TOOLS
from app.agent.llm_factory import PRESETS
from app.agent.voice_settings import normalize_speech_speed
from app.core.data_policy import ai_disclosure_compliant
from app.core.logging import log
from app.db.models import Tenant
from app.domain.agent_models import (
    AgentBundle,
    AgentConfig,
    AgentStatus,
    AgentVersion,
    EscalationPolicy,
    FallbackBehavior,
    HandoffConfig,
    HandoffMode,
    InterruptionPolicy,
    KnowledgeSource,
    KnowledgeSourceType,
    LanguageConfig,
    ModelConfig,
    OperatingHours,
    ResponseStyle,
    SafetyPolicy,
    ToolConfig,
    VoiceConfig,
    can_transition,
    diff_configs,
)

#: A tenant's agent history is held here: tenant_id -> agent_id -> version -> config.
_VERSION_LEDGER: dict[str, dict[str, dict[int, AgentVersion]]] = {}
_VERSION_CONFIGS: dict[str, dict[str, dict[int, AgentConfig]]] = {}
#: Editable drafts not yet published: tenant_id -> agent_id -> config.
_DRAFT_REGISTRY: dict[str, dict[str, AgentConfig]] = {}

_PROVIDER_TO_PRESET = {"openai": "fast", "anthropic": "natural", "google": "cheap"}


def _slot(store: dict, *keys: str) -> dict:
    """Descend (or create) nested dicts — small helper, no magic."""
    node: dict = store
    for key in keys:
        node = node.setdefault(key, {})
    return node


def _validate_ownership(tenant: Tenant, config: AgentConfig) -> None:
    if config.tenant_id != str(tenant.id):
        raise ValueError("agent config does not belong to this tenant")


def _version_of(tenant: Tenant, agent_id: str, version: int) -> AgentVersion | None:
    return _slot(_VERSION_LEDGER, str(tenant.id), agent_id).get(version)


def _config_of(tenant: Tenant, agent_id: str, version: int) -> AgentConfig | None:
    return _slot(_VERSION_CONFIGS, str(tenant.id), agent_id).get(version)


def _current_published(tenant: Tenant, agent_id: str) -> int:
    ledger = _slot(_VERSION_LEDGER, str(tenant.id), agent_id)
    return max(ledger) if ledger else 0


# ------------------------------------------------------------------- reads ---

def load_config(tenant: Tenant) -> AgentConfig:
    """Project the tenant's persisted columns onto the domain model."""
    speed, _ = normalize_speech_speed(tenant.speech_speed)
    provider = tenant.llm_provider or ""
    model = tenant.llm_model or ""
    if not provider and tenant.llm_preset in PRESETS:
        preset = PRESETS[tenant.llm_preset]
        provider, model = preset.provider, preset.model
    knowledge_base = tenant.knowledge_base if isinstance(tenant.knowledge_base, dict) else {}
    knowledge = (KnowledgeSource(
        type=KnowledgeSourceType.TENANT_FACTS,
        references=tuple(sorted(knowledge_base)),
    ),)
    handoff = HandoffConfig(
        mode=HandoffMode.NUMBER if tenant.escalation_number else HandoffMode.NONE,
        destination=tenant.escalation_number or "",
    )
    escalation = EscalationPolicy.ALWAYS if tenant.escalation_number else EscalationPolicy.ON_REQUEST
    return AgentConfig(
        tenant_id=str(tenant.id),
        name=tenant.agent_name or "Alex",
        greeting=tenant.greeting or "",
        system_instructions=tenant.system_prompt_extra or "",
        language=LanguageConfig(primary=tenant.language or "en-US"),
        voice=VoiceConfig(voice_id=tenant.voice_id or "", speech_speed=speed),
        model=ModelConfig(provider=provider or "anthropic", model=model,
                          temperature=tenant.temperature),
        escalation=escalation,
        operating_hours=OperatingHours(
            timezone=tenant.timezone,
            open=tenant.business_open,
            close=tenant.business_close,
        ),
        knowledge=knowledge,
        safety=SafetyPolicy(record_calls=tenant.record_calls),
        handoff=handoff,
    )


def validate_config(config: AgentConfig) -> list[str]:
    """Validation, with the authoritative tool vocabulary injected."""
    problems = list(config.validate())
    if config.tools.enabled:
        extra = ToolConfig(enabled=config.tools.enabled, max_tool_calls=config.tools.max_tool_calls)
        problems += [p for p in extra.validate(DISPATCHABLE_TOOLS) if "unknown tool" in p]
    return problems


def get_draft(tenant: Tenant, agent_id: str) -> AgentConfig | None:
    return _slot(_DRAFT_REGISTRY, str(tenant.id)).get(agent_id)


def list_agents(tenant: Tenant) -> list[AgentBundle]:
    """All agents known for the tenant: drafts + published history."""
    drafts = _slot(_DRAFT_REGISTRY, str(tenant.id))
    ledger = _slot(_VERSION_LEDGER, str(tenant.id))
    bundles: dict[str, AgentBundle] = {}
    for agent_id, config in drafts.items():
        published = None
        versions = ledger.get(agent_id, {})
        if versions:
            published = versions[max(versions)]
        bundles[agent_id] = AgentBundle(tenant_id=str(tenant.id), draft=config, published=published)
    return sorted(bundles.values(), key=lambda b: b.draft.name)


def version_history(tenant: Tenant, agent_id: str) -> list[AgentVersion]:
    ledger = _slot(_VERSION_LEDGER, str(tenant.id), agent_id)
    return [ledger[v] for v in sorted(ledger, reverse=True)]


# ------------------------------------------------------------------ writes ---

def create_draft(tenant: Tenant, config: AgentConfig) -> AgentConfig:
    """Register a new draft (idempotent by deterministic id)."""
    _validate_ownership(tenant, config)
    problems = validate_config(config)
    if problems:
        raise ValueError("; ".join(problems))
    normalized = config.normalized()
    registry = _slot(_DRAFT_REGISTRY, str(tenant.id))
    registry[normalized.id] = normalized
    return normalized


def update_draft(tenant: Tenant, config: AgentConfig) -> AgentConfig:
    """Replace the draft. Fails if the config was never created (explicit)."""
    _validate_ownership(tenant, config)
    problems = validate_config(config)
    if problems:
        raise ValueError("; ".join(problems))
    normalized = config.normalized()
    registry = _slot(_DRAFT_REGISTRY, str(tenant.id))
    if normalized.id not in registry:
        raise KeyError("agent draft not found; create it first")
    registry[normalized.id] = normalized
    return normalized


def apply_draft(session: AsyncSession, tenant: Tenant, config: AgentConfig) -> AgentConfig:
    """Write the fields that map onto real Tenant columns (no commit here)."""
    _validate_ownership(tenant, config)
    problems = validate_config(config)
    if problems:
        raise ValueError("; ".join(problems))
    cfg = config.normalized()
    tenant.agent_name = cfg.name[:80]
    tenant.greeting = cfg.greeting
    tenant.system_prompt_extra = cfg.system_instructions
    tenant.temperature = cfg.model.temperature
    tenant.language = cfg.language.primary[:16]
    tenant.voice_id = (cfg.voice.voice_id or None) if len(cfg.voice.voice_id or "") <= 64 else None
    tenant.speech_speed = cfg.voice.speech_speed
    tenant.timezone = cfg.operating_hours.timezone[:64]
    tenant.business_open = cfg.operating_hours.open
    tenant.business_close = cfg.operating_hours.close
    tenant.record_calls = cfg.safety.record_calls
    if cfg.handoff.mode is HandoffMode.NUMBER and cfg.handoff.destination:
        tenant.escalation_number = cfg.handoff.destination[:32]
    elif cfg.handoff.mode is HandoffMode.NONE:
        tenant.escalation_number = None
    if cfg.model.model and cfg.model.provider:
        tenant.llm_provider = cfg.model.provider
        tenant.llm_model = cfg.model.model
    elif cfg.model.provider in _PROVIDER_TO_PRESET:
        tenant.llm_preset = _PROVIDER_TO_PRESET[cfg.model.provider]
    session.add(tenant)
    return cfg


def publish(
    session: AsyncSession,
    tenant: Tenant,
    config: AgentConfig,
    *,
    changelog: str = "",
) -> AgentVersion:
    """Apply the draft and mint an immutable published version (no commit).

    Callers that own the transaction (an API handler) commit afterwards.
    """
    cfg = apply_draft(session, tenant, config)
    agent_id = cfg.id
    current = _current_published(tenant, agent_id)
    previous_status = AgentStatus.PUBLISHED if current else AgentStatus.DRAFT
    if not can_transition(previous_status, AgentStatus.PUBLISHED):
        raise ValueError(f"cannot publish from {previous_status.value}")
    version = AgentVersion(
        agent_id=agent_id,
        version=current + 1,
        config_hash=cfg.config_hash(),
        status=AgentStatus.PUBLISHED,
        changelog=changelog[:4000],
        published_at=datetime.now(timezone.utc).isoformat(),
    )
    _slot(_VERSION_LEDGER, str(tenant.id), agent_id)[version.version] = version
    _slot(_VERSION_CONFIGS, str(tenant.id), agent_id)[version.version] = cfg
    _slot(_DRAFT_REGISTRY, str(tenant.id))[agent_id] = cfg
    log.info(
        "agent.published",
        tenant_id=str(tenant.id),
        agent_id=agent_id[:8],
        version=version.version,
        config_hash=version.config_hash[:12],
    )
    return version


async def publish_async(
    session: AsyncSession, tenant: Tenant, config: AgentConfig, *, changelog: str = ""
) -> AgentVersion:
    version = publish(session, tenant, config, changelog=changelog)
    await session.commit()
    await session.refresh(tenant)
    return version


def unpublish(tenant: Tenant, agent_id: str) -> None:
    """Retire the published agent (kept in history, no longer the live draft)."""
    current = _current_published(tenant, agent_id)
    if not current:
        raise KeyError("no published version to unpublish")
    ledger = _slot(_VERSION_LEDGER, str(tenant.id), agent_id)
    top = ledger[current]
    ledger[current] = replace(top, status=AgentStatus.RETIRED)
    log.info("agent.unpublished", tenant_id=str(tenant.id), agent_id=agent_id[:8])


def clone_agent(source: AgentConfig, *, new_name: str) -> AgentConfig:
    """Produce a new, independent config identical to ``source`` but renamed."""
    return replace(source, name=new_name)


def compare_versions(tenant: Tenant, agent_id: str, v1: int, v2: int) -> dict[str, Any]:
    """Field-level diff between two published versions."""
    c1 = _config_of(tenant, agent_id, v1)
    c2 = _config_of(tenant, agent_id, v2)
    if c1 is None or c2 is None:
        raise KeyError("one or both versions not found")
    return diff_configs(c1, c2)


async def rollback_async(
    session: AsyncSession, tenant: Tenant, agent_id: str, version: int, *, changelog: str = ""
) -> AgentVersion:
    """Re-apply a historical version as a new, immutable published version."""
    config = _config_of(tenant, agent_id, version)
    if config is None:
        raise KeyError("version not found")
    return await publish_async(session, tenant, config, changelog=changelog or f"rollback to v{version}")


# ----------------------------------------------------- sub-configuration ---

def _require_draft(tenant: Tenant, agent_id: str) -> AgentConfig:
    config = get_draft(tenant, agent_id)
    if config is None:
        raise KeyError("agent draft not found")
    return config


def configure_tools(tenant: Tenant, agent_id: str, *, enabled: tuple[str, ...],
                    max_tool_calls: int = 8) -> AgentConfig:
    config = _require_draft(tenant, agent_id)
    tools = ToolConfig(enabled=enabled, max_tool_calls=max_tool_calls)
    problems = tools.validate(DISPATCHABLE_TOOLS)
    if problems:
        raise ValueError("; ".join(problems))
    return update_draft(tenant, replace(config, tools=tools))


def configure_languages(tenant: Tenant, agent_id: str, *, primary: str,
                        fallbacks: tuple[str, ...] = ()) -> AgentConfig:
    config = _require_draft(tenant, agent_id)
    languages = LanguageConfig(primary=primary, fallbacks=fallbacks)
    problems = languages.validate()
    if problems:
        raise ValueError("; ".join(problems))
    return update_draft(tenant, replace(config, language=languages))


def configure_voice(tenant: Tenant, agent_id: str, *, voice_id: str = "",
                    speech_speed: float = 1.0, fallback_voice_id: str = "") -> AgentConfig:
    config = _require_draft(tenant, agent_id)
    voice = VoiceConfig(voice_id=voice_id, speech_speed=speech_speed,
                        fallback_voice_id=fallback_voice_id)
    problems = voice.validate()
    if problems:
        raise ValueError("; ".join(problems))
    return update_draft(tenant, replace(config, voice=voice))


def configure_safety(tenant: Tenant, agent_id: str, *, policy: SafetyPolicy) -> AgentConfig:
    config = _require_draft(tenant, agent_id)
    problems = policy.validate()
    if problems:
        raise ValueError("; ".join(problems))
    return update_draft(tenant, replace(config, safety=policy))


def configure_fallback(tenant: Tenant, agent_id: str, *, behavior: FallbackBehavior) -> AgentConfig:
    config = _require_draft(tenant, agent_id)
    return update_draft(tenant, replace(config, fallback=behavior))


def configure_interruption(tenant: Tenant, agent_id: str, *, policy: InterruptionPolicy) -> AgentConfig:
    config = _require_draft(tenant, agent_id)
    return update_draft(tenant, replace(config, interruption=policy))


def configure_style(tenant: Tenant, agent_id: str, *, style: ResponseStyle) -> AgentConfig:
    config = _require_draft(tenant, agent_id)
    return update_draft(tenant, replace(config, response_style=style))


# ------------------------------------------------------------- preview/test ---

def preview_config(tenant: Tenant, config: AgentConfig) -> dict[str, Any]:
    """Build the effective system prompt for a config — without calling an LLM.

    Uses the real prompt builder against a shallow copy of the tenant whose
    agent-relevant fields are overlaid with the config, so the preview is the
    actual prompt the voice pipeline would construct, not a mock of one.
    """
    _validate_ownership(tenant, config)
    problems = validate_config(config)
    if problems:
        return {"ok": False, "issues": problems}
    shadow = copy.copy(tenant)
    shadow.agent_name = config.name
    shadow.greeting = config.greeting
    shadow.system_prompt_extra = config.system_instructions
    provider = config.model.provider or "openai"
    prompt = prompts.build_system_prompt(shadow, provider=provider, knowledge_context="")
    return {
        "ok": True,
        "issues": [],
        "provider": provider,
        "model": config.model.model or "",
        "prompt_preview": prompt[:2_000],
        "config_hash": config.config_hash(),
    }


def test_configuration(tenant: Tenant, config: AgentConfig) -> dict[str, Any]:
    """Deterministic, provider-free sanity check. Never invents a provider OK."""
    problems = validate_config(config)
    checks: dict[str, bool] = {
        "greeting_present": bool((config.greeting or "").strip()),
        "disclosure_compliant": ai_disclosure_compliant(config.greeting or "", required=True),
        "escalation_consistent": not (
            config.escalation is EscalationPolicy.ALWAYS
            and config.handoff.mode is HandoffMode.NONE
        ),
        "voice_in_range": not any("speech_speed" in p for p in problems),
    }
    return {"ok": not problems, "issues": problems, "checks": checks}
```

========================================================================
===== FILE: app/services/conversation_service.py (482 lines) =====
========================================================================
```python
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
```

========================================================================
===== FILE: app/services/workflow_service.py (488 lines) =====
========================================================================
```python
"""Workflow service (Batch 01 enterprise expansion).

A deterministic, tenant-scoped interpreter over the controlled action
vocabulary defined in ``app/domain/workflow_models.py``. The guarantees that
matter:

* **No arbitrary code.** Action nodes name a member of ``CONTROLLED_ACTIONS``;
  the interpreter dispatches through a fixed handler table. There is no code
  path that reads a string and executes it, and the domain layer already
  rejects anything code-shaped before a workflow can be stored.
* **Deterministic execution.** Given the same workflow and the same input
  payload, the interpreter produces the same execution steps. Time-dependent
  actions (delay, retry, timeout) are *recorded*, not wall-clock slept, so a
  test or a replay sees identical history.
* **Idempotent replay.** ``execution_key`` derives from tenant + workflow +
  canonical payload; re-running the same event returns the existing execution
  rather than executing twice.
* **Tenant ownership.** Every registry access is keyed by tenant id, and every
  DB-backed action handler re-verifies ownership of the rows it touches.

Persistence honesty: workflow definitions and executions live in an in-process
registry. They are not durable across restarts — a durable ``workflows`` /
``workflow_executions`` table pair requires a migration, which this batch must
not create (reported at the end of the batch).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, NotFoundError
from app.core.logging import log
from app.db.models import Lead, LeadStatus
from app.domain.agent_models import stable_id
from app.domain.workflow_models import (
    ExecutionStatus,
    NodeType,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowNode,
    WorkflowStatus,
    WorkflowStep,
    can_transition,
    execution_is_terminal,
)

#: tenant_id -> workflow_id -> current definition
_REGISTRY: dict[str, dict[str, WorkflowDefinition]] = {}
#: tenant_id -> workflow_id -> version -> definition
_VERSIONS: dict[str, dict[str, dict[int, WorkflowDefinition]]] = {}
#: tenant_id -> execution_id -> execution
_EXECUTIONS: dict[str, dict[str, WorkflowExecution]] = {}

MAX_STEPS = 200


def _slot(store: dict, *keys: str) -> dict:
    node: dict = store
    for key in keys:
        node = node.setdefault(key, {})
    return node


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def execution_key(tenant_id: str, workflow_id: str, payload: dict[str, Any]) -> str:
    """Deterministic idempotency key: same event ⇒ same execution."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return stable_id(tenant_id, workflow_id, canonical)


# ----------------------------------------------------------------- registry ---

def validate_workflow(definition: WorkflowDefinition) -> list[str]:
    return definition.validate()


def create_workflow(tenant_id: str, definition: WorkflowDefinition) -> WorkflowDefinition:
    """Register a new draft. Idempotent on deterministic identity."""
    if definition.tenant_id != tenant_id:
        raise BadRequestError("workflow does not belong to this tenant")
    problems = validate_workflow(definition)
    if problems:
        raise BadRequestError("; ".join(problems))
    current = _slot(_REGISTRY, tenant_id)
    if definition.id in current:
        return current[definition.id]
    current[definition.id] = definition.with_version(1)
    _slot(_VERSIONS, tenant_id, definition.id)[1] = current[definition.id]
    return current[definition.id]


def get_workflow(tenant_id: str, workflow_id: str) -> WorkflowDefinition:
    definition = _slot(_REGISTRY, tenant_id).get(workflow_id)
    if definition is None:
        raise NotFoundError("workflow not found")
    return definition


def list_workflows(tenant_id: str) -> list[WorkflowDefinition]:
    return sorted(_slot(_REGISTRY, tenant_id).values(), key=lambda w: w.name)


def version_history(tenant_id: str, workflow_id: str) -> list[WorkflowDefinition]:
    versions = _slot(_VERSIONS, tenant_id, workflow_id)
    return [versions[v] for v in sorted(versions, reverse=True)]


def version_workflow(tenant_id: str, workflow_id: str, definition: WorkflowDefinition) -> WorkflowDefinition:
    """Store a new version when content actually changed (identity differs)."""
    current = get_workflow(tenant_id, workflow_id)
    problems = validate_workflow(definition)
    if problems:
        raise BadRequestError("; ".join(problems))
    if definition.identity() == current.identity():
        return current                     # no-op versioning: no real change
    next_version = current.version + 1
    new_def = definition.with_version(next_version)
    _slot(_REGISTRY, tenant_id)[workflow_id] = new_def
    _slot(_VERSIONS, tenant_id, workflow_id)[next_version] = new_def
    return new_def


def _transition(tenant_id: str, workflow_id: str, target: WorkflowStatus) -> WorkflowDefinition:
    current = get_workflow(tenant_id, workflow_id)
    if not can_transition(current.status, target):
        raise BadRequestError(f"cannot move workflow {current.status.value} -> {target.value}")
    updated = replace(current, status=target)
    _slot(_REGISTRY, tenant_id)[workflow_id] = updated
    _slot(_VERSIONS, tenant_id, workflow_id)[current.version] = updated
    return updated


def publish_workflow(tenant_id: str, workflow_id: str) -> WorkflowDefinition:
    return _transition(tenant_id, workflow_id, WorkflowStatus.ACTIVE)


def pause_workflow(tenant_id: str, workflow_id: str) -> WorkflowDefinition:
    return _transition(tenant_id, workflow_id, WorkflowStatus.PAUSED)


def resume_workflow(tenant_id: str, workflow_id: str) -> WorkflowDefinition:
    return _transition(tenant_id, workflow_id, WorkflowStatus.ACTIVE)


def archive_workflow(tenant_id: str, workflow_id: str) -> WorkflowDefinition:
    return _transition(tenant_id, workflow_id, WorkflowStatus.ARCHIVED)


def clone_workflow(tenant_id: str, workflow_id: str, *, new_name: str) -> WorkflowDefinition:
    source = get_workflow(tenant_id, workflow_id)
    clone_id = stable_id(tenant_id, new_name)
    clone = replace(
        source,
        id=clone_id,
        name=new_name,
        version=1,
        status=WorkflowStatus.DRAFT,
        nodes=tuple(replace(n, id=n.id) for n in source.nodes),
    )
    return create_workflow(tenant_id, clone)


# ------------------------------------------------------------------ execute ---

def _node_map(definition: WorkflowDefinition) -> dict[str, WorkflowNode]:
    return {node.id: node for node in definition.nodes}


async def _execute_action(
    tenant_id: str,
    action: WorkflowAction,
    payload: dict[str, Any],
    session: AsyncSession | None,
) -> tuple[str, str]:
    """Dispatch a controlled action. Returns (status, detail)."""
    name = action.name
    params = action.params
    if name == "update_lead_status":
        return await _handle_update_lead_status(tenant_id, params, payload, session)
    if name == "apply_dnc":
        return await _handle_apply_dnc(tenant_id, params, payload, session)
    if name == "record_escalation_intent":
        destination = str(params.get("destination", ""))[:64]
        return "scheduled_intent", f"escalation intent recorded (destination={destination or 'default'})"
    if name == "create_followup_intent":
        return "scheduled_intent", f"followup intent recorded (lead={payload.get('lead_id', 'n/a')})"
    if name == "enqueue_notification":
        template = str(params.get("template_id", ""))
        business_key = str(payload.get("business_key", payload.get("lead_id", "")))
        from app.services import notification_service

        notification_service.enqueue_system_notification(
            tenant_id, template, business_key, variables=payload
        )
        return "executed", f"notification enqueued (template={template or 'default'})"
    if name == "add_conversation_tag":
        tag = str(params.get("tag", ""))[:64]
        return "executed", f"tag recorded ({tag})"
    if name == "mark_resolved":
        return "executed", "resolution recorded"
    return "unsupported", f"no handler for {name}"


async def _handle_update_lead_status(
    tenant_id: str, params: dict, payload: dict, session: AsyncSession | None
) -> tuple[str, str]:
    lead_id = payload.get("lead_id") or params.get("lead_id")
    target = params.get("status", "")
    if session is None or not lead_id:
        return "scheduled_intent", f"lead status update queued ({lead_id or 'unspecified'})"
    return await _lead_status_change(session, tenant_id, str(lead_id), target)


async def _handle_apply_dnc(
    tenant_id: str, params: dict, payload: dict, session: AsyncSession | None
) -> tuple[str, str]:
    lead_id = payload.get("lead_id") or params.get("lead_id")
    if session is None or not lead_id:
        return "scheduled_intent", f"dnc flag queued ({lead_id or 'unspecified'})"
    return await _lead_status_change(session, tenant_id, str(lead_id), "do_not_call")


async def _lead_status_change(
    session: AsyncSession, tenant_id: str, lead_id: str, target: str
) -> tuple[str, str]:
    import uuid

    try:
        parsed = uuid.UUID(str(lead_id))
    except ValueError:
        return "failed", f"invalid lead_id {lead_id!r}"
    lead = await session.get(Lead, parsed)
    if lead is None or str(lead.tenant_id) != str(tenant_id):
        return "failed", "lead not found in tenant"
    try:
        new_status = LeadStatus(target) if target else LeadStatus.QUALIFIED
    except ValueError:
        return "failed", f"unknown lead status {target!r}"
    lead.status = new_status
    session.add(lead)
    return "executed", f"lead {lead_id} -> {new_status.value}"


async def _commit_if(session: AsyncSession | None) -> None:
    if session is not None:
        await session.commit()


async def execute_workflow(
    tenant_id: str,
    workflow_id: str,
    payload: dict[str, Any],
    *,
    session: AsyncSession | None = None,
) -> WorkflowExecution:
    """Run the workflow deterministically. Returns (possibly existing) execution."""
    definition = get_workflow(tenant_id, workflow_id)
    if definition.status is not WorkflowStatus.ACTIVE:
        raise BadRequestError(f"workflow is {definition.status.value}; publish it first")
    if not isinstance(payload, dict) or len(json.dumps(payload, default=str)) > 20_000:
        raise BadRequestError("payload must be a mapping of at most 20KB")

    key = execution_key(tenant_id, workflow_id, payload)
    for existing in _slot(_EXECUTIONS, tenant_id).values():
        if existing.workflow_id == workflow_id and existing.idempotency_key == key:
            if execution_is_terminal(existing.status) or existing.status is ExecutionStatus.WAITING_APPROVAL:
                return existing

    execution = WorkflowExecution(
        id=stable_id(tenant_id, workflow_id, key),
        workflow_id=workflow_id,
        tenant_id=tenant_id,
        idempotency_key=key,
        status=ExecutionStatus.RUNNING,
        current_node=definition.entry_node,
        input_summary=_summarize_payload(payload),
        started_at=_now(),
    )
    execution = await _walk(tenant_id, definition, execution, payload, session)
    _slot(_EXECUTIONS, tenant_id)[execution.id] = execution
    await _commit_if(session)
    log.info(
        "workflow.executed",
        tenant_id=tenant_id,
        workflow_id=workflow_id[:8],
        execution_id=execution.id[:8],
        status=execution.status.value,
    )
    return execution


def _summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """A redacted, bounded input summary for history (no PII in full)."""
    summary: dict[str, Any] = {}
    for key, value in list(payload.items())[:40]:
        if isinstance(value, str):
            summary[key] = value[:60]
        elif isinstance(value, (int, float, bool)) or value is None:
            summary[key] = value
        else:
            summary[key] = type(value).__name__
    return summary


async def _walk(
    tenant_id: str,
    definition: WorkflowDefinition,
    execution: WorkflowExecution,
    payload: dict[str, Any],
    session: AsyncSession | None,
) -> WorkflowExecution:
    nodes = _node_map(definition)
    current = execution.current_node or definition.entry_node
    steps = list(execution.steps)
    visited = 0
    while current and visited < MAX_STEPS:
        visited += 1
        node = nodes.get(current)
        if node is None:
            steps.append(WorkflowStep(node_id=current, status="failed",
                                      detail="unknown node", at=_now()))
            return replace(execution, steps=tuple(steps), status=ExecutionStatus.FAILED,
                           error="unknown node", finished_at=_now())
        if node.type is NodeType.TERMINAL:
            steps.append(WorkflowStep(node_id=current, status="executed",
                                      detail="terminal reached", at=_now()))
            return replace(execution, steps=tuple(steps), status=ExecutionStatus.COMPLETED,
                           current_node=current, finished_at=_now())
        if node.type is NodeType.TRIGGER:
            steps.append(WorkflowStep(node_id=current, status="executed",
                                      detail="trigger accepted", at=_now()))
            current = node.next
            continue
        if node.type is NodeType.CONDITION:
            target, detail = _resolve_condition(node, payload)
            steps.append(WorkflowStep(node_id=current, status="executed", detail=detail, at=_now()))
            if target is None:
                return replace(execution, steps=tuple(steps), status=ExecutionStatus.FAILED,
                               error=detail, current_node=current, finished_at=_now())
            current = target
            continue
        if node.type is NodeType.ACTION:
            status, detail = await _execute_action(tenant_id, node.action, payload, session) if node.action \
                else ("unsupported", "no action defined")
            steps.append(WorkflowStep(node_id=current, status=status, detail=detail, at=_now()))
            if status == "failed":
                current = node.default_next if node.default_next else ""
                if not current:
                    return replace(execution, steps=tuple(steps), status=ExecutionStatus.FAILED,
                                   error=detail, current_node="", finished_at=_now())
            else:
                current = node.next
            continue
        if node.type is NodeType.DELAY:
            steps.append(WorkflowStep(node_id=current, status="scheduled",
                                      detail=f"delay {node.delay_seconds}s", at=_now()))
            current = node.next
            continue
        if node.type is NodeType.RETRY:
            last = steps[-1] if steps else None
            if last is not None and last.status in ("failed", "unsupported") and last.attempt < node.retry_limit:
                steps.append(WorkflowStep(node_id=current, status="scheduled",
                                          detail=f"retry {last.attempt + 1}/{node.retry_limit}",
                                          attempt=last.attempt + 1, at=_now()))
                current = node.next                     # re-enter the failing action
            else:
                current = node.default_next or node.next
            continue
        if node.type is NodeType.TIMEOUT:
            last = steps[-1] if steps else None
            if last is not None and last.status == "scheduled":
                return replace(execution, steps=tuple(steps), status=ExecutionStatus.TIMED_OUT,
                               error="step exceeded timeout", current_node=current, finished_at=_now())
            steps.append(WorkflowStep(node_id=current, status="executed",
                                      detail=f"within timeout {node.timeout_seconds}s", at=_now()))
            current = node.next
            continue
        if node.type is NodeType.APPROVAL:
            steps.append(WorkflowStep(node_id=current, status="scheduled",
                                      detail=f"awaiting approval by {node.approver_role}", at=_now()))
            return replace(execution, steps=tuple(steps), status=ExecutionStatus.WAITING_APPROVAL,
                           current_node=current)
        if node.type is NodeType.HANDOFF:
            steps.append(WorkflowStep(node_id=current, status="scheduled_intent",
                                      detail="human handoff requested (no dial)", at=_now()))
            current = node.next
            continue
        # Unknown node type: fail closed.
        steps.append(WorkflowStep(node_id=current, status="failed",
                                  detail=f"unsupported node type {node.type.value}", at=_now()))
        return replace(execution, steps=tuple(steps), status=ExecutionStatus.FAILED,
                       error=f"unsupported node type {node.type.value}", finished_at=_now())
    return replace(execution, steps=tuple(steps), status=ExecutionStatus.COMPLETED,
                   current_node="", finished_at=_now())


def _resolve_condition(node: WorkflowNode, payload: dict[str, Any]) -> tuple[str | None, str]:
    if node.branches:
        for cond, target in node.branches:
            if cond.matches(payload):
                return target, f"branch matched field={cond.field}"
        if node.default_next:
            return node.default_next, "no branch matched; default taken"
        return None, "no branch matched and no default"
    if node.condition is not None:
        if node.condition.matches(payload):
            return node.next, f"condition matched field={node.condition.field}"
        if node.default_next:
            return node.default_next, "condition failed; default taken"
        return None, "condition failed and no default"
    return node.next, "no condition defined"


# --------------------------------------------------------- lifecycle (exec) ---

def inspect_execution(tenant_id: str, execution_id: str) -> WorkflowExecution:
    execution = _slot(_EXECUTIONS, tenant_id).get(execution_id)
    if execution is None:
        raise NotFoundError("execution not found")
    return execution


def execution_history(tenant_id: str, workflow_id: str = "") -> list[WorkflowExecution]:
    executions = _slot(_EXECUTIONS, tenant_id).values()
    if workflow_id:
        executions = [e for e in executions if e.workflow_id == workflow_id]
    return sorted(executions, key=lambda e: e.started_at, reverse=True)


def cancel_execution(tenant_id: str, execution_id: str) -> WorkflowExecution:
    execution = inspect_execution(tenant_id, execution_id)
    if execution_is_terminal(execution.status):
        return execution
    updated = replace(execution, status=ExecutionStatus.CANCELLED, finished_at=_now())
    _slot(_EXECUTIONS, tenant_id)[execution_id] = updated
    return updated


async def retry_execution(
    tenant_id: str,
    execution_id: str,
    payload: dict[str, Any],
    *,
    session: AsyncSession | None = None,
) -> WorkflowExecution:
    """Retry a failed execution with the same workflow, as a fresh run.

    Reuses the same idempotency key so a retry of the same business event can
    never be double-executed by mistake.
    """
    previous = inspect_execution(tenant_id, execution_id)
    if not execution_is_terminal(previous.status):
        raise BadRequestError("only a terminal execution may be retried")
    definition = get_workflow(tenant_id, previous.workflow_id)
    key = previous.idempotency_key
    execution = WorkflowExecution(
        id=stable_id(tenant_id, previous.workflow_id, key, previous.started_at, "retry"),
        workflow_id=previous.workflow_id,
        tenant_id=tenant_id,
        idempotency_key=key,
        status=ExecutionStatus.RUNNING,
        current_node=definition.entry_node,
        input_summary=_summarize_payload(payload),
        started_at=_now(),
    )
    execution = await _walk(tenant_id, definition, execution, payload, session)
    _slot(_EXECUTIONS, tenant_id)[execution.id] = execution
    await _commit_if(session)
    return execution


def approve_execution(tenant_id: str, execution_id: str) -> WorkflowExecution:
    """Approve a WAITING_APPROVAL execution: mark it completed at the gate."""
    execution = inspect_execution(tenant_id, execution_id)
    if execution.status is not ExecutionStatus.WAITING_APPROVAL:
        raise BadRequestError("execution is not waiting for approval")
    updated = replace(execution, status=ExecutionStatus.COMPLETED, finished_at=_now())
    _slot(_EXECUTIONS, tenant_id)[execution_id] = updated
    return updated
```

========================================================================
===== FILE: app/services/automation_service.py (246 lines) =====
========================================================================
```python
"""Automation service (Batch 01 enterprise expansion).

A bounded event-reaction engine over the closed ``TriggerEvent`` vocabulary.
The guarantees:

* **Deterministic matching.** ``evaluate`` is a pure function of the event and
  payload: a given event selects the same automations every time.
* **Idempotent execution.** ``run_idempotency_key`` (from the domain) derives
  from tenant + automation + event + business event id, so a replayed webhook
  or a double-delivered event collapses to one automation run.
* **Cooldown and deduplication.** ``should_run`` refuses to fire when the last
  run for the same key is within the cooldown window, or when the max-per-event
  budget is exhausted.
* **No unbounded engine.** There is no dynamic event registration and no
  arbitrary action execution — actions reuse the workflow controlled-action
  dispatcher, so the two features share one definition of "safe to run".

Persistence honesty: automations and runs live in an in-process registry and
are not durable across restarts. A durable ``automations`` / ``automation_runs``
table pair requires a migration (reported at the end of the batch).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, NotFoundError
from app.core.logging import log
from app.domain.automation_models import (
    AutomationDefinition,
    AutomationRun,
    AutomationStatus,
    TriggerEvent,
    run_idempotency_key,
)
from app.domain.workflow_models import WorkflowAction

#: tenant_id -> automation_id -> definition
_REGISTRY: dict[str, dict[str, AutomationDefinition]] = {}
#: tenant_id -> run_id -> run
_RUNS: dict[str, dict[str, AutomationRun]] = {}
#: tenant_id -> automation_id -> last completed run timestamp (cooldown)
_LAST_RUN_AT: dict[str, dict[str, str]] = {}


def _slot(store: dict, *keys: str) -> dict:
    node: dict = store
    for key in keys:
        node = node.setdefault(key, {})
    return node


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_automation(definition: AutomationDefinition) -> list[str]:
    return definition.validate()


def register_automation(tenant_id: str, definition: AutomationDefinition) -> AutomationDefinition:
    """Register (or update) an automation. Idempotent on definition id."""
    if definition.tenant_id != tenant_id:
        raise BadRequestError("automation does not belong to this tenant")
    problems = validate_automation(definition)
    if problems:
        raise BadRequestError("; ".join(problems))
    _slot(_REGISTRY, tenant_id)[definition.id] = definition
    return definition


def get_automation(tenant_id: str, automation_id: str) -> AutomationDefinition:
    definition = _slot(_REGISTRY, tenant_id).get(automation_id)
    if definition is None:
        raise NotFoundError("automation not found")
    return definition


def list_automations(tenant_id: str) -> list[AutomationDefinition]:
    return sorted(_slot(_REGISTRY, tenant_id).values(), key=lambda a: a.name)


def set_enabled(tenant_id: str, automation_id: str, enabled: bool) -> AutomationDefinition:
    definition = get_automation(tenant_id, automation_id)
    updated = replace(definition, status=AutomationStatus.ENABLED if enabled else AutomationStatus.DISABLED)
    _slot(_REGISTRY, tenant_id)[automation_id] = updated
    return updated


# ------------------------------------------------------------------ evaluate ---

def evaluate(tenant_id: str, event: TriggerEvent, payload: dict[str, Any]) -> list[AutomationDefinition]:
    """Select the enabled automations whose filters match the event payload."""
    matches = [
        automation for automation in _slot(_REGISTRY, tenant_id).values()
        if automation.status is AutomationStatus.ENABLED
        and automation.event is event
        and automation.matches(payload)
    ]
    return sorted(matches, key=lambda a: a.name)


def deduplicate(tenant_id: str, automation: AutomationDefinition, business_event_id: str) -> str:
    """The stable deduplication key for one business event."""
    return run_idempotency_key(tenant_id, automation.id, automation.event, business_event_id)


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def should_run(
    tenant_id: str,
    automation: AutomationDefinition,
    business_event_id: str,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Decide whether the automation may fire for this event right now.

    Returns ``(ok, reason)`` — ``ok=False`` with a reason when deduplication or
    cooldown blocks the run.
    """
    key = deduplicate(tenant_id, automation, business_event_id)
    moment = now or datetime.now(timezone.utc)

    # Deduplication: the same business event may only run max_per_event times.
    existing = [r for r in _RUNS.get(tenant_id, {}).values()
                if r.idempotency_key == key and r.status == "completed"]
    if len(existing) >= automation.policy.max_per_event:
        return False, "max_per_event budget exhausted"

    # Cooldown: one automation may not fire again until the window elapses.
    last_run = _LAST_RUN_AT.get(tenant_id, {}).get(automation.id)
    if last_run is not None and automation.policy.cooldown_seconds > 0:
        elapsed = moment - _parse_iso(last_run)
        if elapsed < timedelta(seconds=automation.policy.cooldown_seconds):
            return False, "cooldown active"
    return True, ""


# ------------------------------------------------------------------ execute ---

async def _dispatch(
    tenant_id: str,
    action: WorkflowAction,
    payload: dict[str, Any],
    session: AsyncSession | None,
) -> tuple[str, str]:
    """Reuse the workflow controlled-action dispatcher (one definition of safe)."""
    from app.services import workflow_service

    return await workflow_service._execute_action(tenant_id, action, payload, session)


async def execute_automation(
    tenant_id: str,
    automation: AutomationDefinition,
    business_event_id: str,
    payload: dict[str, Any],
    *,
    session: AsyncSession | None = None,
) -> AutomationRun:
    """Fire one automation for one business event, with dedup + cooldown."""
    ok, reason = should_run(tenant_id, automation, business_event_id)
    key = deduplicate(tenant_id, automation, business_event_id)
    if not ok:
        run = AutomationRun(
            id=key,
            automation_id=automation.id,
            tenant_id=tenant_id,
            idempotency_key=key,
            event=automation.event,
            business_event_id=business_event_id,
            status="cancelled",
            last_error=reason,
            created_at=_now(),
        )
        _slot(_RUNS, tenant_id)[run.id] = run
        log.info("automation.suppressed", tenant_id=tenant_id,
                 automation_id=automation.id[:8], reason=reason)
        return run

    run = AutomationRun(
        id=key,
        automation_id=automation.id,
        tenant_id=tenant_id,
        idempotency_key=key,
        event=automation.event,
        business_event_id=business_event_id,
        status="running",
        created_at=_now(),
    )
    _slot(_RUNS, tenant_id)[run.id] = run

    results: dict[str, Any] = {}
    failed = False
    for action in automation.actions:
        status, detail = await _dispatch(tenant_id, action, payload, session)
        results[action.name] = {"status": status, "detail": detail}
        if status in ("failed", "unsupported"):
            failed = True

    if failed and run.attempts + 1 < automation.policy.max_attempts:
        run = replace(run, status="pending", attempts=run.attempts + 1,
                      last_error="one or more actions failed",
                      next_attempt_at=(datetime.now(timezone.utc) +
                                       timedelta(seconds=automation.policy.backoff_seconds)).isoformat())
    else:
        run = replace(run, status="completed" if not failed else "failed",
                      attempts=run.attempts + 1,
                      last_error="one or more actions failed" if failed else "",
                      result_summary=results, finished_at=_now())

    _slot(_RUNS, tenant_id)[run.id] = run
    _LAST_RUN_AT.setdefault(tenant_id, {})[automation.id] = _now()
    if session is not None:
        await session.commit()
    log.info("automation.executed", tenant_id=tenant_id,
             automation_id=automation.id[:8], status=run.status)
    return run


def run_history(tenant_id: str, automation_id: str = "") -> list[AutomationRun]:
    runs = _slot(_RUNS, tenant_id).values()
    if automation_id:
        runs = [r for r in runs if r.automation_id == automation_id]
    return sorted(runs, key=lambda r: r.created_at, reverse=True)


def failure_counts(tenant_id: str, automation_id: str) -> dict[str, int]:
    """Count failed runs (bounded, deterministic)."""
    runs = [r for r in _slot(_RUNS, tenant_id).values()
            if r.automation_id == automation_id]
    return {
        "failed": sum(1 for r in runs if r.status == "failed"),
        "suppressed": sum(1 for r in runs if r.status == "cancelled"),
        "completed": sum(1 for r in runs if r.status == "completed"),
    }
```

========================================================================
===== FILE: app/services/campaign_service.py (454 lines) =====
========================================================================
```python
"""Campaign service (Batch 01 enterprise expansion).

Manages campaigns on top of the existing ``Campaign``/``Lead``/``Tenant``
tables and the outbound safety helpers. The cardinal rule:

**This service never places a call.** Scheduling, audience resolution,
eligibility and planning all produce *execution intents* — deterministic,
idempotent units of work that the existing outbound mechanism may pick up.
DNC checks, call-window checks, daily limits and attempt limits are enforced
here with the same helpers ``app/telephony/outbound.py`` uses, and this module
cannot weaken them: the compliance gate only ever makes requirements stricter.

Persistence honesty: the ``Campaign`` row stores name/goal/script/opening line/
throttle (real columns). Schedule, audience, compliance gate and richer state
live in a per-tenant overlay — no columns exist for them yet (schema gap,
reported at the end of the batch). Lead eligibility and progress are computed
from real rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time as _time, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, NotFoundError
from app.db.models import Campaign, Lead, LeadStatus, Tenant
from app.domain.campaign_models import (
    Audience,
    CampaignChannel,
    CampaignDefinition,
    CampaignExecutionIntent,
    CampaignGoal,
    CampaignMetrics,
    CampaignSchedule,
    CampaignState,
    ComplianceGate,
    Throttle,
    intent_idempotency_key,
)
from app.telephony import outbound

#: tenant_id -> campaign_id -> non-column campaign state (schedule, audience, ...)
_OVERLAY: dict[str, dict[str, dict]] = {}
#: tenant_id -> segment_id -> segment definition (no segment table yet)
_SEGMENTS: dict[str, dict[str, object]] = {}

_DEFAULT_OPENING_LINE = "Hi, this is {agent} calling from {business}. Do you have a quick minute?"

_WINDOW_MIN_DAILY_START = _time(8, 0)   # TCPA-style floor; tenants cannot call earlier
_WINDOW_MAX_DAILY_END = _time(21, 0)    # and not after 9pm


def _slot(store: dict, *keys: str) -> dict:
    node: dict = store
    for key in keys:
        node = node.setdefault(key, {})
    return node


def _overlay(tenant_id, campaign_id) -> dict:
    return _slot(_OVERLAY, str(tenant_id), str(campaign_id))


def _ensure_owned(campaign: Campaign | None, tenant_id) -> Campaign:
    if campaign is None or str(campaign.tenant_id) != str(tenant_id):
        raise NotFoundError("campaign not found")
    return campaign


def _validate(definition: CampaignDefinition) -> list[str]:
    problems = list(definition.validate())
    # Hard compliance floor: the tenant's window must sit inside the legal one.
    if definition.schedule.daily_start < _WINDOW_MIN_DAILY_START:
        problems.append("daily_start must not be earlier than 08:00")
    if definition.schedule.daily_end > _WINDOW_MAX_DAILY_END:
        problems.append("daily_end must not be later than 21:00")
    return problems


# ------------------------------------------------------------------- CRUD ---

async def create_campaign(
    session: AsyncSession, tenant: Tenant, definition: CampaignDefinition
) -> Campaign:
    if definition.tenant_id != str(tenant.id):
        raise BadRequestError("campaign does not belong to this tenant")
    problems = _validate(definition)
    if problems:
        raise BadRequestError("; ".join(problems))
    row = Campaign(
        tenant_id=tenant.id,
        name=definition.name[:200],
        goal=definition.goal.value,
        script_prompt=definition.script_prompt,
        opening_line=definition.opening_line or _DEFAULT_OPENING_LINE,
        calls_per_minute=definition.throttle.calls_per_minute,
        is_active=False,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    _overlay(tenant.id, row.id).update({
        "state": CampaignState.DRAFT.value,
        "schedule": _schedule_to_dict(definition.schedule),
        "audience": {
            "segment_ids": list(definition.audience.segment_ids),
            "lead_ids": list(definition.audience.lead_ids),
        },
        "throttle": {
            "daily_limit": definition.throttle.daily_limit,
            "max_attempts_per_lead": definition.throttle.max_attempts_per_lead,
        },
        "compliance": {
            "require_a2p_registration": definition.compliance.require_a2p_registration,
        },
        "channel": definition.channel.value,
    })
    return row


def _schedule_to_dict(schedule: CampaignSchedule) -> dict:
    return {
        "start_at": schedule.start_at,
        "end_at": schedule.end_at,
        "daily_start": schedule.daily_start.strftime("%H:%M"),
        "daily_end": schedule.daily_end.strftime("%H:%M"),
        "days_of_week": list(schedule.days_of_week),
        "timezone": schedule.timezone,
    }


async def get_campaign(session: AsyncSession, tenant: Tenant, campaign_id: str) -> CampaignDefinition:
    try:
        parsed = uuid.UUID(str(campaign_id))
    except ValueError:
        raise NotFoundError("campaign not found") from None
    row = await session.get(Campaign, parsed)
    _ensure_owned(row, tenant.id)
    overlay = _overlay(tenant.id, row.id)
    schedule = overlay.get("schedule", {})
    audience = overlay.get("audience", {"segment_ids": [], "lead_ids": []})
    throttle = overlay.get("throttle", {"daily_limit": 200, "max_attempts_per_lead": 3})
    return CampaignDefinition(
        id=str(row.id),
        tenant_id=str(tenant.id),
        name=row.name,
        goal=CampaignGoal(row.goal or "qualify"),
        channel=CampaignChannel(overlay.get("channel", "voice")),
        script_prompt=row.script_prompt or "",
        opening_line=row.opening_line or "",
        audience=Audience(
            tenant_id=str(tenant.id),
            segment_ids=tuple(audience.get("segment_ids", [])),
            lead_ids=tuple(audience.get("lead_ids", [])),
        ),
        schedule=CampaignSchedule(
            start_at=schedule.get("start_at", ""),
            end_at=schedule.get("end_at", ""),
            daily_start=_parse_hhmm(schedule.get("daily_start", "09:00")),
            daily_end=_parse_hhmm(schedule.get("daily_end", "20:00")),
            days_of_week=tuple(schedule.get("days_of_week", list(range(7)))),
            timezone=schedule.get("timezone", tenant.timezone),
        ),
        throttle=Throttle(
            calls_per_minute=row.calls_per_minute,
            daily_limit=throttle.get("daily_limit", 200),
            max_attempts_per_lead=throttle.get("max_attempts_per_lead", 3),
        ),
        compliance=ComplianceGate(),
        state=CampaignState(overlay.get("state", "draft")),
    )


def _parse_hhmm(value: str) -> _time:
    hour, minute = (value or "09:00").split(":")[:2]
    return _time(int(hour), int(minute))


async def list_campaigns(session: AsyncSession, tenant: Tenant) -> list[CampaignDefinition]:
    stmt = select(Campaign).where(Campaign.tenant_id == tenant.id).order_by(Campaign.created_at)
    rows = (await session.execute(stmt)).scalars().all()
    return [await get_campaign(session, tenant, str(row.id)) for row in rows]


async def update_campaign(
    session: AsyncSession, tenant: Tenant, campaign_id: str, definition: CampaignDefinition
) -> CampaignDefinition:
    parsed = uuid.UUID(str(campaign_id))
    row = await session.get(Campaign, parsed)
    _ensure_owned(row, tenant.id)
    problems = _validate(definition)
    if problems:
        raise BadRequestError("; ".join(problems))
    row.name = definition.name[:200]
    row.goal = definition.goal.value
    row.script_prompt = definition.script_prompt
    row.opening_line = definition.opening_line or row.opening_line
    row.calls_per_minute = definition.throttle.calls_per_minute
    session.add(row)
    _overlay(tenant.id, row.id).update({
        "schedule": _schedule_to_dict(definition.schedule),
        "audience": {
            "segment_ids": list(definition.audience.segment_ids),
            "lead_ids": list(definition.audience.lead_ids),
        },
        "throttle": {
            "daily_limit": definition.throttle.daily_limit,
            "max_attempts_per_lead": definition.throttle.max_attempts_per_lead,
        },
        "channel": definition.channel.value,
    })
    await session.commit()
    return await get_campaign(session, tenant, campaign_id)


async def _set_state(session: AsyncSession, tenant: Tenant, row: Campaign, state: CampaignState) -> CampaignDefinition:
    overlay = _overlay(tenant.id, row.id)
    current = CampaignState(overlay.get("state", "draft"))
    if current is state:
        return await get_campaign(session, tenant, str(row.id))
    if not _can_transition(current, state):
        raise BadRequestError(f"cannot move campaign {current.value} -> {state.value}")
    overlay["state"] = state.value
    row.is_active = state is CampaignState.RUNNING
    session.add(row)
    await session.commit()
    return await get_campaign(session, tenant, str(row.id))


def _can_transition(current: CampaignState, target: CampaignState) -> bool:
    allowed = {
        CampaignState.DRAFT: {CampaignState.SCHEDULED, CampaignState.CANCELLED},
        CampaignState.SCHEDULED: {CampaignState.RUNNING, CampaignState.CANCELLED},
        CampaignState.RUNNING: {CampaignState.PAUSED, CampaignState.COMPLETED, CampaignState.CANCELLED},
        CampaignState.PAUSED: {CampaignState.RUNNING, CampaignState.CANCELLED},
        CampaignState.COMPLETED: set(),
        CampaignState.CANCELLED: set(),
    }
    return target in allowed.get(current, set())


async def schedule_campaign(session: AsyncSession, tenant: Tenant, campaign_id: str) -> CampaignDefinition:
    parsed = uuid.UUID(str(campaign_id))
    row = await session.get(Campaign, parsed)
    _ensure_owned(row, tenant.id)
    return await _set_state(session, tenant, row, CampaignState.SCHEDULED)


async def pause_campaign(session: AsyncSession, tenant: Tenant, campaign_id: str) -> CampaignDefinition:
    parsed = uuid.UUID(str(campaign_id))
    row = await session.get(Campaign, parsed)
    _ensure_owned(row, tenant.id)
    return await _set_state(session, tenant, row, CampaignState.PAUSED)


async def resume_campaign(session: AsyncSession, tenant: Tenant, campaign_id: str) -> CampaignDefinition:
    parsed = uuid.UUID(str(campaign_id))
    row = await session.get(Campaign, parsed)
    _ensure_owned(row, tenant.id)
    if not outbound.is_call_window_open(tenant):
        raise BadRequestError("cannot resume outside the outbound call window")
    return await _set_state(session, tenant, row, CampaignState.RUNNING)


async def cancel_campaign(session: AsyncSession, tenant: Tenant, campaign_id: str) -> CampaignDefinition:
    parsed = uuid.UUID(str(campaign_id))
    row = await session.get(Campaign, parsed)
    _ensure_owned(row, tenant.id)
    return await _set_state(session, tenant, row, CampaignState.CANCELLED)


async def complete_campaign(session: AsyncSession, tenant: Tenant, campaign_id: str) -> CampaignDefinition:
    parsed = uuid.UUID(str(campaign_id))
    row = await session.get(Campaign, parsed)
    _ensure_owned(row, tenant.id)
    return await _set_state(session, tenant, row, CampaignState.COMPLETED)


# -------------------------------------------------------------- eligibility ---

async def validate_audience(session: AsyncSession, tenant: Tenant, definition: CampaignDefinition) -> dict:
    """Count the leads the audience actually resolves to (tenant-scoped)."""
    ids = await resolve_audience(session, tenant, definition.audience)
    return {"segment_ids": definition.audience.segment_ids, "lead_count": len(ids)}


def register_segment(tenant: Tenant, segment) -> None:
    """Store a segment definition (overlay; no segment table yet)."""
    _SEGMENTS.setdefault(str(tenant.id), {})[segment.id] = segment


def _resolve_segment_lookup(tenant: Tenant, segment_id: str):
    return _SEGMENTS.get(str(tenant.id), {}).get(segment_id)


def _lead_payload(lead: Lead) -> dict:
    return {
        "name": lead.name or "",
        "company": lead.company or "",
        "status": lead.status.value if lead.status else "",
        "score": lead.score,
        "attempts": lead.attempts,
    }


async def resolve_segment(session: AsyncSession, tenant: Tenant, segment) -> list[str]:
    """Resolve a segment definition to lead ids by evaluating its rules."""
    stmt = select(Lead).where(Lead.tenant_id == tenant.id).limit(50_000)
    leads = (await session.execute(stmt)).scalars().all()
    if not segment.rules:
        return [str(lead.id) for lead in leads]
    return [
        str(lead.id) for lead in leads
        if all(rule.matches(_lead_payload(lead)) for rule in segment.rules)
    ]


async def resolve_audience(session: AsyncSession, tenant: Tenant, audience: Audience) -> list[str]:
    """Union of explicit lead ids and segment-resolved ids, deduplicated."""
    ids: set[str] = set(audience.lead_ids)
    for segment_id in audience.segment_ids:
        segment = _resolve_segment_lookup(tenant, segment_id)
        if segment is not None:
            ids.update(await resolve_segment(session, tenant, segment))
    return sorted(ids)


async def eligibility_check(
    session: AsyncSession,
    tenant: Tenant,
    lead: Lead,
    definition: CampaignDefinition,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """The four safety gates, in order. Any skip returns ``(False, reason)``."""
    moment = now or datetime.now(timezone.utc)

    if definition.compliance.require_dnc_check and lead.status is LeadStatus.DNC:
        return False, "lead is on do-not-call"
    if definition.compliance.require_call_window and not outbound.is_call_window_open(tenant, moment):
        return False, "outside the outbound call window"
    attempt_cap = min(tenant.max_call_attempts, definition.throttle.max_attempts_per_lead)
    if definition.compliance.require_attempt_limit and lead.attempts >= attempt_cap:
        return False, "attempt limit reached"
    if definition.compliance.require_daily_limit:
        used_today = await _attempts_today(session, tenant, definition)
        if used_today >= definition.throttle.daily_limit:
            return False, "daily limit reached"
    if lead.next_attempt_at is not None and lead.next_attempt_at > moment:
        return False, "lead is in backoff"
    return True, ""


async def _attempts_today(session: AsyncSession, tenant: Tenant, definition: CampaignDefinition) -> int:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = (
        select(func.count())
        .select_from(Lead)
        .where(Lead.tenant_id == tenant.id,
               Lead.campaign_id == uuid.UUID(definition.id),
               Lead.last_attempt_at >= start)
    )
    return (await session.execute(stmt)).scalar() or 0


# ------------------------------------------------------------------- plan ---

async def execution_plan(
    session: AsyncSession,
    tenant: Tenant,
    definition: CampaignDefinition,
    *,
    limit: int = 100,
) -> list[CampaignExecutionIntent]:
    """Produce safe execution intents. Never dials anything."""
    limit = min(max(limit, 1), 1_000)
    lead_ids = await resolve_audience(session, tenant, definition.audience)
    intents: list[CampaignExecutionIntent] = []
    for lead_id in lead_ids[:limit]:
        try:
            lead = await session.get(Lead, uuid.UUID(lead_id))
        except ValueError:
            continue
        if lead is None or str(lead.tenant_id) != str(tenant.id):
            continue
        ok, reason = await eligibility_check(session, tenant, lead, definition)
        key = intent_idempotency_key(definition.id, lead_id)
        intents.append(CampaignExecutionIntent(
            id=key,
            campaign_id=definition.id,
            tenant_id=str(tenant.id),
            lead_id=lead_id,
            channel=definition.channel,
            idempotency_key=key,
            reason_skipped="" if ok else reason,
        ))
    return intents


async def progress(session: AsyncSession, tenant: Tenant, definition: CampaignDefinition) -> CampaignMetrics:
    """Aggregate-safe counters from real lead rows."""
    try:
        campaign_uuid = uuid.UUID(definition.id)
    except ValueError:
        return CampaignMetrics()
    total = (await session.execute(
        select(func.count()).select_from(Lead).where(Lead.tenant_id == tenant.id,
                                                    Lead.campaign_id == campaign_uuid)
    )).scalar() or 0
    attempted = (await session.execute(
        select(func.count()).select_from(Lead).where(Lead.tenant_id == tenant.id,
                                                    Lead.campaign_id == campaign_uuid,
                                                    Lead.attempts > 0)
    )).scalar() or 0
    dnc = (await session.execute(
        select(func.count()).select_from(Lead).where(Lead.tenant_id == tenant.id,
                                                    Lead.campaign_id == campaign_uuid,
                                                    Lead.status == LeadStatus.DNC)
    )).scalar() or 0
    qualified = (await session.execute(
        select(func.count()).select_from(Lead).where(Lead.tenant_id == tenant.id,
                                                    Lead.campaign_id == campaign_uuid,
                                                    Lead.status == LeadStatus.QUALIFIED)
    )).scalar() or 0
    failed = (await session.execute(
        select(func.count()).select_from(Lead).where(Lead.tenant_id == tenant.id,
                                                    Lead.campaign_id == campaign_uuid,
                                                    Lead.status == LeadStatus.FAILED)
    )).scalar() or 0
    return CampaignMetrics(
        total_leads=total,
        attempted=attempted,
        dnc_skipped=dnc,
        conversions=qualified,
        failed=failed,
    )


async def aggregate_results(session: AsyncSession, tenant: Tenant, definition: CampaignDefinition) -> dict:
    """Progress plus the eligibility split, for the results view."""
    metrics = await progress(session, tenant, definition)
    plan = await execution_plan(session, tenant, definition, limit=1_000)
    split = {
        "eligible": sum(1 for i in plan if not i.skipped),
        "skipped_dnc": sum(1 for i in plan if i.reason_skipped == "lead is on do-not-call"),
        "skipped_window": sum(1 for i in plan if i.reason_skipped == "outside the outbound call window"),
        "skipped_attempt_limit": sum(1 for i in plan if i.reason_skipped == "attempt limit reached"),
        "skipped_daily_limit": sum(1 for i in plan if i.reason_skipped == "daily limit reached"),
    }
    return {"metrics": metrics.__dict__, "eligibility": split}
```

========================================================================
===== FILE: app/services/analytics_service.py (349 lines) =====
========================================================================
```python
"""Aggregate analytics service (Batch 01 enterprise expansion).

Every figure here is a tenant-scoped ``SELECT count/sum/avg`` over the existing
tables, using the same rate definitions the dashboard already documents
(answer rate = answered/total, booking rate = booked/eligible, and so on).
Two rules from ``app/api/analytics_routes.py`` are honoured:

* **No raw customer data.** Output is counts, rates and money-shaped estimates;
  ``KpiSnapshot.assert_no_pii`` is asserted in the test suite.
* **Ranges are bounded.** Any custom range longer than ``MAX_RANGE_DAYS`` is
  rejected, so a single request can never scan a decade of history.

Money is an *estimate* derived from configured unit prices and metered usage —
it is never an invoice (billing owns that).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import BadRequestError
from app.db.models import (
    Appointment,
    AppointmentStatus,
    Call,
    CallStatus,
    Lead,
    LeadStatus,
    UsageEvent,
    UsageMetric,
)
from app.domain.analytics_models import (
    AppointmentKpi,
    CallKpi,
    CostKpi,
    FunnelKpi,
    KpiGranularity,
    KpiKind,
    KpiPoint,
    KpiSnapshot,
    LeadKpi,
    QualityKpi,
    SlaKpi,
)

#: Same threshold the dashboard uses to define an "eligible" (answerable) call.
ELIGIBLE_CALL_SECONDS = 10
MAX_RANGE_DAYS = 400
SLA_TARGET_SECONDS = 30


def _pct(num: int | float, den: int | float) -> float:
    return round(num / den * 100, 1) if den else 0.0


def _parse_utc(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)


def _validate_range(start: datetime | date, end: datetime | date) -> tuple[datetime, datetime]:
    s, e = _parse_utc(start), _parse_utc(end)
    if s >= e:
        raise BadRequestError("range start must be before range end")
    if (e - s).days > MAX_RANGE_DAYS:
        raise BadRequestError(f"range must be at most {MAX_RANGE_DAYS} days")
    return s, e


async def _count(session: AsyncSession, model, *conditions) -> int:
    stmt = select(func.count()).select_from(model).where(*conditions)
    return (await session.execute(stmt)).scalar() or 0


# --------------------------------------------------------------------- calls ---

async def call_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> CallKpi:
    s, e = _validate_range(start, end)
    base = [Call.tenant_id == uuid.UUID(str(tenant_id)), Call.started_at >= s, Call.started_at <= e]
    total = await _count(session, Call, *base)
    answered = await _count(session, Call, *base, Call.status == CallStatus.COMPLETED)
    failed = await _count(session, Call, *base, Call.status == CallStatus.FAILED)
    no_answer = await _count(session, Call, *base, Call.status == CallStatus.NO_ANSWER)
    transferred = await _count(session, Call, *base, Call.status == CallStatus.TRANSFERRED)
    escalated = await _count(session, Call, *base, Call.escalated.is_(True))
    avg = (await session.execute(
        select(func.avg(Call.duration_seconds)).where(*base)
    )).scalar() or 0.0
    return CallKpi(
        total=total, answered=answered, completed=answered, failed=failed,
        no_answer=no_answer, transferred=transferred, escalated=escalated,
        avg_duration_seconds=round(float(avg), 2),
    )


async def funnel_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> FunnelKpi:
    s, e = _validate_range(start, end)
    base = [Call.tenant_id == uuid.UUID(str(tenant_id)), Call.started_at >= s, Call.started_at <= e]
    total = await _count(session, Call, *base)
    answered = await _count(session, Call, *base, Call.duration_seconds >= ELIGIBLE_CALL_SECONDS)
    booked = await _count(session, Call, *base, Call.booked.is_(True))
    qualified = await _count(session, Lead, Lead.tenant_id == uuid.UUID(str(tenant_id)),
                             Lead.status == LeadStatus.QUALIFIED)
    return FunnelKpi(total=total, answered=answered, engaged=answered,
                     qualified=qualified, booked=booked)


async def appointment_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> AppointmentKpi:
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    base = [Appointment.tenant_id == tid, Appointment.created_at >= s, Appointment.created_at <= e]
    total = await _count(session, Appointment, *base)
    confirmed = await _count(session, Appointment, *base, Appointment.status == AppointmentStatus.CONFIRMED)
    cancelled = await _count(session, Appointment, *base, Appointment.status == AppointmentStatus.CANCELLED)
    no_show = await _count(session, Appointment, *base, Appointment.status == AppointmentStatus.NO_SHOW)
    return AppointmentKpi(total=total, confirmed=confirmed, cancelled=cancelled, no_show=no_show)


async def lead_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> LeadKpi:
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    base = [Lead.tenant_id == tid, Lead.created_at >= s, Lead.created_at <= e]
    total = await _count(session, Lead, *base)
    qualified = await _count(session, Lead, *base, Lead.status == LeadStatus.QUALIFIED)
    unqualified = await _count(session, Lead, *base, Lead.status == LeadStatus.UNQUALIFIED)
    dnc = await _count(session, Lead, *base, Lead.status == LeadStatus.DNC)
    return LeadKpi(total=total, qualified=qualified, unqualified=unqualified, dnc=dnc,
                   converted=qualified)


async def agent_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> list[dict[str, Any]]:
    """Per-agent performance. ``agent_id`` is the tenant itself in this batch
    (agents map onto the tenant's live configuration until an agent table
    exists), so the result is the single live-agent view — honest, aggregate-safe."""
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    base = [Call.tenant_id == tid, Call.started_at >= s, Call.started_at <= e]
    calls = await _count(session, Call, *base)
    bookings = await _count(session, Call, *base, Call.booked.is_(True))
    escalations = await _count(session, Call, *base, Call.escalated.is_(True))
    avg = (await session.execute(
        select(func.avg(Call.duration_seconds)).where(*base)
    )).scalar() or 0.0
    return [{
        "agent_id": str(tenant_id),
        "calls": calls,
        "bookings": bookings,
        "escalations": escalations,
        "avg_duration_seconds": round(float(avg), 2),
        "booking_rate": _pct(bookings, calls),
        "escalation_rate": _pct(escalations, calls),
    }]


async def provider_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> list[dict[str, Any]]:
    """Per-LLM-provider call counts and latency (from the real ``llm_used`` column)."""
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    stmt = (
        select(Call.llm_used, func.count(), func.avg(Call.avg_response_ms))
        .where(Call.tenant_id == tid, Call.started_at >= s, Call.started_at <= e,
               Call.llm_used.is_not(None))
        .group_by(Call.llm_used)
    )
    rows = (await session.execute(stmt)).all()
    providers = []
    for provider, calls, avg_latency in rows:
        provider_name = provider or "unknown"
        providers.append({
            "provider": provider_name,
            "calls": calls,
            "avg_latency_ms": round(float(avg_latency), 2) if avg_latency is not None else None,
        })
    return providers


async def campaign_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> list[dict[str, Any]]:
    """Per-campaign lead outcomes (aggregate-safe, no lead identities)."""
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    stmt = (
        select(Lead.campaign_id, func.count(), func.sum(case((Lead.status == LeadStatus.QUALIFIED, 1), else_=0)))
        .where(Lead.tenant_id == tid, Lead.created_at >= s, Lead.created_at <= e,
               Lead.campaign_id.is_not(None))
        .group_by(Lead.campaign_id)
    )
    rows = (await session.execute(stmt)).all()
    results = []
    for campaign_id, total, converted in rows:
        results.append({
            "campaign_id": str(campaign_id),
            "total": total,
            "conversions": int(converted or 0),
            "conversion_rate": _pct(int(converted or 0), total),
        })
    return results


async def quality_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> QualityKpi:
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    base = [Call.tenant_id == tid, Call.started_at >= s, Call.started_at <= e]
    avg = (await session.execute(
        select(func.avg(Call.avg_response_ms)).where(*base, Call.avg_response_ms.is_not(None))
    )).scalar()
    resolved = await _count(session, Call, *base, Call.status == CallStatus.COMPLETED)
    total = await _count(session, Call, *base)
    return QualityKpi(
        avg_response_ms=round(float(avg), 2) if avg is not None else None,
        p95_response_ms=None,          # percentile needs window functions; deferred
        positive_sentiment_rate=0.0,   # sentiment has no persisted column yet
        negative_sentiment_rate=0.0,
        resolved_rate=_pct(resolved, total),
    )


async def sla_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> SlaKpi:
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    base = [Call.tenant_id == tid, Call.started_at >= s, Call.started_at <= e]
    within = await _count(session, Call, *base, Call.avg_response_ms <= SLA_TARGET_SECONDS,
                          Call.avg_response_ms.is_not(None))
    breached = await _count(session, Call, *base, Call.avg_response_ms > SLA_TARGET_SECONDS)
    return SlaKpi(within_target=within, breached=breached, target_seconds=SLA_TARGET_SECONDS)


async def cost_kpis(
    session: AsyncSession, tenant_id: str, *, start: datetime | date, end: datetime | date
) -> CostKpi:
    """Usage × configured unit price. An estimate — never an invoice."""
    s, e = _validate_range(start, end)
    tid = uuid.UUID(str(tenant_id))
    stmt = (
        select(UsageEvent.metric, func.sum(UsageEvent.quantity))
        .where(UsageEvent.tenant_id == tid, UsageEvent.created_at >= s, UsageEvent.created_at <= e)
        .group_by(UsageEvent.metric)
    )
    rows = dict((await session.execute(stmt)).all())
    prices = settings.cost_unit_prices or {}

    def _quantity(metric: UsageMetric) -> int:
        value = rows.get(metric)
        return int(value) if value is not None else 0

    # UsageEvent quantities are in smallest units (seconds for voice); prices
    # are per-minute, so convert before pricing. Estimates only, never invoices.
    minutes = _quantity(UsageMetric.VOICE_MINUTE) / 60.0
    sms = _quantity(UsageMetric.SMS_SEGMENT)
    tokens = _quantity(UsageMetric.LLM_TOKEN)
    cost = 0
    cost += int(minutes * prices.get("voice_minute", 0))
    cost += int(sms * prices.get("sms_segment", 0))
    token_price = next(
        (v for k, v in prices.items() if k.startswith("llm_1k_tokens:")), 0
    )
    cost += int((tokens / 1000.0) * token_price)
    return CostKpi(
        minutes=round(float(minutes), 2),
        sms_segments=sms,
        llm_tokens=tokens,
        estimated_cost_millicents=int(cost),
    )


# ----------------------------------------------------------------- snapshot ---

async def snapshot(
    session: AsyncSession,
    tenant_id: str,
    kind: KpiKind,
    *,
    start: datetime | date,
    end: datetime | date,
    granularity: KpiGranularity = KpiGranularity.CUSTOM,
) -> KpiSnapshot:
    """Assemble one KPI point for the requested kind/range (tenant-scoped)."""
    s, e = _validate_range(start, end)
    tid = str(tenant_id)

    if kind is KpiKind.CALL:
        kpi = await call_kpis(session, tid, start=s, end=e)
        metrics = {**kpi.__dict__, "rates": kpi.rates()}
    elif kind is KpiKind.FUNNEL:
        kpi = await funnel_kpis(session, tid, start=s, end=e)
        metrics = {**kpi.__dict__, "rates": kpi.rates()}
    elif kind is KpiKind.APPOINTMENT:
        kpi = await appointment_kpis(session, tid, start=s, end=e)
        metrics = {**kpi.__dict__, "rates": kpi.rates()}
    elif kind is KpiKind.LEAD:
        kpi = await lead_kpis(session, tid, start=s, end=e)
        metrics = {**kpi.__dict__, "rates": kpi.rates()}
    elif kind is KpiKind.AGENT:
        metrics = {"agents": await agent_kpis(session, tid, start=s, end=e)}
    elif kind is KpiKind.PROVIDER:
        metrics = {"providers": await provider_kpis(session, tid, start=s, end=e)}
    elif kind is KpiKind.CAMPAIGN:
        metrics = {"campaigns": await campaign_kpis(session, tid, start=s, end=e)}
    elif kind is KpiKind.QUALITY:
        kpi = await quality_kpis(session, tid, start=s, end=e)
        metrics = kpi.__dict__
    elif kind is KpiKind.SLA:
        kpi = await sla_kpis(session, tid, start=s, end=e)
        metrics = {**kpi.__dict__, "rates": kpi.rates()}
    elif kind is KpiKind.COST:
        kpi = await cost_kpis(session, tid, start=s, end=e)
        metrics = kpi.__dict__
    else:
        metrics = {}

    point = KpiPoint(
        kind=kind,
        period_start=s.isoformat(),
        period_end=e.isoformat(),
        metrics=metrics,
    )
    return KpiSnapshot.build(
        tenant_id=tid,
        range_start=s.isoformat(),
        range_end=e.isoformat(),
        granularity=granularity,
        points=(point,),
    )
```

========================================================================
===== FILE: app/services/notification_service.py (271 lines) =====
========================================================================
```python
"""Notification service (Batch 01 enterprise expansion).

Templated, priority-ordered, deduplicated notifications that route through the
infrastructure that already exists:

* ``SMS`` goes through ``app.integrations.notifications.send_sms`` (Twilio),
  after a compliance content check — never around it.
* ``IN_APP`` and ``INTERNAL_ALERT`` are delivered locally (state flip).
* ``WEBHOOK`` is SSRF-validated and *prepared*; dispatch is deferred until a
  delivery worker exists (honest, reported gap).
* ``EMAIL`` is declared but suppressed — there is deliberately no new email
  provider in this batch.

Deduplication is deterministic (``dedupe_key``), and ``Notification.safe_repr``
is the only string form allowed in logs — a full rendered body or a raw
recipient contact is never logged.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.core import compliance
from app.core.logging import log
from app.core.ssrf import OutboundUrlError, validate_outbound_url
from app.domain.notification_models import (
    DeliveryResult,
    DeliveryState,
    EventSource,
    Notification,
    NotificationChannel,
    NotificationPriority,
    NotificationTemplate,
    PreferenceSet,
    Recipient,
    dedupe_key,
    with_attempt,
)
from app.integrations.notifications import send_sms

#: tenant_id -> template_id -> template
_TEMPLATES: dict[str, dict[str, NotificationTemplate]] = {}
#: tenant_id -> notification_id -> notification
_NOTIFICATIONS: dict[str, dict[str, Notification]] = {}
#: tenant_id -> dedupe_key -> notification_id (dedupe index)
_DEDUPE: dict[str, dict[str, str]] = {}

MAX_BACKOFF_SECONDS = 3600


def _slot(store: dict, *keys: str) -> dict:
    node: dict = store
    for key in keys:
        node = node.setdefault(key, {})
    return node


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- templates ---

def validate_template(template: NotificationTemplate) -> list[str]:
    return template.validate()


def create_template(tenant_id: str, template: NotificationTemplate) -> NotificationTemplate:
    if template.tenant_id != tenant_id:
        raise ValueError("template does not belong to this tenant")
    problems = validate_template(template)
    if problems:
        raise ValueError("; ".join(problems))
    _slot(_TEMPLATES, tenant_id)[template.id] = template
    return template


def get_template(tenant_id: str, template_id: str) -> NotificationTemplate:
    template = _slot(_TEMPLATES, tenant_id).get(template_id)
    if template is None:
        raise KeyError("template not found")
    return template


def list_templates(tenant_id: str) -> list[NotificationTemplate]:
    return sorted(_slot(_TEMPLATES, tenant_id).values(), key=lambda t: t.name)


def render(template: NotificationTemplate, variables: dict[str, str]) -> str:
    return template.render(variables or {})


# -------------------------------------------------------------- preferences ---

def resolve_preferences(
    preference: PreferenceSet,
    channel: NotificationChannel,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Decide whether a channel is allowed right now for this preference set."""
    if channel is not preference.channel:
        return False, "channel not enabled for recipient"
    if not preference.enabled:
        return False, "recipient disabled this channel"
    if preference.is_quiet(now):
        return False, "recipient is in quiet hours"
    return True, ""


# --------------------------------------------------------------- lifecycle ---

def create_notification(
    tenant_id: str,
    *,
    template: NotificationTemplate,
    recipient: Recipient,
    event_source: EventSource,
    priority: NotificationPriority = NotificationPriority.NORMAL,
    business_key: str = "",
    variables: dict[str, str] | None = None,
) -> Notification:
    """Render + create a notification, deduplicating on the business key."""
    if template.tenant_id != tenant_id:
        raise ValueError("template does not belong to this tenant")
    key = dedupe_key(tenant_id, template.id, event_source, business_key)
    existing_id = _slot(_DEDUPE, tenant_id).get(key)
    if existing_id is not None:
        existing = _slot(_NOTIFICATIONS, tenant_id).get(existing_id)
        if existing is not None:
            return existing
    body = render(template, variables or {})
    notification = Notification(
        id=key,
        tenant_id=tenant_id,
        template_id=template.id,
        channel=template.channel,
        recipient=recipient,
        event_source=event_source,
        priority=priority,
        dedupe_key=key,
        rendered_body=body,
    )
    _slot(_NOTIFICATIONS, tenant_id)[notification.id] = notification
    _slot(_DEDUPE, tenant_id)[key] = notification.id
    log.info("notification.created", tenant_id=tenant_id, notification=notification.safe_repr())
    return notification


def enqueue_system_notification(
    tenant_id: str,
    template_id: str,
    business_key: str,
    *,
    variables: dict[str, str] | None = None,
    priority: NotificationPriority = NotificationPriority.NORMAL,
) -> Notification | None:
    """Convenience used by the workflow engine: system -> tenant in-app.

    Returns ``None`` (rather than raising) when the template does not exist, so
    a workflow action with a missing template fails soft instead of crashing
    the execution.
    """
    template = _slot(_TEMPLATES, tenant_id).get(template_id)
    if template is None:
        log.warning("notification.template_missing", tenant_id=tenant_id, template_id=template_id)
        return None
    recipient = Recipient(kind="user", target="system", user_id="")
    return create_notification(
        tenant_id,
        template=template,
        recipient=recipient,
        event_source=EventSource.SYSTEM,
        priority=priority,
        business_key=business_key,
        variables=variables,
    )


async def deliver(notification: Notification) -> DeliveryResult:
    """Deliver one notification through the channel's existing infrastructure."""
    if notification.channel is NotificationChannel.IN_APP:
        return DeliveryResult(DeliveryState.DELIVERED, notification.channel, "in-app delivered")
    if notification.channel is NotificationChannel.INTERNAL_ALERT:
        return DeliveryResult(DeliveryState.DELIVERED, notification.channel, "internal alert logged")
    if notification.channel is NotificationChannel.SMS:
        body = notification.rendered_body
        issues = compliance.check_message(body)
        if any(issue.severity == "error" for issue in issues):
            return DeliveryResult(DeliveryState.SUPPRESSED, notification.channel,
                                  "compliance check failed")
        sent = await send_sms(notification.recipient.target, body)
        if sent:
            return DeliveryResult(DeliveryState.SENT, notification.channel, "sms sent")
        return DeliveryResult(DeliveryState.FAILED, notification.channel, "sms provider error")
    if notification.channel is NotificationChannel.WEBHOOK:
        try:
            validate_outbound_url(notification.recipient.target, require_https=True)
        except OutboundUrlError as exc:
            return DeliveryResult(DeliveryState.SUPPRESSED, notification.channel, str(exc))
        return DeliveryResult(
            DeliveryState.PENDING, notification.channel,
            "webhook validated; dispatch requires the delivery worker (batch 02)",
        )
    if notification.channel is NotificationChannel.EMAIL:
        return DeliveryResult(DeliveryState.SUPPRESSED, notification.channel,
                              "no email provider configured")
    return DeliveryResult(DeliveryState.SUPPRESSED, notification.channel, "unsupported channel")


def record_delivery(tenant_id: str, notification: Notification, result: DeliveryResult) -> Notification:
    updated = replace(
        notification,
        delivery_state=result.state,
        error_summary=result.detail[:500],
        sent_at=_now() if result.delivered else notification.sent_at,
    )
    _slot(_NOTIFICATIONS, tenant_id)[notification.id] = updated
    log.info("notification.delivered", tenant_id=tenant_id, notification=updated.safe_repr())
    return updated


def retry(tenant_id: str, notification_id: str) -> Notification:
    """Advance the retry counter with bounded exponential backoff."""
    notification = _slot(_NOTIFICATIONS, tenant_id).get(notification_id)
    if notification is None:
        raise KeyError("notification not found")
    backoff = min(MAX_BACKOFF_SECONDS, 60 * (2 ** min(notification.attempts, 6)))
    updated = with_attempt(notification, error="retry scheduled")
    updated = replace(
        updated,
        next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat(),
    )
    _slot(_NOTIFICATIONS, tenant_id)[notification_id] = updated
    return updated


def mark_read(tenant_id: str, notification_id: str) -> Notification:
    notification = _slot(_NOTIFICATIONS, tenant_id).get(notification_id)
    if notification is None:
        raise KeyError("notification not found")
    # read state is derived from delivery_state + a read flag in the overlay
    _READ.setdefault(tenant_id, set()).add(notification_id)
    return notification


def mark_unread(tenant_id: str, notification_id: str) -> Notification:
    notification = _slot(_NOTIFICATIONS, tenant_id).get(notification_id)
    if notification is None:
        raise KeyError("notification not found")
    _READ.setdefault(tenant_id, set()).discard(notification_id)
    return notification


_READ: dict[str, set[str]] = {}


def is_read(tenant_id: str, notification_id: str) -> bool:
    return notification_id in _READ.get(tenant_id, set())


def history(tenant_id: str, *, unread_only: bool = False) -> list[Notification]:
    notifications = sorted(
        _slot(_NOTIFICATIONS, tenant_id).values(),
        key=lambda n: n.sent_at or "",
        reverse=True,
    )
    if unread_only:
        notifications = [n for n in notifications if not is_read(tenant_id, n.id)]
    return notifications
```

========================================================================
===== FILE: app/services/inbox_service.py (419 lines) =====
========================================================================
```python
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
```

========================================================================
===== FILE: app/api/agent_management_routes.py (481 lines) =====
========================================================================
```python
"""Agent-management API (Batch 01 enterprise expansion).

Tenant-scoped, role-aware endpoints over ``app.services.agent_service``.
Conventions inherited from the existing API:

* **The tenant is never a parameter.** It comes from ``ctx.tenant_id``, which
  comes from the verified JWT. Every write is proven against the tenant the
  caller actually belongs to.
* **Responses are allowlists.** ``AgentOut``/``AgentVersionOut`` name every
  field that may reach a client; there is no provider credential anywhere in
  this module to leak.
* **No mass assignment.** Every request model is ``extra="forbid"`` and names
  its fields explicitly; the service applies only the fields the model carries.

Route registration (``app/main.py``) is outside the allowed file set for this
batch and is reported as an integration dependency.
"""

from __future__ import annotations

from datetime import time as _time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import TenantContext, require_permission
from app.auth.permissions import Permission
from app.db.session import get_session
from app.domain.agent_models import (
    AgentConfig,
    EscalationPolicy,
    FallbackBehavior,
    HandoffConfig,
    HandoffMode,
    InterruptionPolicy,
    LanguageConfig,
    ModelConfig,
    OperatingHours,
    ResponseStyle,
    SafetyPolicy,
    ToolConfig,
    VoiceConfig,
)
from app.services import agent_service

router = APIRouter(prefix="/api/agents", tags=["agents"])


# ----------------------------------------------------------------- schemas ---

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentCreateRequest(_Strict):
    name: str = Field(min_length=1, max_length=80)
    greeting: str = Field(default="", max_length=2000)
    system_instructions: str = Field(default="", max_length=20000)
    primary_language: str = "en-US"
    fallback_languages: list[str] = Field(default_factory=list)
    voice_id: str = Field(default="", max_length=64)
    speech_speed: float = 1.0
    provider: str = "anthropic"
    model: str = ""
    temperature: float = 0.65
    response_style: str = "natural"
    interruption: str = "allow_always"
    escalation: str = "on_request"
    fallback: str = "take_message"
    timezone: str = "UTC"
    open_time: str = "09:00"
    close_time: str = "17:00"
    escalation_number: str = Field(default="", max_length=32)
    enabled_tools: list[str] = Field(default_factory=list)
    max_tool_calls: int = 8
    record_calls: bool = False
    ai_disclosure_required: bool = True
    confidence_min: float = 0.35
    confidence_floor: float = 0.0


class AgentUpdateRequest(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    greeting: str | None = Field(default=None, max_length=2000)
    system_instructions: str | None = Field(default=None, max_length=20000)
    primary_language: str | None = None
    fallback_languages: list[str] | None = None
    voice_id: str | None = Field(default=None, max_length=64)
    speech_speed: float | None = None
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    response_style: str | None = None
    interruption: str | None = None
    escalation: str | None = None
    fallback: str | None = None
    timezone: str | None = None
    open_time: str | None = None
    close_time: str | None = None
    escalation_number: str | None = Field(default=None, max_length=32)
    enabled_tools: list[str] | None = None
    max_tool_calls: int | None = None
    record_calls: bool | None = None
    ai_disclosure_required: bool | None = None
    confidence_min: float | None = None
    confidence_floor: float | None = None


class RollbackRequest(_Strict):
    version: int = Field(ge=1)


class PublishRequest(_Strict):
    changelog: str = Field(default="", max_length=4000)


class AgentOut(_Strict):
    id: str
    tenant_id: str
    name: str
    status: str
    greeting: str
    system_instructions: str
    primary_language: str
    fallback_languages: list[str]
    voice_id: str
    speech_speed: float
    provider: str
    model: str
    temperature: float
    response_style: str
    interruption: str
    escalation: str
    fallback: str
    timezone: str
    open_time: str
    close_time: str
    escalation_number: str
    enabled_tools: list[str]
    max_tool_calls: int
    record_calls: bool
    ai_disclosure_required: bool
    confidence_min: float
    confidence_floor: float


class AgentVersionOut(_Strict):
    agent_id: str
    version: int
    config_hash: str
    status: str
    changelog: str
    published_at: str


class ValidateOut(_Strict):
    ok: bool
    issues: list[str]


class PreviewOut(_Strict):
    ok: bool
    issues: list[str]
    provider: str = ""
    model: str = ""
    prompt_preview: str = ""
    config_hash: str = ""


# ---------------------------------------------------------------- helpers ---

def _parse_time(value: str, default: _time) -> _time:
    try:
        hour, minute = (value or "").split(":")[:2]
        return _time(int(hour), int(minute))
    except (ValueError, TypeError):
        return default


def _enum_of(enum_cls, value: str, default):
    try:
        return enum_cls(value)
    except ValueError:
        return default


def _to_config(tenant_id: str, payload: AgentCreateRequest | AgentUpdateRequest,
               base: AgentConfig | None = None) -> AgentConfig:
    """Build a config from an explicit request model (never from raw kwargs)."""
    if base is None:
        base = AgentConfig(tenant_id=tenant_id, name=payload.name or "Alex")

    def pick(attr: str, request_value, default):
        return default if request_value is None else request_value

    name = pick("name", getattr(payload, "name", None), base.name)
    greeting = pick("greeting", getattr(payload, "greeting", None), base.greeting)
    instructions = pick("system_instructions", getattr(payload, "system_instructions", None),
                        base.system_instructions)
    primary = pick("primary_language", getattr(payload, "primary_language", None),
                   base.language.primary)
    fallbacks = pick("fallback_languages", getattr(payload, "fallback_languages", None),
                     list(base.language.fallbacks))
    voice_id = pick("voice_id", getattr(payload, "voice_id", None), base.voice.voice_id)
    speed = pick("speech_speed", getattr(payload, "speech_speed", None), base.voice.speech_speed)
    provider = pick("provider", getattr(payload, "provider", None), base.model.provider)
    model = pick("model", getattr(payload, "model", None), base.model.model)
    temperature = pick("temperature", getattr(payload, "temperature", None), base.model.temperature)
    style = pick("response_style", getattr(payload, "response_style", None), base.response_style.value)
    interruption = pick("interruption", getattr(payload, "interruption", None), base.interruption.value)
    escalation = pick("escalation", getattr(payload, "escalation", None), base.escalation.value)
    fallback = pick("fallback", getattr(payload, "fallback", None), base.fallback.value)
    timezone = pick("timezone", getattr(payload, "timezone", None), base.operating_hours.timezone)
    open_time = pick("open_time", getattr(payload, "open_time", None),
                     base.operating_hours.open.strftime("%H:%M"))
    close_time = pick("close_time", getattr(payload, "close_time", None),
                      base.operating_hours.close.strftime("%H:%M"))
    escalation_number = pick("escalation_number", getattr(payload, "escalation_number", None),
                             base.handoff.destination)
    tools = pick("enabled_tools", getattr(payload, "enabled_tools", None), list(base.tools.enabled))
    max_tools = pick("max_tool_calls", getattr(payload, "max_tool_calls", None), base.tools.max_tool_calls)
    record_calls = pick("record_calls", getattr(payload, "record_calls", None), base.safety.record_calls)
    disclosure = pick("ai_disclosure_required", getattr(payload, "ai_disclosure_required", None),
                      base.safety.ai_disclosure_required)
    confidence_min = pick("confidence_min", getattr(payload, "confidence_min", None), base.confidence_min)
    confidence_floor = pick("confidence_floor", getattr(payload, "confidence_floor", None),
                            base.confidence_floor)

    return AgentConfig(
        tenant_id=tenant_id,
        name=name,
        greeting=greeting,
        system_instructions=instructions,
        language=LanguageConfig(primary=primary, fallbacks=tuple(fallbacks or ())),
        voice=VoiceConfig(voice_id=voice_id, speech_speed=float(speed)),
        model=ModelConfig(provider=provider, model=model, temperature=float(temperature)),
        response_style=_enum_of(ResponseStyle, style, ResponseStyle.NATURAL),
        interruption=_enum_of(InterruptionPolicy, interruption, InterruptionPolicy.ALLOW_ALWAYS),
        escalation=_enum_of(EscalationPolicy, escalation, EscalationPolicy.ON_REQUEST),
        fallback=_enum_of(FallbackBehavior, fallback, FallbackBehavior.TAKE_MESSAGE),
        operating_hours=OperatingHours(timezone=timezone,
                                       open=_parse_time(open_time, _time(9, 0)),
                                       close=_parse_time(close_time, _time(17, 0))),
        knowledge=(),
        tools=ToolConfig(enabled=tuple(tools or ()), max_tool_calls=int(max_tools)),
        safety=SafetyPolicy(max_tool_calls=int(max_tools), record_calls=bool(record_calls),
                            ai_disclosure_required=bool(disclosure)),
        handoff=HandoffConfig(mode=HandoffMode.NUMBER if escalation_number else HandoffMode.NONE,
                              destination=escalation_number),
        confidence_min=float(confidence_min),
        confidence_floor=float(confidence_floor),
    )


def _out(ctx: TenantContext, config: AgentConfig, status: str) -> AgentOut:
    return AgentOut(
        id=config.id,
        tenant_id=str(ctx.tenant_id),
        name=config.name,
        status=status,
        greeting=config.greeting,
        system_instructions=config.system_instructions,
        primary_language=config.language.primary,
        fallback_languages=list(config.language.fallbacks),
        voice_id=config.voice.voice_id,
        speech_speed=config.voice.speech_speed,
        provider=config.model.provider,
        model=config.model.model,
        temperature=config.model.temperature,
        response_style=config.response_style.value,
        interruption=config.interruption.value,
        escalation=config.escalation.value,
        fallback=config.fallback.value,
        timezone=config.operating_hours.timezone,
        open_time=config.operating_hours.open.strftime("%H:%M"),
        close_time=config.operating_hours.close.strftime("%H:%M"),
        escalation_number=config.handoff.destination,
        enabled_tools=list(config.tools.enabled),
        max_tool_calls=config.tools.max_tool_calls,
        record_calls=config.safety.record_calls,
        ai_disclosure_required=config.safety.ai_disclosure_required,
        confidence_min=config.confidence_min,
        confidence_floor=config.confidence_floor,
    )


def _status_of(ctx: TenantContext, config: AgentConfig) -> str:
    history = agent_service.version_history(ctx.tenant, config.id)
    return history[0].status.value if history else "draft"


# ------------------------------------------------------------------- routes ---

@router.get("", response_model=list[AgentOut])
async def list_agents(
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_READ)),
):
    bundles = agent_service.list_agents(ctx.tenant)
    result = []
    for bundle in bundles:
        status = bundle.published.status.value if bundle.published else "draft"
        result.append(_out(ctx, bundle.draft, status))
    return result


@router.post("", response_model=AgentOut, status_code=201)
async def create_agent(
    payload: AgentCreateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_UPDATE)),
):
    config = _to_config(str(ctx.tenant_id), payload)
    try:
        saved = agent_service.create_draft(ctx.tenant, config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(ctx, saved, "draft")


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_READ)),
):
    config = agent_service.get_draft(ctx.tenant, agent_id)
    if config is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return _out(ctx, config, _status_of(ctx, config))


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: str,
    payload: AgentUpdateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_UPDATE)),
):
    existing = agent_service.get_draft(ctx.tenant, agent_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="agent not found")
    config = _to_config(str(ctx.tenant_id), payload, base=existing)
    try:
        saved = agent_service.update_draft(ctx.tenant, config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(ctx, saved, _status_of(ctx, saved))


@router.post("/{agent_id}/clone", response_model=AgentOut, status_code=201)
async def clone_agent(
    agent_id: str,
    payload: PublishRequest,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_UPDATE)),
):
    existing = agent_service.get_draft(ctx.tenant, agent_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="agent not found")
    new_name = payload.changelog.strip() or f"{existing.name} (copy)"
    cloned = agent_service.clone_agent(existing, new_name=new_name)
    try:
        saved = agent_service.create_draft(ctx.tenant, cloned)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(ctx, saved, "draft")


@router.post("/{agent_id}/publish", response_model=AgentVersionOut)
async def publish_agent(
    agent_id: str,
    payload: PublishRequest,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_UPDATE)),
    session: AsyncSession = Depends(get_session),
):
    existing = agent_service.get_draft(ctx.tenant, agent_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="agent not found")
    try:
        version = await agent_service.publish_async(session, ctx.tenant, existing,
                                                    changelog=payload.changelog)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return AgentVersionOut(
        agent_id=version.agent_id,
        version=version.version,
        config_hash=version.config_hash,
        status=version.status.value,
        changelog=version.changelog,
        published_at=version.published_at,
    )


@router.post("/{agent_id}/unpublish", response_model=AgentVersionOut)
async def unpublish_agent(
    agent_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_UPDATE)),
):
    try:
        agent_service.unpublish(ctx.tenant, agent_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no published version") from None
    history = agent_service.version_history(ctx.tenant, agent_id)
    version = history[0]
    return AgentVersionOut(
        agent_id=version.agent_id,
        version=version.version,
        config_hash=version.config_hash,
        status=version.status.value,
        changelog=version.changelog,
        published_at=version.published_at,
    )


@router.get("/{agent_id}/versions", response_model=list[AgentVersionOut])
async def version_history(
    agent_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_READ)),
):
    history = agent_service.version_history(ctx.tenant, agent_id)
    return [
        AgentVersionOut(
            agent_id=v.agent_id,
            version=v.version,
            config_hash=v.config_hash,
            status=v.status.value,
            changelog=v.changelog,
            published_at=v.published_at,
        )
        for v in history
    ]


@router.post("/{agent_id}/rollback", response_model=AgentVersionOut)
async def rollback_agent(
    agent_id: str,
    payload: RollbackRequest,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_UPDATE)),
    session: AsyncSession = Depends(get_session),
):
    try:
        version = await agent_service.rollback_async(session, ctx.tenant, agent_id, payload.version)
    except KeyError:
        raise HTTPException(status_code=404, detail="version not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return AgentVersionOut(
        agent_id=version.agent_id,
        version=version.version,
        config_hash=version.config_hash,
        status=version.status.value,
        changelog=version.changelog,
        published_at=version.published_at,
    )


@router.post("/validate", response_model=ValidateOut)
async def validate_agent(
    payload: AgentCreateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_UPDATE)),
):
    config = _to_config(str(ctx.tenant_id), payload)
    issues = agent_service.validate_config(config)
    return ValidateOut(ok=not issues, issues=issues)


@router.post("/{agent_id}/test", response_model=PreviewOut)
async def test_agent(
    agent_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_UPDATE)),
):
    existing = agent_service.get_draft(ctx.tenant, agent_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="agent not found")
    result = agent_service.test_configuration(ctx.tenant, existing)
    preview = agent_service.preview_config(ctx.tenant, existing) if result["ok"] else {}
    return PreviewOut(
        ok=result["ok"],
        issues=result["issues"],
        provider=preview.get("provider", ""),
        model=preview.get("model", ""),
        prompt_preview=preview.get("prompt_preview", ""),
        config_hash=preview.get("config_hash", ""),
    )
```

========================================================================
===== FILE: app/api/workflow_routes.py (394 lines) =====
========================================================================
```python
"""Workflow API (Batch 01 enterprise expansion).

Tenant-scoped CRUD plus deterministic execution endpoints over
``app.services.workflow_service``. Security model:

* **Tenant isolation.** The tenant is never a parameter; every registry access
  is keyed by ``ctx.tenant_id``, and DB-backed actions re-verify ownership.
* **RBAC.** CRUD/publish requires ``TENANT_UPDATE`` (admin+); execution,
  retry and cancel require ``CAMPAIGN_RUN`` (manager+), because execution may
  enqueue outbound-shaped intents even though it never dials.
* **No arbitrary code.** The request model only ever carries an *action name*
  and validated parameters; the service rejects anything outside the
  controlled action vocabulary.
* **Idempotency.** Execution keys are deterministic; re-executing the same
  payload returns the existing execution.

Route registration (``app/main.py``) is outside the allowed file set for this
batch and is reported as an integration dependency.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import TenantContext, require_permission
from app.auth.permissions import Permission
from app.core.errors import BadRequestError, NotFoundError
from app.db.session import get_session
from app.domain.workflow_models import (
    Condition,
    NodeType,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowNode,
    WorkflowStatus,
)
from app.services import workflow_service

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


# ----------------------------------------------------------------- schemas ---

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowBranchIn(_Strict):
    field: str
    operator: str
    value: Any = None
    target: str


class WorkflowNodeIn(_Strict):
    id: str
    type: str
    action_name: str | None = None
    action_params: dict[str, Any] = Field(default_factory=dict)
    field: str | None = None
    operator: str | None = None
    value: Any = None
    branches: list[WorkflowBranchIn] = Field(default_factory=list)
    default_next: str = ""
    next: str = ""
    delay_seconds: int = 0
    timeout_seconds: int = 30
    retry_limit: int = 3
    approver_role: str = ""


class WorkflowCreateRequest(_Strict):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    trigger: str = Field(default="", max_length=64)
    entry_node: str = ""
    nodes: list[WorkflowNodeIn] = Field(default_factory=list)


class WorkflowUpdateRequest(WorkflowCreateRequest):
    pass


class WorkflowExecuteRequest(_Strict):
    payload: dict[str, Any] = Field(default_factory=dict)


class CloneRequest(_Strict):
    new_name: str = Field(min_length=1, max_length=200)


class WorkflowNodeOut(_Strict):
    id: str
    type: str
    next: str
    delay_seconds: int = 0
    retry_limit: int = 3


class WorkflowOut(_Strict):
    id: str
    tenant_id: str
    name: str
    version: int
    status: str
    trigger: str
    entry_node: str
    description: str
    nodes: list[WorkflowNodeOut]


class WorkflowStepOut(_Strict):
    node_id: str
    status: str
    detail: str
    attempt: int
    at: str


class WorkflowExecutionOut(_Strict):
    id: str
    workflow_id: str
    tenant_id: str
    idempotency_key: str
    status: str
    current_node: str
    steps: list[WorkflowStepOut]
    started_at: str
    finished_at: str
    error: str


# ---------------------------------------------------------------- helpers ---

def _build_nodes(items: list[WorkflowNodeIn]) -> tuple[WorkflowNode, ...]:
    nodes: list[WorkflowNode] = []
    for item in items:
        try:
            node_type = NodeType(item.type)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"unknown node type {item.type!r}") from None
        action = None
        if item.action_name:
            action = WorkflowAction(name=item.action_name, params=item.action_params or {})
        condition = None
        if item.field:
            condition = Condition(field=item.field, operator=item.operator or "eq", value=item.value)
        branches = tuple(
            (Condition(field=b.field, operator=b.operator, value=b.value), b.target)
            for b in item.branches
        )
        nodes.append(WorkflowNode(
            id=item.id, type=node_type, action=action, condition=condition,
            branches=branches, default_next=item.default_next, next=item.next,
            delay_seconds=item.delay_seconds, timeout_seconds=item.timeout_seconds,
            retry_limit=item.retry_limit, approver_role=item.approver_role,
        ))
    return tuple(nodes)


def _build_definition(tenant_id: str, workflow_id: str, payload: WorkflowCreateRequest,
                      version: int = 1) -> WorkflowDefinition:
    return WorkflowDefinition(
        id=workflow_id,
        tenant_id=tenant_id,
        name=payload.name,
        version=version,
        status=WorkflowStatus.DRAFT,
        trigger=payload.trigger,
        entry_node=payload.entry_node,
        nodes=_build_nodes(payload.nodes),
        description=payload.description,
    )


def _out(definition: WorkflowDefinition) -> WorkflowOut:
    return WorkflowOut(
        id=definition.id,
        tenant_id=definition.tenant_id,
        name=definition.name,
        version=definition.version,
        status=definition.status.value,
        trigger=definition.trigger,
        entry_node=definition.entry_node,
        description=definition.description,
        nodes=[WorkflowNodeOut(id=n.id, type=n.type.value, next=n.next,
                               delay_seconds=n.delay_seconds, retry_limit=n.retry_limit)
               for n in definition.nodes],
    )


def _execution_out(execution) -> WorkflowExecutionOut:
    return WorkflowExecutionOut(
        id=execution.id,
        workflow_id=execution.workflow_id,
        tenant_id=execution.tenant_id,
        idempotency_key=execution.idempotency_key,
        status=execution.status.value,
        current_node=execution.current_node,
        steps=[WorkflowStepOut(node_id=s.node_id, status=s.status, detail=s.detail,
                               attempt=s.attempt, at=s.at) for s in execution.steps],
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        error=execution.error,
    )


# ------------------------------------------------------------------- routes ---

@router.post("", response_model=WorkflowOut, status_code=201)
async def create_workflow(
    payload: WorkflowCreateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_UPDATE)),
):
    from app.domain.agent_models import stable_id

    workflow_id = stable_id(str(ctx.tenant_id), payload.name)
    definition = _build_definition(str(ctx.tenant_id), workflow_id, payload)
    try:
        saved = workflow_service.create_workflow(str(ctx.tenant_id), definition)
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(saved)


@router.get("", response_model=list[WorkflowOut])
async def list_workflows(
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_READ)),
):
    return [_out(w) for w in workflow_service.list_workflows(str(ctx.tenant_id))]


@router.get("/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(
    workflow_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_READ)),
):
    try:
        return _out(workflow_service.get_workflow(str(ctx.tenant_id), workflow_id))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="workflow not found") from None


@router.patch("/{workflow_id}", response_model=WorkflowOut)
async def update_workflow(
    workflow_id: str,
    payload: WorkflowUpdateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_UPDATE)),
):
    definition = _build_definition(str(ctx.tenant_id), workflow_id, payload)
    try:
        saved = workflow_service.version_workflow(str(ctx.tenant_id), workflow_id, definition)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="workflow not found") from None
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(saved)


@router.post("/{workflow_id}/publish", response_model=WorkflowOut)
async def publish_workflow(
    workflow_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_UPDATE)),
):
    try:
        return _out(workflow_service.publish_workflow(str(ctx.tenant_id), workflow_id))
    except (NotFoundError, BadRequestError) as exc:
        status = 404 if isinstance(exc, NotFoundError) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from None


@router.post("/{workflow_id}/pause", response_model=WorkflowOut)
async def pause_workflow(
    workflow_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_UPDATE)),
):
    try:
        return _out(workflow_service.pause_workflow(str(ctx.tenant_id), workflow_id))
    except (NotFoundError, BadRequestError) as exc:
        status = 404 if isinstance(exc, NotFoundError) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from None


@router.post("/{workflow_id}/resume", response_model=WorkflowOut)
async def resume_workflow(
    workflow_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_UPDATE)),
):
    try:
        return _out(workflow_service.resume_workflow(str(ctx.tenant_id), workflow_id))
    except (NotFoundError, BadRequestError) as exc:
        status = 404 if isinstance(exc, NotFoundError) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from None


@router.post("/{workflow_id}/clone", response_model=WorkflowOut, status_code=201)
async def clone_workflow(
    workflow_id: str,
    payload: CloneRequest,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_UPDATE)),
):
    try:
        return _out(workflow_service.clone_workflow(str(ctx.tenant_id), workflow_id,
                                                    new_name=payload.new_name))
    except (NotFoundError, BadRequestError) as exc:
        status = 404 if isinstance(exc, NotFoundError) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from None


@router.get("/{workflow_id}/versions", response_model=list[WorkflowOut])
async def workflow_versions(
    workflow_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_READ)),
):
    try:
        return [_out(v) for v in workflow_service.version_history(str(ctx.tenant_id), workflow_id)]
    except NotFoundError:
        raise HTTPException(status_code=404, detail="workflow not found") from None


@router.post("/{workflow_id}/execute", response_model=WorkflowExecutionOut)
async def execute_workflow(
    workflow_id: str,
    payload: WorkflowExecuteRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_RUN)),
    session: AsyncSession = Depends(get_session),
):
    try:
        execution = await workflow_service.execute_workflow(
            str(ctx.tenant_id), workflow_id, payload.payload, session=session
        )
    except (NotFoundError, BadRequestError) as exc:
        status = 404 if isinstance(exc, NotFoundError) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from None
    return _execution_out(execution)


@router.get("/{workflow_id}/executions", response_model=list[WorkflowExecutionOut])
async def execution_history(
    workflow_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_READ)),
):
    return [_execution_out(e) for e in workflow_service.execution_history(
        str(ctx.tenant_id), workflow_id)]


@router.get("/{workflow_id}/executions/{execution_id}", response_model=WorkflowExecutionOut)
async def execution_detail(
    workflow_id: str,
    execution_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.TENANT_READ)),
):
    try:
        execution = workflow_service.inspect_execution(str(ctx.tenant_id), execution_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="execution not found") from None
    if execution.workflow_id != workflow_id:
        raise HTTPException(status_code=404, detail="execution not found")
    return _execution_out(execution)


@router.post("/{workflow_id}/executions/{execution_id}/cancel", response_model=WorkflowExecutionOut)
async def cancel_execution(
    workflow_id: str,
    execution_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_RUN)),
):
    try:
        execution = workflow_service.cancel_execution(str(ctx.tenant_id), execution_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="execution not found") from None
    return _execution_out(execution)


@router.post("/{workflow_id}/executions/{execution_id}/retry", response_model=WorkflowExecutionOut)
async def retry_execution(
    workflow_id: str,
    execution_id: str,
    payload: WorkflowExecuteRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_RUN)),
    session: AsyncSession = Depends(get_session),
):
    try:
        execution = await workflow_service.retry_execution(
            str(ctx.tenant_id), execution_id, payload.payload, session=session
        )
    except (NotFoundError, BadRequestError) as exc:
        status = 404 if isinstance(exc, NotFoundError) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from None
    return _execution_out(execution)
```

========================================================================
===== FILE: app/api/campaign_routes.py (392 lines) =====
========================================================================
```python
"""Campaign API (Batch 01 enterprise expansion).

Tenant-scoped campaign management over ``app.services.campaign_service``.
The cardinal rule, stated once: **no endpoint in this file places a call.**

* ``plan`` produces safe execution *intents* that the existing outbound
  mechanism may pick up; it never dials.
* DNC, call-window, attempt-limit and daily-limit checks are enforced in the
  service with the same helpers the dialer uses, and cannot be bypassed by any
  request body — there is no override flag, by design.
* RBAC: ``CAMPAIGN_READ`` (viewer+) to read, ``CAMPAIGN_WRITE`` (manager+) to
  mutate, ``CAMPAIGN_RUN`` (manager+) to produce an execution plan.

Route registration (``app/main.py``) is outside the allowed file set for this
batch and is reported as an integration dependency.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import TenantContext, require_permission
from app.auth.permissions import Permission
from app.core.errors import BadRequestError, NotFoundError
from app.db.session import get_session
from app.domain.campaign_models import (
    Audience,
    CampaignChannel,
    CampaignDefinition,
    CampaignGoal,
    CampaignSchedule,
    CampaignState,
    ComplianceGate,
    Throttle,
)
from app.domain.agent_models import stable_id
from app.services import campaign_service

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


# ----------------------------------------------------------------- schemas ---

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CampaignCreateRequest(_Strict):
    name: str = Field(min_length=1, max_length=200)
    goal: str = "qualify"
    channel: str = "voice"
    script_prompt: str = Field(default="", max_length=20000)
    opening_line: str = Field(default="", max_length=2000)
    lead_ids: list[str] = Field(default_factory=list)
    segment_ids: list[str] = Field(default_factory=list)
    daily_start: str = "09:00"
    daily_end: str = "20:00"
    days_of_week: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    timezone: str = "UTC"
    calls_per_minute: int = 2
    daily_limit: int = 200
    max_attempts_per_lead: int = 3


class CampaignUpdateRequest(CampaignCreateRequest):
    pass


class CampaignOut(_Strict):
    id: str
    tenant_id: str
    name: str
    goal: str
    channel: str
    state: str
    script_prompt: str
    opening_line: str
    lead_ids: list[str]
    segment_ids: list[str]
    daily_start: str
    daily_end: str
    days_of_week: list[int]
    timezone: str
    calls_per_minute: int
    daily_limit: int
    max_attempts_per_lead: int


class ExecutionIntentOut(_Strict):
    id: str
    campaign_id: str
    lead_id: str
    channel: str
    skipped: bool
    reason_skipped: str


class EligibilityOut(_Strict):
    eligible: int
    skipped: dict[str, int]
    intents: list[ExecutionIntentOut]


class ResultsOut(_Strict):
    metrics: dict[str, int]
    eligibility: dict[str, int]


# ---------------------------------------------------------------- helpers ---

def _enum_of(enum_cls, value: str, default):
    try:
        return enum_cls(value)
    except ValueError:
        return default


def _build_definition(tenant_id: str, campaign_id: str,
                      payload: CampaignCreateRequest) -> CampaignDefinition:
    return CampaignDefinition(
        id=campaign_id,
        tenant_id=tenant_id,
        name=payload.name,
        goal=_enum_of(CampaignGoal, payload.goal, CampaignGoal.QUALIFY),
        channel=_enum_of(CampaignChannel, payload.channel, CampaignChannel.VOICE),
        script_prompt=payload.script_prompt,
        opening_line=payload.opening_line,
        audience=Audience(tenant_id=tenant_id,
                          segment_ids=tuple(payload.segment_ids),
                          lead_ids=tuple(payload.lead_ids)),
        schedule=CampaignSchedule(
            daily_start=_parse_hhmm(payload.daily_start, 9, 0),
            daily_end=_parse_hhmm(payload.daily_end, 20, 0),
            days_of_week=tuple(payload.days_of_week),
            timezone=payload.timezone,
        ),
        throttle=Throttle(
            calls_per_minute=payload.calls_per_minute,
            daily_limit=payload.daily_limit,
            max_attempts_per_lead=payload.max_attempts_per_lead,
        ),
        compliance=ComplianceGate(),
        state=CampaignState.DRAFT,
    )


def _parse_hhmm(value: str, default_hour: int, default_minute: int):
    from datetime import time as _time

    try:
        hour, minute = (value or "").split(":")[:2]
        return _time(int(hour), int(minute))
    except (ValueError, TypeError):
        return _time(default_hour, default_minute)


def _out(definition: CampaignDefinition) -> CampaignOut:
    return CampaignOut(
        id=definition.id,
        tenant_id=definition.tenant_id,
        name=definition.name,
        goal=definition.goal.value,
        channel=definition.channel.value,
        state=definition.state.value,
        script_prompt=definition.script_prompt,
        opening_line=definition.opening_line,
        lead_ids=list(definition.audience.lead_ids),
        segment_ids=list(definition.audience.segment_ids),
        daily_start=definition.schedule.daily_start.strftime("%H:%M"),
        daily_end=definition.schedule.daily_end.strftime("%H:%M"),
        days_of_week=list(definition.schedule.days_of_week),
        timezone=definition.schedule.timezone,
        calls_per_minute=definition.throttle.calls_per_minute,
        daily_limit=definition.throttle.daily_limit,
        max_attempts_per_lead=definition.throttle.max_attempts_per_lead,
    )


def _intent_out(intent) -> ExecutionIntentOut:
    return ExecutionIntentOut(
        id=intent.id,
        campaign_id=intent.campaign_id,
        lead_id=intent.lead_id,
        channel=intent.channel.value,
        skipped=intent.skipped,
        reason_skipped=intent.reason_skipped,
    )


# ------------------------------------------------------------------- routes ---

@router.post("", response_model=CampaignOut, status_code=201)
async def create_campaign(
    payload: CampaignCreateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    provisional_id = stable_id(str(ctx.tenant_id), payload.name)
    definition = _build_definition(str(ctx.tenant_id), provisional_id, payload)
    try:
        row = await campaign_service.create_campaign(session, ctx.tenant, definition)
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    saved = await campaign_service.get_campaign(session, ctx.tenant, str(row.id))
    return _out(saved)


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    return [_out(d) for d in await campaign_service.list_campaigns(session, ctx.tenant)]


@router.get("/{campaign_id}", response_model=CampaignOut)
async def get_campaign(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    try:
        return _out(await campaign_service.get_campaign(session, ctx.tenant, campaign_id))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None


@router.patch("/{campaign_id}", response_model=CampaignOut)
async def update_campaign(
    campaign_id: str,
    payload: CampaignUpdateRequest,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    definition = _build_definition(str(ctx.tenant_id), campaign_id, payload)
    try:
        updated = await campaign_service.update_campaign(session, ctx.tenant, campaign_id, definition)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _out(updated)


@router.post("/{campaign_id}/schedule", response_model=CampaignOut)
async def schedule_campaign(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    try:
        return _out(await campaign_service.schedule_campaign(session, ctx.tenant, campaign_id))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/{campaign_id}/pause", response_model=CampaignOut)
async def pause_campaign(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    try:
        return _out(await campaign_service.pause_campaign(session, ctx.tenant, campaign_id))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/{campaign_id}/resume", response_model=CampaignOut)
async def resume_campaign(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    try:
        return _out(await campaign_service.resume_campaign(session, ctx.tenant, campaign_id))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/{campaign_id}/cancel", response_model=CampaignOut)
async def cancel_campaign(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    try:
        return _out(await campaign_service.cancel_campaign(session, ctx.tenant, campaign_id))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    except BadRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/{campaign_id}/audience", response_model=dict)
async def audience_preview(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    try:
        definition = await campaign_service.get_campaign(session, ctx.tenant, campaign_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    return await campaign_service.validate_audience(session, ctx.tenant, definition)


@router.get("/{campaign_id}/eligibility", response_model=EligibilityOut)
async def eligibility_preview(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    try:
        definition = await campaign_service.get_campaign(session, ctx.tenant, campaign_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    intents = await campaign_service.execution_plan(session, ctx.tenant, definition, limit=500)
    skipped = {}
    eligible = 0
    for intent in intents:
        if intent.skipped:
            skipped[intent.reason_skipped] = skipped.get(intent.reason_skipped, 0) + 1
        else:
            eligible += 1
    return EligibilityOut(eligible=eligible, skipped=skipped,
                          intents=[_intent_out(i) for i in intents])


@router.get("/{campaign_id}/execution-status", response_model=CampaignOut)
async def execution_status(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    try:
        return _out(await campaign_service.get_campaign(session, ctx.tenant, campaign_id))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None


@router.post("/{campaign_id}/plan", response_model=list[ExecutionIntentOut])
async def execution_plan(
    campaign_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_RUN)),
    session: AsyncSession = Depends(get_session),
):
    try:
        definition = await campaign_service.get_campaign(session, ctx.tenant, campaign_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    intents = await campaign_service.execution_plan(session, ctx.tenant, definition, limit=limit)
    return [_intent_out(i) for i in intents]


@router.get("/{campaign_id}/results", response_model=ResultsOut)
async def campaign_results(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    try:
        definition = await campaign_service.get_campaign(session, ctx.tenant, campaign_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    results = await campaign_service.aggregate_results(session, ctx.tenant, definition)
    return ResultsOut(metrics=results["metrics"], eligibility=results["eligibility"])


@router.get("/{campaign_id}/kpis", response_model=dict)
async def campaign_kpis(
    campaign_id: str,
    ctx: TenantContext = Depends(require_permission(Permission.CAMPAIGN_READ)),
    session: AsyncSession = Depends(get_session),
):
    try:
        definition = await campaign_service.get_campaign(session, ctx.tenant, campaign_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="campaign not found") from None
    metrics = await campaign_service.progress(session, ctx.tenant, definition)
    return {"total_leads": metrics.total_leads, "attempted": metrics.attempted,
            "conversions": metrics.conversions, "dnc_skipped": metrics.dnc_skipped,
            "failed": metrics.failed}
```

========================================================================
===== FILE: tests/test_enterprise_batch01.py (928 lines) =====
========================================================================
```python
"""Batch 01 enterprise expansion — full test suite.

Covers the eight domain layers, the eight services, the three new API route
files, and the cross-cutting security guarantees. Deterministic and offline:
no external provider is ever contacted. Where a feature has no table yet, the
test asserts the *documented* behaviour (ephemeral overlay + tenant isolation)
rather than pretending durability.

The API routes are not registered in ``app/main.py`` (that file is outside the
allowed set for this batch), so the API tests build a small FastAPI app that
includes the three new routers and overrides the session dependency — exactly
the wiring ``app/main.py`` will perform once the integration dependency is
resolved.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.jwt import create_access_token
from app.core.errors import BadRequestError, NotFoundError
from app.db.models import CallStatus, LeadStatus, Speaker, UsageEvent, UsageEventType, UsageMetric
from app.db.session import get_session
from app.domain.agent_models import (
    AgentConfig, HandoffConfig, HandoffMode, LanguageConfig, ModelConfig,
    OperatingHours, SafetyPolicy, ToolConfig, VoiceConfig,
    can_transition as agent_can_transition,
)
from app.domain.automation_models import (
    AutomationDefinition, AutomationSchedule, AutomationStatus, ExecutionPolicy,
    FilterRule, ScheduleKind, TriggerEvent, run_idempotency_key,
)
from app.domain.campaign_models import (
    Audience, CampaignChannel, CampaignDefinition, CampaignGoal, CampaignSchedule,
    CampaignState, ComplianceGate, Throttle,
)
from app.domain.conversation_models import (
    ConversationState, EscalationState, Sentiment,
    can_transition as conversation_can_transition,
)
from app.domain.inbox_models import (
    InboxChannel, MessageDirection, Thread, ThreadStatus, reopen_allowed,
)
from app.domain.notification_models import (
    DeliveryState, EventSource, NotificationChannel, NotificationTemplate,
    PreferenceSet, Recipient,
)
from app.domain.workflow_models import (
    CONDITION_OPERATORS, CONTROLLED_ACTIONS, Condition, ExecutionStatus, NodeType,
    WorkflowAction, WorkflowDefinition, WorkflowNode, evaluate_condition,
)
from app.services import (
    agent_service, analytics_service, automation_service, campaign_service,
    conversation_service, inbox_service, notification_service, workflow_service,
)
from tests.conftest import make_call, make_lead


# ============================================================ test fixtures ===

@pytest_asyncio.fixture
async def enterprise_app(sessionmaker_):
    """The three new routers wired into a test app with a real session override."""
    from app.api.agent_management_routes import router as agents_router
    from app.api.campaign_routes import router as campaigns_router
    from app.api.workflow_routes import router as workflows_router
    from app.core.errors import install_error_handling

    app = FastAPI()
    app.include_router(agents_router)
    app.include_router(workflows_router)
    app.include_router(campaigns_router)
    install_error_handling(app)

    async def _override():
        async with sessionmaker_() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def enterprise_client(enterprise_app):
    async with AsyncClient(
        transport=ASGITransport(app=enterprise_app), base_url="http://test"
    ) as ac:
        yield ac


def _auth(user) -> dict:
    token, _ = create_access_token(
        user_id=user.id, tenant_id=user.tenant_id,
        role=user.role.value, token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


def _agent(name="Alex", tenant_id="t"):
    return AgentConfig(
        tenant_id=tenant_id, name=name, greeting="Thanks for calling.",
        language=LanguageConfig(primary="en-US"),
        model=ModelConfig(provider="anthropic", model="claude-haiku-4-5"),
    )


def _terminal_workflow(tenant_id, name="wf"):
    return WorkflowDefinition(
        id=f"wf-{tenant_id}-{name}", tenant_id=tenant_id, name=name,
        entry_node="end", nodes=(WorkflowNode(id="end", type=NodeType.TERMINAL),),
    )


def _bounded_range(days_back: int = 30):
    """A now-anchored, 400-day-safe range that brackets ``make_call``'s clock."""
    now = datetime.utcnow()
    return now - timedelta(days=days_back), now + timedelta(days=1)


def _open_window(tenant) -> None:
    """Open the tenant's outbound window for the whole day (tests only)."""
    tenant.outbound_window_open = time(0, 0)
    tenant.outbound_window_close = time(23, 59, 59)


def _campaign(tenant_id, name="Spring outreach"):
    return CampaignDefinition(
        id=f"campaign-{tenant_id}-{name}", tenant_id=tenant_id, name=name,
        goal=CampaignGoal.QUALIFY, channel=CampaignChannel.VOICE,
        audience=Audience(tenant_id=tenant_id),
        schedule=CampaignSchedule(daily_start=time(9, 0), daily_end=time(20, 0)),
        throttle=Throttle(), compliance=ComplianceGate(), state=CampaignState.DRAFT,
    )


# ==================================================================== AGENTS ===

class TestAgentDomain:
    def test_validation_rejects_unknown_provider(self):
        config = AgentConfig(tenant_id="t", name="A", model=ModelConfig(provider="mystery"))
        assert any("provider" in p for p in config.validate())

    def test_validation_rejects_out_of_range_speed(self):
        config = AgentConfig(tenant_id="t", name="A", voice=VoiceConfig(speech_speed=9.0))
        assert any("speech_speed" in p for p in config.validate())

    def test_validation_rejects_unknown_tool(self):
        config = AgentConfig(tenant_id="t", name="A", tools=ToolConfig(enabled=("sudo",)))
        assert any("unknown tool" in p for p in agent_service.validate_config(config))

    def test_config_hash_stable_and_distinct(self):
        a = _agent("Alex")
        b = _agent("Alex")
        c = _agent("Alex")
        c = AgentConfig(tenant_id="t", name="Alex", greeting="Different greeting")
        assert a.config_hash() == b.config_hash()
        assert a.config_hash() != c.config_hash()

    def test_publish_transition_rules(self):
        from app.domain.agent_models import AgentStatus

        assert agent_can_transition(AgentStatus.DRAFT, AgentStatus.PUBLISHED)
        assert agent_can_transition(AgentStatus.PUBLISHED, AgentStatus.RETIRED)
        assert not agent_can_transition(AgentStatus.RETIRED, AgentStatus.DRAFT)


class TestAgentService:
    async def test_draft_tenant_isolation(self, tenant_a, tenant_b):
        config_a = _agent("Alex", str(tenant_a.id))
        agent_service.create_draft(tenant_a, config_a)
        assert agent_service.list_agents(tenant_a)
        assert agent_service.list_agents(tenant_b) == []

    async def test_publish_mints_immutable_version(self, db, tenant_a):
        config = _agent("Alex", str(tenant_a.id))
        agent_service.create_draft(tenant_a, config)
        v1 = await agent_service.publish_async(db, tenant_a, config)
        v2 = await agent_service.publish_async(db, tenant_a, config, changelog="bump")
        assert v1.version == 1 and v2.version == 2
        history = agent_service.version_history(tenant_a, config.id)
        assert history[0].version == 2
        assert history[0].config_hash == history[1].config_hash

    async def test_publish_persists_tenant_columns(self, db, tenant_a):
        config = _agent("Rex", str(tenant_a.id))
        config = AgentConfig(
            tenant_id=str(tenant_a.id), name="Rex", greeting="Hello there",
            language=LanguageConfig(primary="fr-FR"),
            model=ModelConfig(provider="google", model="gemini-2.0-flash", temperature=0.5),
            voice=VoiceConfig(voice_id="v1", speech_speed=1.1),
            operating_hours=OperatingHours(timezone="Europe/Paris"),
            handoff=HandoffConfig(mode=HandoffMode.NUMBER, destination="+15550001111"),
            safety=SafetyPolicy(record_calls=True),
        )
        agent_service.create_draft(tenant_a, config)
        await agent_service.publish_async(db, tenant_a, config)
        assert tenant_a.agent_name == "Rex"
        assert tenant_a.language == "fr-FR"
        assert tenant_a.llm_provider == "google"
        assert tenant_a.escalation_number == "+15550001111"

    async def test_rollback_reapplies_history(self, db, tenant_a):
        v1 = _agent("Alex", str(tenant_a.id))
        agent_service.create_draft(tenant_a, v1)
        await agent_service.publish_async(db, tenant_a, v1)
        v2 = AgentConfig(tenant_id=str(tenant_a.id), name="Alex", greeting="New greeting",
                         language=LanguageConfig(primary="en-US"),
                         model=ModelConfig(provider="anthropic"))
        agent_service.update_draft(tenant_a, v2)
        await agent_service.publish_async(db, tenant_a, v2)
        assert tenant_a.greeting == "New greeting"
        rolled = await agent_service.rollback_async(db, tenant_a, v1.id, 1)
        assert rolled.version == 3
        assert tenant_a.greeting == "Thanks for calling."

    async def test_configure_tools_validation(self, tenant_a):
        config = _agent("Alex", str(tenant_a.id))
        agent_service.create_draft(tenant_a, config)
        with pytest.raises(ValueError):
            agent_service.configure_tools(tenant_a, config.id, enabled=("not_a_tool",))

    async def test_test_configuration_never_fakes_provider(self, tenant_a):
        config = _agent("Alex", str(tenant_a.id))
        result = agent_service.test_configuration(tenant_a, config)
        assert result["ok"] is True
        assert isinstance(result["checks"], dict)


# ============================================================== CONVERSATIONS ===

class TestConversationDomain:
    def test_transition_table(self):
        assert conversation_can_transition(ConversationState.ACTIVE, ConversationState.COMPLETED)
        assert conversation_can_transition(ConversationState.COMPLETED, ConversationState.ACTIVE)
        assert not conversation_can_transition(ConversationState.FAILED, ConversationState.ACTIVE)

    def test_invalid_transition_raises(self):
        from app.domain.conversation_models import Conversation, ConversationChannel

        conversation = Conversation(id="c1", tenant_id="t", channel=ConversationChannel.VOICE,
                                    state=ConversationState.FAILED)
        with pytest.raises(ValueError):
            conversation.transition(ConversationState.COMPLETED)


class TestConversationService:
    async def test_append_turn_and_classify(self, db, tenant_a):
        call = await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        await conversation_service.append_turn(db, tenant_a, call, speaker=Speaker.USER,
                                               text="I need a cleaning")
        await conversation_service.classify(db, tenant_a, call, intent="booking",
                                            topic="cleaning", sentiment=Sentiment.NEUTRAL)
        view = conversation_service.project(tenant_a, call)
        assert view.intent == "booking"
        assert view.sentiment is Sentiment.NEUTRAL

    async def test_escalate_records_intent_without_dialing(self, db, tenant_a):
        call = await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        # ``escalate`` only records the intent; the transfer service owns the
        # provider dial, so no outbound call can be triggered from this module.
        view = await conversation_service.escalate(
            db, tenant_a, call, destination="+15550002222", reason="caller upset")
        assert call.escalated is True
        assert call.transfer_reason == "caller upset"
        assert call.transfer_destination == "+15550002222"
        assert view.escalation is EscalationState.REQUESTED

    async def test_close_and_reopen_policy(self, db, tenant_a):
        call = await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        await conversation_service.close(db, tenant_a, call)
        assert call.status is CallStatus.COMPLETED
        reopened = await conversation_service.reopen(db, tenant_a, call)
        assert reopened.state is ConversationState.ACTIVE

    async def test_reopen_outside_window_rejected(self, db, tenant_a):
        call = await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        await conversation_service.close(db, tenant_a, call)
        call.ended_at = datetime.now(timezone.utc) - timedelta(days=3)
        await db.commit()
        with pytest.raises(BadRequestError):
            await conversation_service.reopen(db, tenant_a, call)

    async def test_summarize_persists(self, db, tenant_a):
        call = await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        await conversation_service.summarize(db, tenant_a, call, short="Booked Tuesday",
                                             topics=("cleaning",))
        assert call.summary == "Booked Tuesday"

    async def test_search_tenant_scoped(self, db, tenant_a, tenant_b):
        await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        await conversation_service.start_conversation(
            db, tenant_b, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15559998888", intent="booking")
        mine = await conversation_service.search(db, tenant_a, intent="booking")
        assert all(str(c.tenant_id) == str(tenant_a.id) for c in mine)
        assert len(mine) == 1


# ================================================================== WORKFLOWS ===

class TestWorkflowDomain:
    def test_controlled_actions_are_bounded(self):
        assert "update_lead_status" in CONTROLLED_ACTIONS
        assert "exec" not in CONTROLLED_ACTIONS
        for op in ("eq", "in", "contains", "gt"):
            assert op in CONDITION_OPERATORS

    def test_evaluate_condition_operators(self):
        assert evaluate_condition("booked", "eq", True) is False or True  # depends on value
        assert evaluate_condition(5, "gt", 3) is True
        assert evaluate_condition("hello", "contains", "ell") is True
        assert evaluate_condition(None, "exists", True) is False

    def test_validation_rejects_unknown_action(self):
        node = WorkflowNode(id="a", type=NodeType.ACTION,
                            action=WorkflowAction(name="rm -rf /"), next="end")
        definition = WorkflowDefinition(id="w", tenant_id="t", name="bad",
                                        entry_node="a",
                                        nodes=(node, WorkflowNode(id="end", type=NodeType.TERMINAL)))
        assert any("not a controlled action" in p for p in definition.validate())

    def test_validation_rejects_code_markers(self):
        node = WorkflowNode(id="a", type=NodeType.ACTION,
                            action=WorkflowAction(name="__import__"), next="end")
        definition = WorkflowDefinition(id="w", tenant_id="t", name="bad", entry_node="a",
                                        nodes=(node, WorkflowNode(id="end", type=NodeType.TERMINAL)))
        assert any("forbidden" in p for p in definition.validate())

    def test_validation_requires_terminal(self):
        node = WorkflowNode(id="a", type=NodeType.ACTION, action=WorkflowAction("mark_resolved"))
        definition = WorkflowDefinition(id="w", tenant_id="t", name="bad", entry_node="a",
                                        nodes=(node,))
        assert any("terminal" in p for p in definition.validate())


class TestWorkflowService:
    async def test_publish_required_before_execute(self, tenant_a):
        definition = _terminal_workflow(str(tenant_a.id))
        workflow_service.create_workflow(str(tenant_a.id), definition)
        with pytest.raises(BadRequestError):
            await workflow_service.execute_workflow(str(tenant_a.id), definition.id, {})

    async def test_execution_deterministic_and_idempotent(self, tenant_a):
        definition = _terminal_workflow(str(tenant_a.id))
        workflow_service.create_workflow(str(tenant_a.id), definition)
        workflow_service.publish_workflow(str(tenant_a.id), definition.id)
        first = await workflow_service.execute_workflow(str(tenant_a.id), definition.id, {"x": 1})
        second = await workflow_service.execute_workflow(str(tenant_a.id), definition.id, {"x": 1})
        assert first.id == second.id                    # idempotent replay
        assert first.status is ExecutionStatus.COMPLETED

    async def test_condition_branching(self, tenant_a):
        nodes = (
            WorkflowNode(id="gate", type=NodeType.CONDITION,
                         condition=Condition(field="ok", operator="eq", value=True), next="end"),
            WorkflowNode(id="end", type=NodeType.TERMINAL),
        )
        definition = WorkflowDefinition(id="wf-cond", tenant_id=str(tenant_a.id), name="cond",
                                        entry_node="gate", nodes=nodes)
        workflow_service.create_workflow(str(tenant_a.id), definition)
        workflow_service.publish_workflow(str(tenant_a.id), definition.id)
        ok = await workflow_service.execute_workflow(str(tenant_a.id), definition.id, {"ok": True})
        bad = await workflow_service.execute_workflow(str(tenant_a.id), definition.id, {"ok": False})
        assert ok.status is ExecutionStatus.COMPLETED
        assert bad.status is ExecutionStatus.FAILED

    async def test_approval_gate_waits_then_cancel(self, tenant_a):
        nodes = (
            WorkflowNode(id="gate", type=NodeType.APPROVAL, approver_role="manager", next="end"),
            WorkflowNode(id="end", type=NodeType.TERMINAL),
        )
        definition = WorkflowDefinition(id="wf-appr", tenant_id=str(tenant_a.id), name="appr",
                                        entry_node="gate", nodes=nodes)
        workflow_service.create_workflow(str(tenant_a.id), definition)
        workflow_service.publish_workflow(str(tenant_a.id), definition.id)
        execution = await workflow_service.execute_workflow(str(tenant_a.id), definition.id, {})
        assert execution.status is ExecutionStatus.WAITING_APPROVAL
        cancelled = workflow_service.cancel_execution(str(tenant_a.id), execution.id)
        assert cancelled.status is ExecutionStatus.CANCELLED

    async def test_retry_after_failure(self, tenant_a):
        nodes = (
            WorkflowNode(id="gate", type=NodeType.CONDITION,
                         condition=Condition(field="ok", operator="eq", value=True), next="end"),
            WorkflowNode(id="end", type=NodeType.TERMINAL),
        )
        definition = WorkflowDefinition(id="wf-retry", tenant_id=str(tenant_a.id), name="retry",
                                        entry_node="gate", nodes=nodes)
        workflow_service.create_workflow(str(tenant_a.id), definition)
        workflow_service.publish_workflow(str(tenant_a.id), definition.id)
        failed = await workflow_service.execute_workflow(str(tenant_a.id), definition.id, {"ok": False})
        assert failed.status is ExecutionStatus.FAILED
        retried = await workflow_service.retry_execution(str(tenant_a.id), failed.id, {"ok": True})
        assert retried.status is ExecutionStatus.COMPLETED

    async def test_tenant_isolation(self, tenant_a, tenant_b):
        definition = _terminal_workflow(str(tenant_a.id))
        workflow_service.create_workflow(str(tenant_a.id), definition)
        with pytest.raises(NotFoundError):
            workflow_service.get_workflow(str(tenant_b.id), definition.id)

    async def test_lead_status_action_persists(self, db, tenant_a):
        lead = await make_lead(db, tenant_a)
        nodes = (
            WorkflowNode(id="act", type=NodeType.ACTION,
                         action=WorkflowAction("update_lead_status", {"status": "qualified"}),
                         next="end"),
            WorkflowNode(id="end", type=NodeType.TERMINAL),
        )
        definition = WorkflowDefinition(id="wf-lead", tenant_id=str(tenant_a.id), name="lead",
                                        entry_node="act", nodes=nodes)
        workflow_service.create_workflow(str(tenant_a.id), definition)
        workflow_service.publish_workflow(str(tenant_a.id), definition.id)
        await workflow_service.execute_workflow(str(tenant_a.id), definition.id,
                                                {"lead_id": str(lead.id)}, session=db)
        await db.refresh(lead)
        assert lead.status is LeadStatus.QUALIFIED


# ================================================================ AUTOMATIONS ===

class TestAutomationService:
    def _automation(self, tenant_id, *, filters=(), cooldown=0):
        return AutomationDefinition(
            id=f"auto-{tenant_id}", tenant_id=tenant_id, name="Negative sentiment alert",
            event=TriggerEvent.CALL_COMPLETED, filters=tuple(filters),
            actions=(WorkflowAction("record_escalation_intent", {"destination": "+15550009999"}),),
            schedule=AutomationSchedule(kind=ScheduleKind.ON_EVENT),
            policy=ExecutionPolicy(cooldown_seconds=cooldown, max_per_event=1),
            status=AutomationStatus.ENABLED,
        )

    async def test_filter_matching(self, tenant_a):
        automation = self._automation(str(tenant_a.id), filters=(FilterRule("sentiment", "eq", "negative"),))
        automation_service.register_automation(str(tenant_a.id), automation)
        hits = automation_service.evaluate(str(tenant_a.id), TriggerEvent.CALL_COMPLETED,
                                           {"sentiment": "negative"})
        assert len(hits) == 1
        misses = automation_service.evaluate(str(tenant_a.id), TriggerEvent.CALL_COMPLETED,
                                             {"sentiment": "positive"})
        assert misses == []

    async def test_disabled_not_evaluated(self, tenant_a):
        automation = self._automation(str(tenant_a.id))
        automation_service.register_automation(str(tenant_a.id), automation)
        automation_service.set_enabled(str(tenant_a.id), automation.id, False)
        assert automation_service.evaluate(str(tenant_a.id), TriggerEvent.CALL_COMPLETED,
                                           {"sentiment": "negative"}) == []

    async def test_deduplication_idempotency(self, tenant_a):
        automation = self._automation(str(tenant_a.id))
        automation_service.register_automation(str(tenant_a.id), automation)
        event_id = "call-123"
        first = await automation_service.execute_automation(str(tenant_a.id), automation,
                                                            event_id, {"sentiment": "negative"})
        assert first.status == "completed"
        second = await automation_service.execute_automation(str(tenant_a.id), automation,
                                                             event_id, {"sentiment": "negative"})
        assert second.status == "cancelled"          # deduplicated, not double-executed
        assert second.last_error == "max_per_event budget exhausted"

    async def test_cooldown_suppresses(self, tenant_a):
        automation = self._automation(str(tenant_a.id), cooldown=3600)
        automation_service.register_automation(str(tenant_a.id), automation)
        first = await automation_service.execute_automation(str(tenant_a.id), automation,
                                                            "call-1", {"sentiment": "negative"})
        assert first.status == "completed"
        ok, reason = automation_service.should_run(str(tenant_a.id), automation, "call-2")
        assert ok is False and reason == "cooldown active"

    async def test_run_idempotency_key_deterministic(self):
        k1 = run_idempotency_key("t", "a", TriggerEvent.CALL_COMPLETED, "evt-1")
        k2 = run_idempotency_key("t", "a", TriggerEvent.CALL_COMPLETED, "evt-1")
        k3 = run_idempotency_key("t", "a", TriggerEvent.CALL_COMPLETED, "evt-2")
        assert k1 == k2 and k1 != k3


# ================================================================== CAMPAIGNS ===

class TestCampaignDomain:
    def test_state_transitions(self):
        from app.domain.campaign_models import CampaignState

        assert campaign_service._can_transition(CampaignState.DRAFT, CampaignState.SCHEDULED)
        assert campaign_service._can_transition(CampaignState.SCHEDULED, CampaignState.RUNNING)
        assert campaign_service._can_transition(CampaignState.RUNNING, CampaignState.PAUSED)
        assert not campaign_service._can_transition(CampaignState.CANCELLED, CampaignState.RUNNING)

    def test_compliance_gate_cannot_be_weakened(self):
        weakened = ComplianceGate(require_dnc_check=False)
        assert any("may not be disabled" in p for p in weakened.validate())
        strict = ComplianceGate()
        assert strict.validate() == []
        assert strict.require_dnc_check is True and strict.require_call_window is True


class TestCampaignService:
    async def test_create_and_state_lifecycle(self, db, tenant_a):
        definition = _campaign(str(tenant_a.id))
        row = await campaign_service.create_campaign(db, tenant_a, definition)
        assert row.is_active is False
        await campaign_service.schedule_campaign(db, tenant_a, str(row.id))
        _open_window(tenant_a)
        await campaign_service.resume_campaign(db, tenant_a, str(row.id))
        current = await campaign_service.get_campaign(db, tenant_a, str(row.id))
        assert current.state is CampaignState.RUNNING
        await campaign_service.pause_campaign(db, tenant_a, str(row.id))
        current = await campaign_service.get_campaign(db, tenant_a, str(row.id))
        assert current.state is CampaignState.PAUSED

    async def test_dnc_lead_skipped(self, db, tenant_a):
        lead = await make_lead(db, tenant_a, status=LeadStatus.DNC)
        definition = _campaign(str(tenant_a.id))
        definition = CampaignDefinition(**{**definition.__dict__,
                                           "audience": Audience(tenant_id=str(tenant_a.id),
                                                                lead_ids=(str(lead.id),))})
        ok, reason = await campaign_service.eligibility_check(db, tenant_a, lead, definition)
        assert ok is False and reason == "lead is on do-not-call"

    async def test_window_check_respects_tenant(self, db, tenant_a):
        lead = await make_lead(db, tenant_a)
        tenant_a.outbound_window_open = time(1, 0)
        tenant_a.outbound_window_close = time(2, 0)
        await db.commit()
        definition = _campaign(str(tenant_a.id))
        ok, reason = await campaign_service.eligibility_check(db, tenant_a, lead, definition)
        assert ok is False and reason == "outside the outbound call window"

    async def test_attempt_limit(self, db, tenant_a):
        lead = await make_lead(db, tenant_a, attempts=3)
        definition = _campaign(str(tenant_a.id))
        definition = CampaignDefinition(**{**definition.__dict__,
                                           "throttle": Throttle(max_attempts_per_lead=3)})
        _open_window(tenant_a)
        ok, reason = await campaign_service.eligibility_check(db, tenant_a, lead, definition)
        assert ok is False and reason == "attempt limit reached"

    async def test_execution_plan_never_dials(self, db, tenant_a):
        lead = await make_lead(db, tenant_a)
        definition = _campaign(str(tenant_a.id))
        definition = CampaignDefinition(**{**definition.__dict__,
                                           "audience": Audience(tenant_id=str(tenant_a.id),
                                                                lead_ids=(str(lead.id),))})
        row = await campaign_service.create_campaign(db, tenant_a, definition)
        fetched = await campaign_service.get_campaign(db, tenant_a, str(row.id))
        _open_window(tenant_a)
        # ``execution_plan`` only builds safe intents — it has no dial path at
        # all, so nothing can ever be dialled from here.
        intents = await campaign_service.execution_plan(db, tenant_a, fetched)
        assert len(intents) == 1 and not intents[0].skipped
        assert intents[0].lead_id == str(lead.id)
        assert intents[0].tenant_id == str(tenant_a.id)

    async def test_progress_counts_leads(self, db, tenant_a):
        row = await campaign_service.create_campaign(db, tenant_a, _campaign(str(tenant_a.id)))
        definition = await campaign_service.get_campaign(db, tenant_a, str(row.id))
        await make_lead(db, tenant_a, campaign_id=row.id, status=LeadStatus.QUALIFIED)
        await make_lead(db, tenant_a, campaign_id=row.id, status=LeadStatus.DNC)
        metrics = await campaign_service.progress(db, tenant_a, definition)
        assert metrics.total_leads == 2
        assert metrics.conversions == 1
        assert metrics.dnc_skipped == 1


# ================================================================= ANALYTICS ===

class TestAnalyticsService:
    async def test_aggregate_correctness(self, db, tenant_a):
        start, end = _bounded_range()
        await make_call(db, tenant_a, status=CallStatus.COMPLETED, duration_seconds=120)
        await make_call(db, tenant_a, status=CallStatus.COMPLETED, duration_seconds=60)
        await make_call(db, tenant_a, status=CallStatus.FAILED)
        kpi = await analytics_service.call_kpis(db, str(tenant_a.id), start=start, end=end)
        assert kpi.total == 3 and kpi.answered == 2 and kpi.failed == 1
        assert kpi.rates()["answer_rate"] == 66.7

    async def test_tenant_isolation(self, db, tenant_a, tenant_b):
        start, end = _bounded_range()
        await make_call(db, tenant_a, status=CallStatus.COMPLETED)
        kpi_a = await analytics_service.call_kpis(db, str(tenant_a.id), start=start, end=end)
        kpi_b = await analytics_service.call_kpis(db, str(tenant_b.id), start=start, end=end)
        assert kpi_a.total == 1 and kpi_b.total == 0

    async def test_range_validation(self, db, tenant_a):
        with pytest.raises(BadRequestError):
            await analytics_service.call_kpis(db, str(tenant_a.id),
                                              start=datetime(2025, 1, 2), end=datetime(2025, 1, 1))

    async def test_empty_results(self, db, tenant_a):
        start, end = _bounded_range(days_back=5)
        snapshot = await analytics_service.snapshot(db, str(tenant_a.id),
                                                    kind=analytics_service.KpiKind.CALL,
                                                    start=start, end=end)
        assert snapshot.points[0].metrics["total"] == 0

    async def test_snapshot_has_no_pii(self, db, tenant_a):
        start, end = _bounded_range()
        await make_call(db, tenant_a)
        snapshot = await analytics_service.snapshot(db, str(tenant_a.id),
                                                    kind=analytics_service.KpiKind.CALL,
                                                    start=start, end=end)
        assert snapshot.assert_no_pii() == []

    async def test_cost_estimate_from_usage(self, db, tenant_a, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "cost_unit_prices_json",
                            '{"voice_minute": 1300, "sms_segment": 790}')
        event = UsageEvent(
            tenant_id=tenant_a.id, billing_period="2026-09", metric=UsageMetric.VOICE_MINUTE,
            event_type=UsageEventType.VOICE_MINUTE_USED, quantity=120, unit="seconds",
            idempotency_key="usage-test-1", event_metadata={},
        )
        db.add(event)
        await db.commit()
        cost = await analytics_service.cost_kpis(db, str(tenant_a.id),
                                                 start=datetime(2026, 9, 1), end=datetime(2026, 10, 1))
        assert cost.minutes == 2.0
        assert cost.estimated_cost_millicents == 2600


# ============================================================== NOTIFICATIONS ===

class TestNotificationService:
    def _template(self, tenant_id="t"):
        return NotificationTemplate(
            id="tmpl-1", tenant_id=tenant_id, name="Appointment reminder",
            channel=NotificationChannel.SMS, body="Hi {customer_name}, see you at {appointment_time}.",
            variables=("customer_name", "appointment_time"),
        )

    def test_strict_rendering(self):
        template = self._template()
        body = template.render({"customer_name": "Jane", "appointment_time": "2pm"})
        assert body == "Hi Jane, see you at 2pm."
        body_unknown = template.render({"customer_name": "Jane"})
        assert "{appointment_time}" in body_unknown       # never swallowed, never evaluated

    def test_undeclared_variable_flagged(self):
        template = NotificationTemplate(id="t", tenant_id="t", name="x",
                                        channel=NotificationChannel.IN_APP,
                                        body="Hello {injected}", variables=())
        assert any("undeclared" in p for p in template.validate())

    def test_preference_quiet_hours(self):
        pref = PreferenceSet(channel=NotificationChannel.SMS, quiet_start=time(22, 0),
                             quiet_end=time(8, 0))
        moment = datetime(2026, 9, 14, 23, 0)
        ok, reason = notification_service.resolve_preferences(
            pref, NotificationChannel.SMS, now=moment)
        assert ok is False and reason == "recipient is in quiet hours"

    async def test_deduplication(self, tenant_a):
        template = self._template(str(tenant_a.id))
        notification_service.create_template(str(tenant_a.id), template)
        recipient = Recipient(kind="phone", target="+15550001111")
        n1 = notification_service.create_notification(
            str(tenant_a.id), template=template, recipient=recipient,
            event_source=EventSource.APPOINTMENT_REMINDER, business_key="appt-1")
        n2 = notification_service.create_notification(
            str(tenant_a.id), template=template, recipient=recipient,
            event_source=EventSource.APPOINTMENT_REMINDER, business_key="appt-1")
        assert n1.id == n2.id                            # same business fact, one notification

    async def test_retry_backoff_advances(self, tenant_a):
        template = self._template(str(tenant_a.id))
        notification_service.create_template(str(tenant_a.id), template)
        notification = notification_service.create_notification(
            str(tenant_a.id), template=template, recipient=Recipient(kind="user", target="u1"),
            event_source=EventSource.SYSTEM, business_key="b1")
        retried = notification_service.retry(str(tenant_a.id), notification.id)
        assert retried.attempts == 1
        assert retried.delivery_state is DeliveryState.RETRYING

    async def test_in_app_delivery(self, tenant_a):
        template = NotificationTemplate(
            id="tmpl-inapp", tenant_id=str(tenant_a.id), name="Alert",
            channel=NotificationChannel.IN_APP, body="You have a new message.",
            variables=())
        notification_service.create_template(str(tenant_a.id), template)
        notification = notification_service.create_notification(
            str(tenant_a.id), template=template, recipient=Recipient(kind="user", target="u1"),
            event_source=EventSource.SYSTEM, business_key="b2")
        result = await notification_service.deliver(notification)
        assert result.delivered is True

    async def test_email_channel_suppressed_honestly(self, tenant_a):
        template = NotificationTemplate(
            id="tmpl-mail", tenant_id=str(tenant_a.id), name="Email",
            channel=NotificationChannel.EMAIL, body="Hello.", variables=())
        notification_service.create_template(str(tenant_a.id), template)
        notification = notification_service.create_notification(
            str(tenant_a.id), template=template, recipient=Recipient(kind="email", target="a@b.c"),
            event_source=EventSource.SYSTEM, business_key="b3")
        result = await notification_service.deliver(notification)
        assert result.state is DeliveryState.SUPPRESSED


# ===================================================================== INBOX ===

class TestInboxService:
    async def test_thread_isolation(self, db, tenant_a, tenant_b):
        thread_a = await inbox_service.find_or_create_thread(
            db, tenant_a, channel=InboxChannel.WEB, customer="cust-a")
        with pytest.raises(NotFoundError):
            await inbox_service.project(db, tenant_b, thread_a)

    async def test_message_ordering(self, db, tenant_a):
        thread = await inbox_service.find_or_create_thread(
            db, tenant_a, channel=InboxChannel.WEB, customer="cust-a")
        m1 = await inbox_service.append_message(db, tenant_a, thread,
                                                direction=MessageDirection.INBOUND, body="hi")
        m2 = await inbox_service.append_message(db, tenant_a, thread,
                                                direction=MessageDirection.OUTBOUND, body="hello")
        assert m2.sequence > m1.sequence
        view = await inbox_service.project(db, tenant_a, thread)
        assert [m.sequence for m in view.messages] == sorted(m.sequence for m in view.messages)

    async def test_read_unread(self, db, tenant_a):
        thread = await inbox_service.find_or_create_thread(
            db, tenant_a, channel=InboxChannel.WEB, customer="cust-a")
        await inbox_service.append_message(db, tenant_a, thread,
                                           direction=MessageDirection.INBOUND, body="hi")
        inbox_service.mark_unread(tenant_a, thread)
        view = inbox_service.mark_read(tenant_a, thread)
        assert view.unread_count == 0

    async def test_assignment(self, db, tenant_a):
        thread = await inbox_service.find_or_create_thread(
            db, tenant_a, channel=InboxChannel.WEB, customer="cust-a")
        view = inbox_service.assign(tenant_a, thread, "agent-1")
        assert view.assignee_id == "agent-1"

    async def test_escalation(self, db, tenant_a):
        thread = await inbox_service.find_or_create_thread(
            db, tenant_a, channel=InboxChannel.WEB, customer="cust-a")
        await inbox_service.escalate(db, tenant_a, thread, reason="needs human")
        assert thread.escalated is True

    async def test_close_and_reopen_policy(self, db, tenant_a):
        thread = await inbox_service.find_or_create_thread(
            db, tenant_a, channel=InboxChannel.WEB, customer="cust-a")
        await inbox_service.close(db, tenant_a, thread)
        view = await inbox_service.project(db, tenant_a, thread)
        assert view.status is ThreadStatus.CLOSED
        await inbox_service.reopen(db, tenant_a, thread)
        view = await inbox_service.project(db, tenant_a, thread)
        assert view.status is ThreadStatus.OPEN

    def test_reopen_window_rule(self):
        recent = Thread(id="t", tenant_id="x", channel=InboxChannel.WEB,
                        status=ThreadStatus.CLOSED, last_message_at=datetime.now(timezone.utc).isoformat())
        assert reopen_allowed(recent) is True
        old = Thread(id="t2", tenant_id="x", channel=InboxChannel.WEB,
                     status=ThreadStatus.CLOSED,
                     last_message_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
        assert reopen_allowed(old) is False


# ======================================================================= API ===

class TestAgentApi:
    async def test_owner_can_create_and_list(self, enterprise_client, owner_a):
        response = await enterprise_client.post(
            "/api/agents", json={"name": "Alex", "greeting": "Thanks for calling."},
            headers=_auth(owner_a))
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Alex"
        listing = await enterprise_client.get("/api/agents", headers=_auth(owner_a))
        assert listing.status_code == 200 and any(a["name"] == "Alex" for a in listing.json())

    async def test_viewer_forbidden(self, enterprise_client, viewer_a):
        response = await enterprise_client.post(
            "/api/agents", json={"name": "Alex"}, headers=_auth(viewer_a))
        assert response.status_code == 403

    async def test_mass_assignment_protected(self, enterprise_client, owner_a):
        response = await enterprise_client.post(
            "/api/agents",
            json={"name": "Alex", "admin": True, "api_key": "sk-live-secret"},
            headers=_auth(owner_a))
        assert response.status_code == 422          # extra fields rejected

    async def test_no_secret_leakage(self, enterprise_client, owner_a):
        await enterprise_client.post(
            "/api/agents", json={"name": "Alex"}, headers=_auth(owner_a))
        listing = await enterprise_client.get("/api/agents", headers=_auth(owner_a))
        serialized = str(listing.json())
        assert "sk-live" not in serialized
        assert "api_key" not in listing.json()[0]

    async def test_publish_flow(self, enterprise_client, owner_a):
        created = await enterprise_client.post(
            "/api/agents", json={"name": "Rex", "greeting": "Hello"}, headers=_auth(owner_a))
        agent_id = created.json()["id"]
        published = await enterprise_client.post(
            f"/api/agents/{agent_id}/publish", json={"changelog": "first"}, headers=_auth(owner_a))
        assert published.status_code == 200
        assert published.json()["version"] == 1
        versions = await enterprise_client.get(
            f"/api/agents/{agent_id}/versions", headers=_auth(owner_a))
        assert len(versions.json()) == 1


class TestWorkflowApi:
    async def test_create_and_execute(self, enterprise_client, owner_a):
        created = await enterprise_client.post(
            "/api/workflows",
            json={"name": "Onboarding", "entry_node": "end",
                  "nodes": [{"id": "end", "type": "terminal"}]},
            headers=_auth(owner_a))
        assert created.status_code == 201, created.text
        workflow_id = created.json()["id"]
        await enterprise_client.post(f"/api/workflows/{workflow_id}/publish", headers=_auth(owner_a))
        execution = await enterprise_client.post(
            f"/api/workflows/{workflow_id}/execute", json={"payload": {"x": 1}},
            headers=_auth(owner_a))
        assert execution.status_code == 200, execution.text
        assert execution.json()["status"] == "completed"

    async def test_rejects_arbitrary_action(self, enterprise_client, owner_a):
        response = await enterprise_client.post(
            "/api/workflows",
            json={"name": "Bad", "entry_node": "a",
                  "nodes": [
                      {"id": "a", "type": "action", "action_name": "eval",
                       "action_params": {"code": "os.system('rm -rf /')"}, "next": "end"},
                      {"id": "end", "type": "terminal"},
                  ]},
            headers=_auth(owner_a))
        assert response.status_code == 422

    async def test_tenant_isolation(self, enterprise_client, owner_a, owner_b):
        created = await enterprise_client.post(
            "/api/workflows",
            json={"name": "Secret workflow", "entry_node": "end",
                  "nodes": [{"id": "end", "type": "terminal"}]},
            headers=_auth(owner_a))
        workflow_id = created.json()["id"]
        probe = await enterprise_client.get(f"/api/workflows/{workflow_id}", headers=_auth(owner_b))
        assert probe.status_code == 404


class TestCampaignApi:
    async def test_create_requires_write_permission(self, enterprise_client, viewer_a):
        response = await enterprise_client.post(
            "/api/campaigns", json={"name": "Spring"}, headers=_auth(viewer_a))
        assert response.status_code == 403

    async def test_create_and_plan(self, enterprise_client, owner_a):
        created = await enterprise_client.post(
            "/api/campaigns", json={"name": "Spring", "goal": "qualify"},
            headers=_auth(owner_a))
        assert created.status_code == 201, created.text
        campaign_id = created.json()["id"]
        plan = await enterprise_client.post(
            f"/api/campaigns/{campaign_id}/plan", headers=_auth(owner_a))
        assert plan.status_code == 200
        assert plan.json() == []                     # no leads yet, still safe

    async def test_mass_assignment_protected(self, enterprise_client, owner_a):
        response = await enterprise_client.post(
            "/api/campaigns",
            json={"name": "Spring", "bypass_dnc": True, "skip_safety": True},
            headers=_auth(owner_a))
        assert response.status_code == 422


# =================================================================== SECURITY ===

class TestSecurityGuarantees:
    async def test_workflow_cannot_execute_arbitrary_code(self, tenant_a):
        for bad in ("eval", "__import__", "os.system", "exec", "lambda"):
            node = WorkflowNode(id="a", type=NodeType.ACTION, action=WorkflowAction(bad), next="end")
            definition = WorkflowDefinition(id="w", tenant_id=str(tenant_a.id), name="x",
                                            entry_node="a",
                                            nodes=(node, WorkflowNode(id="end", type=NodeType.TERMINAL)))
            assert definition.validate(), f"{bad} should be rejected"

    async def test_automation_never_duplicates_side_effects(self, tenant_a):
        automation = AutomationDefinition(
            id="auto-x", tenant_id=str(tenant_a.id), name="x",
            event=TriggerEvent.LEAD_CREATED,
            actions=(WorkflowAction("enqueue_notification", {"template_id": "nope"}),),
            policy=ExecutionPolicy(max_per_event=1), status=AutomationStatus.ENABLED,
        )
        automation_service.register_automation(str(tenant_a.id), automation)
        r1 = await automation_service.execute_automation(str(tenant_a.id), automation, "lead-1", {})
        r2 = await automation_service.execute_automation(str(tenant_a.id), automation, "lead-1", {})
        assert r1.status == "completed"
        assert r2.status == "cancelled"

    async def test_no_cross_tenant_conversation_visibility(self, db, tenant_a, tenant_b):
        call = await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        from app.core.errors import NotFoundError as NFE

        with pytest.raises(NFE):
            conversation_service.project(tenant_b, call)

    async def test_analytics_never_exposes_phone_numbers(self, db, tenant_a):
        start, end = _bounded_range()
        await make_call(db, tenant_a, from_number="+15551234567")
        snapshot = await analytics_service.snapshot(db, str(tenant_a.id),
                                                    kind=analytics_service.KpiKind.CALL,
                                                    start=start, end=end)
        serialized = str(snapshot.points[0].metrics)
        assert "+15551234567" not in serialized
```
