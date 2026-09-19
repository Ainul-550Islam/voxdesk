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
