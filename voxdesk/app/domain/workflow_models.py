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
