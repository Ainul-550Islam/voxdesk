"""
The appointment service.

The one place that holds a tenant's calendar integration, its scheduling
policy, a provider adapter and the database at the same time. Routes and voice
tools call this; nothing calls a provider SDK directly.

Four rules shape every function here, and each one exists because the audit
found the opposite:

**Never claim a booking the provider did not accept.** `_confirm` is the only
path that writes `CONFIRMED`, and it requires an `external_event_id`. A
provider returning 200 with no id is a failure to record, not a success. The
pre-STEP-6 code wrote the row and said "Booked" regardless.

**An unreachable calendar is not an empty calendar.** `get_busy` raises; this
layer catches it and either refuses to book (default) or degrades explicitly,
never silently.

**The slot lock is in the database, not in application logic.** Two callers
racing compute the same `slot_key`; the unique constraint decides. No amount
of interleaving can produce two confirmed bookings for one slot.

**An ambiguous timeout is reconciled, never blind-retried.** If a provider
call times out we ask the provider whether the event exists before doing
anything else. "I could not check" is treated as unknown, not as absence.
"""
from __future__ import annotations

import time as _time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import log
from app.db.models import (
    Appointment,
    AppointmentStatus,
    BLOCKING_APPOINTMENT_STATUSES,
    CalendarIntegration,
    CalendarProviderType,
    SchedulingPolicy,
    Tenant,
)
from app.integrations.calendar import policy as policy_engine
from app.integrations.calendar.base import (
    CalendarCapability,
    CalendarContext,
    CalendarProvider,
)
from app.integrations.calendar.errors import (
    CalendarConflictError,
    CalendarError,
    CalendarTimeout,
    CalendarUnsupportedError,
    safe_message,
)
from app.integrations.calendar.models import (
    Attendee,
    AvailabilitySlot,
    BookingOutcome,
    BookingResult,
    BusyPeriod,
    CalendarEvent,
    EventRequest,
    HealthResult,
    TimeWindow,
    booking_idempotency_key,
    slot_key,
)
from app.integrations.calendar.registry import build as build_provider
from app.integrations.calendar.timezones import (
    as_utc,
    format_spoken,
    format_spoken_date,
    get_zone,
    now_utc,
    to_local,
)


@dataclass(frozen=True)
class BookingRequest:
    """Everything needed to book, already resolved to an instant."""

    tenant_id: uuid.UUID
    start: datetime
    customer_name: str
    customer_phone: str
    reason: str = ""
    customer_email: str | None = None
    call_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    #: Client-supplied request id. Without one the key is derived from
    #: (tenant, caller identity, start) -- see `booking_idempotency_key`.
    request_id: str | None = None


# ---------------------------------------------------------------- lookups ---

async def get_policy(session: AsyncSession, tenant_id: uuid.UUID) -> SchedulingPolicy | None:
    return (
        await session.execute(
            select(SchedulingPolicy).where(SchedulingPolicy.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()


async def get_rules(session: AsyncSession, tenant: Tenant) -> policy_engine.SchedulingRules:
    return policy_engine.rules_from(tenant, await get_policy(session, tenant.id))


async def get_integration(
    session: AsyncSession, *, tenant_id: uuid.UUID, provider: CalendarProviderType
) -> CalendarIntegration | None:
    """
    Keyed on `(tenant_id, provider)`, never provider alone.

    Requirement 23 states that explicitly, and the unique constraint makes the
    pair a real key rather than a filter someone might forget.
    """
    return (
        await session.execute(
            select(CalendarIntegration).where(
                CalendarIntegration.tenant_id == tenant_id,
                CalendarIntegration.provider == provider,
            )
        )
    ).scalar_one_or_none()


async def primary_integration(
    session: AsyncSession, tenant_id: uuid.UUID
) -> CalendarIntegration | None:
    """
    Which provider a booking goes to.

    An explicit `is_primary` flag wins; otherwise the single enabled
    integration. With several enabled and none marked primary this returns
    `None` rather than picking one — booking onto an arbitrary calendar is
    worse than telling the tenant to choose.
    """
    rows = (
        (
            await session.execute(
                select(CalendarIntegration).where(
                    CalendarIntegration.tenant_id == tenant_id,
                    CalendarIntegration.is_enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    primary = [row for row in rows if row.is_primary]
    if primary:
        return primary[0]
    return rows[0] if len(rows) == 1 else None


def build_context(integration: CalendarIntegration, tenant: Tenant) -> CalendarContext:
    """
    Decrypt this integration's credentials and package them for an adapter.

    Reuses STEP 5's cipher, including its `(tenant_id, provider)` associated
    data — so a row whose ciphertext was copied from another tenant's
    integration fails to decrypt here rather than yielding a usable token.
    """
    from app.core.config import settings
    from app.integrations.crm import crypto
    from app.integrations.calendar.errors import CalendarConfigurationError

    credentials: dict[str, Any] = {}
    if integration.credentials_encrypted:
        key_ring = crypto.key_ring_from_settings()
        if key_ring is None:
            raise CalendarConfigurationError(
                "calendar credential encryption is not configured on this "
                "instance, so stored credentials cannot be read"
            )
        credentials = crypto.decrypt_credentials(
            integration.credentials_encrypted,
            tenant_id=str(integration.tenant_id),
            provider=integration.provider.value,
            key_ring=key_ring,
        )

    return CalendarContext(
        tenant_id=str(integration.tenant_id),
        credentials=credentials,
        config=dict(integration.config or {}),
        timezone=tenant.timezone or "UTC",
        timeout_seconds=settings.calendar_request_timeout_seconds,
    )


def build_adapter(integration: CalendarIntegration, tenant: Tenant) -> CalendarProvider:
    return build_provider(integration.provider, build_context(integration, tenant))


async def _resolve_provider(
    session: AsyncSession, tenant: Tenant
) -> tuple[CalendarIntegration | None, CalendarProvider]:
    """
    The tenant's calendar, or the internal one.

    A tenant with nothing connected still gets a working booking flow backed
    by VoxDesk's own appointments table — which is the honest position: we
    know what we booked, we just cannot see their other commitments.
    """
    integration = await primary_integration(session, tenant.id)
    if integration is None:
        from app.integrations.calendar.providers.internal import InternalCalendarProvider

        return None, InternalCalendarProvider(CalendarContext(
            tenant_id=str(tenant.id), credentials={}, config={},
            timezone=tenant.timezone or "UTC",
        ))
    return integration, build_adapter(integration, tenant)


# ----------------------------------------------------------- availability ---

async def _booked_windows(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    window: TimeWindow,
    *,
    exclude_id: uuid.UUID | None = None,
) -> list[TimeWindow]:
    """
    VoxDesk's own commitments in a window.

    The pre-STEP-6 code never consulted this table at all, so two callers
    could book the same slot whenever the provider had not yet propagated the
    first event — and always, when the provider was unreachable.

    Only `BLOCKING_APPOINTMENT_STATUSES` count: a cancelled appointment frees
    its slot, which is the whole point of cancelling.
    """
    query = select(Appointment).where(
        Appointment.tenant_id == tenant_id,
        Appointment.status.in_(list(BLOCKING_APPOINTMENT_STATUSES)),
        Appointment.starts_at < window.end,
        Appointment.ends_at > window.start,
    )
    if exclude_id is not None:
        query = query.where(Appointment.id != exclude_id)

    rows = (await session.execute(query)).scalars().all()
    return [
        TimeWindow(start=as_utc(row.starts_at), end=as_utc(row.ends_at))
        for row in rows
    ]


async def _provider_busy(
    provider: CalendarProvider, window: TimeWindow, rules
) -> tuple[list[BusyPeriod], str | None]:
    """
    Ask the provider what is busy.

    Returns `(periods, degraded_reason)`. A failure is *reported*, never
    swallowed into an empty list — the caller decides whether to proceed. That
    single change is the audit's F2 fixed.
    """
    if not provider.supports(CalendarCapability.FREE_BUSY):
        return [], None
    try:
        return await provider.get_busy(window), None
    except CalendarUnsupportedError:
        return [], None
    except CalendarError as exc:
        log.warning(
            "calendar.freebusy_failed", provider=provider.name,
            error_code=exc.code, error=exc.safe_message,
        )
        return [], exc.code


async def find_slots(
    session: AsyncSession,
    tenant: Tenant,
    *,
    day: date,
    part_of_day: str = "any",
    limit: int | None = None,
    now: datetime | None = None,
) -> tuple[list[AvailabilitySlot], str | None]:
    """
    Bookable slots on a local date.

    Returns `(slots, degraded_reason)`. `degraded_reason` is non-None when the
    provider could not be consulted, so the caller can decide whether offering
    slots is safe.
    """
    rules = await get_rules(session, tenant)
    integration, provider = await _resolve_provider(session, tenant)

    candidates = policy_engine.candidate_slots(rules, day, now=now)
    candidates = policy_engine.part_of_day_filter(
        candidates, part_of_day, rules.timezone
    )
    if not candidates:
        return [], None

    span = TimeWindow(
        start=min(slot.start for slot in candidates) - rules.buffer_before,
        end=max(slot.end for slot in candidates) + rules.buffer_after,
    )

    busy: list[BusyPeriod] = []
    degraded: str | None = None

    if provider.supports(CalendarCapability.GET_AVAILABILITY) and hasattr(
        provider, "get_slots"
    ):
        # Cal.com returns bookable slots rather than busy blocks. Intersect
        # rather than subtract: the provider has already applied the owner's
        # own rules, and VoxDesk's business hours still apply on top.
        try:
            offered = await provider.get_slots(span, timezone_name=rules.timezone)
            allowed = {slot.start for slot in offered}
            candidates = [slot for slot in candidates if slot.start in allowed]
        except CalendarError as exc:
            log.warning(
                "calendar.slots_failed", provider=provider.name,
                error_code=exc.code, error=exc.safe_message,
            )
            degraded = exc.code
    else:
        busy, degraded = await _provider_busy(provider, span, rules)

    booked = await _booked_windows(session, tenant.id, span)
    slots = policy_engine.filter_slots(
        candidates, rules=rules, busy=busy, booked=booked
    )

    ceiling = limit or rules.max_slots_offered
    return slots[:ceiling], degraded


# ---------------------------------------------------------------- booking ---

async def book(
    session: AsyncSession,
    tenant: Tenant,
    request: BookingRequest,
    *,
    now: datetime | None = None,
) -> BookingResult:
    """
    Book one appointment.

    The order of operations is the design:

    1. **Idempotency lookup** — has this exact request already produced an
       appointment? Return it. This runs first so a retry never even reaches
       the policy checks.
    2. **Policy** — business hours, notice, horizon. Cheap, local, no network.
    3. **Reserve the slot in the database** — a PENDING row whose unique
       `slot_key` is the lock. This happens *before* the provider call, so two
       racing callers are separated before either touches the network.
    4. **Call the provider** — only the winner gets here.
    5. **Confirm** — write the external id and CONFIRMED together.

    Steps 3 and 4 in that order matter. Reserving after the provider call
    would mean both callers create events and one of them then fails to
    persist, leaving an orphan on the business's calendar.
    """
    started = _time.perf_counter()
    moment = now or now_utc()
    rules = await get_rules(session, tenant)
    zone = get_zone(rules.timezone)

    start = as_utc(request.start)
    window = TimeWindow(start=start, end=start + rules.duration)

    key = booking_idempotency_key(
        str(tenant.id), start,
        phone=request.customer_phone, email=request.customer_email,
        request_id=request.request_id,
    )

    # ---- 1. idempotency -------------------------------------------------
    existing = (
        await session.execute(
            select(Appointment).where(
                Appointment.tenant_id == tenant.id,
                Appointment.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status not in (
        AppointmentStatus.CANCELLED, AppointmentStatus.FAILED
    ):
        log.info(
            "calendar.book_duplicate", tenant_id=str(tenant.id),
            appointment_id=str(existing.id), idempotency_key=key,
        )
        return BookingResult(
            outcome=BookingOutcome.ALREADY_BOOKED,
            message=(
                f"That's already booked for "
                f"{format_spoken_date(as_utc(existing.starts_at), zone)} at "
                f"{format_spoken(as_utc(existing.starts_at), zone)}."
            ),
            appointment_id=str(existing.id),
        )

    # ---- 2. policy ------------------------------------------------------
    rejection = _check_policy(rules, window, moment, zone)
    if rejection is not None:
        return rejection

    # ---- 3. reserve -----------------------------------------------------
    reserved, conflict = await _reserve(
        session, tenant, request, window, key=key, rules=rules
    )
    if conflict is not None:
        return conflict

    # ---- 4. provider ----------------------------------------------------
    integration, provider = await _resolve_provider(session, tenant)
    event_request = EventRequest(
        title=_event_title(tenant, request),
        start=window.start,
        end=window.end,
        timezone=rules.timezone,
        attendee=Attendee(
            name=request.customer_name,
            phone=request.customer_phone,
            email=request.customer_email,
        ),
        description=_event_description(request),
        calendar_reference=(integration.config or {}).get("calendar_id")
        if integration else None,
        idempotency_key=key,
    )

    try:
        event = await _create_with_reconciliation(provider, event_request, window, key)
    except CalendarConflictError as exc:
        await _fail(session, reserved, exc, terminal=True)
        return BookingResult(
            outcome=BookingOutcome.CONFLICT,
            message="Sorry, that time was just taken. Can I offer you another?",
        )
    except CalendarError as exc:
        return await _handle_provider_failure(
            session, reserved, exc, rules, zone, started, tenant
        )

    # ---- 5. confirm -----------------------------------------------------
    await _confirm(session, reserved, event, integration)

    from app.integrations.crm import hooks as crm_hooks

    await crm_hooks.on_appointment_booked(session, reserved)
    await _schedule_reminder(session, tenant, reserved, now=moment)
    await session.commit()

    log.info(
        "calendar.booked", tenant_id=str(tenant.id),
        appointment_id=str(reserved.id), provider=reserved.provider.value,
        external_event_id=event.external_id,
        duration_ms=round((_time.perf_counter() - started) * 1000, 2),
        outcome="booked",
    )
    return BookingResult(
        outcome=BookingOutcome.BOOKED,
        message=(
            f"You're booked for {format_spoken_date(window.start, zone)} at "
            f"{format_spoken(window.start, zone)}."
        ),
        appointment_id=str(reserved.id),
    )


def _check_policy(rules, window: TimeWindow, moment: datetime, zone) -> BookingResult | None:
    """Local checks, in the order a caller would notice them."""
    notice = timedelta(minutes=rules.minimum_notice_minutes)
    if window.start < moment + notice:
        if window.start < moment:
            return BookingResult(
                outcome=BookingOutcome.TOO_SOON,
                message="That time has already passed. What else works for you?",
            )
        return BookingResult(
            outcome=BookingOutcome.TOO_SOON,
            message=(
                f"We need at least {rules.minimum_notice_minutes} minutes' "
                f"notice. Can I find you something a little later?"
            ),
        )

    if window.start > moment + timedelta(days=rules.booking_horizon_days):
        return BookingResult(
            outcome=BookingOutcome.TOO_FAR,
            message=(
                f"We only book up to {rules.booking_horizon_days} days ahead. "
                f"Shall we find something sooner?"
            ),
        )

    if policy_engine.is_blocked(rules, window):
        return BookingResult(
            outcome=BookingOutcome.OUTSIDE_HOURS,
            message="We're closed then. Can I offer you another day?",
        )

    if not policy_engine.is_within_business_hours(rules, window):
        local = to_local(window.start, zone)
        hours = policy_engine.describe_hours(rules, local.date())
        if hours == "closed":
            return BookingResult(
                outcome=BookingOutcome.OUTSIDE_HOURS,
                message=f"We're closed on {local:%A}. Can I offer you another day?",
            )
        return BookingResult(
            outcome=BookingOutcome.OUTSIDE_HOURS,
            message=f"On {local:%A} we're open {hours}. Would one of those work?",
        )
    return None


async def _reserve(
    session: AsyncSession, tenant: Tenant, request: BookingRequest,
    window: TimeWindow, *, key: str, rules,
) -> tuple[Appointment | None, BookingResult | None]:
    """
    Take the slot lock.

    A PENDING row is inserted with a deterministic `slot_key`. The unique
    constraint on `(tenant_id, slot_key)` is what makes the concurrency
    guarantee real: two racing requests compute the same key and the database
    rejects the second, whatever order the Python interleaves in.

    The pre-check against `_booked_windows` before the insert is not the
    guarantee — it is a courtesy, so the common case produces a friendly
    message rather than an exception path. The constraint is the guarantee.
    """
    guarded = window.expanded(before=rules.buffer_before, after=rules.buffer_after)
    conflicts = await _booked_windows(session, tenant.id, guarded)
    if conflicts:
        return None, BookingResult(
            outcome=BookingOutcome.CONFLICT,
            message="That time is already taken. Can I offer you another?",
        )

    appointment = Appointment(
        tenant_id=tenant.id,
        call_id=request.call_id,
        lead_id=request.lead_id,
        customer_name=request.customer_name,
        customer_phone=request.customer_phone,
        attendee_email=request.customer_email,
        reason=request.reason,
        starts_at=window.start,
        ends_at=window.end,
        timezone=rules.timezone,
        status=AppointmentStatus.PENDING,
        slot_key=slot_key(str(tenant.id), window.start, window.end),
        idempotency_key=key,
    )
    try:
        # A SAVEPOINT, not a bare flush.
        #
        # Losing the race must undo *the reservation*, not the caller's whole
        # transaction. `session.rollback()` here would discard everything the
        # caller had already done -- and on a shared connection it can also
        # tear down a concurrent winner's uncommitted insert. `begin_nested`
        # scopes the undo to exactly the row that failed.
        async with session.begin_nested():
            session.add(appointment)
            await session.flush()
    except IntegrityError:
        # Someone else took the slot between the check and the insert -- the
        # exact race requirement 8 asks about. The database arbitrated, which
        # is the point: no interleaving of application code can produce two
        # confirmed bookings for one slot.
        log.info(
            "calendar.slot_lock_lost", tenant_id=str(tenant.id),
            start=window.start.isoformat(),
        )
        return None, BookingResult(
            outcome=BookingOutcome.CONFLICT,
            message="Sorry, someone just took that slot. Can I offer you another?",
        )

    return appointment, None


async def _create_with_reconciliation(
    provider: CalendarProvider, event_request: EventRequest,
    window: TimeWindow, key: str,
) -> CalendarEvent:
    """
    Create the event, reconciling an ambiguous timeout.

    Requirement 9 and 26: a timeout must never become "create a second
    booking blindly". On timeout we ask the provider whether our event
    already exists.

    Note what happens when the reconciliation query *itself* fails: the
    original timeout is re-raised. "I could not check" is not evidence of
    absence, and the only safe reading of unknown is to leave the appointment
    unconfirmed for a human rather than risk a duplicate.
    """
    try:
        return await provider.create_event(event_request)
    except CalendarTimeout as timeout:
        log.warning(
            "calendar.create_timeout_reconciling", provider=provider.name,
            idempotency_key=key,
        )
        try:
            found = await provider.find_event_by_key(key, window)
        except CalendarError:
            raise timeout
        if found is not None:
            log.info(
                "calendar.reconciled_after_timeout", provider=provider.name,
                external_event_id=found.external_id,
            )
            return found
        raise


async def _confirm(
    session: AsyncSession, appointment: Appointment, event: CalendarEvent,
    integration: CalendarIntegration | None,
) -> None:
    """
    The only path that writes CONFIRMED, and it requires proof.

    `event.external_id` is guaranteed non-empty by `CalendarEvent`, so there
    is no way to reach this function without a provider acknowledgement.
    """
    appointment.status = AppointmentStatus.CONFIRMED
    appointment.external_event_id = event.external_id
    appointment.provider = (
        integration.provider if integration else CalendarProviderType.INTERNAL
    )
    appointment.calendar_reference = event.calendar_reference
    appointment.meeting_url = event.meeting_url
    appointment.confirmed_at = now_utc()
    appointment.last_error = None
    # Keep the legacy column populated so pre-STEP-6 readers keep working.
    if appointment.provider in (
        CalendarProviderType.GOOGLE, CalendarProviderType.GOOGLE_SERVICE_ACCOUNT
    ):
        appointment.google_event_id = event.external_id


async def _fail(
    session: AsyncSession, appointment: Appointment | None, error: CalendarError,
    *, terminal: bool,
) -> None:
    if appointment is None:
        return
    appointment.status = (
        AppointmentStatus.FAILED if terminal else AppointmentStatus.PENDING
    )
    appointment.last_error = safe_message(str(error))[:500]
    await session.commit()


async def _handle_provider_failure(
    session: AsyncSession, appointment: Appointment, error: CalendarError,
    rules, zone, started: float, tenant: Tenant,
) -> BookingResult:
    """
    The provider refused or could not be reached.

    Two policies, and the default is the safe one. With
    `require_provider_confirmation` (default True) the appointment is marked
    FAILED and the caller is offered another time — because telling someone
    they are booked when the calendar does not know is the failure this whole
    step exists to eliminate.

    A tenant may opt out, in which case the row stays PENDING for staff to
    reconcile and the caller is told it is provisional. That is a real
    business choice — a busy salon may prefer a provisional booking to a lost
    customer — but it is opt-in and it never says "confirmed".
    """
    duration = round((_time.perf_counter() - started) * 1000, 2)
    log.warning(
        "calendar.book_provider_failed", tenant_id=str(tenant.id),
        appointment_id=str(appointment.id), error_code=error.code,
        error=error.safe_message, duration_ms=duration, outcome="failed",
    )

    if rules.require_provider_confirmation:
        await _fail(session, appointment, error, terminal=True)
        return BookingResult(
            outcome=BookingOutcome.FAILED,
            # Requirement 15 and STEP 5's failure-UX rule: the caller never
            # hears the provider's name or its status code.
            message=(
                "I'm having trouble confirming that with our calendar right "
                "now. Let me take your details and someone will call you back."
            ),
        )

    appointment.status = AppointmentStatus.PENDING
    appointment.last_error = safe_message(str(error))[:500]
    await session.commit()
    return BookingResult(
        outcome=BookingOutcome.BOOKED,
        message=(
            f"I've put you down for "
            f"{format_spoken_date(as_utc(appointment.starts_at), zone)} at "
            f"{format_spoken(as_utc(appointment.starts_at), zone)}. "
            f"We'll confirm it shortly."
        ),
        appointment_id=str(appointment.id),
        details={"provisional": True},
    )


# ------------------------------------------------------------- reschedule ---

async def reschedule(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    new_start: datetime,
    *,
    reason: str = "",
    now: datetime | None = None,
) -> BookingResult:
    """
    Move an appointment.

    Requirement 17's critical clause: **if the provider update fails, the
    existing appointment must remain intact.** So the row is not touched until
    the provider has accepted, and the local fields are captured beforehand so
    nothing half-writes.
    """
    started = _time.perf_counter()
    moment = now or now_utc()
    rules = await get_rules(session, tenant)
    zone = get_zone(rules.timezone)

    if appointment.status in (AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW):
        return BookingResult(
            outcome=BookingOutcome.NOT_FOUND,
            message="That appointment isn't active any more.",
        )

    start = as_utc(new_start)
    window = TimeWindow(start=start, end=start + rules.duration)

    rejection = _check_policy(rules, window, moment, zone)
    if rejection is not None:
        return rejection

    guarded = window.expanded(before=rules.buffer_before, after=rules.buffer_after)
    if await _booked_windows(session, tenant.id, guarded, exclude_id=appointment.id):
        return BookingResult(
            outcome=BookingOutcome.CONFLICT,
            message="That time is already taken. Can I offer you another?",
        )

    integration, provider = await _resolve_provider(session, tenant)

    if not appointment.external_event_id:
        # Never confirmed with a provider. Moving the local row is all there
        # is to do, and pretending we updated a calendar would be a lie.
        return await _apply_reschedule(
            session, tenant, appointment, window, rules, zone, event=None,
            reason=reason, started=started, now=moment,
        )

    event_request = EventRequest(
        title=appointment.reason or "Appointment",
        start=window.start,
        end=window.end,
        timezone=rules.timezone,
        attendee=Attendee(
            name=appointment.customer_name,
            phone=appointment.customer_phone,
            email=appointment.attendee_email,
        ),
        calendar_reference=appointment.calendar_reference,
        idempotency_key=appointment.idempotency_key,
    )

    try:
        if hasattr(provider, "reschedule"):
            # Cal.com has a real reschedule endpoint that moves the booking
            # atomically on its side. Better than cancel-then-rebook, where a
            # failure between the two leaves the customer with nothing.
            event = await provider.reschedule(
                appointment.external_event_id, window.start, reason=reason
            )
        elif provider.supports(CalendarCapability.UPDATE_EVENT):
            event = await provider.update_event(
                appointment.external_event_id, event_request
            )
        else:
            raise CalendarUnsupportedError(
                f"{provider.name} cannot reschedule", provider=provider.name
            )
    except CalendarConflictError:
        return BookingResult(
            outcome=BookingOutcome.CONFLICT,
            message="That time was just taken. Can I offer you another?",
        )
    except CalendarError as exc:
        # The appointment is untouched. Nothing was written, so the customer
        # still has their original slot.
        log.warning(
            "calendar.reschedule_failed", tenant_id=str(tenant.id),
            appointment_id=str(appointment.id), error_code=exc.code,
            error=exc.safe_message, outcome="failed",
        )
        return BookingResult(
            outcome=BookingOutcome.FAILED,
            message=(
                "I couldn't move that just now, so I've left your original "
                "time as it is. Someone will call you back."
            ),
            appointment_id=str(appointment.id),
        )

    return await _apply_reschedule(
        session, tenant, appointment, window, rules, zone, event=event,
        reason=reason, started=started, now=moment,
    )


async def _apply_reschedule(
    session, tenant, appointment, window, rules, zone, *, event, reason, started,
    now: datetime | None = None,
) -> BookingResult:
    previous_start = as_utc(appointment.starts_at)

    appointment.rescheduled_from = previous_start
    appointment.starts_at = window.start
    appointment.ends_at = window.end
    appointment.timezone = rules.timezone
    appointment.status = AppointmentStatus.RESCHEDULED
    appointment.slot_key = slot_key(str(tenant.id), window.start, window.end)
    appointment.last_error = None
    if event is not None:
        appointment.external_event_id = event.external_id
        appointment.meeting_url = event.meeting_url or appointment.meeting_url

    from app.integrations.crm import hooks as crm_hooks

    await session.flush()
    await crm_hooks.on_appointment_rescheduled(
        session, appointment, previous_start=previous_start
    )
    await _schedule_reminder(session, tenant, appointment, replace=True, now=now)
    await session.commit()

    log.info(
        "calendar.rescheduled", tenant_id=str(tenant.id),
        appointment_id=str(appointment.id),
        duration_ms=round((_time.perf_counter() - started) * 1000, 2),
        outcome="rescheduled",
    )
    return BookingResult(
        outcome=BookingOutcome.RESCHEDULED,
        message=(
            f"Moved to {format_spoken_date(window.start, zone)} at "
            f"{format_spoken(window.start, zone)}."
        ),
        appointment_id=str(appointment.id),
    )


# ------------------------------------------------------------------ cancel ---

async def cancel(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    *,
    reason: str = "",
    cancelled_by: str = "customer",
) -> BookingResult:
    """
    Cancel, idempotently.

    Requirement 18: cancelling an already-cancelled appointment must not hit
    the provider again. The early return is not just an optimisation — a
    second cancel on Google is a 404 and on Cal.com a 400, and turning those
    into user-visible errors would make a harmless double-click look broken.
    """
    if appointment.status is AppointmentStatus.CANCELLED:
        return BookingResult(
            outcome=BookingOutcome.CANCELLED,
            message="That appointment is already cancelled.",
            appointment_id=str(appointment.id),
        )

    integration, provider = await _resolve_provider(session, tenant)

    if appointment.external_event_id and provider.supports(
        CalendarCapability.CANCEL_EVENT
    ):
        try:
            await provider.cancel_event(
                appointment.external_event_id,
                reason=reason,
                calendar_reference=appointment.calendar_reference,
            )
        except CalendarError as exc:
            # The local state is still moved to CANCELLED. The customer said
            # cancel; refusing to record that because a vendor API was slow
            # would mean the slot stays blocked and the business turns away
            # the next caller. The error is kept for staff to reconcile.
            log.warning(
                "calendar.cancel_provider_failed", tenant_id=str(tenant.id),
                appointment_id=str(appointment.id), error_code=exc.code,
                error=exc.safe_message,
            )
            appointment.last_error = safe_message(str(exc))[:500]

    appointment.status = AppointmentStatus.CANCELLED
    appointment.cancelled_at = now_utc()
    appointment.cancellation_reason = (reason or "")[:500] or None
    appointment.cancelled_by = cancelled_by[:64]
    # Free the slot lock so the time can be rebooked; the unique constraint
    # treats NULLs as distinct, so this does not collide with anything.
    appointment.slot_key = None

    from app.integrations.crm import hooks as crm_hooks

    await session.flush()
    await crm_hooks.on_appointment_cancelled(session, appointment)
    await _cancel_reminders(session, appointment)
    await session.commit()

    log.info(
        "calendar.cancelled", tenant_id=str(tenant.id),
        appointment_id=str(appointment.id), cancelled_by=cancelled_by,
        outcome="cancelled",
    )
    return BookingResult(
        outcome=BookingOutcome.CANCELLED,
        message="That's cancelled for you.",
        appointment_id=str(appointment.id),
    )


async def mark_no_show(
    session: AsyncSession, appointment: Appointment
) -> BookingResult:
    appointment.status = AppointmentStatus.NO_SHOW
    appointment.slot_key = None
    await session.commit()
    return BookingResult(
        outcome=BookingOutcome.CONFIRMED,
        message="Marked as a no-show.",
        appointment_id=str(appointment.id),
    )


# --------------------------------------------------------------- reminders ---

async def _schedule_reminder(
    session: AsyncSession, tenant: Tenant, appointment: Appointment,
    *, replace: bool = False, now: datetime | None = None,
) -> None:
    """
    Queue the reminder, idempotently.

    Requirement 20: prevent duplicate reminders. Rescheduling replaces any
    unsent reminder rather than adding a second one — the audit found no
    dedupe at all, so a rescheduled appointment would have produced two.

    Uses `now_utc()` rather than `datetime.utcnow()`. That is the audit's F3:
    the old comparison relabelled a naive UTC clock with the appointment's
    timezone and silently dropped every reminder due within the offset.
    """
    from app.db.models import Reminder

    if not getattr(tenant, "reminder_enabled", False):
        return

    existing = (
        (
            await session.execute(
                select(Reminder).where(
                    Reminder.tenant_id == tenant.id,
                    Reminder.appointment_id == appointment.id,
                    Reminder.sent.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )

    send_at = as_utc(appointment.starts_at) - timedelta(
        hours=int(getattr(tenant, "reminder_hours_before", 24) or 24)
    )
    # The *caller's* clock, not a second reading of the wall clock. A service
    # with two notions of "now" will eventually disagree with itself, and here
    # it would mean a booking accepted as future having its reminder dropped
    # as past.
    if send_at <= (now or now_utc()):
        # Too late to be useful. Drop any stale one from a previous time.
        for reminder in existing if replace else []:
            await session.delete(reminder)
        return

    if existing:
        if not replace:
            return
        for reminder in existing[1:]:
            await session.delete(reminder)
        existing[0].send_at = send_at
        return

    session.add(Reminder(
        tenant_id=tenant.id, appointment_id=appointment.id,
        channel="sms", send_at=send_at,
    ))


async def _cancel_reminders(session: AsyncSession, appointment: Appointment) -> None:
    """A cancelled appointment must not still text the customer."""
    from app.db.models import Reminder

    rows = (
        (
            await session.execute(
                select(Reminder).where(
                    Reminder.appointment_id == appointment.id,
                    Reminder.sent.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    for reminder in rows:
        await session.delete(reminder)


# ------------------------------------------------------------------ health ---

async def check_health(
    session: AsyncSession, tenant: Tenant, integration: CalendarIntegration
) -> HealthResult:
    """Always returns a normalized result; never raises."""
    try:
        provider = build_adapter(integration, tenant)
        result = await provider.health_check()
    except CalendarError as exc:
        result = HealthResult(
            connected=False, provider=integration.provider.value,
            latency_ms=0.0, safe_message=exc.safe_message,
        )
    except Exception as exc:
        result = HealthResult(
            connected=False, provider=integration.provider.value, latency_ms=0.0,
            safe_message=f"unexpected error ({type(exc).__name__})",
        )

    integration.last_health_check_at = now_utc()
    integration.last_health_ok = result.connected
    integration.last_error = None if result.connected else result.safe_message[:500]
    await session.commit()
    return result


# ----------------------------------------------------------------- helpers ---

def _event_title(tenant: Tenant, request: BookingRequest) -> str:
    who = request.customer_name or "Caller"
    what = request.reason or "Appointment"
    return f"{who} - {what}"


def _event_description(request: BookingRequest) -> str:
    lines = ["Booked by VoxDesk AI"]
    if request.customer_phone:
        lines.append(f"Phone: {request.customer_phone}")
    if request.reason:
        lines.append(f"Reason: {request.reason}")
    return "\n".join(lines)
