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
