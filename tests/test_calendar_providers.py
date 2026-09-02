"""
Per-provider adapter tests.

`test_calendar_contract.py` covers what every provider must do identically.
This file covers what each one does *differently* — the vendor quirks that are
the reason an adapter exists at all.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.integrations.calendar.base import CalendarCapability, CalendarContext
from app.integrations.calendar.errors import (
    CalendarAuthError,
    CalendarConfigurationError,
    CalendarConflictError,
    CalendarNotFoundError,
    CalendarTemporaryError,
    CalendarValidationError,
)
from app.integrations.calendar.models import (
    Attendee,
    EventRequest,
    TimeWindow,
)
from app.integrations.calendar.providers.calcom import CalComProvider
from app.integrations.calendar.providers.google import GoogleCalendarProvider
from app.integrations.calendar.providers.internal import (
    GoogleServiceAccountProvider,
    InternalCalendarProvider,
)
from app.integrations.calendar.providers.microsoft import MicrosoftCalendarProvider
from app.integrations.calendar.timezones import UTC
from tests.conftest import FakeTransport

START = datetime(2026, 6, 16, 14, 0, tzinfo=UTC)
WINDOW = TimeWindow(start=START, end=START + timedelta(hours=8))
REQUEST = EventRequest(
    title="Cleaning",
    start=START,
    end=START + timedelta(minutes=30),
    timezone="America/New_York",
    attendee=Attendee(name="Jane Doe", phone="+15551230000", email="jane@example.com"),
    idempotency_key="abc123def456abc789012345",
)


def ctx(**over):
    base = dict(
        tenant_id="t-1", credentials={"access_token": "SECRET-token-123456"},
        config={}, timezone="America/New_York", timeout_seconds=5.0,
    )
    base.update(over)
    return CalendarContext(**base)


# ================================================================== Google ===

def google(**config):
    settings = {"calendar_id": "primary", "base_url": "https://gcal.test/v3"}
    settings.update(config)
    return GoogleCalendarProvider(ctx(config=settings))


class TestGooglePayload:
    def test_event_body_shape(self):
        body = google().event_payload(REQUEST)

        assert body["summary"] == "Cleaning"
        assert body["start"]["dateTime"] == "2026-06-16T14:00:00+00:00"
        assert body["start"]["timeZone"] == "America/New_York"
        assert body["end"]["dateTime"] == "2026-06-16T14:30:00+00:00"

    def test_utc_plus_zone_name_rather_than_a_local_wall_clock(self):
        """
        The instant transmitted is unambiguous, and the event still renders in
        the business's local time in the Google UI. Sending a local wall clock
        with a bare offset is what breaks across DST.
        """
        body = google().event_payload(REQUEST)
        assert body["start"]["dateTime"].endswith("+00:00")
        assert body["start"]["timeZone"] == "America/New_York"

    def test_the_event_id_is_derived_from_the_idempotency_key(self):
        """
        Google lets the client set the event id and returns 409 if it exists.
        That turns the ambiguous-timeout problem into a solved one.
        """
        body = google().event_payload(REQUEST)
        assert body["id"] == "abc123def456abc789012345"

    def test_the_event_id_is_filtered_to_googles_charset(self):
        """
        Google ids are base32hex: a-v and 0-9. A key with other characters
        must be cleaned, not sent and rejected.
        """
        # x, y and z are *outside* base32hex (which stops at v), so they go
        # too -- not just the punctuation.
        assert GoogleCalendarProvider._event_id("ABC-xyz_123!") == "abc123"
        assert GoogleCalendarProvider._event_id("abc") is None      # too short
        assert GoogleCalendarProvider._event_id(None) is None

    def test_an_email_attendee_is_invited_and_a_phone_goes_in_the_body(self):
        body = google().event_payload(REQUEST)
        assert body["attendees"][0]["email"] == "jane@example.com"
        assert "+15551230000" in body["description"]

    def test_a_missing_token_is_a_configuration_error(self):
        with pytest.raises(CalendarConfigurationError):
            GoogleCalendarProvider(ctx(credentials={})).\
                _headers()


class TestGoogleFreeBusy:
    async def test_busy_periods_are_parsed(self, monkeypatch):
        FakeTransport((200, {
            "calendars": {"primary": {"busy": [
                {"start": "2026-06-16T14:00:00Z", "end": "2026-06-16T15:00:00Z"}
            ]}}
        })).install(monkeypatch)

        busy = await google().get_busy(WINDOW)
        assert len(busy) == 1
        assert busy[0].start == START
        assert busy[0].source == "google"

    async def test_an_empty_calendar_returns_nothing(self, monkeypatch):
        FakeTransport((200, {"calendars": {"primary": {"busy": []}}})).install(
            monkeypatch
        )
        assert await google().get_busy(WINDOW) == []

    async def test_a_per_calendar_error_raises_rather_than_looking_free(
        self, monkeypatch
    ):
        """
        Google can answer 200 while reporting that it could not read *that*
        calendar. Returning `[]` there would reproduce the audit's F2 exactly:
        an unreadable calendar looking completely free.
        """
        FakeTransport((200, {
            "calendars": {"primary": {"errors": [{"reason": "notFound"}]}}
        })).install(monkeypatch)

        with pytest.raises(CalendarNotFoundError):
            await google().get_busy(WINDOW)

    async def test_a_permission_error_on_one_calendar_raises(self, monkeypatch):
        FakeTransport((200, {
            "calendars": {"primary": {"errors": [{"reason": "notACalendarUser"}]}}
        })).install(monkeypatch)
        with pytest.raises(CalendarAuthError):
            await google().get_busy(WINDOW)


class TestGoogleEvents:
    async def test_create_returns_the_event(self, monkeypatch):
        transport = FakeTransport((200, {
            "id": "gcal-1", "summary": "Cleaning", "status": "confirmed",
            "start": {"dateTime": "2026-06-16T14:00:00Z"},
            "end": {"dateTime": "2026-06-16T14:30:00Z"},
        })).install(monkeypatch)

        event = await google().create_event(REQUEST)
        assert event.external_id == "gcal-1"
        assert event.start == START
        assert transport.last()["headers"]["Authorization"].startswith("Bearer ")

    async def test_a_409_on_our_own_id_means_already_booked(self, monkeypatch):
        """
        **The reconciliation payoff.** A 409 on a client-supplied id is our own
        earlier attempt landing, not someone else taking the slot.
        """
        FakeTransport(
            (409, {"error": "duplicate"}),
            (200, {
                "id": "abc123def456abc789012345", "status": "confirmed",
                "start": {"dateTime": "2026-06-16T14:00:00Z"},
                "end": {"dateTime": "2026-06-16T14:30:00Z"},
            }),
        ).install(monkeypatch)

        event = await google().create_event(REQUEST)
        assert event.already_existed is True
        assert event.external_id == "abc123def456abc789012345"

    async def test_a_409_with_nothing_behind_it_still_conflicts(self, monkeypatch):
        FakeTransport(
            (409, {"error": "duplicate"}),
            (404, {"error": "not found"}),
        ).install(monkeypatch)

        with pytest.raises(CalendarConflictError):
            await google().create_event(REQUEST)

    async def test_update_does_not_resend_the_immutable_id(self, monkeypatch):
        transport = FakeTransport((200, {
            "id": "gcal-1",
            "start": {"dateTime": "2026-06-16T16:00:00Z"},
            "end": {"dateTime": "2026-06-16T16:30:00Z"},
        })).install(monkeypatch)

        await google().update_event("gcal-1", REQUEST)
        assert "id" not in transport.last()["json"]
        assert transport.last()["method"] == "PATCH"

    async def test_cancelling_a_missing_event_succeeds(self, monkeypatch):
        FakeTransport((404, {"error": "gone"})).install(monkeypatch)
        await google().cancel_event("gcal-1")

    async def test_a_410_gone_also_counts_as_cancelled(self, monkeypatch):
        """Google uses 410 for a deleted event; the end state we want holds."""
        FakeTransport((410, {"error": "gone"})).install(monkeypatch)
        await google().cancel_event("gcal-1")

    async def test_a_cancelled_tombstone_is_reported_as_cancelled(self, monkeypatch):
        FakeTransport((200, {
            "id": "gcal-1", "status": "cancelled",
            "start": {"dateTime": "2026-06-16T14:00:00Z"},
            "end": {"dateTime": "2026-06-16T14:30:00Z"},
        })).install(monkeypatch)

        event = await google().get_event("gcal-1")
        assert event.status == "cancelled"

    async def test_reconciliation_ignores_a_cancelled_event(self, monkeypatch):
        """A tombstone is not a live booking; reconciling to it would be wrong."""
        FakeTransport((200, {
            "id": "abc123def456abc789012345", "status": "cancelled",
            "start": {"dateTime": "2026-06-16T14:00:00Z"},
            "end": {"dateTime": "2026-06-16T14:30:00Z"},
        })).install(monkeypatch)

        assert await google().find_event_by_key(
            REQUEST.idempotency_key, WINDOW
        ) is None

    async def test_an_event_with_no_times_is_a_validation_error(self, monkeypatch):
        """Never fabricate "now" -- it would land as a real appointment."""
        FakeTransport((200, {"id": "gcal-1"})).install(monkeypatch)
        with pytest.raises(CalendarValidationError):
            await google().create_event(REQUEST)


class TestGoogleOAuth:
    async def test_refresh_keeps_the_refresh_token_google_omits(self, monkeypatch):
        """
        Google does not resend `refresh_token` on a refresh. Dropping it is how
        an integration works for an hour and then dies permanently.
        """
        FakeTransport((200, {"access_token": "new-access", "expires_in": 3599})).\
            install(monkeypatch)

        provider = GoogleCalendarProvider(ctx(credentials={
            "access_token": "old", "refresh_token": "keep-me",
            "client_id": "cid", "client_secret": "csec",
        }, config={"token_url": "https://oauth.test/token"}))

        refreshed = await provider.refresh_access_token()
        assert refreshed["access_token"] == "new-access"
        assert refreshed["refresh_token"] == "keep-me"

    async def test_a_revoked_grant_is_permanent_not_retryable(self, monkeypatch):
        """
        Requirement 26 forbids retrying revoked auth, and hammering Google's
        token endpoint with a dead grant gets an app flagged.
        """
        FakeTransport((400, {"error": "invalid_grant"})).install(monkeypatch)

        provider = GoogleCalendarProvider(ctx(credentials={
            "refresh_token": "dead", "client_id": "cid", "client_secret": "csec",
        }, config={"token_url": "https://oauth.test/token"}))

        with pytest.raises(CalendarAuthError) as caught:
            await provider.refresh_access_token()
        assert caught.value.retryable is False

    async def test_refreshing_without_a_client_secret_is_a_config_error(self):
        provider = GoogleCalendarProvider(ctx(credentials={"refresh_token": "x"}))
        with pytest.raises(CalendarConfigurationError):
            await provider.refresh_access_token()


# =============================================================== Microsoft ===

def microsoft(**config):
    settings = {"mailbox": "diary@example.com", "base_url": "https://graph.test/v1.0"}
    settings.update(config)
    return MicrosoftCalendarProvider(ctx(config=settings))


class TestMicrosoftPayload:
    def test_times_are_naive_utc_with_an_explicit_zone(self):
        """
        Graph rejects a body carrying both an offset inside `dateTime` and a
        separate `timeZone`, so the offset is stripped after converting.
        """
        body = microsoft().event_payload(REQUEST)
        assert body["start"] == {"dateTime": "2026-06-16T14:00:00", "timeZone": "UTC"}
        assert body["end"] == {"dateTime": "2026-06-16T14:30:00", "timeZone": "UTC"}

    def test_utc_is_sent_rather_than_a_windows_zone_name(self):
        """
        Graph historically speaks "Pacific Standard Time". Shipping an
        IANA-to-Windows mapping would be a second source of truth for DST, so
        the adapter sends UTC and keeps all local rendering in one place.
        """
        body = microsoft().event_payload(REQUEST)
        assert body["start"]["timeZone"] == "UTC"
        assert "America/New_York" not in json.dumps(body)

    def test_the_transaction_id_carries_the_idempotency_key(self):
        """Graph's documented client-supplied dedupe value."""
        assert microsoft().event_payload(REQUEST)["transactionId"] == (
            "abc123def456abc789012345"
        )

    def test_the_prefer_header_asks_for_utc_responses(self):
        assert microsoft()._headers()["Prefer"] == 'outlook.timezone="UTC"'

    def test_attendee_shape(self):
        attendee = microsoft().event_payload(REQUEST)["attendees"][0]
        assert attendee["emailAddress"]["address"] == "jane@example.com"
        assert attendee["type"] == "required"

    def test_a_shared_mailbox_changes_the_path(self):
        assert microsoft()._events_path() == "/users/diary@example.com/events"
        assert MicrosoftCalendarProvider(ctx(config={}))._events_path() == "/me/events"


class TestMicrosoftFreeBusy:
    async def test_schedule_items_are_parsed(self, monkeypatch):
        FakeTransport((200, {"value": [{"scheduleItems": [
            {
                "status": "busy",
                "start": {"dateTime": "2026-06-16T14:00:00.0000000", "timeZone": "UTC"},
                "end": {"dateTime": "2026-06-16T15:00:00.0000000", "timeZone": "UTC"},
            }
        ]}]})).install(monkeypatch)

        busy = await microsoft().get_busy(WINDOW)
        assert len(busy) == 1
        assert busy[0].start == START

    async def test_seven_digit_fractional_seconds_parse(self, monkeypatch):
        """
        Graph sends 7 fractional digits; `fromisoformat` accepts at most 6 on
        older Pythons, so they are truncated rather than risking a crash.
        """
        FakeTransport((200, {"value": [{"scheduleItems": [
            {
                "status": "busy",
                "start": {"dateTime": "2026-06-16T14:00:00.1234567"},
                "end": {"dateTime": "2026-06-16T15:00:00.7654321"},
            }
        ]}]})).install(monkeypatch)

        busy = await microsoft().get_busy(WINDOW)
        assert busy[0].start.hour == 14

    async def test_free_items_are_ignored(self, monkeypatch):
        FakeTransport((200, {"value": [{"scheduleItems": [
            {
                "status": "free",
                "start": {"dateTime": "2026-06-16T14:00:00"},
                "end": {"dateTime": "2026-06-16T15:00:00"},
            }
        ]}]})).install(monkeypatch)
        assert await microsoft().get_busy(WINDOW) == []

    async def test_availability_view_is_the_fallback_for_a_restricted_mailbox(
        self, monkeypatch
    ):
        """
        `scheduleItems` is omitted when the caller lacks detail permission.
        Falling back means a restricted mailbox still yields correct busy
        blocks rather than looking completely free.
        """
        # 15-minute intervals: free, free, busy, busy, free...
        FakeTransport((200, {"value": [{"availabilityView": "0022000"}]})).install(
            monkeypatch
        )

        busy = await microsoft().get_busy(WINDOW)
        assert len(busy) == 1
        assert busy[0].start == START + timedelta(minutes=30)
        assert busy[0].end == START + timedelta(minutes=60)

    async def test_a_per_mailbox_error_raises(self, monkeypatch):
        FakeTransport((200, {"value": [{"error": {"message": "no access"}}]})).\
            install(monkeypatch)
        with pytest.raises(CalendarAuthError):
            await microsoft().get_busy(WINDOW)

    async def test_no_schedule_at_all_is_a_validation_error(self, monkeypatch):
        FakeTransport((200, {"value": []})).install(monkeypatch)
        with pytest.raises(CalendarValidationError):
            await microsoft().get_busy(WINDOW)

    async def test_free_busy_without_a_mailbox_is_a_config_error(self):
        with pytest.raises(CalendarConfigurationError):
            await MicrosoftCalendarProvider(ctx(config={})).get_busy(WINDOW)


class TestMicrosoftEvents:
    async def test_create(self, monkeypatch):
        FakeTransport((201, {
            "id": "graph-1", "subject": "Cleaning",
            "start": {"dateTime": "2026-06-16T14:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-16T14:30:00", "timeZone": "UTC"},
        })).install(monkeypatch)

        event = await microsoft().create_event(REQUEST)
        assert event.external_id == "graph-1"
        assert event.start == START

    async def test_update_drops_the_create_only_transaction_id(self, monkeypatch):
        transport = FakeTransport((200, {
            "id": "graph-1",
            "start": {"dateTime": "2026-06-16T16:00:00"},
            "end": {"dateTime": "2026-06-16T16:30:00"},
        })).install(monkeypatch)

        await microsoft().update_event("graph-1", REQUEST)
        assert "transactionId" not in transport.last()["json"]

    async def test_cancel_falls_back_to_delete(self, monkeypatch):
        """
        Graph rejects `/cancel` for a non-meeting event or a non-organiser.
        Leaving the event on the calendar would be worse than removing it
        without notifying.
        """
        transport = FakeTransport(
            (400, {"error": "not the organizer"}),
            (204, None),
        ).install(monkeypatch)

        await microsoft().cancel_event("graph-1")
        assert transport.requests[0]["url"].endswith("/cancel")
        assert transport.requests[1]["method"] == "DELETE"

    async def test_a_cancelled_event_is_reported_as_such(self, monkeypatch):
        FakeTransport((200, {
            "id": "graph-1", "isCancelled": True,
            "start": {"dateTime": "2026-06-16T14:00:00"},
            "end": {"dateTime": "2026-06-16T14:30:00"},
        })).install(monkeypatch)

        assert (await microsoft().get_event("graph-1")).status == "cancelled"

    async def test_reconciliation_matches_on_transaction_id(self, monkeypatch):
        FakeTransport((200, {"value": [
            {
                "id": "other", "transactionId": "someone-else",
                "start": {"dateTime": "2026-06-16T14:00:00"},
                "end": {"dateTime": "2026-06-16T14:30:00"},
            },
            {
                "id": "graph-mine", "transactionId": "abc123def456abc789012345",
                "start": {"dateTime": "2026-06-16T14:00:00"},
                "end": {"dateTime": "2026-06-16T14:30:00"},
            },
        ]})).install(monkeypatch)

        found = await microsoft().find_event_by_key(
            REQUEST.idempotency_key, WINDOW
        )
        assert found.external_id == "graph-mine"

    async def test_a_failed_reconciliation_raises_rather_than_claiming_absence(
        self, monkeypatch
    ):
        """
        "I could not check" must not be mistaken for "it is definitely not
        there", or the service would book a duplicate.
        """
        FakeTransport((503, {})).install(monkeypatch)
        with pytest.raises(CalendarTemporaryError):
            await microsoft().find_event_by_key(REQUEST.idempotency_key, WINDOW)

    async def test_reconciliation_skips_a_cancelled_match(self, monkeypatch):
        FakeTransport((200, {"value": [{
            "id": "graph-1", "transactionId": "abc123def456abc789012345",
            "isCancelled": True,
            "start": {"dateTime": "2026-06-16T14:00:00"},
            "end": {"dateTime": "2026-06-16T14:30:00"},
        }]})).install(monkeypatch)

        assert await microsoft().find_event_by_key(
            REQUEST.idempotency_key, WINDOW
        ) is None

    async def test_refresh_stores_the_rotated_token(self, monkeypatch):
        """
        Microsoft *does* rotate the refresh token, unlike Google. Keeping the
        old one works until it is invalidated, then fails at the worst moment.
        """
        FakeTransport((200, {
            "access_token": "new-access", "refresh_token": "rotated",
        })).install(monkeypatch)

        provider = MicrosoftCalendarProvider(ctx(credentials={
            "refresh_token": "old", "client_id": "cid", "client_secret": "csec",
        }, config={"token_url": "https://login.test/token"}))

        refreshed = await provider.refresh_access_token()
        assert refreshed["refresh_token"] == "rotated"


# ================================================================= Cal.com ===

def calcom(**config):
    settings = {"event_type_id": 4242, "base_url": "https://cal.test/v2"}
    settings.update(config)
    return CalComProvider(ctx(credentials={"api_key": "cal_live_SECRET"}, config=settings))


class TestCalComCapabilities:
    def test_free_busy_is_not_claimed(self):
        """
        Cal.com exposes bookable *slots*, having already applied the owner's
        schedule and connected calendars. There is no "busy blocks on the
        underlying calendar" endpoint, so declaring FREE_BUSY would be
        inventing an operation.
        """
        assert not calcom().supports(CalendarCapability.FREE_BUSY)
        assert calcom().supports(CalendarCapability.GET_AVAILABILITY)

    def test_update_event_is_not_claimed_but_reschedule_is(self):
        """
        Cal.com's PATCH does not move a booking's time; the dedicated
        reschedule endpoint does. Declaring UPDATE_EVENT would let the service
        call something that silently does not do what it means.
        """
        assert not calcom().supports(CalendarCapability.UPDATE_EVENT)
        assert calcom().supports(CalendarCapability.RESCHEDULE)
        assert hasattr(calcom(), "reschedule")

    def test_the_api_version_header_is_sent(self):
        assert calcom()._headers()["cal-api-version"] == "2024-08-13"

    def test_a_missing_event_type_is_a_clear_configuration_error(self):
        with pytest.raises(CalendarConfigurationError) as caught:
            CalComProvider(ctx(config={})). _event_type_id
        assert "event_type_id" in str(caught.value)

    def test_a_non_numeric_event_type_is_rejected(self):
        with pytest.raises(CalendarConfigurationError):
            calcom(event_type_id="not-a-number")._event_type_id


class TestCalComSlots:
    @pytest.mark.parametrize("shape", [
        {"2026-06-16": [{"start": "2026-06-16T14:00:00Z"}]},
        {"slots": {"2026-06-16": [{"start": "2026-06-16T14:00:00Z"}]}},
        [{"start": "2026-06-16T14:00:00Z"}],
    ])
    async def test_every_documented_slot_shape_is_accepted(self, shape, monkeypatch):
        """
        Cal.com has shipped several slot shapes across v2 minor versions. A
        tenant on a different `cal-api-version` should not silently get zero
        availability.
        """
        FakeTransport((200, {"status": "success", "data": shape})).install(monkeypatch)

        slots = await calcom().get_slots(WINDOW, timezone_name="America/New_York")
        assert len(slots) == 1
        assert slots[0].start == START

    async def test_no_slots_is_an_empty_list(self, monkeypatch):
        FakeTransport((200, {"status": "success", "data": {}})).install(monkeypatch)
        assert await calcom().get_slots(WINDOW) == []


class TestCalComBooking:
    async def test_booking_payload(self, monkeypatch):
        transport = FakeTransport((200, {"status": "success", "data": {
            "uid": "cal-1", "status": "accepted",
            "start": "2026-06-16T14:00:00Z", "end": "2026-06-16T14:30:00Z",
        }})).install(monkeypatch)

        event = await calcom().create_event(REQUEST)

        body = transport.last()["json"]
        assert body["eventTypeId"] == 4242
        assert body["start"] == "2026-06-16T14:00:00+00:00"
        assert body["attendee"]["email"] == "jane@example.com"
        assert body["attendee"]["timeZone"] == "America/New_York"
        assert body["metadata"]["voxdesk_key"] == REQUEST.idempotency_key
        assert event.external_id == "cal-1"

    async def test_booking_without_an_email_is_refused_clearly(self):
        """
        Cal.com requires an attendee email. A clear message beats a 400 the
        tenant cannot interpret.
        """
        request = EventRequest(
            title="x", start=START, end=START + timedelta(minutes=30),
            timezone="UTC", attendee=Attendee(name="Jane", phone="+1555"),
        )
        with pytest.raises(CalendarValidationError) as caught:
            await calcom().create_event(request)
        assert "email" in str(caught.value).lower()

    async def test_a_status_other_than_success_at_200_is_a_rejection(
        self, monkeypatch
    ):
        """
        The same trap as Jobber's `userErrors` in the CRM layer: a refusal
        wearing an HTTP 200.
        """
        FakeTransport((200, {
            "status": "error", "error": {"message": "event type not found"}
        })).install(monkeypatch)

        with pytest.raises(CalendarValidationError) as caught:
            await calcom().create_event(REQUEST)
        assert "event type not found" in str(caught.value)

    async def test_no_longer_available_becomes_a_conflict_not_a_validation_error(
        self, monkeypatch
    ):
        """
        Cal.com answers a taken slot with 400, not 409. Classifying that as
        validation would make it permanent and unhelpful; it is a conflict and
        the agent should offer another time.
        """
        FakeTransport((400, {"message": "This slot is no longer available"})).\
            install(monkeypatch)

        with pytest.raises(CalendarConflictError):
            await calcom().create_event(REQUEST)

    async def test_reschedule_uses_the_dedicated_endpoint(self, monkeypatch):
        """
        Better than cancel-then-rebook: a failure between those two would
        leave the customer with no booking at all.
        """
        transport = FakeTransport((200, {"status": "success", "data": {
            "uid": "cal-1", "status": "accepted",
            "start": "2026-06-16T16:00:00Z", "end": "2026-06-16T16:30:00Z",
        }})).install(monkeypatch)

        event = await calcom().reschedule(
            "cal-1", datetime(2026, 6, 16, 16, 0, tzinfo=UTC), reason="conflict"
        )
        assert transport.last()["url"].endswith("/bookings/cal-1/reschedule")
        assert transport.last()["json"]["reschedulingReason"] == "conflict"
        assert event.start.hour == 16

    async def test_cancel_uses_the_cancel_endpoint(self, monkeypatch):
        transport = FakeTransport((200, {"status": "success", "data": {}})).\
            install(monkeypatch)
        await calcom().cancel_event("cal-1", reason="customer called")

        assert transport.last()["url"].endswith("/bookings/cal-1/cancel")
        assert transport.last()["json"]["cancellationReason"] == "customer called"

    async def test_cancelling_an_already_cancelled_booking_is_silent(
        self, monkeypatch
    ):
        FakeTransport((400, {"message": "Booking already cancelled"})).\
            install(monkeypatch)
        await calcom().cancel_event("cal-1")        # must not raise

    async def test_reconciliation_matches_on_metadata(self, monkeypatch):
        FakeTransport((200, {"status": "success", "data": [
            {"uid": "other", "metadata": {"voxdesk_key": "someone-else"},
             "status": "accepted",
             "start": "2026-06-16T14:00:00Z", "end": "2026-06-16T14:30:00Z"},
            {"uid": "cal-mine", "metadata": {"voxdesk_key": REQUEST.idempotency_key},
             "status": "accepted",
             "start": "2026-06-16T14:00:00Z", "end": "2026-06-16T14:30:00Z"},
        ]})).install(monkeypatch)

        found = await calcom().find_event_by_key(REQUEST.idempotency_key, WINDOW)
        assert found.external_id == "cal-mine"

    async def test_reconciliation_skips_a_cancelled_match(self, monkeypatch):
        FakeTransport((200, {"status": "success", "data": [
            {"uid": "cal-1", "metadata": {"voxdesk_key": REQUEST.idempotency_key},
             "status": "cancelled",
             "start": "2026-06-16T14:00:00Z", "end": "2026-06-16T14:30:00Z"},
        ]})).install(monkeypatch)

        assert await calcom().find_event_by_key(
            REQUEST.idempotency_key, WINDOW
        ) is None

    async def test_an_unsupported_capability_is_reported_not_faked(self):
        """Requirement 12: return a clear internal unsupported error."""
        from app.integrations.calendar.errors import CalendarUnsupportedError

        with pytest.raises(CalendarUnsupportedError):
            await calcom().update_event("cal-1", REQUEST)
        with pytest.raises(CalendarUnsupportedError):
            await calcom().get_busy(WINDOW)


# ================================================== internal / legacy Google ===

class TestInternalProvider:
    def test_it_claims_no_free_busy(self):
        """
        There is no third-party calendar to be busy on, and the service checks
        VoxDesk's own appointments separately. Declaring it would double-count.
        """
        provider = InternalCalendarProvider(ctx(credentials={}, config={}))
        assert not provider.supports(CalendarCapability.FREE_BUSY)

    async def test_the_event_id_is_deterministic_from_the_key(self):
        """
        So a retry after a crash produces the *same* id and the service's
        reconciliation logic behaves like a real provider's.
        """
        provider = InternalCalendarProvider(ctx(credentials={}, config={}))
        first = await provider.create_event(REQUEST)
        second = await provider.create_event(REQUEST)
        assert first.external_id == second.external_id

    async def test_health_reports_connected(self):
        provider = InternalCalendarProvider(ctx(credentials={}, config={}))
        assert (await provider.health_check()).connected is True


class TestGoogleServiceAccountProvider:
    def test_it_claims_only_what_the_legacy_client_can_do(self):
        """
        `CalendarClient` has exactly two methods. Declaring cancel or
        reschedule would let the service route work to methods that do not
        exist.
        """
        provider = GoogleServiceAccountProvider(
            ctx(credentials={}, config={"calendar_id": "shared@example.com"})
        )
        assert provider.supports(CalendarCapability.CREATE_EVENT)
        assert provider.supports(CalendarCapability.FREE_BUSY)
        assert not provider.supports(CalendarCapability.CANCEL_EVENT)
        assert not provider.supports(CalendarCapability.UPDATE_EVENT)
        assert not provider.supports(CalendarCapability.GET_EVENT)

    async def test_a_silent_legacy_failure_becomes_a_raise(self, monkeypatch):
        """
        **The single most important line in that adapter.** The legacy client
        returns `None` on any failure, and the old code treated that as
        success and told the caller the appointment was booked.
        """
        from app.integrations import google_calendar

        async def returns_none(self, **kwargs):
            return None

        monkeypatch.setattr(
            google_calendar.CalendarClient, "create_event", returns_none
        )

        provider = GoogleServiceAccountProvider(
            ctx(credentials={}, config={"calendar_id": "shared@example.com"})
        )
        with pytest.raises(CalendarTemporaryError):
            await provider.create_event(REQUEST)

    async def test_a_missing_calendar_id_is_a_config_error(self):
        provider = GoogleServiceAccountProvider(ctx(credentials={}, config={}))
        with pytest.raises(CalendarConfigurationError):
            await provider.get_busy(WINDOW)

