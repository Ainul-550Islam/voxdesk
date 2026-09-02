"""
CRM integration configuration API.

    GET    /api/integrations/crm                     list this tenant's integrations
    GET    /api/integrations/crm/providers           catalogue + capabilities
    GET    /api/integrations/crm/{provider}          one integration
    PUT    /api/integrations/crm/{provider}          connect or update
    DELETE /api/integrations/crm/{provider}          delete outright
    POST   /api/integrations/crm/{provider}/test     health check
    POST   /api/integrations/crm/{provider}/disconnect  keep config, drop creds
    GET    /api/integrations/crm/syncs               sync status feed

Two rules run through the whole file.

**The tenant is never a parameter.** It comes from `ctx.tenant_id`, which
comes from the verified JWT. There is no path that reads a tenant id from a
body, a query string or a header — requirement 6 asks for that explicitly, and
a test asserts a body containing `tenant_id` changes nothing.

**Responses are allowlists.** `IntegrationOut` names every field that may
reach a client. `credentials_encrypted` is not among them, and neither is
anything derived from it. A field reaches a dashboard because someone wrote it
out here, not because it happened to be on the row.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import TenantContext, record_audit, require_permission
from app.auth.permissions import Permission
from app.core.logging import log
from app.db.models import (
    AuditAction,
    CrmEvent,
    CrmIntegration,
    CrmProviderType,
    CrmSync,
    CrmSyncStatus,
)
from app.db.session import get_session
from app.integrations.crm import crypto, service
from app.integrations.crm.mapping import MappingError, validate_field_mappings
from app.integrations.crm.providers.webhook import generate_signing_secret
from app.integrations.crm.registry import capabilities_of

router = APIRouter(prefix="/api/integrations/crm", tags=["integrations"])


# ----------------------------------------------------------------- schemas ---

#: Credential fields each provider accepts. An allowlist, so a tenant cannot
#: stuff arbitrary keys into the encrypted blob and so the API can tell them
#: exactly what is expected.
CREDENTIAL_FIELDS: dict[CrmProviderType, tuple[str, ...]] = {
    CrmProviderType.GOHIGHLEVEL: ("access_token",),
    CrmProviderType.HUBSPOT: ("access_token",),
    CrmProviderType.JOBBER: ("access_token", "refresh_token"),
    CrmProviderType.WEBHOOK: ("signing_secret",),
}

#: Non-secret settings each provider accepts. Everything here is returned by
#: the API, so nothing credential-shaped may be added to these tuples.
CONFIG_FIELDS: dict[CrmProviderType, tuple[str, ...]] = {
    CrmProviderType.GOHIGHLEVEL: ("location_id", "calendar_id", "base_url"),
    CrmProviderType.HUBSPOT: ("base_url",),
    CrmProviderType.JOBBER: ("api_version", "base_url"),
    CrmProviderType.WEBHOOK: ("url",),
}


class IntegrationOut(BaseModel):
    """
    The tenant-visible view.

    Requirement 15 lists what a dashboard may see and what it may not. The
    absent fields are the point of this class: no access token, no refresh
    token, no client secret, no encryption key, no ciphertext, and no key id
    beyond the boolean below.
    """

    provider: str
    is_enabled: bool
    connected: bool = Field(
        description="whether credentials are stored -- not whether they work"
    )
    capabilities: list[str] = Field(default_factory=list)
    config: dict = Field(default_factory=dict)
    field_mappings: dict = Field(default_factory=dict)
    subscribed_events: list[str] = Field(default_factory=list)
    share_transcripts: bool = False
    #: When credentials were last written. Named `connected_at` rather than
    #: `credentials_updated_at` on purpose: a response field whose name starts
    #: with "credentials" invites someone to add a sibling that holds the
    #: actual credentials. There is a test asserting no field in this model is
    #: named like a secret.
    connected_at: str | None = None
    last_health_check_at: str | None = None
    last_health_ok: bool | None = None
    last_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class IntegrationListOut(BaseModel):
    integrations: list[IntegrationOut]


class ProviderInfo(BaseModel):
    provider: str
    capabilities: list[str]
    credential_fields: list[str]
    config_fields: list[str]


class ProviderCatalogueOut(BaseModel):
    providers: list[ProviderInfo]


class IntegrationIn(BaseModel):
    """
    Connect or update payload.

    `credentials` is write-only: it goes in, it is encrypted, and no response
    model can echo it back. Omitting it on an update leaves the stored
    credentials untouched, so a tenant editing their location id does not have
    to re-paste a token.
    """

    is_enabled: bool = True
    credentials: dict[str, str] | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    field_mappings: dict[str, str] = Field(default_factory=dict)
    subscribed_events: list[str] = Field(default_factory=list)
    share_transcripts: bool = False

    @field_validator("credentials")
    @classmethod
    def _reject_empty_values(cls, value):
        if value is None:
            return None
        for key, item in value.items():
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"credential {key!r} must be a non-empty string")
        return value


class HealthOut(BaseModel):
    """Requirement 24's normalized shape."""

    connected: bool
    provider: str
    latency_ms: float
    safe_message: str


class SyncOut(BaseModel):
    id: uuid.UUID
    provider: str
    entity_type: str
    entity_id: uuid.UUID
    event_type: str | None = None
    status: str
    external_id: str | None = None
    attempt_count: int
    last_attempt_at: str | None = None
    next_attempt_at: str | None = None
    synced_at: str | None = None
    last_error: str | None = None
    last_error_code: str | None = None


class SyncListOut(BaseModel):
    syncs: list[SyncOut]
    total: int


# ------------------------------------------------------------- serializers ---

def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def to_out(integration: CrmIntegration) -> IntegrationOut:
    return IntegrationOut(
        provider=integration.provider.value,
        is_enabled=integration.is_enabled,
        connected=bool(integration.credentials_encrypted),
        capabilities=sorted(c.value for c in capabilities_of(integration.provider)),
        config=dict(integration.config or {}),
        field_mappings=dict(integration.field_mappings or {}),
        subscribed_events=list(integration.subscribed_events or []),
        share_transcripts=bool(integration.share_transcripts),
        connected_at=_iso(integration.credentials_updated_at),
        last_health_check_at=_iso(integration.last_health_check_at),
        last_health_ok=integration.last_health_ok,
        last_error=integration.last_error,
        created_at=_iso(integration.created_at),
        updated_at=_iso(integration.updated_at),
    )


def _parse_provider(provider: str) -> CrmProviderType:
    try:
        return CrmProviderType(provider.lower())
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown provider. Supported: "
                f"{', '.join(p.value for p in CrmProviderType)}"
            ),
        )


async def _owned(
    session: AsyncSession, ctx: TenantContext, provider: CrmProviderType
) -> CrmIntegration:
    """
    Fetch this tenant's integration, or 404.

    404 rather than 403 on someone else's row, matching `get_owned()` from
    STEP 2: a different status code would confirm that another tenant has that
    provider connected.
    """
    integration = await service.get_integration(
        session, tenant_id=ctx.tenant_id, provider=provider
    )
    if integration is None:
        raise HTTPException(status_code=404, detail="Integration is not configured")
    return integration


# ------------------------------------------------------------------ routes ---

@router.get("/providers", response_model=ProviderCatalogueOut)
async def list_providers(
    ctx: TenantContext = Depends(require_permission(Permission.INTEGRATION_READ)),
) -> ProviderCatalogueOut:
    """
    What can be connected, and what each one can do.

    Serving capabilities means a dashboard can grey out "sync appointments"
    for Jobber instead of offering it and producing a permanent failure.
    """
    return ProviderCatalogueOut(providers=[
        ProviderInfo(
            provider=provider.value,
            capabilities=sorted(c.value for c in capabilities_of(provider)),
            credential_fields=list(CREDENTIAL_FIELDS[provider]),
            config_fields=list(CONFIG_FIELDS[provider]),
        )
        for provider in CrmProviderType
    ])


@router.get("", response_model=IntegrationListOut)
@router.get("/", response_model=IntegrationListOut)
async def list_integrations(
    ctx: TenantContext = Depends(require_permission(Permission.INTEGRATION_READ)),
    session: AsyncSession = Depends(get_session),
) -> IntegrationListOut:
    rows = (
        (
            await session.execute(
                select(CrmIntegration)
                .where(CrmIntegration.tenant_id == ctx.tenant_id)
                .order_by(CrmIntegration.provider)
            )
        )
        .scalars()
        .all()
    )
    return IntegrationListOut(integrations=[to_out(row) for row in rows])


@router.get("/syncs", response_model=SyncListOut)
async def list_syncs(
    status: str | None = Query(None, description="filter by sync status"),
    provider: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    ctx: TenantContext = Depends(require_permission(Permission.INTEGRATION_READ)),
    session: AsyncSession = Depends(get_session),
) -> SyncListOut:
    """
    Sync status feed (requirement 15).

    `last_error` is included because an operator needs to know *why* a sync
    failed, and it is safe to include because `errors.safe_message` scrubbed
    it before it was ever written.
    """
    query = select(CrmSync).where(CrmSync.tenant_id == ctx.tenant_id)
    if status:
        try:
            query = query.where(CrmSync.status == CrmSyncStatus(status.lower()))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown status {status!r}")
    if provider:
        query = query.where(CrmSync.provider == _parse_provider(provider))

    rows = (
        (await session.execute(query.order_by(CrmSync.created_at.desc()).limit(limit)))
        .scalars()
        .all()
    )

    # One extra query rather than a join, to attach the event type without
    # widening the sync model. The id list is already tenant-scoped.
    event_types: dict[uuid.UUID, str] = {}
    if rows:
        events = (
            (
                await session.execute(
                    select(CrmEvent).where(
                        CrmEvent.tenant_id == ctx.tenant_id,
                        CrmEvent.id.in_([r.event_id for r in rows]),
                    )
                )
            )
            .scalars()
            .all()
        )
        event_types = {e.id: e.event_type.value for e in events}

    return SyncListOut(
        total=len(rows),
        syncs=[
            SyncOut(
                id=row.id, provider=row.provider.value,
                entity_type=row.entity_type.value, entity_id=row.entity_id,
                event_type=event_types.get(row.event_id),
                status=row.status.value, external_id=row.external_id,
                attempt_count=row.attempt_count,
                last_attempt_at=_iso(row.last_attempt_at),
                next_attempt_at=_iso(row.next_attempt_at),
                synced_at=_iso(row.synced_at),
                last_error=row.last_error, last_error_code=row.last_error_code,
            )
            for row in rows
        ],
    )


@router.get("/{provider}", response_model=IntegrationOut)
async def get_integration_route(
    provider: str,
    ctx: TenantContext = Depends(require_permission(Permission.INTEGRATION_READ)),
    session: AsyncSession = Depends(get_session),
) -> IntegrationOut:
    return to_out(await _owned(session, ctx, _parse_provider(provider)))


@router.put("/{provider}", response_model=IntegrationOut)
async def upsert_integration(
    provider: str,
    body: IntegrationIn,
    ctx: TenantContext = Depends(require_permission(Permission.INTEGRATION_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> IntegrationOut:
    """
    Connect a provider, or update an existing connection.

    Credentials are encrypted before the row is written. There is no code path
    that stores them in any other form: if encryption is not configured, this
    returns 503 rather than falling back to plaintext.
    """
    provider_type = _parse_provider(provider)

    config = _validated_config(provider_type, body.config)
    mappings = _validated_mappings(body.field_mappings)
    events = _validated_events(body.subscribed_events)

    integration = await service.get_integration(
        session, tenant_id=ctx.tenant_id, provider=provider_type
    )
    creating = integration is None
    if creating:
        integration = CrmIntegration(
            tenant_id=ctx.tenant_id, provider=provider_type, config={}
        )
        session.add(integration)

    integration.is_enabled = body.is_enabled
    integration.config = config
    integration.field_mappings = mappings
    integration.subscribed_events = events
    integration.share_transcripts = body.share_transcripts
    integration.updated_at = datetime.utcnow()

    credentials = _credentials_for(provider_type, body, creating)
    if credentials is not None:
        _store_credentials(integration, ctx, provider_type, credentials)

    await session.commit()
    await session.refresh(integration)

    await record_audit(
        session,
        action=(
            AuditAction.INTEGRATION_CONNECTED if creating
            else AuditAction.INTEGRATION_UPDATED
        ),
        tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        # Provider and the *names* of the fields supplied -- never a value.
        # Requirement 22 asks for a test that no secret reaches an audit row.
        detail={
            "provider": provider_type.value,
            "credential_fields": sorted(credentials or {}),
            "config_keys": sorted(config),
            "enabled": body.is_enabled,
        },
    )
    await session.commit()

    log.info(
        "crm.integration_saved", tenant_id=str(ctx.tenant_id),
        provider=provider_type.value, outcome="created" if creating else "updated",
    )
    return to_out(integration)


def _credentials_for(
    provider_type: CrmProviderType, body: IntegrationIn, creating: bool
) -> dict[str, str] | None:
    """
    Decide what credential bundle to store, if any.

    `None` means "leave what is already there" — an update that only changes
    a location id must not wipe the token.
    """
    supplied = body.credentials
    if supplied is not None:
        allowed = set(CREDENTIAL_FIELDS[provider_type])
        unknown = sorted(set(supplied) - allowed)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Unknown credential field(s) for {provider_type.value}: "
                    f"{', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}"
                ),
            )
        return dict(supplied)

    if creating and provider_type is CrmProviderType.WEBHOOK:
        # A webhook's "credential" is a signing secret that we generate rather
        # than the tenant supplying it. Generating it here means an unsigned
        # webhook integration cannot exist, which is what makes the receiver's
        # signature check meaningful.
        return {"signing_secret": generate_signing_secret()}

    return None


def _store_credentials(
    integration: CrmIntegration, ctx: TenantContext,
    provider_type: CrmProviderType, credentials: dict[str, str],
) -> None:
    key_ring = crypto.key_ring_from_settings()
    if key_ring is None:
        # Never silently degrade to plaintext.
        raise HTTPException(
            status_code=503,
            detail=(
                "CRM credential encryption is not configured on this instance. "
                "Set CRM_ENCRYPTION_KEYS before connecting a provider."
            ),
        )
    try:
        envelope, key_id = crypto.encrypt_credentials(
            credentials, tenant_id=str(ctx.tenant_id),
            provider=provider_type.value, key_ring=key_ring,
        )
    except crypto.CredentialCryptoError as exc:
        raise HTTPException(status_code=503, detail=f"Cannot store credentials: {exc}")

    integration.credentials_encrypted = envelope
    integration.credentials_key_id = key_id
    integration.credentials_updated_at = datetime.utcnow()


def _validated_config(
    provider_type: CrmProviderType, config: dict[str, Any]
) -> dict[str, Any]:
    allowed = set(CONFIG_FIELDS[provider_type])
    unknown = sorted(set(config or {}) - allowed)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown config field(s) for {provider_type.value}: "
                f"{', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}"
            ),
        )
    cleaned = {k: v for k, v in (config or {}).items() if v not in (None, "")}

    if provider_type is CrmProviderType.WEBHOOK:
        url = cleaned.get("url", "")
        if not url:
            raise HTTPException(
                status_code=422, detail="A webhook integration requires config.url"
            )
        if not str(url).startswith("https://"):
            raise HTTPException(
                status_code=422,
                detail=(
                    "config.url must be https -- webhook payloads carry customer "
                    "phone numbers and call summaries"
                ),
            )
    return cleaned


def _validated_mappings(mappings: dict[str, str]) -> dict[str, str]:
    try:
        return validate_field_mappings(mappings)
    except MappingError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _validated_events(events: list[str]) -> list[str]:
    from app.db.models import CrmEventType

    known = {e.value for e in CrmEventType}
    unknown = sorted(set(events or []) - known)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown event type(s): {', '.join(unknown)}. "
                f"Known: {', '.join(sorted(known))}"
            ),
        )
    return list(events or [])


@router.post("/{provider}/test", response_model=HealthOut)
async def test_integration(
    provider: str,
    ctx: TenantContext = Depends(require_permission(Permission.INTEGRATION_READ)),
    session: AsyncSession = Depends(get_session),
) -> HealthOut:
    """
    Live connection test.

    Returns 200 with `connected=false` on a bad credential rather than an
    error status: this is a diagnostic, and the answer "your token is
    rejected" is a successful diagnosis.
    """
    provider_type = _parse_provider(provider)
    integration = await _owned(session, ctx, provider_type)
    result = await service.check_health(session, integration)

    await record_audit(
        session, action=AuditAction.INTEGRATION_TESTED, tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id, actor_email=ctx.user.email,
        detail={"provider": provider_type.value, "connected": result.connected},
    )
    await session.commit()

    return HealthOut(
        connected=result.connected, provider=result.provider,
        latency_ms=result.latency_ms, safe_message=result.safe_message,
    )


@router.post("/{provider}/disconnect", response_model=IntegrationOut)
async def disconnect_integration(
    provider: str,
    ctx: TenantContext = Depends(require_permission(Permission.INTEGRATION_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> IntegrationOut:
    """
    Drop the credentials, keep the configuration.

    Distinct from DELETE on purpose: a tenant rotating a token, or pausing an
    integration during a CRM migration, should not have to re-enter their
    location id and field mappings afterwards.
    """
    provider_type = _parse_provider(provider)
    integration = await _owned(session, ctx, provider_type)

    integration.credentials_encrypted = None
    integration.credentials_key_id = None
    integration.credentials_updated_at = None
    integration.is_enabled = False
    integration.last_health_ok = None
    integration.last_error = None
    await session.commit()
    await session.refresh(integration)

    await record_audit(
        session, action=AuditAction.INTEGRATION_DISCONNECTED,
        tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        detail={"provider": provider_type.value, "kept_config": True},
    )
    await session.commit()
    return to_out(integration)


@router.delete("/{provider}", status_code=204)
async def delete_integration(
    provider: str,
    ctx: TenantContext = Depends(require_permission(Permission.INTEGRATION_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    """
    Remove the integration entirely.

    Sync history is left in place. Those rows are the record of what was sent
    to a customer's CRM, and deleting them because someone unplugged the
    connector would destroy the audit trail exactly when it matters.
    """
    provider_type = _parse_provider(provider)
    integration = await _owned(session, ctx, provider_type)

    await session.delete(integration)
    await session.commit()

    await record_audit(
        session, action=AuditAction.INTEGRATION_DISCONNECTED,
        tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id,
        actor_email=ctx.user.email,
        detail={"provider": provider_type.value, "deleted": True},
    )
    await session.commit()
    return None
