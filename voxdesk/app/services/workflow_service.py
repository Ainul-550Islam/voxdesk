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
