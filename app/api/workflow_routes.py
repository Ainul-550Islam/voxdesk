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
