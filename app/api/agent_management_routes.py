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
