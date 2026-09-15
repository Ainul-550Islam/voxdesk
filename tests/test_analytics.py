"""
Analytics endpoints: aggregation, formulas, isolation and boundaries.

Requirement 31 asks for formulas verified with small deterministic datasets,
and specifically that 2 of 10 renders as `20.0` — not `0.2`, not `2000`.

The formula that matters most is `booking_rate`. The pre-STEP-8 version was
`booked / all calls`, which put wrong numbers, hang-ups and missed calls in
the denominator and made a competent operator look like a bad one.
`TestFormulas` pins the replacement.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api.analytics_routes import ELIGIBLE_CALL_SECONDS, _pct
from app.db.models import (
    Appointment,
    AppointmentStatus,
    Call,
    CallDirection,
    CallStatus,
    Lead,
    LeadStatus,
    UsageMetric,
)
from tests.conftest import add_usage, auth_headers, subscribe

UTC = timezone.utc
NY = "America/New_York"


async def seed_call(
    db, tenant, *, status=CallStatus.COMPLETED, duration=120.0,
    booked=False, escalated=False, direction=CallDirection.INBOUND,
    started_at=None,
):
    call = Call(
        tenant_id=tenant.id,
        direction=direction,
        status=status,
        from_number="+15551230000",
        to_number=tenant.twilio_number,
        call_sid=f"CA{uuid.uuid4().hex}",
        started_at=started_at or datetime.now(UTC),
        duration_seconds=duration,
        booked=booked,
        escalated=escalated,
    )
    db.add(call)
    await db.commit()
    await db.refresh(call)
    return call


# ================================================================ formulas ===

class TestPercentageHelper:
    @pytest.mark.parametrize("num,den,expected", [
        (2, 10, 20.0),          # requirement 31's example
        (1, 3, 33.3),
        (10, 10, 100.0),
        (0, 10, 0.0),
        (5, 0, 0.0),            # zero denominator, not a crash
        (0, 0, 0.0),
    ])
    def test_percentages_are_0_to_100(self, num, den, expected):
        assert _pct(num, den) == expected

    def test_it_is_not_a_fraction_and_not_basis_points(self):
        """The two ways this is usually got wrong."""
        assert _pct(2, 10) != 0.2
        assert _pct(2, 10) != 2000


class TestFormulas:
    async def test_booking_rate_excludes_calls_that_could_never_book(
        self, client, db, owner_a, tenant_a
    ):
        """
        **The formula the audit found wrong.**

        Ten calls arrive: 2 booked, 4 answered-and-eligible-but-not-booked,
        2 missed, 1 failed, 1 answered but only 3 seconds long.

        eligible = answered AND >= 10s = 2 + 4 = 6
        booking_rate = 2 / 6 = 33.3%, not 2 / 10 = 20%.

        The old denominator counted a wrong number and a rung-out call against
        the agent.
        """
        for _ in range(2):
            await seed_call(db, tenant_a, booked=True, duration=180)
        for _ in range(4):
            await seed_call(db, tenant_a, duration=90)
        for _ in range(2):
            await seed_call(db, tenant_a, status=CallStatus.NO_ANSWER, duration=0)
        await seed_call(db, tenant_a, status=CallStatus.FAILED, duration=0)
        await seed_call(db, tenant_a, duration=3.0)      # misdial

        headers = await auth_headers(client, owner_a)
        body = (await client.get(
            "/api/analytics/overview?range=last_30_days", headers=headers
        )).json()

        assert body["calls"]["total"] == 10
        assert body["conversion"]["eligible_calls"] == 6
        assert body["operations"]["booking_rate"] == 33.3
        # ...and the naive figure would have been:
        assert _pct(2, 10) == 20.0

    async def test_answer_transfer_and_failure_rates(
        self, client, db, owner_a, tenant_a
    ):
        for _ in range(6):
            await seed_call(db, tenant_a, duration=60)
        for _ in range(3):
            await seed_call(db, tenant_a, duration=60, escalated=True)
        await seed_call(db, tenant_a, status=CallStatus.FAILED, duration=0)

        headers = await auth_headers(client, owner_a)
        ops = (await client.get(
            "/api/analytics/overview?range=last_30_days", headers=headers
        )).json()["operations"]

        assert ops["answer_rate"] == 90.0        # 9 answered / 10 total
        assert ops["transfer_rate"] == 33.3      # 3 transferred / 9 answered
        assert ops["failure_rate"] == 10.0       # 1 failed / 10 total

    async def test_every_rate_is_zero_with_no_data(
        self, client, db, owner_a, tenant_a
    ):
        """Zero denominators everywhere. No division by zero, no NaN."""
        headers = await auth_headers(client, owner_a)
        body = (await client.get("/api/analytics/overview", headers=headers)).json()

        assert body["calls"]["total"] == 0
        for rate in ("answer_rate", "booking_rate", "transfer_rate", "failure_rate"):
            assert body["operations"][rate] == 0.0
        assert body["operations"]["average_call_seconds"] == 0.0

    async def test_average_duration_uses_answered_calls(
        self, client, db, owner_a, tenant_a
    ):
        """
        A missed call has zero duration. Including it in the denominator would
        halve the reported average and make the agent look terse.
        """
        await seed_call(db, tenant_a, duration=100)
        await seed_call(db, tenant_a, duration=200)
        await seed_call(db, tenant_a, status=CallStatus.NO_ANSWER, duration=0)

        headers = await auth_headers(client, owner_a)
        ops = (await client.get(
            "/api/analytics/overview", headers=headers
        )).json()["operations"]
        assert ops["average_call_seconds"] == 150.0

    async def test_the_eligibility_threshold_is_documented_and_shared(self):
        """
        The same 10-second threshold the outbound dialer already uses to
        decide a lead was reached, so the two figures agree.
        """
        import pathlib

        assert ELIGIBLE_CALL_SECONDS == 10
        source = pathlib.Path("app/telephony/twilio_handler.py").read_text()
        assert "duration_seconds or 0) > 10" in source


# ================================================================ overview ===

class TestOverview:
    async def test_call_counts(self, client, db, owner_a, tenant_a):
        await seed_call(db, tenant_a, duration=120)
        await seed_call(db, tenant_a, status=CallStatus.NO_ANSWER, duration=0)
        await seed_call(
            db, tenant_a, direction=CallDirection.OUTBOUND, duration=60
        )

        headers = await auth_headers(client, owner_a)
        body = (await client.get("/api/analytics/overview", headers=headers)).json()

        assert body["calls"] == {
            "total": 3, "answered": 2, "missed": 1, "failed": 0,
            "inbound": 2, "outbound": 1, "transferred": 0, "minutes": 3.0,
        }

    async def test_lead_and_appointment_counts(
        self, client, db, owner_a, tenant_a
    ):
        db.add(Lead(tenant_id=tenant_a.id, name="A", phone="+1555000001"))
        db.add(Lead(
            tenant_id=tenant_a.id, name="B", phone="+1555000002",
            status=LeadStatus.QUALIFIED,
        ))
        starts = datetime.now(UTC) + timedelta(days=1)
        db.add(Appointment(
            tenant_id=tenant_a.id, customer_name="C", customer_phone="+1555",
            starts_at=starts, ends_at=starts + timedelta(minutes=30),
            timezone="UTC", status=AppointmentStatus.CONFIRMED,
        ))
        await db.commit()

        headers = await auth_headers(client, owner_a)
        conv = (await client.get(
            "/api/analytics/overview", headers=headers
        )).json()["conversion"]

        assert conv["leads_created"] == 2
        assert conv["leads_qualified"] == 1
        assert conv["appointments"] == 1

    async def test_integration_failures_are_counts_not_error_text(
        self, client, db, owner_a, tenant_a, billing_plans
    ):
        """
        The overview is a tile. A provider message belongs on the integrations
        page, where it has been scrubbed for display.
        """
        from app.db.models import (
            CrmEntityType, CrmEvent, CrmEventType, CrmProviderType, CrmSync,
            CrmSyncStatus,
        )
        from tests.conftest import make_integration

        integration = await make_integration(db, tenant_a, "webhook")
        event = CrmEvent(
            tenant_id=tenant_a.id, event_type=CrmEventType.CALL_COMPLETED,
            entity_type=CrmEntityType.CALL, entity_id=uuid.uuid4(),
            idempotency_key="k1", payload={},
        )
        db.add(event)
        await db.flush()
        db.add(CrmSync(
            tenant_id=tenant_a.id, event_id=event.id,
            integration_id=integration.id, provider=CrmProviderType.WEBHOOK,
            entity_type=CrmEntityType.CALL, entity_id=event.entity_id,
            status=CrmSyncStatus.PERMANENT_FAILURE,
            last_error="a provider said something",
        ))
        await db.commit()

        headers = await auth_headers(client, owner_a)
        response = await client.get("/api/analytics/overview", headers=headers)

        assert response.json()["integrations"]["crm_sync_failures"] == 1
        assert "a provider said something" not in response.text

    async def test_usage_is_present_for_a_billing_reader(
        self, client, db, owner_a, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 600)

        headers = await auth_headers(client, owner_a)
        usage = (await client.get(
            "/api/analytics/overview", headers=headers
        )).json()["usage"]

        assert usage["plan_code"] == "pro"
        assert usage["voice_minutes_used"] == 10.0
        assert usage["voice_minutes_included"] == 2000

    async def test_usage_is_absent_rather_than_zeroed_without_permission(
        self, client, db, manager_a, tenant_a, billing_plans
    ):
        """
        A zero would read as "you have used nothing" rather than "you may not
        see this". Absence is the honest answer.
        """
        await subscribe(db, tenant_a, "pro")
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 600)

        headers = await auth_headers(client, manager_a)
        body = (await client.get("/api/analytics/overview", headers=headers)).json()

        assert body["usage"] is None
        assert body["calls"]["total"] == 0       # manager may still read analytics

    async def test_no_estimated_revenue_anywhere(
        self, client, db, owner_a, tenant_a
    ):
        """
        **Requirement 33.** The old dashboard's headline number was
        `booked * 150` -- a constant with no relationship to anything the
        tenant sells. It must not reappear.
        """
        await seed_call(db, tenant_a, booked=True, duration=120)

        headers = await auth_headers(client, owner_a)
        for path in (
            "/api/analytics/overview", "/api/analytics/calls",
            "/api/analytics/conversion",
        ):
            body = (await client.get(path, headers=headers)).text
            assert "estimated_value" not in body, path
            assert "revenue" not in body.lower(), path


# ============================================================ call analytics ===

class TestCallAnalytics:
    async def test_status_and_direction_breakdown(
        self, client, db, owner_a, tenant_a
    ):
        await seed_call(db, tenant_a, duration=60)
        await seed_call(db, tenant_a, status=CallStatus.NO_ANSWER, duration=0)
        await seed_call(db, tenant_a, status=CallStatus.FAILED, duration=0)
        await seed_call(
            db, tenant_a, direction=CallDirection.OUTBOUND, duration=30
        )

        headers = await auth_headers(client, owner_a)
        body = (await client.get("/api/analytics/calls", headers=headers)).json()

        assert body["by_status"] == {
            "answered": 2, "missed": 1, "failed": 1, "other": 0
        }
        assert body["by_direction"] == {"inbound": 3, "outbound": 1}

    async def test_the_daily_series_is_gap_filled(
        self, client, db, owner_a, tenant_a
    ):
        """
        A chart that omits zero days draws a straight line through a weekend
        and makes a quiet Saturday look like normal volume.
        """
        headers = await auth_headers(client, owner_a)
        body = (await client.get(
            "/api/analytics/calls?range=last_7_days", headers=headers
        )).json()

        assert len(body["series"]) == 7
        assert all(point["answered"] == 0 for point in body["series"])
        dates = [point["date"] for point in body["series"]]
        assert dates == sorted(dates)

    async def test_the_series_buckets_by_local_day(
        self, client, db, owner_a, tenant_a
    ):
        """
        03:00 UTC is the *previous* day in New York. Bucketing by UTC would
        file a late-evening call under tomorrow.
        """
        tenant_a.timezone = NY
        await db.commit()

        # 2026-09-02 03:00 UTC == 2026-09-01 23:00 EDT
        await seed_call(
            db, tenant_a, duration=60,
            started_at=datetime(2026, 9, 2, 3, 0, tzinfo=UTC),
        )

        headers = await auth_headers(client, owner_a)
        body = (await client.get(
            "/api/analytics/calls?start=2026-09-01&end=2026-09-02", headers=headers
        )).json()

        series = {point["date"]: point for point in body["series"]}
        assert series["2026-09-01"]["answered"] == 1
        assert series["2026-09-02"]["answered"] == 0

    async def test_the_hourly_distribution_uses_local_hours(
        self, client, db, owner_a, tenant_a
    ):
        tenant_a.timezone = NY
        await db.commit()
        await seed_call(
            db, tenant_a, duration=60,
            started_at=datetime(2026, 9, 1, 18, 0, tzinfo=UTC),   # 14:00 EDT
        )

        headers = await auth_headers(client, owner_a)
        body = (await client.get(
            "/api/analytics/calls?start=2026-09-01&end=2026-09-01", headers=headers
        )).json()

        hours = {row["hour"]: row["total"] for row in body["by_hour"]}
        assert hours[14] == 1
        assert hours[18] == 0
        assert len(body["by_hour"]) == 24


# ============================================================== conversion ===

class TestConversion:
    async def test_the_funnel_stages(self, client, db, owner_a, tenant_a):
        for _ in range(3):
            await seed_call(db, tenant_a, duration=120, booked=True)
        for _ in range(2):
            await seed_call(db, tenant_a, duration=5)     # not eligible

        db.add(Lead(tenant_id=tenant_a.id, name="L", phone="+1555000001"))
        starts = datetime.now(UTC) + timedelta(days=1)
        db.add(Appointment(
            tenant_id=tenant_a.id, customer_name="C", customer_phone="+1555",
            starts_at=starts, ends_at=starts + timedelta(minutes=30),
            timezone="UTC", status=AppointmentStatus.CONFIRMED,
        ))
        db.add(Appointment(
            tenant_id=tenant_a.id, customer_name="D", customer_phone="+1556",
            starts_at=starts, ends_at=starts + timedelta(minutes=30),
            timezone="UTC", status=AppointmentStatus.NO_SHOW,
        ))
        await db.commit()

        headers = await auth_headers(client, owner_a)
        body = (await client.get(
            "/api/analytics/conversion", headers=headers
        )).json()

        stages = {s["stage"]: s["count"] for s in body["funnel"]}
        assert stages["calls"] == 5
        assert stages["eligible"] == 3
        assert stages["leads"] == 1
        # A no-show *was* booked, so it belongs in the booked stage; dropping
        # out is exactly what the next stage measures.
        assert stages["appointments"] == 2
        assert stages["kept"] == 1

    async def test_every_funnel_stage_explains_itself(
        self, client, db, owner_a, tenant_a
    ):
        """A stage nobody can define is a number nobody can defend."""
        headers = await auth_headers(client, owner_a)
        body = (await client.get(
            "/api/analytics/conversion", headers=headers
        )).json()
        assert all(stage["note"] for stage in body["funnel"])

    async def test_a_rate_cannot_exceed_one_hundred(
        self, client, db, owner_a, tenant_a
    ):
        """
        A lead can be created by a bulk import rather than a call, so
        `leads / eligible_calls` can genuinely exceed 1. A funnel stage wider
        than the one above it is confusing rather than informative, so it is
        capped -- and the raw counts are still shown.
        """
        await seed_call(db, tenant_a, duration=120)
        for index in range(50):
            db.add(Lead(
                tenant_id=tenant_a.id, name=f"L{index}",
                phone=f"+1555000{index:04d}",
            ))
        await db.commit()

        headers = await auth_headers(client, owner_a)
        rates = (await client.get(
            "/api/analytics/conversion", headers=headers
        )).json()["rates"]

        assert rates["call_to_lead_rate"] == 100.0


# ============================================================= date windows ===

class TestDateWindows:
    @pytest.mark.parametrize("preset", [
        "today", "yesterday", "last_7_days", "last_30_days",
        "this_month", "previous_month",
    ])
    async def test_every_preset_resolves(
        self, client, db, owner_a, tenant_a, preset
    ):
        headers = await auth_headers(client, owner_a)
        response = await client.get(
            f"/api/analytics/overview?range={preset}", headers=headers
        )
        assert response.status_code == 200
        assert response.json()["window"]["label"] == preset

    async def test_an_unknown_preset_is_422_and_lists_the_valid_ones(
        self, client, db, owner_a, tenant_a
    ):
        headers = await auth_headers(client, owner_a)
        response = await client.get(
            "/api/analytics/overview?range=last_century", headers=headers
        )
        assert response.status_code == 422
        assert "last_7_days" in response.json()["detail"]

    async def test_a_custom_range(self, client, db, owner_a, tenant_a):
        headers = await auth_headers(client, owner_a)
        body = (await client.get(
            "/api/analytics/overview?start=2026-01-01&end=2026-01-31",
            headers=headers,
        )).json()
        assert body["window"]["label"] == "custom"
        assert body["window"]["days"] == 31

    async def test_a_backwards_range_is_refused(self, client, db, owner_a, tenant_a):
        headers = await auth_headers(client, owner_a)
        response = await client.get(
            "/api/analytics/overview?start=2026-02-01&end=2026-01-01",
            headers=headers,
        )
        assert response.status_code == 422

    async def test_an_absurd_range_is_refused(self, client, db, owner_a, tenant_a):
        """One request must not be able to scan a decade."""
        headers = await auth_headers(client, owner_a)
        response = await client.get(
            "/api/analytics/overview?start=2000-01-01&end=2030-01-01",
            headers=headers,
        )
        assert response.status_code == 422

    async def test_the_window_is_resolved_in_the_tenant_timezone(
        self, client, db, owner_a, tenant_a
    ):
        """
        "Today" for a business in Los Angeles is not "today" for the laptop
        viewing it from Dhaka. Letting the client decide means two people see
        different numbers on the same dashboard.
        """
        tenant_a.timezone = "America/Los_Angeles"
        await db.commit()

        headers = await auth_headers(client, owner_a)
        body = (await client.get(
            "/api/analytics/overview?range=today", headers=headers
        )).json()

        assert body["window"]["timezone"] == "America/Los_Angeles"
        # Local midnight in LA is 07:00 or 08:00 UTC, never 00:00.
        assert body["window"]["start"].endswith(("07:00:00+00:00", "08:00:00+00:00"))

    async def test_the_window_is_half_open(self, client, db, owner_a, tenant_a):
        """
        A call at exactly midnight belongs to one day. Closed bounds would
        count it in two.
        """
        await seed_call(
            db, tenant_a, duration=60,
            started_at=datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
        )
        headers = await auth_headers(client, owner_a)

        first = (await client.get(
            "/api/analytics/overview?start=2026-09-01&end=2026-09-01",
            headers=headers,
        )).json()
        second = (await client.get(
            "/api/analytics/overview?start=2026-09-02&end=2026-09-02",
            headers=headers,
        )).json()

        assert first["calls"]["total"] + second["calls"]["total"] == 1

    async def test_calls_outside_the_window_are_excluded(
        self, client, db, owner_a, tenant_a
    ):
        await seed_call(
            db, tenant_a, duration=60,
            started_at=datetime.now(UTC) - timedelta(days=90),
        )
        await seed_call(db, tenant_a, duration=60)

        headers = await auth_headers(client, owner_a)
        body = (await client.get(
            "/api/analytics/overview?range=last_7_days", headers=headers
        )).json()
        assert body["calls"]["total"] == 1


# =============================================== isolation and permissions ===

class TestAnalyticsSecurity:
    async def test_tenant_b_never_sees_tenant_a_numbers(
        self, client, db, owner_b, tenant_a, tenant_b
    ):
        for _ in range(5):
            await seed_call(db, tenant_a, duration=300, booked=True)

        headers = await auth_headers(client, owner_b)
        for path in (
            "/api/analytics/overview", "/api/analytics/calls",
            "/api/analytics/conversion",
        ):
            body = (await client.get(path, headers=headers)).json()
            total = (
                body["calls"]["total"] if "calls" in body and
                isinstance(body["calls"], dict) and "total" in body["calls"]
                else body.get("totals", {}).get("total", 0)
            )
            assert total == 0, path

    async def test_a_tenant_id_query_parameter_is_ignored(
        self, client, db, owner_b, tenant_a, tenant_b
    ):
        """Requirement 24: the tenant comes from the verified JWT, never a filter."""
        for _ in range(5):
            await seed_call(db, tenant_a, duration=300)

        headers = await auth_headers(client, owner_b)
        body = (await client.get(
            f"/api/analytics/overview?tenant_id={tenant_a.id}", headers=headers
        )).json()
        assert body["calls"]["total"] == 0

    @pytest.mark.parametrize("path", [
        "/api/analytics/overview", "/api/analytics/calls",
        "/api/analytics/conversion", "/api/analytics/usage",
    ])
    async def test_unauthenticated_requests_are_rejected(self, client, path):
        assert (await client.get(path)).status_code in (401, 403)

    async def test_analytics_follows_the_existing_rbac_policy(
        self, client, db, agent_a, viewer_a, tenant_a
    ):
        """
        `ANALYTICS_READ` is in `READ_ONLY_OPERATIONAL`, so every role has it --
        an agent seeing how their own calls are going is deliberate, and STEP 8
        does not relitigate a STEP 2 policy decision.

        What matters is that the *billing* half is separately gated, which the
        usage tests assert.
        """
        for user in (agent_a, viewer_a):
            headers = await auth_headers(client, user)
            response = await client.get("/api/analytics/overview", headers=headers)
            assert response.status_code == 200
            # ...but no usage figures without BILLING_READ.
            assert response.json()["usage"] is None

    async def test_usage_analytics_needs_billing_permission(
        self, client, db, manager_a, owner_a, tenant_a, billing_plans
    ):
        await subscribe(db, tenant_a, "pro")

        manager = await auth_headers(client, manager_a)
        assert (
            await client.get("/api/analytics/usage", headers=manager)
        ).status_code == 403

        owner = await auth_headers(client, owner_a)
        assert (
            await client.get("/api/analytics/usage", headers=owner)
        ).status_code == 200


# ============================================================= usage route ===

class TestUsageAnalytics:
    async def test_it_reports_the_billing_period_not_a_date_range(
        self, client, db, owner_a, tenant_a, billing_plans
    ):
        """
        Usage belongs to a subscription-anchored period. Letting an analytics
        date picker slice it would produce a number that looks like a bill and
        is not one.
        """
        await subscribe(db, tenant_a, "pro")
        await add_usage(db, tenant_a, UsageMetric.VOICE_MINUTE, 2_100 * 60)

        headers = await auth_headers(client, owner_a)
        body = (await client.get("/api/analytics/usage", headers=headers)).json()

        assert body["plan_code"] == "pro"
        metrics = {m["metric"]: m for m in body["metrics"]}
        assert metrics["voice_minute"]["used"] == 2_100
        assert metrics["voice_minute"]["included"] == 2_000
        assert metrics["voice_minute"]["overage"] == 100
        assert body["estimated_overage_cents"] == 1_000

    async def test_it_reuses_the_billing_layer(self):
        """
        Requirement: do not reimplement billing. A second implementation of
        "how much have they used" is how two screens start disagreeing.
        """
        import pathlib

        source = pathlib.Path("app/api/analytics_routes.py").read_text()
        assert "from app.billing import metering" in source
        assert "metering.period_usage" in source
        # ...and it does not re-sum the event table itself.
        assert "UsageEvent" not in source


# ============================================================= performance ===

class TestAggregationShape:
    async def test_the_overview_does_not_scale_its_query_count_with_rows(
        self, client, db, owner_a, tenant_a, capsys
    ):
        """
        Requirement 32: a tenant with 400 calls must cost the same number of
        round trips as one with 4. Asserted by counting statements, which is
        the property that actually matters -- wall-clock on SQLite says
        nothing about PostgreSQL.
        """
        from sqlalchemy import event as sa_event

        headers = await auth_headers(client, owner_a)

        async def count_statements() -> int:
            statements = []
            engine = db.get_bind()

            def before(conn, cursor, statement, *args):
                statements.append(statement)

            sa_event.listen(engine, "before_cursor_execute", before)
            try:
                await client.get("/api/analytics/overview", headers=headers)
            finally:
                sa_event.remove(engine, "before_cursor_execute", before)
            return len(statements)

        for _ in range(4):
            await seed_call(db, tenant_a, duration=60)
        small = await count_statements()

        for _ in range(60):
            await seed_call(db, tenant_a, duration=60)
        large = await count_statements()

        with capsys.disabled():
            print(f"\n  overview: {small} statements at 4 calls, "
                  f"{large} at 64 calls")
        assert large == small, "the query count must not grow with row count"

    async def test_call_totals_are_one_query(self, db, tenant_a):
        """
        Eight counts from one scan, not eight round trips. Conditional
        aggregation rather than N+1.
        """
        from app.api.analytics_routes import _call_totals, resolve_window
        from sqlalchemy import event as sa_event

        for _ in range(5):
            await seed_call(db, tenant_a, duration=60)

        window = resolve_window(tenant_a, start=None, end=None, preset="last_30_days")
        statements = []
        engine = db.get_bind()

        def before(conn, cursor, statement, *args):
            statements.append(statement)

        sa_event.listen(engine, "before_cursor_execute", before)
        try:
            totals = await _call_totals(db, tenant_a.id, window)
        finally:
            sa_event.remove(engine, "before_cursor_execute", before)

        assert totals["total"] == 5
        assert len(statements) == 1, statements