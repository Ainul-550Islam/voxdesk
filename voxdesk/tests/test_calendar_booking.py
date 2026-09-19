"""
Business hours, availability, booking, concurrency, reschedule and cancel.

The heart of STEP 6. Requirement 8 in particular — *"Caller A and Caller B ask
for Tuesday 3 PM at nearly the same moment; only one gets CONFIRMED"* — is
tested against the real database with real concurrent sessions, not by mocking
a lock.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import (
    Appointment,
    AppointmentStatus,
    CalendarProviderType,
    Reminder,
)
from app.integrations.calendar import policy as policy_engine
from app.integrations.calendar import service
from app.integrations.calendar.errors import (
    CalendarAuthError,
    CalendarConflictError,
    CalendarTemporaryError,
    CalendarTimeout,
)
from app.integrations.calendar.models import (
    BookingOutcome,
    BusyPeriod,
    CalendarEvent,
    TimeWindow,
)
from app.integrations.calendar.timezones import as_utc, resolve_local, to_local
from tests.conftest import make_calendar_integration, make_scheduling_policy

NY = "America/New_York"

#: A Tuesday well clear of any DST boundary.
TUESDAY = date(2026, 6, 16)


def ny(day: date, hour: int, minute: int = 0) -> datetime:
    return resolve_local(datetime.combine(day, time(hour, minute)), NY).utc


@pytest.fixture
async def tenant(db, tenant_a):
    tenant_a.timezone = NY
    await db.commit()
    return tenant_a


@pytest.fixture
async def rules(db, tenant):
    await make_scheduling_policy(db, tenant)
    return await service.get_rules(db, tenant)


@pytest.fixture
async def internal(db, tenant):
    return await make_calendar_integration(db, tenant, "internal")


def booking(tenant, start, **over) -> service.BookingRequest:
    fields = dict(
        tenant_id=tenant.id, start=start,
        customer_name="Jane Doe", customer_phone="+15551230000",
    )
    fields.update(over)
    return service.BookingRequest(**fields)


# ======================================================== business hours ===

class TestBusinessHours:
    async def test_a_closed_day_offers_nothing(self, db, tenant, rules):
        saturday = date(2026, 6, 20)
        assert rules.is_open_on(saturday) is False
        assert policy_engine.candidate_slots(
            rules, saturday, now=ny(TUESDAY, 8)
        ) == []

    async def test_an_open_day_offers_slots(self, db, tenant, rules):
        slots = policy_engine.candidate_slots(rules, TUESDAY, now=ny(TUESDAY, 6))
        assert slots
        assert all(slot.timezone == NY for slot in slots)

    async def test_a_lunch_break_is_respected(self, db, tenant, rules):
        """
        The single most common reason a booking lands when nobody is there.
        One open/close pair per day cannot express it, which is why the policy
        stores a list of intervals.
        """
        slots = policy_engine.candidate_slots(rules, TUESDAY, now=ny(TUESDAY, 6))
        local_hours = {to_local(slot.start, NY).hour for slot in slots}

        assert 12 not in local_hours
        assert 11 in local_hours and 13 in local_hours

    async def test_a_holiday_closes_the_whole_day(self, db, tenant):
        await make_scheduling_policy(db, tenant, holidays=[TUESDAY.isoformat()])
        rules = await service.get_rules(db, tenant)
        assert policy_engine.candidate_slots(rules, TUESDAY, now=ny(TUESDAY, 6)) == []

    async def test_a_blocked_period_removes_only_its_own_slots(self, db, tenant):
        await make_scheduling_policy(
            db, tenant,
            blocked_periods=[{
                "start": f"{TUESDAY}T09:00:00", "end": f"{TUESDAY}T11:00:00"
            }],
        )
        rules = await service.get_rules(db, tenant)
        hours = {
            to_local(slot.start, NY).hour
            for slot in policy_engine.candidate_slots(rules, TUESDAY, now=ny(TUESDAY, 6))
        }
        assert 9 not in hours and 10 not in hours
        assert 11 in hours

    async def test_booking_outside_business_hours_is_refused(
        self, db, tenant, rules, internal
    ):
        result = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 20)), now=ny(TUESDAY, 8)
        )
        assert result.outcome is BookingOutcome.OUTSIDE_HOURS
        assert "open" in result.message.lower() or "closed" in result.message.lower()

    async def test_booking_during_the_lunch_break_is_refused(
        self, db, tenant, rules, internal
    ):
        result = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 12, 15)), now=ny(TUESDAY, 8)
        )
        assert result.outcome is BookingOutcome.OUTSIDE_HOURS

    async def test_an_appointment_spanning_the_break_is_refused(self, db, tenant):
        """
        "Mostly open" is a customer sitting in an empty waiting room. The
        appointment must fit entirely inside one interval.
        """
        await make_scheduling_policy(db, tenant, slot_minutes=60)
        rules = await service.get_rules(db, tenant)
        window = TimeWindow(start=ny(TUESDAY, 11, 30), end=ny(TUESDAY, 12, 30))
        assert policy_engine.is_within_business_hours(rules, window) is False

    async def test_a_closed_day_is_refused_with_a_useful_message(
        self, db, tenant, rules, internal
    ):
        saturday = date(2026, 6, 20)
        result = await service.book(
            db, tenant, booking(tenant, ny(saturday, 10)), now=ny(TUESDAY, 8)
        )
        assert result.outcome is BookingOutcome.OUTSIDE_HOURS
        assert "Saturday" in result.message

    async def test_the_escape_hatch_allows_out_of_hours_booking(
        self, db, tenant, internal
    ):
        await make_scheduling_policy(db, tenant, allow_outside_business_hours=True)
        result = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 20)), now=ny(TUESDAY, 8)
        )
        assert result.outcome is BookingOutcome.BOOKED

    async def test_a_tenant_with_no_policy_keeps_the_legacy_behaviour(
        self, db, tenant
    ):
        """
        Backward compatibility: the pre-STEP-6 `Tenant.business_open` /
        `business_close` columns still drive scheduling for anyone who has not
        configured a policy.
        """
        tenant.business_open = time(8, 0)
        tenant.business_close = time(10, 0)
        tenant.appointment_minutes = 30
        await db.commit()

        rules = await service.get_rules(db, tenant)
        hours = sorted(
            to_local(slot.start, NY).hour
            for slot in policy_engine.candidate_slots(rules, TUESDAY, now=ny(TUESDAY, 5))
        )
        assert hours == [8, 8, 9, 9]

    async def test_an_invalid_policy_is_rejected_when_parsed(self):
        with pytest.raises(policy_engine.PolicyError):
            policy_engine.parse_weekly_hours({"mon": [["17:00", "09:00"]]})
        with pytest.raises(policy_engine.PolicyError):
            policy_engine.parse_weekly_hours(
                {"mon": [["09:00", "12:00"], ["11:00", "15:00"]]}
            )
        with pytest.raises(policy_engine.PolicyError):
            policy_engine.parse_weekly_hours({"funday": [["09:00", "17:00"]]})


# =========================================================== availability ===

class TestAvailability:
    async def test_provider_busy_periods_remove_slots(self, db, tenant, rules):
        busy = [BusyPeriod(start=ny(TUESDAY, 9), end=ny(TUESDAY, 11))]
        slots = policy_engine.candidate_slots(rules, TUESDAY, now=ny(TUESDAY, 6))
        surviving = policy_engine.filter_slots(slots, rules=rules, busy=busy)

        hours = {to_local(slot.start, NY).hour for slot in surviving}
        assert 9 not in hours and 10 not in hours
        assert 11 in hours

    async def test_our_own_appointments_remove_slots(
        self, db, tenant, rules, internal
    ):
        """
        The pre-STEP-6 code never consulted this table, so two callers could
        book the same slot whenever the provider had not yet propagated the
        first event.
        """
        await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 10)), now=ny(TUESDAY, 6)
        )
        slots, _ = await service.find_slots(
            db, tenant, day=TUESDAY, limit=50, now=ny(TUESDAY, 6)
        )
        assert ny(TUESDAY, 10) not in {slot.start for slot in slots}

    async def test_a_cancelled_appointment_frees_its_slot(
        self, db, tenant, rules, internal
    ):
        result = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 10)), now=ny(TUESDAY, 6)
        )
        appointment = await db.get(Appointment, uuid.UUID(result.appointment_id))
        await service.cancel(db, tenant, appointment)

        slots, _ = await service.find_slots(
            db, tenant, day=TUESDAY, limit=50, now=ny(TUESDAY, 6)
        )
        assert ny(TUESDAY, 10) in {slot.start for slot in slots}

    async def test_buffers_widen_what_a_booking_blocks(self, db, tenant):
        await make_scheduling_policy(
            db, tenant, buffer_before_minutes=15, buffer_after_minutes=15
        )
        rules = await service.get_rules(db, tenant)
        booked = [TimeWindow(start=ny(TUESDAY, 10), end=ny(TUESDAY, 10, 30))]

        slots = policy_engine.candidate_slots(rules, TUESDAY, now=ny(TUESDAY, 6))
        surviving = policy_engine.filter_slots(slots, rules=rules, booked=booked)
        starts = {slot.start for slot in surviving}

        # 09:30-10:00 now collides through the buffer, and so does 10:30-11:00.
        assert ny(TUESDAY, 9, 30) not in starts
        assert ny(TUESDAY, 10, 30) not in starts
        assert ny(TUESDAY, 9) in starts and ny(TUESDAY, 11) in starts

    async def test_back_to_back_slots_do_not_collide_without_a_buffer(
        self, db, tenant, rules
    ):
        """
        Half-open intervals. Closed comparison would make every consecutive
        slot conflict with its neighbour and silently halve capacity.
        """
        booked = [TimeWindow(start=ny(TUESDAY, 10), end=ny(TUESDAY, 10, 30))]
        slots = policy_engine.candidate_slots(rules, TUESDAY, now=ny(TUESDAY, 6))
        starts = {
            slot.start
            for slot in policy_engine.filter_slots(slots, rules=rules, booked=booked)
        }
        assert ny(TUESDAY, 10, 30) in starts
        assert ny(TUESDAY, 9, 30) in starts

    async def test_minimum_notice_hides_imminent_slots(self, db, tenant):
        await make_scheduling_policy(db, tenant, minimum_notice_minutes=120)
        rules = await service.get_rules(db, tenant)

        slots = policy_engine.candidate_slots(rules, TUESDAY, now=ny(TUESDAY, 9, 30))
        assert all(slot.start >= ny(TUESDAY, 11, 30) for slot in slots)

    async def test_the_booking_horizon_is_enforced(self, db, tenant, internal):
        await make_scheduling_policy(db, tenant, booking_horizon_days=7)
        far = ny(date(2027, 6, 15), 10)
        result = await service.book(
            db, tenant, booking(tenant, far), now=ny(TUESDAY, 8)
        )
        assert result.outcome is BookingOutcome.TOO_FAR

    async def test_a_past_time_is_refused(self, db, tenant, rules, internal):
        result = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 9)), now=ny(TUESDAY, 14)
        )
        assert result.outcome is BookingOutcome.TOO_SOON
        assert "passed" in result.message.lower()

    async def test_slot_interval_can_be_finer_than_duration(self, db, tenant):
        """
        A 60-minute appointment offered on a 30-minute grid gives twice the
        choice. The pre-STEP-6 code used one number for both.
        """
        await make_scheduling_policy(
            db, tenant, slot_minutes=60, slot_interval_minutes=30
        )
        rules = await service.get_rules(db, tenant)
        slots = policy_engine.candidate_slots(rules, TUESDAY, now=ny(TUESDAY, 6))
        starts = {to_local(slot.start, NY).strftime("%H:%M") for slot in slots}
        assert "09:00" in starts and "09:30" in starts
        assert all(slot.window.duration == timedelta(hours=1) for slot in slots)

    async def test_max_slots_offered_is_respected(self, db, tenant, internal):
        await make_scheduling_policy(db, tenant, max_slots_offered=2)
        slots, _ = await service.find_slots(db, tenant, day=TUESDAY, now=ny(TUESDAY, 6))
        assert len(slots) == 2

    async def test_morning_filter_uses_local_time(self, db, tenant, rules):
        """
        9 AM in New York is 13:00 UTC. Filtering on the UTC hour would call it
        an afternoon slot.
        """
        slots = policy_engine.candidate_slots(rules, TUESDAY, now=ny(TUESDAY, 6))
        morning = policy_engine.part_of_day_filter(slots, "morning", NY)
        assert morning
        assert all(to_local(slot.start, NY).hour < 12 for slot in morning)

    async def test_an_unreachable_provider_is_reported_not_hidden(
        self, db, tenant, rules, monkeypatch
    ):
        """
        **The audit's F2.** The old client returned `[]` on any exception, so
        an outage looked like total availability. `find_slots` must say so.
        """
        await make_calendar_integration(db, tenant, "google")

        async def boom(self, window):
            raise CalendarTemporaryError("provider down", provider="google")

        from app.integrations.calendar.providers.google import GoogleCalendarProvider
        monkeypatch.setattr(GoogleCalendarProvider, "get_busy", boom)

        slots, degraded = await service.find_slots(
            db, tenant, day=TUESDAY, now=ny(TUESDAY, 6)
        )
        assert degraded == "temporary"


# ================================================================ booking ===

class TestBooking:
    async def test_a_successful_booking_writes_exact_state(
        self, db, tenant, rules, internal
    ):
        start = ny(TUESDAY, 10)
        result = await service.book(
            db, tenant, booking(tenant, start, reason="Cleaning"), now=ny(TUESDAY, 6)
        )
        assert result.outcome is BookingOutcome.BOOKED
        assert "10 am" in result.message

        appointment = await db.get(Appointment, uuid.UUID(result.appointment_id))
        assert appointment.status is AppointmentStatus.CONFIRMED
        assert appointment.external_event_id            # proof of acceptance
        assert appointment.confirmed_at is not None
        assert appointment.provider is CalendarProviderType.INTERNAL
        assert appointment.timezone == NY
        assert as_utc(appointment.starts_at) == start
        assert as_utc(appointment.ends_at) == start + timedelta(minutes=30)
        assert appointment.slot_key
        assert appointment.idempotency_key
        assert appointment.last_error is None

    async def test_a_provider_rejection_never_reports_success(
        self, db, tenant, rules, monkeypatch
    ):
        """
        **The audit's F1, and the brief's headline rule.** The old code wrote
        the row and said "Booked" whatever the provider did.
        """
        await make_calendar_integration(db, tenant, "google")

        async def refuse(self, request):
            raise CalendarAuthError("token revoked", provider="google")

        from app.integrations.calendar.providers.google import GoogleCalendarProvider
        monkeypatch.setattr(GoogleCalendarProvider, "create_event", refuse)

        result = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 10)), now=ny(TUESDAY, 6)
        )

        assert result.outcome is BookingOutcome.FAILED
        assert "booked" not in result.message.lower()
        # ...and the caller is never told which provider or why.
        assert "google" not in result.message.lower()
        assert "401" not in result.message and "token" not in result.message.lower()

        stored = (
            (await db.execute(select(Appointment).where(
                Appointment.tenant_id == tenant.id
            ))).scalars().all()
        )
        assert len(stored) == 1
        assert stored[0].status is AppointmentStatus.FAILED
        assert stored[0].external_event_id is None

    async def test_a_provider_conflict_offers_another_time(
        self, db, tenant, rules, monkeypatch
    ):
        await make_calendar_integration(db, tenant, "google")

        async def conflict(self, request):
            raise CalendarConflictError("taken", provider="google")

        from app.integrations.calendar.providers.google import GoogleCalendarProvider
        monkeypatch.setattr(GoogleCalendarProvider, "create_event", conflict)

        result = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 10)), now=ny(TUESDAY, 6)
        )
        assert result.outcome is BookingOutcome.CONFLICT
        assert "another" in result.message.lower()

    async def test_a_provisional_booking_is_opt_in_and_never_says_confirmed(
        self, db, tenant, monkeypatch
    ):
        await make_scheduling_policy(db, tenant, require_provider_confirmation=False)
        await make_calendar_integration(db, tenant, "google")

        async def boom(self, request):
            raise CalendarTemporaryError("down", provider="google")

        from app.integrations.calendar.providers.google import GoogleCalendarProvider
        monkeypatch.setattr(GoogleCalendarProvider, "create_event", boom)

        result = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 10)), now=ny(TUESDAY, 6)
        )
        assert result.details.get("provisional") is True
        assert "confirm" in result.message.lower()

        appointment = await db.get(Appointment, uuid.UUID(result.appointment_id))
        # PENDING, never CONFIRMED -- nothing is on a calendar.
        assert appointment.status is AppointmentStatus.PENDING
        assert appointment.external_event_id is None

    async def test_a_conflicting_booking_is_refused(
        self, db, tenant, rules, internal
    ):
        start = ny(TUESDAY, 10)
        first = await service.book(db, tenant, booking(tenant, start), now=ny(TUESDAY, 6))
        assert first.outcome is BookingOutcome.BOOKED

        second = await service.book(
            db, tenant,
            booking(tenant, start, customer_phone="+15559998888", customer_name="Bob"),
            now=ny(TUESDAY, 6),
        )
        assert second.outcome is BookingOutcome.CONFLICT

    async def test_the_crm_event_is_emitted_through_step_5(
        self, db, tenant, rules, internal
    ):
        """
        Requirement 22: the calendar layer emits an internal business event
        and the CRM service consumes it. No calendar module calls GHL.
        """
        from app.db.models import CrmEvent, CrmEventType

        await service.book(db, tenant, booking(tenant, ny(TUESDAY, 10)), now=ny(TUESDAY, 6))

        events = (
            (await db.execute(select(CrmEvent).where(
                CrmEvent.tenant_id == tenant.id
            ))).scalars().all()
        )
        assert [e.event_type for e in events] == [CrmEventType.APPOINTMENT_BOOKED]

    async def test_no_calendar_module_imports_a_crm_provider(self):
        """
        Structural. Requirement 22 forbids calling GHL/HubSpot/Jobber from a
        calendar provider module; asserted against the source so it cannot be
        added later by accident.
        """
        import pathlib

        root = pathlib.Path("app/integrations/calendar")
        for path in root.rglob("*.py"):
            source = path.read_text()
            for forbidden in ("providers.ghl", "providers.hubspot", "providers.jobber"):
                assert forbidden not in source, f"{path} reaches into {forbidden}"


# ============================================================ idempotency ===

class TestIdempotency:
    async def test_the_same_request_twice_books_once(
        self, db, tenant, rules, internal
    ):
        start = ny(TUESDAY, 10)
        first = await service.book(db, tenant, booking(tenant, start), now=ny(TUESDAY, 6))
        second = await service.book(db, tenant, booking(tenant, start), now=ny(TUESDAY, 6))

        assert first.outcome is BookingOutcome.BOOKED
        assert second.outcome is BookingOutcome.ALREADY_BOOKED
        assert second.appointment_id == first.appointment_id

        total = (
            await db.execute(select(func.count(Appointment.id)).where(
                Appointment.tenant_id == tenant.id
            ))
        ).scalar()
        assert total == 1

    async def test_the_key_is_derived_not_random(self, db, tenant):
        """
        Requirement 9: a fresh UUID per retry is not duplicate protection, it
        is the opposite -- every retry would look like a new booking.
        """
        from app.integrations.calendar.models import booking_idempotency_key

        start = ny(TUESDAY, 10)
        first = booking_idempotency_key(str(tenant.id), start, phone="+1 (555) 123-0000")
        second = booking_idempotency_key(str(tenant.id), start, phone="+15551230000")
        assert first == second      # same caller, normalized

        other = booking_idempotency_key(str(tenant.id), start, phone="+15559990000")
        assert other != first

    async def test_a_client_request_id_overrides_the_derived_key(self, db, tenant):
        from app.integrations.calendar.models import booking_idempotency_key

        start = ny(TUESDAY, 10)
        assert booking_idempotency_key(
            str(tenant.id), start, phone="+15551230000", request_id="req-1"
        ) != booking_idempotency_key(
            str(tenant.id), start, phone="+15551230000", request_id="req-2"
        )

    async def test_an_ambiguous_timeout_reconciles_instead_of_duplicating(
        self, db, tenant, rules, monkeypatch
    ):
        """
        **The hardest case.** The provider accepted the event and then the
        response timed out. A blind retry creates a second calendar entry;
        reconciliation finds the first.
        """
        await make_calendar_integration(db, tenant, "google")
        start = ny(TUESDAY, 10)
        calls = {"create": 0, "find": 0}

        from app.integrations.calendar.providers.google import GoogleCalendarProvider

        async def timeout(self, request):
            calls["create"] += 1
            raise CalendarTimeout("no response", provider="google")

        async def found(self, key, window):
            calls["find"] += 1
            return CalendarEvent(
                external_id="gcal-already-there",
                start=window.start, end=window.end, already_existed=True,
            )

        monkeypatch.setattr(GoogleCalendarProvider, "create_event", timeout)
        monkeypatch.setattr(GoogleCalendarProvider, "find_event_by_key", found)

        result = await service.book(db, tenant, booking(tenant, start), now=ny(TUESDAY, 6))

        assert result.outcome is BookingOutcome.BOOKED
        assert calls == {"create": 1, "find": 1}

        appointment = await db.get(Appointment, uuid.UUID(result.appointment_id))
        assert appointment.external_event_id == "gcal-already-there"
        assert appointment.status is AppointmentStatus.CONFIRMED

    async def test_a_timeout_whose_reconciliation_also_fails_does_not_book(
        self, db, tenant, rules, monkeypatch
    ):
        """
        "I could not check" is not evidence of absence. Treating it as such
        would license exactly the duplicate the reconciliation exists to
        prevent.
        """
        await make_calendar_integration(db, tenant, "google")

        from app.integrations.calendar.providers.google import GoogleCalendarProvider

        async def timeout(self, request):
            raise CalendarTimeout("no response", provider="google")

        async def cannot_check(self, key, window):
            raise CalendarTemporaryError("also down", provider="google")

        monkeypatch.setattr(GoogleCalendarProvider, "create_event", timeout)
        monkeypatch.setattr(GoogleCalendarProvider, "find_event_by_key", cannot_check)

        result = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 10)), now=ny(TUESDAY, 6)
        )
        assert result.outcome is BookingOutcome.FAILED

        appointment = (
            (await db.execute(select(Appointment))).scalars().one()
        )
        assert appointment.status is AppointmentStatus.FAILED
        assert appointment.external_event_id is None

    async def test_a_timeout_with_nothing_on_the_provider_does_not_confirm(
        self, db, tenant, rules, monkeypatch
    ):
        await make_calendar_integration(db, tenant, "google")

        from app.integrations.calendar.providers.google import GoogleCalendarProvider

        async def timeout(self, request):
            raise CalendarTimeout("no response", provider="google")

        async def absent(self, key, window):
            return None

        monkeypatch.setattr(GoogleCalendarProvider, "create_event", timeout)
        monkeypatch.setattr(GoogleCalendarProvider, "find_event_by_key", absent)

        result = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 10)), now=ny(TUESDAY, 6)
        )
        assert result.outcome is BookingOutcome.FAILED

    async def test_rebooking_a_cancelled_slot_is_allowed(
        self, db, tenant, rules, internal
    ):
        """
        Idempotency must not turn into a permanent block: a customer who
        cancels and calls back should be able to take the same slot again.
        """
        start = ny(TUESDAY, 10)
        first = await service.book(db, tenant, booking(tenant, start), now=ny(TUESDAY, 6))
        appointment = await db.get(Appointment, uuid.UUID(first.appointment_id))
        await service.cancel(db, tenant, appointment)

        second = await service.book(
            db, tenant, booking(tenant, start, request_id="second-attempt"),
            now=ny(TUESDAY, 6),
        )
        assert second.outcome is BookingOutcome.BOOKED
        assert second.appointment_id != first.appointment_id


# ============================================================ concurrency ===

class TestConcurrency:
    @staticmethod
    async def _seed(maker):
        """Tenant, policy and calendar in the concurrency database."""
        from app.db.models import Tenant

        async with maker() as session:
            tenant = Tenant(
                name="Race Clinic", twilio_number="+15550009999", timezone=NY
            )
            session.add(tenant)
            await session.commit()
            await session.refresh(tenant)
            await make_scheduling_policy(session, tenant)
            await make_calendar_integration(session, tenant, "internal")
            return tenant.id

    @staticmethod
    async def _attempt(maker, tenant_id, start, phone, name):
        from app.db.models import Tenant

        async with maker() as session:
            tenant = await session.get(Tenant, tenant_id)
            return await service.book(
                session, tenant,
                service.BookingRequest(
                    tenant_id=tenant_id, start=start,
                    customer_name=name, customer_phone=phone,
                ),
                now=ny(TUESDAY, 6),
            )

    async def test_two_callers_racing_for_one_slot_produce_one_booking(
        self, concurrent_sessionmaker
    ):
        """
        **Requirement 8.** Two genuinely concurrent sessions on separate
        connections, the real database, no mocking of the lock.

        Exactly one must be CONFIRMED, and the loser must get a safe
        alternative response rather than an exception.
        """
        maker = concurrent_sessionmaker
        tenant_id = await self._seed(maker)
        start = ny(TUESDAY, 10)

        results = await asyncio.gather(
            self._attempt(maker, tenant_id, start, "+15550000001", "Caller A"),
            self._attempt(maker, tenant_id, start, "+15550000002", "Caller B"),
            return_exceptions=True,
        )

        raised = [r for r in results if isinstance(r, BaseException)]
        assert not raised, f"a caller saw an exception: {raised}"

        outcomes = [r.outcome for r in results]
        assert outcomes.count(BookingOutcome.BOOKED) == 1, outcomes
        assert BookingOutcome.CONFLICT in outcomes

        async with maker() as session:
            confirmed = (
                await session.execute(
                    select(func.count(Appointment.id)).where(
                        Appointment.tenant_id == tenant_id,
                        Appointment.status == AppointmentStatus.CONFIRMED,
                    )
                )
            ).scalar()
        assert confirmed == 1

    async def test_five_callers_racing_still_produce_one_booking(
        self, concurrent_sessionmaker
    ):
        maker = concurrent_sessionmaker
        tenant_id = await self._seed(maker)
        start = ny(TUESDAY, 11)

        results = await asyncio.gather(
            *(
                self._attempt(
                    maker, tenant_id, start, f"+1555000{i:04d}", f"Caller {i}"
                )
                for i in range(5)
            ),
            return_exceptions=True,
        )
        raised = [r for r in results if isinstance(r, BaseException)]
        assert not raised, f"a caller saw an exception: {raised}"

        outcomes = [r.outcome for r in results]
        assert outcomes.count(BookingOutcome.BOOKED) == 1, outcomes

        async with maker() as session:
            confirmed = (
                await session.execute(
                    select(func.count(Appointment.id)).where(
                        Appointment.status == AppointmentStatus.CONFIRMED
                    )
                )
            ).scalar()
        assert confirmed == 1

    async def test_the_slot_key_is_the_lock(self, db, tenant):
        """
        The guarantee is a database constraint, not application logic. Two
        callers compute the same key for the same interval.
        """
        from app.integrations.calendar.models import slot_key

        start, end = ny(TUESDAY, 10), ny(TUESDAY, 10, 30)
        assert slot_key(str(tenant.id), start, end) == slot_key(
            str(tenant.id), start, end
        )
        # ...but not across tenants, and not across times.
        assert slot_key(str(tenant.id), start, end) != slot_key(
            str(uuid.uuid4()), start, end
        )
        assert slot_key(str(tenant.id), start, end) != slot_key(
            str(tenant.id), ny(TUESDAY, 11), ny(TUESDAY, 11, 30)
        )

    async def test_different_slots_do_not_block_each_other(
        self, db, tenant, rules, internal
    ):
        for hour in (9, 10, 11):
            result = await service.book(
                db, tenant,
                booking(tenant, ny(TUESDAY, hour), customer_phone=f"+1555000000{hour}"),
                now=ny(TUESDAY, 6),
            )
            assert result.outcome is BookingOutcome.BOOKED, hour


# ============================================================= reschedule ===

class TestReschedule:
    @pytest.fixture
    async def appointment(self, db, tenant, rules, internal):
        result = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 10)), now=ny(TUESDAY, 6)
        )
        return await db.get(Appointment, uuid.UUID(result.appointment_id))

    async def test_a_successful_reschedule_moves_everything(
        self, db, tenant, appointment
    ):
        original = as_utc(appointment.starts_at)
        result = await service.reschedule(
            db, tenant, appointment, ny(TUESDAY, 14), now=ny(TUESDAY, 6)
        )
        assert result.outcome is BookingOutcome.RESCHEDULED

        await db.refresh(appointment)
        assert as_utc(appointment.starts_at) == ny(TUESDAY, 14)
        assert as_utc(appointment.rescheduled_from) == original
        assert appointment.status is AppointmentStatus.RESCHEDULED

    async def test_a_conflict_leaves_the_original_alone(
        self, db, tenant, rules, internal, appointment
    ):
        await service.book(
            db, tenant,
            booking(tenant, ny(TUESDAY, 14), customer_phone="+15559990000"),
            now=ny(TUESDAY, 6),
        )
        result = await service.reschedule(
            db, tenant, appointment, ny(TUESDAY, 14), now=ny(TUESDAY, 6)
        )

        assert result.outcome is BookingOutcome.CONFLICT
        await db.refresh(appointment)
        assert as_utc(appointment.starts_at) == ny(TUESDAY, 10)

    async def test_a_provider_failure_leaves_the_appointment_intact(
        self, db, tenant, rules, monkeypatch
    ):
        """
        **Requirement 17's critical clause.** Nothing may be half-written: the
        customer must still have their original slot.
        """
        await make_calendar_integration(db, tenant, "google")

        from app.integrations.calendar.providers.google import GoogleCalendarProvider

        async def create(self, request):
            return CalendarEvent(
                external_id="gcal-1", start=request.start, end=request.end
            )

        async def fail_update(self, external_id, request):
            raise CalendarTemporaryError("down", provider="google")

        monkeypatch.setattr(GoogleCalendarProvider, "create_event", create)
        booked = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 10)), now=ny(TUESDAY, 6)
        )
        appointment = await db.get(Appointment, uuid.UUID(booked.appointment_id))

        monkeypatch.setattr(GoogleCalendarProvider, "update_event", fail_update)
        result = await service.reschedule(
            db, tenant, appointment, ny(TUESDAY, 14), now=ny(TUESDAY, 6)
        )

        assert result.outcome is BookingOutcome.FAILED
        assert "original" in result.message.lower()

        await db.refresh(appointment)
        assert as_utc(appointment.starts_at) == ny(TUESDAY, 10)   # untouched
        assert appointment.status is AppointmentStatus.CONFIRMED
        assert appointment.external_event_id == "gcal-1"
        assert appointment.rescheduled_from is None

    async def test_rescheduling_outside_business_hours_is_refused(
        self, db, tenant, appointment
    ):
        result = await service.reschedule(
            db, tenant, appointment, ny(TUESDAY, 22), now=ny(TUESDAY, 6)
        )
        assert result.outcome is BookingOutcome.OUTSIDE_HOURS
        await db.refresh(appointment)
        assert as_utc(appointment.starts_at) == ny(TUESDAY, 10)

    async def test_rescheduling_a_cancelled_appointment_is_refused(
        self, db, tenant, appointment
    ):
        await service.cancel(db, tenant, appointment)
        result = await service.reschedule(
            db, tenant, appointment, ny(TUESDAY, 14), now=ny(TUESDAY, 6)
        )
        assert result.outcome is BookingOutcome.NOT_FOUND

    async def test_reschedule_emits_a_crm_event(self, db, tenant, appointment):
        from app.db.models import CrmEvent, CrmEventType

        await service.reschedule(
            db, tenant, appointment, ny(TUESDAY, 14), now=ny(TUESDAY, 6)
        )
        types = {
            e.event_type
            for e in (await db.execute(select(CrmEvent))).scalars().all()
        }
        assert CrmEventType.APPOINTMENT_BOOKED in types

    async def test_rescheduling_to_the_same_time_is_idempotent(
        self, db, tenant, appointment
    ):
        result = await service.reschedule(
            db, tenant, appointment, ny(TUESDAY, 10), now=ny(TUESDAY, 6)
        )
        assert result.outcome is BookingOutcome.RESCHEDULED
        await db.refresh(appointment)
        assert as_utc(appointment.starts_at) == ny(TUESDAY, 10)


# ================================================================= cancel ===

class TestCancel:
    @pytest.fixture
    async def appointment(self, db, tenant, rules, internal):
        result = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 10)), now=ny(TUESDAY, 6)
        )
        return await db.get(Appointment, uuid.UUID(result.appointment_id))

    async def test_a_successful_cancellation_records_everything(
        self, db, tenant, appointment
    ):
        result = await service.cancel(
            db, tenant, appointment, reason="Feeling better",
            cancelled_by="customer",
        )
        assert result.outcome is BookingOutcome.CANCELLED

        await db.refresh(appointment)
        assert appointment.status is AppointmentStatus.CANCELLED
        assert appointment.cancelled_at is not None
        assert appointment.cancellation_reason == "Feeling better"
        assert appointment.cancelled_by == "customer"
        # The slot lock is released so the time can be rebooked.
        assert appointment.slot_key is None

    async def test_cancelling_twice_is_safe_and_hits_the_provider_once(
        self, db, tenant, rules, monkeypatch
    ):
        """
        Requirement 18. A second cancel is a 404 on Google and a 400 on
        Cal.com; surfacing those would make a harmless double-click look
        broken.
        """
        await make_calendar_integration(db, tenant, "google")
        calls = {"cancel": 0}

        from app.integrations.calendar.providers.google import GoogleCalendarProvider

        async def create(self, request):
            return CalendarEvent(
                external_id="gcal-1", start=request.start, end=request.end
            )

        async def cancel_event(self, external_id, *, reason="", calendar_reference=None):
            calls["cancel"] += 1

        monkeypatch.setattr(GoogleCalendarProvider, "create_event", create)
        monkeypatch.setattr(GoogleCalendarProvider, "cancel_event", cancel_event)

        booked = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 10)), now=ny(TUESDAY, 6)
        )
        appointment = await db.get(Appointment, uuid.UUID(booked.appointment_id))

        first = await service.cancel(db, tenant, appointment)
        second = await service.cancel(db, tenant, appointment)

        assert first.outcome is second.outcome is BookingOutcome.CANCELLED
        assert calls["cancel"] == 1, "the provider must not be called twice"

    async def test_a_provider_failure_still_frees_the_slot_locally(
        self, db, tenant, rules, monkeypatch
    ):
        """
        The customer said cancel. Refusing to record that because a vendor API
        was slow would keep the slot blocked and turn away the next caller.
        """
        await make_calendar_integration(db, tenant, "google")

        from app.integrations.calendar.providers.google import GoogleCalendarProvider

        async def create(self, request):
            return CalendarEvent(
                external_id="gcal-1", start=request.start, end=request.end
            )

        async def fail(self, external_id, *, reason="", calendar_reference=None):
            raise CalendarTemporaryError("down", provider="google")

        monkeypatch.setattr(GoogleCalendarProvider, "create_event", create)
        monkeypatch.setattr(GoogleCalendarProvider, "cancel_event", fail)

        booked = await service.book(
            db, tenant, booking(tenant, ny(TUESDAY, 10)), now=ny(TUESDAY, 6)
        )
        appointment = await db.get(Appointment, uuid.UUID(booked.appointment_id))

        result = await service.cancel(db, tenant, appointment)
        assert result.outcome is BookingOutcome.CANCELLED

        await db.refresh(appointment)
        assert appointment.status is AppointmentStatus.CANCELLED
        # The failure is kept for staff to reconcile, scrubbed of secrets.
        assert appointment.last_error

    async def test_cancellation_emits_a_crm_event(self, db, tenant, appointment):
        from app.db.models import CrmEvent, CrmEventType

        await service.cancel(db, tenant, appointment)
        types = {
            e.event_type
            for e in (await db.execute(select(CrmEvent))).scalars().all()
        }
        assert CrmEventType.APPOINTMENT_CANCELLED in types

    async def test_marking_a_no_show_frees_the_slot(self, db, tenant, appointment):
        await service.mark_no_show(db, appointment)
        await db.refresh(appointment)
        assert appointment.status is AppointmentStatus.NO_SHOW
        assert appointment.slot_key is None


# ============================================================== reminders ===

class TestReminders:
    async def test_a_reminder_is_queued_on_booking(self, db, tenant, rules, internal):
        tenant.reminder_enabled = True
        tenant.reminder_hours_before = 24
        await db.commit()

        # Book far enough ahead that the reminder is still in the future.
        start = ny(date(2026, 6, 23), 10)
        await service.book(
            db, tenant, booking(tenant, start), now=ny(TUESDAY, 6)
        )

        reminders = (await db.execute(select(Reminder))).scalars().all()
        assert len(reminders) == 1
        assert as_utc(reminders[0].send_at) == start - timedelta(hours=24)

    async def test_a_reminder_due_within_the_offset_is_not_dropped(
        self, db, tenant, rules, internal
    ):
        """
        **The audit's F3.** The old comparison relabelled a naive UTC clock
        with the appointment's timezone, so it believed "now" was four hours
        ahead and silently dropped every reminder due inside that window.
        """
        tenant.reminder_enabled = True
        tenant.reminder_hours_before = 2
        await db.commit()

        # 11:30 fits inside the 09:00-12:00 morning interval, and the reminder
        # falls 2 hours before a booking 2.5 hours away -- inside the 4-hour
        # error the old code had for America/New_York.
        now = ny(TUESDAY, 9)
        start = ny(TUESDAY, 11, 30)
        await service.book(db, tenant, booking(tenant, start), now=now)

        reminders = (await db.execute(select(Reminder))).scalars().all()
        assert len(reminders) == 1
        assert as_utc(reminders[0].send_at) == ny(TUESDAY, 9, 30)

    async def test_rescheduling_moves_the_reminder_rather_than_adding_one(
        self, db, tenant, rules, internal
    ):
        tenant.reminder_enabled = True
        tenant.reminder_hours_before = 24
        await db.commit()

        first = ny(date(2026, 6, 23), 10)
        result = await service.book(db, tenant, booking(tenant, first), now=ny(TUESDAY, 6))
        appointment = await db.get(Appointment, uuid.UUID(result.appointment_id))

        second = ny(date(2026, 6, 24), 10)
        await service.reschedule(db, tenant, appointment, second, now=ny(TUESDAY, 6))

        reminders = (await db.execute(select(Reminder))).scalars().all()
        assert len(reminders) == 1, "rescheduling must not queue a second reminder"
        assert as_utc(reminders[0].send_at) == second - timedelta(hours=24)

    async def test_cancelling_removes_the_reminder(self, db, tenant, rules, internal):
        """A cancelled appointment must not still text the customer."""
        tenant.reminder_enabled = True
        await db.commit()

        start = ny(date(2026, 6, 23), 10)
        result = await service.book(db, tenant, booking(tenant, start), now=ny(TUESDAY, 6))
        appointment = await db.get(Appointment, uuid.UUID(result.appointment_id))

        assert (await db.execute(select(func.count(Reminder.id)))).scalar() == 1
        await service.cancel(db, tenant, appointment)
        assert (await db.execute(select(func.count(Reminder.id)))).scalar() == 0

    async def test_no_reminder_when_the_tenant_has_them_off(
        self, db, tenant, rules, internal
    ):
        tenant.reminder_enabled = False
        await db.commit()
        await service.book(
            db, tenant, booking(tenant, ny(date(2026, 6, 23), 10)), now=ny(TUESDAY, 6)
        )
        assert (await db.execute(select(func.count(Reminder.id)))).scalar() == 0