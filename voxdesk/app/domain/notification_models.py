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
