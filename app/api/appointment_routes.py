"""
Appointment and calendar-integration API.

    GET    /api/appointments/availability          open slots
    GET    /api/appointments                       list (tenant-scoped)
    POST   /api/appointments                       book
    GET    /api/appointments/{id}                  one appointment
    PATCH  /api/appointments/{id}                  reschedule
    POST   /api/appointments/{id}/cancel           cancel
    POST   /api/appointments/{id}/no-show          mark absent

    GET    /api/calendar/providers                 catalogue + capabilities
    GET    /api/calendar/integrations              list connections
    PUT    /api/calendar/integrations/{provider}   connect or update
    DELETE /api/calendar/integrations/{provider}   disconnect
    POST   /api/calendar/integrations/{provider}/test    health check
    GET    /api/calendar/policy                    scheduling rules
    PUT    /api/calendar/policy                    update scheduling rules

Two rules run through the whole file, unchanged from STEP 5 because they were
right there:

**The tenant is never a parameter.** It comes from `ctx.tenant_id`, which comes
from the verified JWT. Requirement 14 asks for that explicitly, and a test
asserts a body containing `tenant_id` changes nothing.

**Responses are allowlists.** `AppointmentOut` and `CalendarIntegrationOut`
name every field that may reach a client. No OAuth token, no refresh token, no
ciphertext, no client secret.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import TenantContext, get_owned, record_audit, require_permission
from app.auth.permissions import Permission
from app.core.logging import log
from app.db.models import (
    Appointment,
    AppointmentStatus,
    AuditAction,
    CalendarIntegration,
    CalendarProviderType,
    SchedulingPolicy,
)
from app.db.session import get_session
from app.integrations.calendar import policy as policy_engine
from app.integrations.calendar import service
from app.integrations.calendar.models import BookingOutcome
from app.integrations.calendar.registry import capabilities_of
from app.integrations.calendar.timezones import (
    AmbiguousTimeError,
    NonexistentTimeError,
    TimezoneError,
    as_utc,
    is_valid_zone,
    now_utc,
    resolve_local,
)

router = APIRouter(prefix="/api/appointments", tags=["appointments"])
calendar_router = APIRouter(prefix="/api/calendar", tags=["calendar"])


#: Credential fields each provider accepts. An allowlist, so a tenant cannot
#: stuff arbitrary keys into the encrypted blob.
CREDENTIAL_FIELDS: dict[CalendarProviderType, tuple[str, ...]] = {
    CalendarProviderType.GOOGLE: (
        "access_token", "refresh_token", "client_id", "client_secret",
    ),
    CalendarProviderType.MICROSOFT: (
        "access_token", "refresh_token", "client_id", "client_secret", "scope",
    ),
    CalendarProviderType.CALCOM: ("api_key",),
    CalendarProviderType.GOOGLE_SERVICE_ACCOUNT: (),
    CalendarProviderType.INTERNAL: (),
}

#: Non-secret settings. Everything here is returned by the API, so nothing
#: credential-shaped may be added.
CONFIG_FIELDS: dict[CalendarProviderType, tuple[str, ...]] = {
    CalendarProviderType.GOOGLE: ("calendar_id", "base_url", "token_url"),
    CalendarProviderType.MICROSOFT: (
        "calendar_id", "mailbox", "schedule_id", "directory_tenant",
        "availability_interval", "base_url", "token_url",
    ),
    CalendarProviderType.CALCOM: (
        "event_type_id", "api_version", "language", "base_url",
    ),
    CalendarProviderType.GOOGLE_SERVICE_ACCOUNT: ("calendar_id",),
    CalendarProviderType.INTERNAL: (),
}


# ----------------------------------------------------------------- schemas ---

class AppointmentOut(BaseModel):
    id: uuid.UUID
    status: str
    customer_name: str
    customer_phone: str
    attendee_email: str | None = None
    reason: str = ""
    starts_at: str
    ends_at: str
    timezone: str
    provider: str | None = None
    #: The provider's event id. Safe: it is meaningless without the tenant's
    #: own credentials, and staff need it to find the event in their calendar.
    external_event_id: str | None = None
    meeting_url: str | None = None
    call_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    cancelled_at: str | None = None
    cancellation_reason: str | None = None
    cancelled_by: str | None = None
    rescheduled_from: str | None = None
    confirmed_at: str | None = None
    #: Already scrubbed by `errors.safe_message` before it was stored.
    last_error: str | None = None
    created_at: str | None = None


class AppointmentListOut(BaseModel):
    appointments: list[AppointmentOut]
    total: int


class SlotOut(BaseModel):
    start: str
    end: str
    spoken: str


class AvailabilityOut(BaseModel):
    date: str
    timezone: str
    slots: list[SlotOut]
    #: Non-null when the provider could not be reached, so a UI can say
    #: "showing our own diary only" instead of implying certainty.
    degraded: str | None = None


class BookIn(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200)
    customer_phone: str = Field(min_length=3, max_length=32)
    #: Local wall-clock in the tenant's timezone, e.g. "2026-09-08T15:00:00".
    #: Deliberately not UTC: a dashboard user picks a time on a clock, and
    #: making the client convert is where offsets get dropped.
    starts_at_local: str
    reason: str = ""
    customer_email: str | None = None
    lead_id: uuid.UUID | None = None
    #: Client-supplied idempotency. Without one the key is derived from
    #: (tenant, phone, start).
    request_id: str | None = None


class RescheduleIn(BaseModel):
    starts_at_local: str
    reason: str = ""


class CancelIn(BaseModel):
    reason: str = ""


class CalendarIntegrationOut(BaseModel):
    provider: str
    is_enabled: bool
    is_primary: bool
    connected: bool = Field(
        description="whether credentials are stored -- not whether they work"
    )
    capabilities: list[str] = Field(default_factory=list)
    config: dict = Field(default_factory=dict)
    #: Named `connected_at`, not `credentials_updated_at`: a response field
    #: whose name starts with "credentials" invites a sibling that holds them.
    connected_at: str | None = None
    token_expires_at: str | None = None
    last_health_check_at: str | None = None
    last_health_ok: bool | None = None
    last_error: str | None = None


class CalendarIntegrationIn(BaseModel):
    is_enabled: bool = True
    is_primary: bool = False
    credentials: dict[str, str] | None = None
    config: dict = Field(default_factory=dict)

    @field_validator("credentials")
    @classmethod
    def _reject_empty(cls, value):
        if value is None:
            return None
        for key, item in value.items():
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"credential {key!r} must be a non-empty string")
        return value


class ProviderInfo(BaseModel):
    provider: str
    capabilities: list[str]
    credential_fields: list[str]
    config_fields: list[str]


class ProviderCatalogueOut(BaseModel):
    providers: list[ProviderInfo]


class PolicyOut(BaseModel):
    timezone: str
    weekly_hours: dict = Field(default_factory=dict)
    holidays: list = Field(default_factory=list)
    blocked_periods: list = Field(default_factory=list)
    slot_minutes: int
    slot_interval_minutes: int
    buffer_before_minutes: int
    buffer_after_minutes: int
    minimum_notice_minutes: int
    booking_horizon_days: int
    max_slots_offered: int
    allow_outside_business_hours: bool
    require_provider_confirmation: bool
    #: False when the tenant has no policy row and the legacy Tenant columns
    #: are supplying the answer.
    configured: bool = True


class PolicyIn(BaseModel):
    weekly_hours: dict | None = None
    holidays: list | None = None
    blocked_periods: list | None = None
    slot_minutes: int = Field(default=30, ge=5, le=480)
    slot_interval_minutes: int = Field(default=30, ge=5, le=480)
    buffer_before_minutes: int = Field(default=0, ge=0, le=240)
    buffer_after_minutes: int = Field(default=0, ge=0, le=240)
    minimum_notice_minutes: int = Field(default=60, ge=0, le=40320)
    booking_horizon_days: int = Field(default=60, ge=1, le=730)
    max_slots_offered: int = Field(default=3, ge=1, le=20)
    allow_outside_business_hours: bool = False
    require_provider_confirmation: bool = True
    #: Changing the tenant's timezone is a scheduling decision, so it lives
    #: here rather than in the generic tenant settings.
    timezone: str | None = None


# ------------------------------------------------------------- serializers ---

def _iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value else None


def to_out(appointment: Appointment) -> AppointmentOut:
    return AppointmentOut(
        id=appointment.id,
        status=appointment.status.value,
        customer_name=appointment.customer_name,
        customer_phone=appointment.customer_phone,
        attendee_email=appointment.attendee_email,
        reason=appointment.reason or "",
        starts_at=_iso(appointment.starts_at),
        ends_at=_iso(appointment.ends_at),
        timezone=appointment.timezone,
        provider=appointment.provider.value if appointment.provider else None,
        external_event_id=appointment.external_event_id,
        meeting_url=appointment.meeting_url,
        call_id=appointment.call_id,
        lead_id=appointment.lead_id,
        cancelled_at=_iso(appointment.cancelled_at),
        cancellation_reason=appointment.cancellation_reason,
        cancelled_by=appointment.cancelled_by,
        rescheduled_from=_iso(appointment.rescheduled_from),
        confirmed_at=_iso(appointment.confirmed_at),
        last_error=appointment.last_error,
        created_at=_iso(appointment.created_at),
    )


def integration_out(integration: CalendarIntegration) -> CalendarIntegrationOut:
    return CalendarIntegrationOut(
        provider=integration.provider.value,
        is_enabled=integration.is_enabled,
        is_primary=integration.is_primary,
        connected=bool(integration.credentials_encrypted),
        capabilities=sorted(c.value for c in capabilities_of(integration.provider)),
        config=dict(integration.config or {}),
        connected_at=_iso(integration.credentials_updated_at),
        token_expires_at=_iso(integration.token_expires_at),
        last_health_check_at=_iso(integration.last_health_check_at),
        last_health_ok=integration.last_health_ok,
        last_error=integration.last_error,
    )


def _parse_provider(provider: str) -> CalendarProviderType:
    try:
        return CalendarProviderType(provider.lower())
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown provider. Supported: "
                f"{', '.join(p.value for p in CalendarProviderType)}"
            ),
        )


def _resolve_local_input(raw: str, timezone_name: str) -> datetime:
    """
    Parse a local wall-clock string into a UTC instant.

    The two DST cases become 422s with an explanation rather than a silent
    shift. Requirement 4: never silently move an appointment by an hour.
    """
    try:
        naive = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=f"{raw!r} is not an ISO datetime like 2026-09-08T15:00:00",
        )
    if naive.tzinfo is not None:
        return naive.astimezone(as_utc(naive).tzinfo)

    try:
        return resolve_local(naive, timezone_name).utc
    except AmbiguousTimeError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{raw} happens twice in {timezone_name} (clocks go back). "
                f"Send an explicit offset: {exc.first.isoformat()} or "
                f"{exc.second.isoformat()}."
            ),
        )
    except NonexistentTimeError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{raw} does not exist in {timezone_name} (clocks go forward "
                f"from {exc.gap_start:%H:%M} to {exc.gap_end:%H:%M})."
            ),
        )
    except TimezoneError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


#: Outcome -> HTTP status. A conflict is a 409 and a policy refusal a 422, so
#: a client can branch on the status without parsing prose.
_OUTCOME_STATUS = {
    BookingOutcome.CONFLICT: 409,
    BookingOutcome.OUTSIDE_HOURS: 422,
    BookingOutcome.TOO_SOON: 422,
    BookingOutcome.TOO_FAR: 422,
    BookingOutcome.NEEDS_CLARIFICATION: 422,
    BookingOutcome.NOT_FOUND: 404,
    BookingOutcome.FAILED: 502,
}


def _raise_for_outcome(result) -> None:
    status = _OUTCOME_STATUS.get(result.outcome)
    if status is not None:
        raise HTTPException(
            status_code=status,
            detail={"outcome": result.outcome.value, "message": result.message},
        )


# -------------------------------------------------------- appointment routes ---

@router.get("/availability", response_model=AvailabilityOut)
async def get_availability(
    day: date = Query(..., description="local date, YYYY-MM-DD"),
    part_of_day: str = Query("any"),
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_READ)),
    session: AsyncSession = Depends(get_session),
) -> AvailabilityOut:
    rules = await service.get_rules(session, ctx.tenant)
    slots, degraded = await service.find_slots(
        session, ctx.tenant, day=day, part_of_day=part_of_day,
        limit=50,   # an API client can render more than a voice agent can say
    )
    return AvailabilityOut(
        date=day.isoformat(),
        timezone=rules.timezone,
        degraded=degraded,
        slots=[
            SlotOut(
                start=slot.start.isoformat(),
                end=slot.end.isoformat(),
                spoken=slot.spoken(),
            )
            for slot in slots
        ],
    )


@router.get("", response_model=AppointmentListOut)
@router.get("/", response_model=AppointmentListOut)
async def list_appointments(
    status: str | None = Query(None),
    upcoming: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_READ)),
    session: AsyncSession = Depends(get_session),
) -> AppointmentListOut:
    query = select(Appointment).where(Appointment.tenant_id == ctx.tenant_id)
    if status:
        try:
            query = query.where(Appointment.status == AppointmentStatus(status.lower()))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown status {status!r}")
    if upcoming:
        query = query.where(Appointment.starts_at >= now_utc() - timedelta(hours=1))

    rows = (
        (await session.execute(query.order_by(Appointment.starts_at).limit(limit)))
        .scalars()
        .all()
    )
    return AppointmentListOut(
        appointments=[to_out(row) for row in rows], total=len(rows)
    )


@router.post("", response_model=AppointmentOut, status_code=201)
@router.post("/", response_model=AppointmentOut, status_code=201)
async def create_appointment(
    body: BookIn,
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> AppointmentOut:
    rules = await service.get_rules(session, ctx.tenant)
    start = _resolve_local_input(body.starts_at_local, rules.timezone)

    result = await service.book(
        session, ctx.tenant,
        service.BookingRequest(
            tenant_id=ctx.tenant_id,
            start=start,
            customer_name=body.customer_name,
            customer_phone=body.customer_phone,
            customer_email=body.customer_email,
            reason=body.reason,
            lead_id=body.lead_id,
            request_id=body.request_id,
        ),
    )
    _raise_for_outcome(result)

    appointment = await session.get(Appointment, uuid.UUID(result.appointment_id))
    return to_out(appointment)


@router.get("/{appointment_id}", response_model=AppointmentOut)
async def get_appointment(
    appointment_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_READ)),
    session: AsyncSession = Depends(get_session),
) -> AppointmentOut:
    # 404s on another tenant's id rather than 403ing, so ids cannot be probed.
    return to_out(await get_owned(session, Appointment, appointment_id, ctx))


@router.patch("/{appointment_id}", response_model=AppointmentOut)
async def reschedule_appointment(
    appointment_id: uuid.UUID,
    body: RescheduleIn,
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> AppointmentOut:
    appointment = await get_owned(session, Appointment, appointment_id, ctx)
    rules = await service.get_rules(session, ctx.tenant)
    start = _resolve_local_input(body.starts_at_local, rules.timezone)

    result = await service.reschedule(
        session, ctx.tenant, appointment, start, reason=body.reason
    )
    _raise_for_outcome(result)

    await record_audit(
        session, action=AuditAction.APPOINTMENT_RESCHEDULED, tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id, actor_email=ctx.user.email,
        detail={"appointment_id": str(appointment_id)},
    )
    await session.commit()
    await session.refresh(appointment)
    return to_out(appointment)


@router.post("/{appointment_id}/cancel", response_model=AppointmentOut)
async def cancel_appointment(
    appointment_id: uuid.UUID,
    body: CancelIn | None = None,
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> AppointmentOut:
    appointment = await get_owned(session, Appointment, appointment_id, ctx)
    result = await service.cancel(
        session, ctx.tenant, appointment,
        reason=(body.reason if body else ""),
        cancelled_by=f"user:{ctx.user.email}",
    )
    _raise_for_outcome(result)

    await record_audit(
        session, action=AuditAction.APPOINTMENT_CANCELLED, tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id, actor_email=ctx.user.email,
        detail={"appointment_id": str(appointment_id)},
    )
    await session.commit()
    await session.refresh(appointment)
    return to_out(appointment)


@router.post("/{appointment_id}/no-show", response_model=AppointmentOut)
async def mark_no_show(
    appointment_id: uuid.UUID,
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> AppointmentOut:
    appointment = await get_owned(session, Appointment, appointment_id, ctx)
    await service.mark_no_show(session, appointment)
    await session.refresh(appointment)
    return to_out(appointment)


# ----------------------------------------------------------- calendar routes ---

@calendar_router.get("/providers", response_model=ProviderCatalogueOut)
async def list_providers(
    ctx: TenantContext = Depends(require_permission(Permission.INTEGRATION_READ)),
) -> ProviderCatalogueOut:
    return ProviderCatalogueOut(providers=[
        ProviderInfo(
            provider=provider.value,
            capabilities=sorted(c.value for c in capabilities_of(provider)),
            credential_fields=list(CREDENTIAL_FIELDS[provider]),
            config_fields=list(CONFIG_FIELDS[provider]),
        )
        for provider in CalendarProviderType
    ])


@calendar_router.get("/integrations", response_model=list[CalendarIntegrationOut])
async def list_integrations(
    ctx: TenantContext = Depends(require_permission(Permission.INTEGRATION_READ)),
    session: AsyncSession = Depends(get_session),
) -> list[CalendarIntegrationOut]:
    rows = (
        (
            await session.execute(
                select(CalendarIntegration)
                .where(CalendarIntegration.tenant_id == ctx.tenant_id)
                .order_by(CalendarIntegration.provider)
            )
        )
        .scalars()
        .all()
    )
    return [integration_out(row) for row in rows]


@calendar_router.put(
    "/integrations/{provider}", response_model=CalendarIntegrationOut
)
async def upsert_integration(
    provider: str,
    body: CalendarIntegrationIn,
    ctx: TenantContext = Depends(require_permission(Permission.INTEGRATION_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> CalendarIntegrationOut:
    provider_type = _parse_provider(provider)
    config = _validated_config(provider_type, body.config)

    integration = await service.get_integration(
        session, tenant_id=ctx.tenant_id, provider=provider_type
    )
    creating = integration is None
    if creating:
        integration = CalendarIntegration(
            tenant_id=ctx.tenant_id, provider=provider_type, config={}
        )
        session.add(integration)

    integration.is_enabled = body.is_enabled
    integration.is_primary = body.is_primary
    integration.config = config
    integration.updated_at = now_utc()

    if body.credentials is not None:
        _store_credentials(integration, ctx, provider_type, body.credentials)

    if body.is_primary:
        await _demote_other_primaries(session, ctx.tenant_id, provider_type)

    await session.commit()
    await session.refresh(integration)

    await record_audit(
        session, action=AuditAction.CALENDAR_CONNECTED, tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id, actor_email=ctx.user.email,
        # Provider and the *names* of the fields supplied -- never a value.
        detail={
            "provider": provider_type.value,
            "credential_fields": sorted(body.credentials or {}),
            "config_keys": sorted(config),
            "created": creating,
        },
    )
    await session.commit()

    log.info(
        "calendar.integration_saved", tenant_id=str(ctx.tenant_id),
        provider=provider_type.value, outcome="created" if creating else "updated",
    )
    return integration_out(integration)


async def _demote_other_primaries(
    session: AsyncSession, tenant_id: uuid.UUID, keep: CalendarProviderType
) -> None:
    rows = (
        (
            await session.execute(
                select(CalendarIntegration).where(
                    CalendarIntegration.tenant_id == tenant_id,
                    CalendarIntegration.provider != keep,
                    CalendarIntegration.is_primary.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.is_primary = False


def _store_credentials(integration, ctx, provider_type, credentials) -> None:
    """
    Encrypt with STEP 5's cipher and key ring.

    One cipher for the whole product: same envelope, same rotation story, same
    `(tenant, provider)` associated data. There is no code path that stores a
    calendar token in any other form — if encryption is not configured this
    returns 503 rather than falling back to plaintext.
    """
    from app.integrations.crm import crypto

    allowed = set(CREDENTIAL_FIELDS[provider_type])
    unknown = sorted(set(credentials) - allowed)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown credential field(s) for {provider_type.value}: "
                f"{', '.join(unknown)}. Allowed: {', '.join(sorted(allowed)) or 'none'}"
            ),
        )

    key_ring = crypto.key_ring_from_settings()
    if key_ring is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Calendar credential encryption is not configured on this "
                "instance. Set CRM_ENCRYPTION_KEYS before connecting a provider."
            ),
        )
    try:
        envelope, key_id = crypto.encrypt_credentials(
            dict(credentials), tenant_id=str(ctx.tenant_id),
            provider=provider_type.value, key_ring=key_ring,
        )
    except crypto.CredentialCryptoError as exc:
        raise HTTPException(status_code=503, detail=f"Cannot store credentials: {exc}")

    integration.credentials_encrypted = envelope
    integration.credentials_key_id = key_id
    integration.credentials_updated_at = now_utc()


def _validated_config(provider_type: CalendarProviderType, config: dict) -> dict:
    allowed = set(CONFIG_FIELDS[provider_type])
    unknown = sorted(set(config or {}) - allowed)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown config field(s) for {provider_type.value}: "
                f"{', '.join(unknown)}. Allowed: {', '.join(sorted(allowed)) or 'none'}"
            ),
        )
    cleaned = {k: v for k, v in (config or {}).items() if v not in (None, "")}

    if provider_type is CalendarProviderType.CALCOM and "event_type_id" not in cleaned:
        raise HTTPException(
            status_code=422,
            detail="Cal.com requires config.event_type_id; every booking is "
                   "made against an event type",
        )
    return cleaned


@calendar_router.post(
    "/integrations/{provider}/test", response_model=dict
)
async def test_integration(
    provider: str,
    ctx: TenantContext = Depends(require_permission(Permission.INTEGRATION_READ)),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    Live connection test.

    Always 200. A tenant pressing "Test" with a dead token gets a diagnosis,
    not a 500 — and never the provider's raw response.
    """
    provider_type = _parse_provider(provider)
    integration = await service.get_integration(
        session, tenant_id=ctx.tenant_id, provider=provider_type
    )
    if integration is None:
        raise HTTPException(status_code=404, detail="Integration is not configured")

    result = await service.check_health(session, ctx.tenant, integration)
    return {
        "connected": result.connected,
        "provider": result.provider,
        "latency_ms": result.latency_ms,
        "safe_message": result.safe_message,
    }


@calendar_router.delete("/integrations/{provider}", status_code=204)
async def delete_integration(
    provider: str,
    ctx: TenantContext = Depends(require_permission(Permission.INTEGRATION_WRITE)),
    session: AsyncSession = Depends(get_session),
):
    provider_type = _parse_provider(provider)
    integration = await service.get_integration(
        session, tenant_id=ctx.tenant_id, provider=provider_type
    )
    if integration is None:
        raise HTTPException(status_code=404, detail="Integration is not configured")

    await session.delete(integration)
    await session.commit()

    await record_audit(
        session, action=AuditAction.CALENDAR_DISCONNECTED, tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id, actor_email=ctx.user.email,
        detail={"provider": provider_type.value},
    )
    await session.commit()
    return None


# ------------------------------------------------------------- policy routes ---

@calendar_router.get("/policy", response_model=PolicyOut)
async def get_scheduling_policy(
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_READ)),
    session: AsyncSession = Depends(get_session),
) -> PolicyOut:
    stored = await service.get_policy(session, ctx.tenant_id)
    rules = policy_engine.rules_from(ctx.tenant, stored)

    return PolicyOut(
        timezone=rules.timezone,
        weekly_hours=(stored.weekly_hours if stored else _legacy_hours(rules)),
        holidays=list(stored.holidays) if stored else [],
        blocked_periods=list(stored.blocked_periods) if stored else [],
        slot_minutes=rules.slot_minutes,
        slot_interval_minutes=rules.slot_interval_minutes,
        buffer_before_minutes=rules.buffer_before_minutes,
        buffer_after_minutes=rules.buffer_after_minutes,
        minimum_notice_minutes=rules.minimum_notice_minutes,
        booking_horizon_days=rules.booking_horizon_days,
        max_slots_offered=rules.max_slots_offered,
        allow_outside_business_hours=rules.allow_outside_business_hours,
        require_provider_confirmation=rules.require_provider_confirmation,
        configured=stored is not None,
    )


def _legacy_hours(rules) -> dict:
    """Render the Tenant-column fallback in the policy's own shape."""
    return {
        day: [[opens.strftime("%H:%M"), closes.strftime("%H:%M")]]
        for day, intervals in rules.weekly_hours.items()
        for opens, closes in intervals
    }


@calendar_router.put("/policy", response_model=PolicyOut)
async def update_scheduling_policy(
    body: PolicyIn,
    ctx: TenantContext = Depends(require_permission(Permission.APPOINTMENT_WRITE)),
    session: AsyncSession = Depends(get_session),
) -> PolicyOut:
    if body.timezone is not None and not is_valid_zone(body.timezone):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{body.timezone!r} is not a known IANA timezone. Use e.g. "
                f"America/New_York, Europe/London, Asia/Dhaka."
            ),
        )

    # Validate on write, so a bad schedule is a 422 now rather than a mystery
    # at three in the morning when a call comes in.
    try:
        policy_engine.parse_weekly_hours(body.weekly_hours)
        policy_engine.parse_holidays(body.holidays)
        policy_engine.parse_blocked(
            body.blocked_periods, body.timezone or ctx.tenant.timezone or "UTC"
        )
    except policy_engine.PolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if body.slot_interval_minutes > body.slot_minutes:
        raise HTTPException(
            status_code=422,
            detail=(
                "slot_interval_minutes may not exceed slot_minutes, or the "
                "grid would skip bookable time"
            ),
        )

    stored = await service.get_policy(session, ctx.tenant_id)
    if stored is None:
        stored = SchedulingPolicy(tenant_id=ctx.tenant_id)
        session.add(stored)

    stored.weekly_hours = body.weekly_hours or {}
    stored.holidays = body.holidays or []
    stored.blocked_periods = body.blocked_periods or []
    stored.slot_minutes = body.slot_minutes
    stored.slot_interval_minutes = body.slot_interval_minutes
    stored.buffer_before_minutes = body.buffer_before_minutes
    stored.buffer_after_minutes = body.buffer_after_minutes
    stored.minimum_notice_minutes = body.minimum_notice_minutes
    stored.booking_horizon_days = body.booking_horizon_days
    stored.max_slots_offered = body.max_slots_offered
    stored.allow_outside_business_hours = body.allow_outside_business_hours
    stored.require_provider_confirmation = body.require_provider_confirmation

    if body.timezone is not None:
        ctx.tenant.timezone = body.timezone

    await session.commit()
    return await get_scheduling_policy(ctx=ctx, session=session)

