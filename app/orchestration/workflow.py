"""Deterministic workflow execution engine (Phase 4, orchestration slice).

Mirrors the vocabulary of ``app/domain/workflow_models.py`` — the same
controlled-action allowlist, the same node vocabulary, the same validation
rules — and adds the one thing that layer deliberately does not do: a pure
**executor** that walks the graph from the entry node to a terminal node and
records every step.

Two guarantees from the domain layer are re-encoded here verbatim:

* **No arbitrary code.** An action is a name from ``CONTROLLED_ACTIONS`` plus
  validated parameters; there is no ``exec``/``eval``/``import``/``shell`` and
  no user code path. Anything code-shaped is rejected by ``validate``.
* **Deterministic identity + exactly-once runs.** ``Workflow.identity`` is a
  content hash, and ``WorkflowEngine.run`` memoises completed runs by
  ``(tenant_id, idempotency_key)`` so a replayed trigger can never run a
  workflow twice.

The engine never sleeps, never dials and never touches the database: ``DELAY``
and ``TIMEOUT`` nodes are recorded as scheduled steps, and action handlers are
injected callables, so the whole run is pure and replayable.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.orchestration.conditions import CONDITION_OPERATORS as _OPERATORS, evaluate

# ------------------------------------------------------- action vocabulary ---

CONTROLLED_ACTIONS = frozenset({
    "update_lead_status",
    "add_conversation_tag",
    "enqueue_notification",
    "record_escalation_intent",
    "create_followup_intent",
    "mark_resolved",
    "apply_dnc",
})

FORBIDDEN_ACTION_MARKERS = (
    "__", "exec", "eval", "import", "system", "shell", "subprocess",
    "lambda", "compile", "globals", "locals",
)

# ----------------------------------------------------------- node types -----

TRIGGER = "trigger"
CONDITION = "condition"
ACTION = "action"
DELAY = "delay"
RETRY = "retry"
TIMEOUT = "timeout"
APPROVAL = "approval"
HANDOFF = "handoff"
TERMINAL = "terminal"

NODE_TYPES = frozenset({TRIGGER, CONDITION, ACTION, DELAY, RETRY, TIMEOUT, APPROVAL, HANDOFF, TERMINAL})

# ------------------------------------------------------ execution statuses ---

PENDING = "pending"
RUNNING = "running"
WAITING_APPROVAL = "waiting_approval"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
TIMED_OUT = "timed_out"

TERMINAL_EXECUTION = frozenset({COMPLETED, FAILED, CANCELLED, TIMED_OUT})

#: A run that exceeds this many node visits is assumed to be cycling.
MAX_VISITS = 10_000


class WorkflowError(ValueError):
    """Raised when a workflow or node fails validation, or a run goes wrong."""


def _stable_id(*parts: object) -> str:
    payload = "|".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class Condition:
    field: str
    operator: str
    value: Any = None

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.field or not self.field.strip() or len(self.field) > 64:
            problems.append("condition field must be 1-64 characters")
        if self.operator not in _OPERATORS:
            problems.append(f"condition operator {self.operator!r} is not allowed")
        return problems

    def matches(self, payload: dict[str, Any]) -> bool:
        return evaluate(payload.get(self.field), self.operator, self.value)


@dataclass(frozen=True)
class Action:
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
        for key in self.params:
            if not isinstance(key, str) or not key.strip() or len(key) > 64:
                problems.append("action parameter keys must be 1-64 character strings")
        return problems


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    action: Action | None = None
    condition: Condition | None = None
    branches: tuple[tuple[Condition, str], ...] = ()
    default_next: str = ""
    next: str = ""
    delay_seconds: int = 0
    timeout_seconds: int = 30
    retry_limit: int = 3
    approver_role: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.id or not self.id.strip() or len(self.id) > 64:
            problems.append("node id must be 1-64 characters")
        if self.type not in NODE_TYPES:
            problems.append(f"node {self.id!r} has unknown type {self.type!r}")
        if self.type is ACTION and self.action is None:
            problems.append(f"action node {self.id!r} has no action")
        if self.type is CONDITION and not self.branches and self.condition is None:
            problems.append(f"condition node {self.id!r} has no condition")
        if self.type is APPROVAL and not self.approver_role.strip():
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
class Workflow:
    id: str
    tenant_id: str
    name: str
    entry_node: str = ""
    nodes: tuple[Node, ...] = ()

    def node_map(self) -> dict[str, Node]:
        return {node.id: node for node in self.nodes}

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.tenant_id or not self.tenant_id.strip():
            problems.append("tenant_id is required")
        if not self.id or not self.id.strip():
            problems.append("workflow id is required")
        if not self.name.strip() or len(self.name) > 200:
            problems.append("workflow name must be 1-200 characters")
        node_ids = {node.id for node in self.nodes}
        if not self.nodes:
            problems.append("workflow must have at least one node")
        if self.entry_node and self.entry_node not in node_ids:
            problems.append(f"entry node {self.entry_node!r} does not exist")
        if not any(node.type is TERMINAL for node in self.nodes):
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
        return _stable_id(
            self.tenant_id,
            self.name,
            self.entry_node,
            tuple(
                (n.id, n.type, n.action, n.condition, n.branches,
                 n.default_next, n.next, n.delay_seconds, n.timeout_seconds,
                 n.retry_limit, n.approver_role)
                for n in self.nodes
            ),
        )


@dataclass(frozen=True)
class Step:
    node_id: str
    status: str  # executed | scheduled | failed | unsupported
    detail: str = ""
    attempt: int = 1


@dataclass(frozen=True)
class RunResult:
    workflow_id: str
    tenant_id: str
    status: str
    steps: tuple[Step, ...] = ()
    current_node: str = ""
    error: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_EXECUTION


class WorkflowEngine:
    """Deterministic executor over :class:`Workflow` graphs.

    Action handlers are injected callables ``(name, params) -> None``; the
    engine isolates their failures (retrying up to the node's limit) so a bad
    handler can never take down a run or leak state. Runs are memoised by
    ``(tenant_id, idempotency_key)`` for exactly-once semantics.
    """

    def __init__(self, handlers: Mapping[str, Callable[[str, dict[str, Any]], None]] | None = None):
        self._handlers: dict[str, Callable[[str, dict[str, Any]], None]] = dict(handlers or {})
        self._runs: dict[tuple[str, str], RunResult] = {}

    def register(self, action: str, handler: Callable[[str, dict[str, Any]], None]) -> None:
        if action not in CONTROLLED_ACTIONS:
            raise WorkflowError(f"cannot register handler for uncontrolled action {action!r}")
        self._handlers[action] = handler

    def run(self, workflow: Workflow, payload: dict[str, Any], *, idempotency_key: str = "") -> RunResult:
        """Run a workflow once. With an idempotency key, a replay returns the
        memoised result and never re-executes handlers."""
        problems = workflow.validate()
        if problems:
            raise WorkflowError("; ".join(problems))
        key = (workflow.tenant_id, idempotency_key)
        if idempotency_key and key in self._runs:
            return self._runs[key]
        result = self._execute(workflow, payload)
        if idempotency_key:
            self._runs[key] = result
        return result

    # ------------------------------------------------------------------ run ---

    def _execute(self, workflow: Workflow, payload: dict[str, Any]) -> RunResult:
        node_map = workflow.node_map()
        steps: list[Step] = []
        current = workflow.entry_node
        terminal_reached = False
        visits = 0

        while current:
            visits += 1
            if visits > MAX_VISITS:
                return RunResult(
                    workflow.id, workflow.tenant_id, TIMED_OUT,
                    tuple(steps), current_node=current,
                    error="step limit exceeded (possible cycle)",
                )
            node = node_map[current]

            if node.type is TERMINAL:
                terminal_reached = True
                steps.append(Step(current, "executed", "workflow completed"))
                break

            if node.type is CONDITION:
                target = self._resolve_condition(node, payload)
                if target is None:
                    return RunResult(
                        workflow.id, workflow.tenant_id, FAILED,
                        tuple(steps), current_node=current,
                        error=f"condition node {current!r} matched no branch",
                    )
                steps.append(Step(current, "executed", f"branch -> {target}"))
                current = target
                continue

            if node.type is ACTION:
                ok, detail, attempt = self._execute_action(node)
                if not ok:
                    steps.append(Step(current, "failed", detail, attempt))
                    return RunResult(
                        workflow.id, workflow.tenant_id, FAILED,
                        tuple(steps), current_node=current, error=detail,
                    )
                steps.append(Step(current, "executed", detail, attempt))
                current = node.next or ""
                continue

            if node.type is APPROVAL:
                steps.append(Step(current, "scheduled", f"waiting for {node.approver_role}"))
                return RunResult(
                    workflow.id, workflow.tenant_id, WAITING_APPROVAL,
                    tuple(steps), current_node=current,
                )

            if node.type is DELAY:
                steps.append(Step(current, "scheduled", f"delay {node.delay_seconds}s"))
                current = node.next or ""
                continue

            if node.type is TIMEOUT:
                steps.append(Step(current, "scheduled", f"timeout {node.timeout_seconds}s"))
                current = node.next or ""
                continue

            if node.type is RETRY:
                steps.append(Step(current, "executed", f"retry policy limit {node.retry_limit}"))
                current = node.next or ""
                continue

            if node.type is HANDOFF:
                steps.append(Step(current, "executed", "human handoff intent recorded"))
                current = node.next or ""
                continue

            if node.type is TRIGGER:
                current = node.next or ""
                continue

            return RunResult(
                workflow.id, workflow.tenant_id, FAILED,
                tuple(steps), current_node=current,
                error=f"unknown node type {node.type!r}",
            )

        if not terminal_reached:
            return RunResult(
                workflow.id, workflow.tenant_id, FAILED,
                tuple(steps), current_node=current,
                error="workflow ended without reaching a terminal node",
            )
        return RunResult(workflow.id, workflow.tenant_id, COMPLETED, tuple(steps))

    def _resolve_condition(self, node: Node, payload: dict[str, Any]) -> str | None:
        # Branches take priority: the first matching branch wins.
        for condition, target in node.branches:
            if condition.matches(payload):
                return target
        # A lone condition is an if/else: true -> next, false -> default_next.
        if node.condition is not None:
            if node.condition.matches(payload):
                return node.next or node.default_next
            return node.default_next or node.next
        # No branches, no condition: fall through deterministically.
        return node.default_next or node.next or None

    def _execute_action(self, node: Node) -> tuple[bool, str, int]:
        action = node.action
        handler = self._handlers.get(action.name)
        if handler is None:
            return False, f"no handler for action {action.name!r}", 1
        attempt = 1
        while True:
            try:
                handler(action.name, action.params)
                return True, f"action {action.name} executed", attempt
            except Exception as exc:  # handler isolation: never propagate
                if attempt >= node.retry_limit:
                    return False, f"action {action.name} failed after {attempt} attempt(s): {exc}", attempt
                attempt += 1
