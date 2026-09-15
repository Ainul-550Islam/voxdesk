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
