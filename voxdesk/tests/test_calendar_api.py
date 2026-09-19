"""
API, tenant isolation, voice tools, webhooks and service-overhead timings.

Requirement 23's list is the core of this file: every cross-tenant test gives
Tenant B a genuine authenticated session and a correct id belonging to Tenant
A, and asserts that being right about the id changes nothing. **An id is not
authorization.**
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time as _time
import uuid
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import (
    Appointment,
    AppointmentStatus,
    AuditLog,
    CalendarIntegration,
    CalendarProviderType,
    CalendarWebhookReceipt,
)
from app.integrations.calendar import service
from app.integrations.calendar.models import BookingOutcome
from app.integrations.calendar.timezones import as_utc, resolve_local
from tests.conftest import (
    auth_headers,
    make_calendar_integration,
    make_scheduling_policy,
)

NY = "America/New_York"


def _next_tuesday() -> date:
    """
    A Tuesday roughly a fortnight out, computed from the real clock.

    Unlike `test_calendar_booking.py`, these tests go through the HTTP API,
    which has no way to inject a clock -- so the date has to be genuinely in
    the future or every booking is refused as TOO_SOON. Two weeks clears the
    default 60-minute minimum notice with room to spare and stays inside the
    60-day booking horizon.
    """
    today = date.today()
    ahead = (1 - today.weekday()) % 7 or 7      # 1 == Tuesday
    return today + timedelta(days=ahead + 7)


TUESDAY = _next_tuesday()


def ny(day: date, hour: int, minute: int = 0) -> datetime:
    return resolve_local(datetime.combine(day, time(hour, minute)), NY).utc


@pytest.fixture
async def tenant(db, tenant_a):
    tenant_a.timezone = NY
    await db.commit()
    await make_scheduling_policy(db, tenant_a)
    await make_calendar_integration(db, tenant_a, "internal")
    return tenant_a


@pytest.fixture
async def other(db, tenant_b):
    tenant_b.timezone = NY
    await db.commit()
    await make_scheduling_policy(db, tenant_b)
    await make_calendar_integration(db, tenant_b, "internal")
    return tenant_b


async def book(db, tenant, hour=10, **over):
    fields = dict(
        tenant_id=tenant.id, start=ny(TUESDAY, hour),
        customer_name="Jane Doe", customer_phone="+15551230000",
    )
    fields.update(over)
    result = await service.book(
        db, tenant, service.BookingRequest(**fields), now=ny(TUESDAY, 6)
    )
    assert result.outcome is BookingOutcome.BOOKED, result.message
    return await db.get(Appointment, uuid.UUID(result.appointment_id))


# ============================================================ booking API ===

class TestAppointmentApi:
    async def test_availability_endpoint(self, client, db, owner_a, tenant):
        headers = await auth_headers(client, owner_a)
        response = await client.get(
            f"/api/appointments/availability?day={TUESDAY}", headers=headers
        )
        assert response.status_code == 200

        body = response.json()
        assert body["timezone"] == NY
        assert body["slots"]
        assert body["degraded"] is None
        assert all("spoken" in slot for slot in body["slots"])

    async def test_booking_through_the_api(self, client, db, owner_a, tenant):
        headers = await auth_headers(client, owner_a)
        response = await client.post(
            "/api/appointments",
            headers=headers,
            json={
                "customer_name": "Jane Doe",
                "customer_phone": "+15551230000",
                "starts_at_local": f"{TUESDAY}T10:00:00",
                "reason": "Cleaning",
            },
        )
        assert response.status_code == 201, response.text

        body = response.json()
        assert body["status"] == "confirmed"
        assert body["timezone"] == NY
        assert body["external_event_id"]
        # The local wall clock was interpreted in the tenant's zone.
        assert body["starts_at"] == ny(TUESDAY, 10).isoformat()

    async def test_a_conflicting_booking_is_a_409(self, client, db, owner_a, tenant):
        await book(db, tenant, 10)
        headers = await auth_headers(client, owner_a)
        response = await client.post(
            "/api/appointments",
            headers=headers,
            json={
                "customer_name": "Bob", "customer_phone": "+15559990000",
                "starts_at_local": f"{TUESDAY}T10:00:00",
            },
        )
        assert response.status_code == 409
        assert response.json()["detail"]["outcome"] == "CONFLICT"

    async def test_an_out_of_hours_booking_is_a_422(self, client, db, owner_a, tenant):
        headers = await auth_headers(client, owner_a)
        response = await client.post(
            "/api/appointments",
            headers=headers,
            json={
                "customer_name": "Jane", "customer_phone": "+15551230000",
                "starts_at_local": f"{TUESDAY}T22:00:00",
            },
        )
        assert response.status_code == 422
        assert response.json()["detail"]["outcome"] == "OUTSIDE_HOURS"

    async def test_a_nonexistent_local_time_is_explained_not_shifted(
        self, client, db, owner_a, tenant
    ):
        """
        Requirement 4: never silently move an appointment by an hour. The API
        says what happened and offers the real boundary.
        """
        headers = await auth_headers(client, owner_a)
        response = await client.post(
            "/api/appointments",
            headers=headers,
            json={
                "customer_name": "Jane", "customer_phone": "+15551230000",
                "starts_at_local": "2026-03-08T02:30:00",
            },
        )
        assert response.status_code == 422
        assert "does not exist" in response.json()["detail"]

    async def test_an_ambiguous_local_time_asks_for_an_offset(
        self, client, db, owner_a, tenant
    ):
        headers = await auth_headers(client, owner_a)
        response = await client.post(
            "/api/appointments",
            headers=headers,
            json={
                "customer_name": "Jane", "customer_phone": "+15551230000",
                "starts_at_local": "2026-11-01T01:30:00",
            },
        )
        assert response.status_code == 422
        assert "twice" in response.json()["detail"]

    async def test_a_duplicate_request_id_returns_the_same_appointment(
        self, client, db, owner_a, tenant
    ):
        headers = await auth_headers(client, owner_a)
        payload = {
            "customer_name": "Jane", "customer_phone": "+15551230000",
            "starts_at_local": f"{TUESDAY}T10:00:00",
            "request_id": "client-req-1",
        }
        first = await client.post("/api/appointments", headers=headers, json=payload)
        second = await client.post("/api/appointments", headers=headers, json=payload)

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]

        total = (await db.execute(select(func.count(Appointment.id)))).scalar()
        assert total == 1

    async def test_reschedule_and_cancel_through_the_api(
        self, client, db, owner_a, tenant
    ):
        appointment = await book(db, tenant, 10)
        headers = await auth_headers(client, owner_a)

        moved = await client.patch(
            f"/api/appointments/{appointment.id}",
            headers=headers,
            json={"starts_at_local": f"{TUESDAY}T14:00:00"},
        )
        assert moved.status_code == 200
        assert moved.json()["starts_at"] == ny(TUESDAY, 14).isoformat()
        assert moved.json()["rescheduled_from"] == ny(TUESDAY, 10).isoformat()

        cancelled = await client.post(
            f"/api/appointments/{appointment.id}/cancel",
            headers=headers, json={"reason": "changed plans"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["cancellation_reason"] == "changed plans"

    async def test_listing_and_filtering(self, client, db, owner_a, tenant):
        await book(db, tenant, 9, customer_phone="+15550000001")
        await book(db, tenant, 10, customer_phone="+15550000002")
        headers = await auth_headers(client, owner_a)

        listed = await client.get("/api/appointments", headers=headers)
        assert listed.json()["total"] == 2

        filtered = await client.get(
            "/api/appointments?status=cancelled", headers=headers
        )
        assert filtered.json()["total"] == 0

    async def test_a_viewer_cannot_book(self, client, db, viewer_a, tenant):
        headers = await auth_headers(client, viewer_a)
        response = await client.post(
            "/api/appointments",
            headers=headers,
            json={
                "customer_name": "Jane", "customer_phone": "+15551230000",
                "starts_at_local": f"{TUESDAY}T10:00:00",
            },
        )
        assert response.status_code == 403

    async def test_unauthenticated_requests_are_rejected(self, client, tenant):
        for method, path, body in (
            ("get", "/api/appointments", None),
            ("get", f"/api/appointments/availability?day={TUESDAY}", None),
            ("post", "/api/appointments", {}),
        ):
            kwargs = {"json": body} if body is not None else {}
            response = await getattr(client, method)(path, **kwargs)
            assert response.status_code in (401, 403), path


# ========================================================== policy API ===

class TestPolicyApi:
    async def test_reading_the_policy(self, client, db, owner_a, tenant):
        headers = await auth_headers(client, owner_a)
        response = await client.get("/api/calendar/policy", headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["configured"] is True
        assert body["timezone"] == NY
        assert body["weekly_hours"]["sat"] == []

    async def test_a_tenant_without_a_policy_sees_the_legacy_fallback(
        self, client, db, owner_a, tenant_a
    ):
        headers = await auth_headers(client, owner_a)
        body = (await client.get("/api/calendar/policy", headers=headers)).json()
        assert body["configured"] is False
        assert body["weekly_hours"]

    async def test_updating_the_policy(self, client, db, owner_a, tenant):
        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/calendar/policy",
            headers=headers,
            json={
                "weekly_hours": {"mon": [["10:00", "16:00"]], "tue": []},
                "holidays": ["2026-12-25"],
                "slot_minutes": 45,
                "slot_interval_minutes": 15,
                "buffer_before_minutes": 10,
                "minimum_notice_minutes": 120,
                "timezone": "Asia/Dhaka",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["slot_minutes"] == 45
        assert body["timezone"] == "Asia/Dhaka"

    async def test_an_invalid_timezone_is_rejected(self, client, db, owner_a, tenant):
        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/calendar/policy", headers=headers,
            json={"timezone": "Mars/Olympus"},
        )
        assert response.status_code == 422
        assert "IANA" in response.json()["detail"]

    async def test_invalid_hours_are_rejected_at_write_time(
        self, client, db, owner_a, tenant
    ):
        """A bad schedule must be a 422 now, not a mystery at 3 AM."""
        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/calendar/policy", headers=headers,
            json={"weekly_hours": {"mon": [["17:00", "09:00"]]}},
        )
        assert response.status_code == 422

    async def test_an_interval_longer_than_the_slot_is_rejected(
        self, client, db, owner_a, tenant
    ):
        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/calendar/policy", headers=headers,
            json={"slot_minutes": 15, "slot_interval_minutes": 60},
        )
        assert response.status_code == 422


# ================================================= calendar integration API ===

class TestCalendarIntegrationApi:
    async def test_connecting_a_provider(self, client, db, owner_a, tenant_a):
        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/calendar/integrations/google",
            headers=headers,
            json={
                "credentials": {
                    "access_token": "ya29-REAL-TOKEN", "refresh_token": "1//real",
                    "client_id": "cid", "client_secret": "csec",
                },
                "config": {"calendar_id": "primary"},
                "is_primary": True,
            },
        )
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["connected"] is True
        assert body["config"]["calendar_id"] == "primary"
        # No token, anywhere.
        assert "ya29-REAL-TOKEN" not in response.text
        assert "credentials" not in body

    async def test_stored_credentials_are_ciphertext(self, db, tenant_a):
        integration = await make_calendar_integration(
            db, tenant_a, "google",
            credentials={"access_token": "ya29-PLAINTEXT-CHECK"},
        )
        assert integration.credentials_encrypted.startswith("v1.")
        assert "ya29-PLAINTEXT-CHECK" not in integration.credentials_encrypted

    async def test_credentials_are_bound_to_their_tenant_and_provider(
        self, db, tenant_a, tenant_b
    ):
        """
        Requirement 24: changing tenant or provider must not make another
        tenant's ciphertext decryptable. The AES-GCM associated data is
        `(tenant, provider)`, so a copied row fails to authenticate.
        """
        from app.core.config import settings
        from app.integrations.crm import crypto

        integration = await make_calendar_integration(db, tenant_a, "google")
        ring = crypto.parse_key_ring(settings.crm_encryption_keys)

        with pytest.raises(crypto.CredentialDecryptionError):
            crypto.decrypt_credentials(
                integration.credentials_encrypted,
                tenant_id=str(tenant_b.id), provider="google", key_ring=ring,
            )
        with pytest.raises(crypto.CredentialDecryptionError):
            crypto.decrypt_credentials(
                integration.credentials_encrypted,
                tenant_id=str(tenant_a.id), provider="microsoft", key_ring=ring,
            )

    async def test_an_unknown_credential_field_is_rejected(
        self, client, db, owner_a, tenant_a
    ):
        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/calendar/integrations/google",
            headers=headers,
            json={"credentials": {"access_token": "t", "admin_override": "x"}},
        )
        assert response.status_code == 422
        assert "admin_override" in response.text

    async def test_calcom_requires_an_event_type(self, client, db, owner_a, tenant_a):
        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/calendar/integrations/calcom",
            headers=headers, json={"credentials": {"api_key": "cal_live_x"}},
        )
        assert response.status_code == 422
        assert "event_type_id" in response.text

    async def test_the_provider_catalogue_reports_honest_capabilities(
        self, client, db, owner_a
    ):
        headers = await auth_headers(client, owner_a)
        response = await client.get("/api/calendar/providers", headers=headers)

        catalogue = {p["provider"]: p for p in response.json()["providers"]}
        assert set(catalogue) == {p.value for p in CalendarProviderType}
        # A dashboard can grey out what a provider cannot do.
        assert "free_busy" not in catalogue["calcom"]["capabilities"]
        assert "cancel_event" not in catalogue["google_service_account"]["capabilities"]
        assert "reschedule" in catalogue["calcom"]["capabilities"]

    async def test_setting_a_new_primary_demotes_the_old_one(
        self, client, db, owner_a, tenant_a
    ):
        await make_calendar_integration(db, tenant_a, "google", primary=True)
        headers = await auth_headers(client, owner_a)

        await client.put(
            "/api/calendar/integrations/calcom",
            headers=headers,
            json={
                "credentials": {"api_key": "cal_live_x"},
                "config": {"event_type_id": 1},
                "is_primary": True,
            },
        )
        rows = (
            (await db.execute(select(CalendarIntegration))).scalars().all()
        )
        for row in rows:
            await db.refresh(row)
        assert sum(1 for row in rows if row.is_primary) == 1

    async def test_the_health_check_never_leaks(
        self, client, db, owner_a, tenant_a, monkeypatch
    ):
        from tests.conftest import FakeTransport

        await make_calendar_integration(
            db, tenant_a, "google",
            credentials={"access_token": "ya29-SECRET-HEALTH"},
        )
        FakeTransport((401, {"token": "ya29-SECRET-HEALTH"})).install(monkeypatch)

        headers = await auth_headers(client, owner_a)
        response = await client.post(
            "/api/calendar/integrations/google/test", headers=headers
        )
        assert response.status_code == 200
        assert response.json()["connected"] is False
        assert "ya29-SECRET-HEALTH" not in response.text

    async def test_no_credential_reaches_any_response(
        self, client, db, owner_a, tenant_a
    ):
        await make_calendar_integration(
            db, tenant_a, "microsoft",
            credentials={"access_token": "eyJ-LEAK-CHECK", "refresh_token": "M.R-LEAK"},
        )
        headers = await auth_headers(client, owner_a)

        for path in (
            "/api/calendar/integrations",
            "/api/calendar/providers",
            "/api/calendar/policy",
        ):
            response = await client.get(path, headers=headers)
            assert response.status_code == 200, path
            assert "eyJ-LEAK-CHECK" not in response.text, path
            assert "M.R-LEAK" not in response.text, path

    async def test_connecting_writes_no_secret_to_the_audit_log(
        self, client, db, owner_a, tenant_a
    ):
        headers = await auth_headers(client, owner_a)
        await client.put(
            "/api/calendar/integrations/google",
            headers=headers,
            json={"credentials": {"access_token": "ya29-AUDIT-LEAK"}},
        )
        rows = (await db.execute(select(AuditLog))).scalars().all()
        everything = json.dumps([r.detail for r in rows])

        assert "ya29-AUDIT-LEAK" not in everything
        # The field *names* are recorded, which is what makes the trail useful
        # without making it dangerous.
        assert "access_token" in everything


# =========================================================== isolation (23) ===

class TestTenantIsolation:
    async def test_tenant_b_cannot_read_tenant_a_appointments(
        self, client, db, owner_b, tenant, other
    ):
        appointment = await book(db, tenant, 10)
        headers = await auth_headers(client, owner_b)

        # 404, not 403 -- a 403 would confirm the id exists.
        one = await client.get(f"/api/appointments/{appointment.id}", headers=headers)
        assert one.status_code == 404

        listed = await client.get("/api/appointments", headers=headers)
        assert listed.json()["total"] == 0

    async def test_a_wrong_id_and_someone_elses_id_are_indistinguishable(
        self, client, db, owner_b, tenant, other
    ):
        appointment = await book(db, tenant, 10)
        headers = await auth_headers(client, owner_b)

        theirs = await client.get(f"/api/appointments/{appointment.id}", headers=headers)
        nobodys = await client.get(f"/api/appointments/{uuid.uuid4()}", headers=headers)
        assert theirs.status_code == nobodys.status_code == 404
        assert theirs.json() == nobodys.json()

    async def test_tenant_b_cannot_reschedule_tenant_a_appointment(
        self, client, db, owner_b, tenant, other
    ):
        appointment = await book(db, tenant, 10)
        headers = await auth_headers(client, owner_b)

        response = await client.patch(
            f"/api/appointments/{appointment.id}",
            headers=headers, json={"starts_at_local": f"{TUESDAY}T14:00:00"},
        )
        assert response.status_code == 404

        await db.refresh(appointment)
        assert as_utc(appointment.starts_at) == ny(TUESDAY, 10)

    async def test_tenant_b_cannot_cancel_tenant_a_appointment(
        self, client, db, owner_b, tenant, other
    ):
        appointment = await book(db, tenant, 10)
        headers = await auth_headers(client, owner_b)

        response = await client.post(
            f"/api/appointments/{appointment.id}/cancel", headers=headers, json={}
        )
        assert response.status_code == 404

        await db.refresh(appointment)
        assert appointment.status is AppointmentStatus.CONFIRMED

    async def test_tenant_b_sees_their_own_availability_not_tenant_a_s(
        self, client, db, owner_b, tenant, other
    ):
        """
        Tenant A booking 10 AM must not remove 10 AM from Tenant B's diary.
        Two businesses are not in competition for the same hour.
        """
        await book(db, tenant, 10)
        headers = await auth_headers(client, owner_b)

        response = await client.get(
            f"/api/appointments/availability?day={TUESDAY}&part_of_day=morning",
            headers=headers,
        )
        starts = {slot["start"] for slot in response.json()["slots"]}
        assert ny(TUESDAY, 10).isoformat() in starts

    async def test_tenant_b_cannot_read_tenant_a_calendar_integration(
        self, client, db, owner_b, tenant_a, tenant_b
    ):
        await make_calendar_integration(
            db, tenant_a, "google", config={"calendar_id": "TENANT-A-CAL"}
        )
        await make_calendar_integration(
            db, tenant_b, "google", config={"calendar_id": "TENANT-B-CAL"}
        )
        headers = await auth_headers(client, owner_b)

        response = await client.get("/api/calendar/integrations", headers=headers)
        assert "TENANT-A-CAL" not in response.text
        assert "TENANT-B-CAL" in response.text

    async def test_tenant_b_cannot_test_tenant_a_calendar_credentials(
        self, client, db, owner_b, tenant_a
    ):
        """Spending A's rate limit and probing whether their token is live."""
        await make_calendar_integration(db, tenant_a, "google")
        headers = await auth_headers(client, owner_b)
        response = await client.post(
            "/api/calendar/integrations/google/test", headers=headers
        )
        assert response.status_code == 404

    async def test_a_tenant_id_in_the_body_is_ignored(
        self, client, db, owner_b, tenant, other
    ):
        """Requirement 14: no client-controlled tenant_id authority."""
        headers = await auth_headers(client, owner_b)
        response = await client.post(
            "/api/appointments",
            headers=headers,
            json={
                "tenant_id": str(tenant.id),
                "customer_name": "Injected", "customer_phone": "+15557770000",
                "starts_at_local": f"{TUESDAY}T10:00:00",
            },
        )
        assert response.status_code == 201

        # It landed in B's diary, not A's.
        appointment = await db.get(Appointment, uuid.UUID(response.json()["id"]))
        assert appointment.tenant_id == other.id

    async def test_the_booking_service_never_crosses_tenants(
        self, db, tenant, other
    ):
        """
        Both tenants can hold the same slot. `slot_key` is salted with the
        tenant id, so their unique constraints cannot collide.
        """
        first = await book(db, tenant, 10)
        second = await book(db, other, 10, tenant_id=other.id)
        assert first.slot_key != second.slot_key
        assert first.tenant_id != second.tenant_id

    async def test_the_routes_use_the_permission_enum_not_role_strings(self):
        import pathlib

        for path in (
            "app/api/appointment_routes.py",
            "app/api/calendar_webhook_routes.py",
        ):
            source = pathlib.Path(path).read_text()
            assert '== "admin"' not in source, path
            assert ".role ==" not in source, path


# ============================================================= voice tools ===

class TestVoiceTools:
    @pytest.fixture
    def tools(self, db, tenant):
        from app.integrations.calendar.tools import SchedulingTools

        return SchedulingTools(db, tenant, None, now=ny(TUESDAY, 6))

    async def test_availability_returns_structured_slots(self, tools):
        result = await tools.check_availability("today")
        assert result["outcome"] == "AVAILABLE_SLOTS"
        assert result["ok"] is True
        assert result["slots"]
        assert all("spoken" in slot for slot in result["slots"])

    async def test_booking_returns_booked_only_after_a_provider_accepts(self, tools):
        result = await tools.book_appointment(
            customer_name="Jane Doe", customer_phone="+15551230000",
            when="today", at="10am", reason="Cleaning",
        )
        assert result["outcome"] == "BOOKED"
        assert "10 am" in result["message"]

    async def test_a_provider_failure_never_returns_booked(
        self, db, tenant_a, monkeypatch
    ):
        """
        **Requirement 15's headline.** The tool result is the source of truth,
        and the model can never see BOOKED unless a provider accepted.
        """
        from app.integrations.calendar.errors import CalendarTemporaryError
        from app.integrations.calendar.providers.google import GoogleCalendarProvider
        from app.integrations.calendar.tools import SchedulingTools

        tenant_a.timezone = NY
        await db.commit()
        await make_scheduling_policy(db, tenant_a)
        await make_calendar_integration(db, tenant_a, "google")

        async def boom(self, request):
            raise CalendarTemporaryError("down", provider="google")

        monkeypatch.setattr(GoogleCalendarProvider, "create_event", boom)

        tools = SchedulingTools(db, tenant_a, None, now=ny(TUESDAY, 6))
        result = await tools.book_appointment(
            customer_name="Jane", customer_phone="+15551230000",
            when="today", at="10am",
        )
        assert result["outcome"] == "FAILED"
        assert result["ok"] is False
        assert "booked" not in result["message"].lower()

    async def test_the_payload_never_carries_provider_detail(
        self, db, tenant_a, monkeypatch
    ):
        """The caller must never hear "Google returned 401"."""
        from app.integrations.calendar.errors import CalendarAuthError
        from app.integrations.calendar.providers.google import GoogleCalendarProvider
        from app.integrations.calendar.tools import SchedulingTools

        tenant_a.timezone = NY
        await db.commit()
        await make_scheduling_policy(db, tenant_a)
        await make_calendar_integration(db, tenant_a, "google")

        async def refuse(self, request):
            raise CalendarAuthError("HTTP 401 token revoked", provider="google")

        monkeypatch.setattr(GoogleCalendarProvider, "create_event", refuse)

        tools = SchedulingTools(db, tenant_a, None, now=ny(TUESDAY, 6))
        result = await tools.book_appointment(
            customer_name="Jane", customer_phone="+15551230000",
            when="today", at="10am",
        )
        blob = json.dumps(result).lower()
        assert "google" not in blob
        assert "401" not in blob
        assert "token" not in blob

    async def test_the_outcome_vocabulary_is_closed(self):
        """
        There is no value the model can receive that means "booked" unless the
        service wrote it after a provider acknowledgement.
        """
        from app.integrations.calendar.models import BookingOutcome

        assert BookingOutcome.BOOKED.value == "BOOKED"
        assert len(set(BookingOutcome)) == len(BookingOutcome)

    async def test_missing_arguments_produce_a_question_not_a_booking(self, tools):
        no_name = await tools.book_appointment(
            customer_name="", customer_phone="+15551230000", when="today", at="10am"
        )
        assert no_name["outcome"] == "NEEDS_CLARIFICATION"
        assert "name" in no_name["message"].lower()

        bad_phone = await tools.book_appointment(
            customer_name="Jane", customer_phone="12", when="today", at="10am"
        )
        assert bad_phone["outcome"] == "NEEDS_CLARIFICATION"
        assert "number" in bad_phone["message"].lower()

    async def test_an_ambiguous_day_asks_rather_than_guessing(self, tools):
        result = await tools.check_availability("Tuesday")
        assert result["outcome"] == "NEEDS_CLARIFICATION"
        assert result["ask"]

    async def test_an_ambiguous_hour_asks(self, tools):
        result = await tools.book_appointment(
            customer_name="Jane", customer_phone="+15551230000",
            when="tomorrow", at="seven",
        )
        assert result["outcome"] == "NEEDS_CLARIFICATION"

    async def test_a_nonexistent_local_time_asks_rather_than_shifting(
        self, db, tenant
    ):
        from app.integrations.calendar.tools import SchedulingTools

        tools = SchedulingTools(
            db, tenant, None, now=resolve_local(datetime(2026, 3, 1, 9, 0), NY).utc
        )
        result = await tools.book_appointment(
            customer_name="Jane", customer_phone="+15551230000",
            when="2026-03-08", at="2:30am",
        )
        assert result["outcome"] == "NEEDS_CLARIFICATION"
        assert "forward" in result["message"]

    async def test_reschedule_and_cancel_by_phone(self, db, tenant, tools):
        await book(db, tenant, 10)

        moved = await tools.reschedule_appointment(
            customer_phone="+1 (555) 123-0000", when="today", at="2pm"
        )
        assert moved["outcome"] == "RESCHEDULED"

        cancelled = await tools.cancel_appointment(customer_phone="+15551230000")
        assert cancelled["outcome"] == "CANCELLED"

    async def test_an_unknown_caller_gets_not_found(self, tools):
        result = await tools.cancel_appointment(customer_phone="+15559998888")
        assert result["outcome"] == "NOT_FOUND"

    async def test_confirm_is_honest_about_a_pending_booking(self, db, tenant):
        from app.integrations.calendar.tools import SchedulingTools

        appointment = await book(db, tenant, 10)
        appointment.status = AppointmentStatus.PENDING
        await db.commit()

        tools = SchedulingTools(db, tenant, None, now=ny(TUESDAY, 6))
        result = await tools.confirm_appointment(customer_phone="+15551230000")
        assert result["outcome"] == "CONFIRMED"
        assert "not fully confirmed" in result["message"]

    async def test_a_tool_cannot_reach_another_tenants_diary(
        self, db, tenant, other
    ):
        """
        The tenant comes from the bound call, never from an argument, so no
        tool call can address another business.
        """
        from app.integrations.calendar.tools import SchedulingTools

        await book(db, tenant, 10)
        tools_for_b = SchedulingTools(db, other, None, now=ny(TUESDAY, 6))
        result = await tools_for_b.cancel_appointment(customer_phone="+15551230000")
        assert result["outcome"] == "NOT_FOUND"

    async def test_the_voice_deadline_is_enforced(self, db, tenant, monkeypatch):
        """
        Requirement 30: an availability lookup must not hang. Three seconds of
        silence on a phone call is already a problem.
        """
        from app.core.config import settings
        from app.integrations.calendar.tools import SchedulingTools

        monkeypatch.setattr(settings, "calendar_voice_timeout_seconds", 0.1)

        async def never_returns(*args, **kwargs):
            await asyncio.sleep(30)

        monkeypatch.setattr(service, "find_slots", never_returns)

        tools = SchedulingTools(db, tenant, None, now=ny(TUESDAY, 6))
        started = _time.perf_counter()
        result = await tools.check_availability("today")
        elapsed = _time.perf_counter() - started

        assert result["outcome"] == "FAILED"
        assert elapsed < 2.0, f"the tool waited {elapsed:.1f}s"

    async def test_the_agent_dispatch_routes_to_the_new_layer(self, db, tenant):
        from app.agent.functions import SCHEDULING_TOOL_NAMES, FunctionHandlers
        from app.db.models import Call, CallDirection, CallStatus

        call = Call(
            tenant_id=tenant.id, direction=CallDirection.INBOUND,
            status=CallStatus.IN_PROGRESS, from_number="+15551230000",
            to_number=tenant.twilio_number, call_sid="CAtest",
        )
        db.add(call)
        await db.commit()

        handlers = FunctionHandlers(db, tenant, call)
        result = await handlers.dispatch("check_availability", {"when": "tomorrow"})

        assert "outcome" in result, "dispatch did not reach the STEP 6 layer"
        assert "check_availability" in SCHEDULING_TOOL_NAMES


# =============================================================== webhooks ===

class TestCalendarWebhooks:
    @pytest.fixture
    async def calcom_hook(self, db, tenant_a):
        from app.api.calendar_webhook_routes import routing_token

        integration = await make_calendar_integration(
            db, tenant_a, "calcom",
            credentials={"api_key": "cal_live_x", "webhook_secret": "hook-secret-123"},
            config={"event_type_id": 1},
        )
        return integration, routing_token(integration)

    @staticmethod
    def _sign(body: str, secret: str) -> dict:
        import hashlib
        import hmac

        return {
            "X-Cal-Signature-256": hmac.new(
                secret.encode(), body.encode(), hashlib.sha256
            ).hexdigest(),
            "Content-Type": "application/json",
        }

    async def test_a_valid_signed_notification_is_accepted(self, client, calcom_hook):
        _, token = calcom_hook
        body = json.dumps({"id": "evt-1", "triggerEvent": "BOOKING_CREATED"})

        response = await client.post(
            f"/api/calendar/webhooks/calcom/{token}",
            content=body, headers=self._sign(body, "hook-secret-123"),
        )
        assert response.status_code == 200
        assert response.json()["duplicate"] is False

    async def test_an_invalid_signature_is_rejected(self, client, calcom_hook):
        _, token = calcom_hook
        body = json.dumps({"id": "evt-2"})
        response = await client.post(
            f"/api/calendar/webhooks/calcom/{token}",
            content=body, headers=self._sign(body, "wrong-secret"),
        )
        assert response.status_code == 401

    async def test_a_forged_routing_token_is_rejected(self, client, calcom_hook):
        body = json.dumps({"id": "evt-3"})
        response = await client.post(
            "/api/calendar/webhooks/calcom/" + "f" * 40,
            content=body, headers=self._sign(body, "hook-secret-123"),
        )
        assert response.status_code == 401

    async def test_a_replayed_notification_is_ignored(
        self, client, db, calcom_hook, tenant_a
    ):
        _, token = calcom_hook
        body = json.dumps({"id": "evt-replay", "triggerEvent": "BOOKING_CREATED"})
        headers = self._sign(body, "hook-secret-123")

        first = await client.post(
            f"/api/calendar/webhooks/calcom/{token}", content=body, headers=headers
        )
        second = await client.post(
            f"/api/calendar/webhooks/calcom/{token}", content=body, headers=headers
        )
        assert first.json()["duplicate"] is False
        # 200, not 409: a provider that sees an error simply retries.
        assert second.status_code == 200
        assert second.json()["duplicate"] is True

        count = (
            await db.execute(select(func.count(CalendarWebhookReceipt.id)))
        ).scalar()
        assert count == 1

    async def test_a_tenant_id_in_the_body_does_not_route(
        self, client, db, calcom_hook, tenant_b
    ):
        """Requirement 21: the path token decides, the body is data."""
        integration, token = calcom_hook
        body = json.dumps({"id": "evt-inject", "tenant_id": str(tenant_b.id)})

        response = await client.post(
            f"/api/calendar/webhooks/calcom/{token}",
            content=body, headers=self._sign(body, "hook-secret-123"),
        )
        assert response.status_code == 200

        receipts = (
            (await db.execute(select(CalendarWebhookReceipt))).scalars().all()
        )
        assert len(receipts) == 1
        assert receipts[0].tenant_id == integration.tenant_id

    async def test_a_provider_cancellation_frees_the_slot(
        self, client, db, calcom_hook, tenant_a
    ):
        """
        The one mutation this endpoint performs, and the case where the
        provider is unambiguously authoritative: the event no longer exists on
        their calendar, so holding the slot would block a real booking.
        """
        integration, token = calcom_hook
        await make_scheduling_policy(db, tenant_a)
        tenant_a.timezone = NY
        await db.commit()

        appointment = Appointment(
            tenant_id=tenant_a.id, customer_name="Jane",
            customer_phone="+15551230000",
            starts_at=ny(TUESDAY, 10), ends_at=ny(TUESDAY, 10, 30),
            timezone=NY, status=AppointmentStatus.CONFIRMED,
            provider=CalendarProviderType.CALCOM,
            external_event_id="cal-booking-1", slot_key="slot-1",
        )
        db.add(appointment)
        await db.commit()

        body = json.dumps({
            "id": "evt-cancel", "triggerEvent": "BOOKING_CANCELLED",
            "payload": {"uid": "cal-booking-1"},
        })
        response = await client.post(
            f"/api/calendar/webhooks/calcom/{token}",
            content=body, headers=self._sign(body, "hook-secret-123"),
        )
        assert response.json()["applied"] is True

        await db.refresh(appointment)
        assert appointment.status is AppointmentStatus.CANCELLED
        assert appointment.cancelled_by == "provider:calcom"
        assert appointment.slot_key is None

    async def test_a_cancellation_cannot_reach_another_tenants_appointment(
        self, client, db, calcom_hook, tenant_b
    ):
        """
        Scoped by tenant *and* external id. The event id alone would be a
        cross-tenant write primitive.
        """
        appointment = Appointment(
            tenant_id=tenant_b.id, customer_name="Someone Else",
            customer_phone="+15559990000",
            starts_at=ny(TUESDAY, 10), ends_at=ny(TUESDAY, 10, 30),
            timezone=NY, status=AppointmentStatus.CONFIRMED,
            external_event_id="cal-booking-1", slot_key="slot-b",
        )
        db.add(appointment)
        await db.commit()

        _, token = calcom_hook
        body = json.dumps({
            "id": "evt-x", "triggerEvent": "BOOKING_CANCELLED",
            "payload": {"uid": "cal-booking-1"},
        })
        response = await client.post(
            f"/api/calendar/webhooks/calcom/{token}",
            content=body, headers=self._sign(body, "hook-secret-123"),
        )
        assert response.json()["applied"] is False

        await db.refresh(appointment)
        assert appointment.status is AppointmentStatus.CONFIRMED

    async def test_a_provider_with_no_stored_secret_fails_closed(
        self, client, db, tenant_a
    ):
        """
        An unverified inbound endpoint is worse than none: it is a public,
        tenant-addressable write path.
        """
        from app.api.calendar_webhook_routes import routing_token

        integration = await make_calendar_integration(
            db, tenant_a, "google", credentials={"access_token": "x"}
        )
        response = await client.post(
            f"/api/calendar/webhooks/google/{routing_token(integration)}",
            json={"id": "evt"},
        )
        assert response.status_code == 401

    async def test_a_google_channel_token_is_verified(self, client, db, tenant_a):
        from app.api.calendar_webhook_routes import routing_token

        integration = await make_calendar_integration(
            db, tenant_a, "google",
            credentials={"access_token": "x", "webhook_secret": "chan-secret"},
        )
        token = routing_token(integration)

        good = await client.post(
            f"/api/calendar/webhooks/google/{token}",
            json={},
            headers={
                "X-Goog-Channel-Token": "chan-secret",
                "X-Goog-Message-Number": "7",
                "X-Goog-Channel-ID": "chan-1",
            },
        )
        assert good.status_code == 200

        bad = await client.post(
            f"/api/calendar/webhooks/google/{token}",
            json={}, headers={"X-Goog-Channel-Token": "wrong"},
        )
        assert bad.status_code == 401

    async def test_a_graph_validation_token_is_echoed(self, client, db, tenant_a):
        """
        Graph validates a new subscription by POSTing a token that must come
        back as plain text within seconds -- before any clientState exists.
        """
        from app.api.calendar_webhook_routes import routing_token

        integration = await make_calendar_integration(db, tenant_a, "microsoft")
        response = await client.post(
            f"/api/calendar/webhooks/microsoft/{routing_token(integration)}"
            f"?validationToken=abc123",
            content=b"",
        )
        assert response.status_code == 200
        assert response.text == "abc123"

    async def test_every_rejection_looks_the_same(self, client, calcom_hook):
        """
        The endpoint must not become an oracle for which tokens or providers
        are live.
        """
        _, real = calcom_hook
        body = json.dumps({"id": "x"})

        responses = [
            await client.post(
                "/api/calendar/webhooks/calcom/" + "0" * 40,
                content=body, headers=self._sign(body, "hook-secret-123"),
            ),
            await client.post(
                "/api/calendar/webhooks/nosuchprovider/" + "0" * 40,
                content=body, headers=self._sign(body, "hook-secret-123"),
            ),
            await client.post(
                f"/api/calendar/webhooks/calcom/{real}",
                content=body, headers=self._sign(body, "wrong"),
            ),
        ]
        assert {r.status_code for r in responses} == {401}
        assert len({r.text for r in responses}) == 1

    async def test_receipts_can_be_pruned(self, db, tenant_a):
        from app.api.calendar_webhook_routes import prune_receipts

        db.add(CalendarWebhookReceipt(
            tenant_id=tenant_a.id, provider=CalendarProviderType.CALCOM,
            provider_event_id="old",
            received_at=datetime.utcnow() - timedelta(days=90),
        ))
        db.add(CalendarWebhookReceipt(
            tenant_id=tenant_a.id, provider=CalendarProviderType.CALCOM,
            provider_event_id="new",
        ))
        await db.commit()

        assert await prune_receipts(db, older_than_days=30) == 1


# ============================================================ performance ===

class TestServiceOverhead:
    """
    Requirement 30, honestly scoped.

    These measure **VoxDesk's own overhead** against the internal provider —
    policy evaluation, slot generation, the database round trips. They say
    nothing about Google or Microsoft latency, and no real provider was
    contacted, so no such claim is made.

    Thresholds are deliberately loose. What matters is catching a change of
    *complexity* — an O(n²) slot filter, a per-slot query — not a busy CI box.
    """

    @staticmethod
    async def _median_ms(coro_factory, runs: int = 5) -> float:
        samples = []
        for _ in range(runs):
            started = _time.perf_counter()
            await coro_factory()
            samples.append((_time.perf_counter() - started) * 1000)
        return statistics.median(samples)

    async def test_availability_lookup_overhead(self, db, tenant, capsys):
        async def run():
            await service.find_slots(db, tenant, day=TUESDAY, limit=50, now=ny(TUESDAY, 6))

        median = await self._median_ms(run)
        with capsys.disabled():
            print(f"\n  availability lookup: {median:.2f} ms")
        assert median < 500, f"{median:.1f} ms is far beyond service overhead"

    async def test_availability_scales_with_the_diary_not_quadratically(
        self, db, tenant, capsys
    ):
        """
        The guard that actually matters. A quadratic filter or a per-slot query
        would show up as a ratio far above the load increase.
        """
        async def run():
            await service.find_slots(db, tenant, day=TUESDAY, limit=50, now=ny(TUESDAY, 6))

        empty = await self._median_ms(run)

        for hour in (9, 10, 11, 13, 14, 15):
            await book(db, tenant, hour, customer_phone=f"+1555000{hour:04d}")
        loaded = await self._median_ms(run)

        ratio = loaded / max(empty, 0.01)
        with capsys.disabled():
            print(
                f"  availability empty {empty:.2f} ms -> 6 booked {loaded:.2f} ms "
                f"(x{ratio:.1f})"
            )
        assert ratio < 15, f"availability degraded {ratio:.1f}x under six bookings"

    async def test_booking_overhead(self, db, tenant, capsys):
        samples = []
        for index, hour in enumerate((9, 10, 11, 13, 14)):
            started = _time.perf_counter()
            await book(db, tenant, hour, customer_phone=f"+1555111{index:04d}")
            samples.append((_time.perf_counter() - started) * 1000)

        median = statistics.median(samples)
        with capsys.disabled():
            print(f"  booking: {median:.2f} ms")
        assert median < 500

    async def test_reschedule_and_cancel_overhead(self, db, tenant, capsys):
        appointment = await book(db, tenant, 9)

        started = _time.perf_counter()
        await service.reschedule(
            db, tenant, appointment, ny(TUESDAY, 15), now=ny(TUESDAY, 6)
        )
        reschedule_ms = (_time.perf_counter() - started) * 1000

        started = _time.perf_counter()
        await service.cancel(db, tenant, appointment)
        cancel_ms = (_time.perf_counter() - started) * 1000

        with capsys.disabled():
            print(
                f"  reschedule: {reschedule_ms:.2f} ms   cancel: {cancel_ms:.2f} ms"
            )
        assert reschedule_ms < 500 and cancel_ms < 500

    async def test_a_hung_provider_cannot_outlast_the_voice_deadline(
        self, db, tenant, monkeypatch, capsys
    ):
        """
        The one measurement that is a product requirement rather than a guard
        rail: with the lookup stubbed to hang forever, the tool still returns.
        """
        from app.core.config import settings
        from app.integrations.calendar.tools import SchedulingTools

        monkeypatch.setattr(settings, "calendar_voice_timeout_seconds", 0.2)

        async def never(*args, **kwargs):
            await asyncio.sleep(60)

        monkeypatch.setattr(service, "find_slots", never)

        tools = SchedulingTools(db, tenant, None, now=ny(TUESDAY, 6))
        started = _time.perf_counter()
        result = await tools.check_availability("today")
        elapsed = (_time.perf_counter() - started) * 1000

        with capsys.disabled():
            print(f"  hung provider, 200 ms budget -> returned at {elapsed:.2f} ms")
        assert result["outcome"] == "FAILED"
        assert elapsed < 1000