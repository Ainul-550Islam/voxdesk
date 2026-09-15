"""
Timezone, DST and natural date/time interpretation.

Requirement 4 calls this critical, and it is the part of scheduling that fails
silently. An off-by-one-hour bug reproduces on two Sundays a year, never in
testing, and shows up as a customer standing outside a locked door.

Every DST date used here is real: `America/New_York` springs forward
2026-03-08 and falls back 2026-11-01; `Europe/London` on 2026-03-29 and
2026-10-25; `Australia/Lord_Howe` shifts by **30 minutes**, which is the case
that breaks implementations assuming a whole hour.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.integrations.calendar.nlp import parse_request
from app.integrations.calendar.timezones import (
    UTC,
    AmbiguousTimeError,
    NonexistentTimeError,
    TimezoneError,
    add_days_local,
    as_utc,
    combine_local,
    format_spoken,
    get_zone,
    is_valid_zone,
    now_utc,
    parse_clock,
    resolve_local,
    to_local,
    to_utc,
)

NY = "America/New_York"
LA = "America/Los_Angeles"
DHAKA = "Asia/Dhaka"
LONDON = "Europe/London"
LORD_HOWE = "Australia/Lord_Howe"


# ================================================================== zones ===

class TestZoneResolution:
    @pytest.mark.parametrize("name", [NY, LA, DHAKA, LONDON, "UTC", "Europe/Berlin"])
    def test_valid_iana_identifiers_resolve(self, name):
        assert isinstance(get_zone(name), ZoneInfo)
        assert is_valid_zone(name)

    @pytest.mark.parametrize("name", [
        "", None, "Mars/Olympus", "EST5EDT-nonsense", "America/New York",
    ])
    def test_invalid_identifiers_raise_rather_than_defaulting(self, name):
        """
        A typo'd zone silently becoming UTC is the exact class of bug this
        module exists to prevent, so `get_zone` raises and the caller decides.
        """
        with pytest.raises(TimezoneError):
            get_zone(name)
        assert not is_valid_zone(name)

    def test_a_windows_style_name_is_rejected(self):
        """
        Microsoft Graph speaks "Pacific Standard Time". Accepting that here
        would mean shipping an IANA-to-Windows mapping table, which is a
        second source of truth for DST. The adapter sends UTC instead.
        """
        assert not is_valid_zone("Pacific Standard Time")

    def test_tenant_zone_falls_back_without_raising(self):
        from app.integrations.calendar.timezones import tenant_zone

        class BadTenant:
            timezone = "Nowhere/Nothing"

        # A bad row must degrade, not take every call down. Validation
        # happens at write time in the API.
        assert str(tenant_zone(BadTenant())) == "UTC"


# ============================================================ UTC storage ===

class TestUtcConversion:
    def test_a_local_time_converts_to_the_right_instant(self):
        resolved = resolve_local(datetime(2026, 6, 15, 15, 0), NY)
        assert resolved.utc == datetime(2026, 6, 15, 19, 0, tzinfo=UTC)   # EDT, -4
        assert resolved.local.isoformat() == "2026-06-15T15:00:00-04:00"

    def test_the_same_wall_clock_in_winter_is_a_different_instant(self):
        """EST is -5; EDT is -4. A fixed offset would get one of them wrong."""
        summer = resolve_local(datetime(2026, 6, 15, 15, 0), NY).utc
        winter = resolve_local(datetime(2026, 1, 15, 15, 0), NY).utc
        assert summer.hour == 19
        assert winter.hour == 20

    def test_a_half_hour_offset_zone_works(self):
        """Asia/Kolkata is +05:30. Integer-hour assumptions break here."""
        resolved = resolve_local(datetime(2026, 6, 15, 15, 0), "Asia/Kolkata")
        assert resolved.utc == datetime(2026, 6, 15, 9, 30, tzinfo=UTC)

    def test_dhaka_has_no_dst(self):
        summer = resolve_local(datetime(2026, 6, 15, 15, 0), DHAKA).utc
        winter = resolve_local(datetime(2026, 1, 15, 15, 0), DHAKA).utc
        assert summer.hour == winter.hour == 9

    def test_round_trip_is_lossless(self):
        for zone in (NY, LA, DHAKA, LONDON):
            original = datetime(2026, 7, 4, 11, 45)
            resolved = resolve_local(original, zone)
            assert to_local(resolved.utc, zone).replace(tzinfo=None) == original

    def test_to_utc_refuses_a_naive_value_with_no_zone(self):
        """
        "Assume UTC" and "assume server local" are both wrong about half the
        time, so neither is the default.
        """
        with pytest.raises(TimezoneError):
            to_utc(datetime(2026, 6, 15, 15, 0))

    def test_to_utc_passes_through_an_aware_value(self):
        aware = datetime(2026, 6, 15, 19, 0, tzinfo=UTC)
        assert to_utc(aware) == aware

    def test_now_utc_is_aware(self):
        """
        `datetime.utcnow()` returns a *naive* value that compares wrongly
        against aware ones. The pre-STEP-6 reminder scheduler did exactly that
        and silently dropped every reminder due within the tenant's offset.
        """
        assert now_utc().tzinfo is not None

    def test_as_utc_attaches_a_zone_to_a_database_read(self):
        """SQLite hands back naive values even for timezone=True columns."""
        assert as_utc(datetime(2026, 6, 15, 19, 0)).tzinfo is UTC


# ======================================================== spring forward ===

class TestNonexistentTimes:
    def test_a_skipped_local_time_is_detected(self):
        """
        2026-03-08 02:30 does not exist in New York. `zoneinfo` does not
        raise -- it silently produces a datetime that round-trips to a
        different wall clock, which is worse than an error.
        """
        with pytest.raises(NonexistentTimeError):
            resolve_local(datetime(2026, 3, 8, 2, 30), NY)

    def test_the_reported_gap_is_the_real_transition(self):
        with pytest.raises(NonexistentTimeError) as caught:
            resolve_local(datetime(2026, 3, 8, 2, 30), NY)

        # Useful for a clarification question: "we skip from 2 to 3".
        assert caught.value.gap_start.hour == 1
        assert caught.value.gap_end.hour == 3
        assert caught.value.gap_end.minute == 0

    @pytest.mark.parametrize("zone,moment,expected_end", [
        (NY, datetime(2026, 3, 8, 2, 30), 3),
        (LA, datetime(2026, 3, 8, 2, 15), 3),
        (LONDON, datetime(2026, 3, 29, 1, 30), 2),
    ])
    def test_detected_across_zones(self, zone, moment, expected_end):
        with pytest.raises(NonexistentTimeError) as caught:
            resolve_local(moment, zone)
        assert caught.value.gap_end.hour == expected_end

    def test_a_thirty_minute_shift_is_detected(self):
        """
        Lord Howe Island shifts by 30 minutes, not an hour. Implementations
        that hard-code a one-hour jump miss this entirely.
        """
        with pytest.raises(NonexistentTimeError) as caught:
            resolve_local(datetime(2026, 10, 4, 2, 15), LORD_HOWE)
        assert caught.value.gap_start.hour == 1
        assert (caught.value.gap_end.hour, caught.value.gap_end.minute) == (2, 30)

    def test_shift_mode_moves_to_the_first_real_instant(self):
        """
        03:00, not 03:30.

        "Shift" means the first instant that actually exists after the gap,
        not "the requested time plus the DST offset". For the case this
        actually serves -- a business whose opening time falls inside the gap
        -- opening as soon as the clock permits is the right answer, and it is
        the one instant that is guaranteed to exist in every zone regardless
        of whether the shift is 30 or 60 minutes.
        """
        resolved = resolve_local(
            datetime(2026, 3, 8, 2, 30), NY, on_nonexistent="shift"
        )
        assert (resolved.local.hour, resolved.local.minute) == (3, 0)
        # ...and it really is a valid instant.
        assert to_local(resolved.utc, NY) == resolved.local

    def test_times_either_side_of_the_gap_are_fine(self):
        assert resolve_local(datetime(2026, 3, 8, 1, 30), NY).utc.hour == 6
        assert resolve_local(datetime(2026, 3, 8, 3, 30), NY).utc.hour == 7

    def test_a_zone_without_dst_never_reports_a_gap(self):
        assert resolve_local(datetime(2026, 3, 8, 2, 30), DHAKA).local.hour == 2


# ============================================================= fall back ===

class TestAmbiguousTimes:
    def test_a_doubled_local_time_is_detected(self):
        """2026-11-01 01:30 happens twice in New York."""
        with pytest.raises(AmbiguousTimeError):
            resolve_local(datetime(2026, 11, 1, 1, 30), NY)

    def test_both_candidates_are_offered(self):
        with pytest.raises(AmbiguousTimeError) as caught:
            resolve_local(datetime(2026, 11, 1, 1, 30), NY)

        first, second = caught.value.first, caught.value.second
        assert first.utcoffset() == timedelta(hours=-4)      # EDT
        assert second.utcoffset() == timedelta(hours=-5)     # EST
        assert (second.astimezone(UTC) - first.astimezone(UTC)) == timedelta(hours=1)

    def test_earliest_and_latest_resolve_deterministically(self):
        early = resolve_local(
            datetime(2026, 11, 1, 1, 30), NY, on_ambiguous="earliest"
        )
        late = resolve_local(
            datetime(2026, 11, 1, 1, 30), NY, on_ambiguous="latest"
        )
        assert early.was_ambiguous and late.was_ambiguous
        assert late.utc - early.utc == timedelta(hours=1)

    def test_raising_is_the_default(self):
        """
        Requirement 16: an ambiguous request produces a clarification
        question. The only way to guarantee that is to make silence
        impossible to get by accident.
        """
        with pytest.raises(AmbiguousTimeError):
            resolve_local(datetime(2026, 10, 25, 1, 30), LONDON)

    def test_an_unambiguous_time_is_not_flagged(self):
        assert resolve_local(datetime(2026, 11, 1, 3, 30), NY).was_ambiguous is False


# ======================================================= DST arithmetic ===

class TestDstArithmetic:
    def test_same_time_tomorrow_survives_spring_forward(self):
        """
        **The bug this exists to prevent.** Adding `timedelta(days=1)` adds 24
        hours of *elapsed* time, which across a spring-forward boundary lands
        an hour late on the wall clock. A caller who says "same time tomorrow"
        means the wall clock.
        """
        before = resolve_local(datetime(2026, 3, 7, 15, 0), NY).utc

        naive = to_local(before + timedelta(days=1), NY)
        correct = to_local(add_days_local(before, 1, NY), NY)

        assert naive.hour == 16, "sanity: the naive approach really is wrong"
        assert correct.hour == 15

    def test_same_time_tomorrow_survives_fall_back(self):
        before = resolve_local(datetime(2026, 10, 31, 15, 0), NY).utc

        naive = to_local(before + timedelta(days=1), NY)
        correct = to_local(add_days_local(before, 1, NY), NY)

        assert naive.hour == 14
        assert correct.hour == 15

    def test_a_week_ahead_across_a_transition_keeps_its_hour(self):
        before = resolve_local(datetime(2026, 3, 3, 9, 30), NY).utc
        after = add_days_local(before, 7, NY)
        local = to_local(after, NY)
        assert (local.hour, local.minute) == (9, 30)
        assert local.date() == date(2026, 3, 10)

    def test_no_drift_in_a_zone_without_dst(self):
        before = resolve_local(datetime(2026, 3, 7, 15, 0), DHAKA).utc
        assert to_local(add_days_local(before, 1, DHAKA), DHAKA).hour == 15


# ========================================================= tenant zone ===

class TestTenantTimezone:
    async def test_a_tenant_carries_its_own_zone(self, db, tenant_a):
        tenant_a.timezone = DHAKA
        await db.commit()

        from app.integrations.calendar.timezones import tenant_zone

        assert str(tenant_zone(tenant_a)) == DHAKA

    async def test_two_tenants_interpret_the_same_wall_clock_differently(
        self, db, tenant_a, tenant_b
    ):
        """
        The reason a tenant timezone exists at all. "3 PM Tuesday" is a
        different instant for a business in New York than for one in Dhaka,
        and a shared server clock cannot tell them apart.
        """
        tenant_a.timezone = NY
        tenant_b.timezone = DHAKA
        await db.commit()

        wall = datetime(2026, 6, 15, 15, 0)
        assert resolve_local(wall, tenant_a.timezone).utc != resolve_local(
            wall, tenant_b.timezone
        ).utc

    async def test_an_appointment_stores_the_zone_it_was_agreed_in(
        self, db, tenant_a
    ):
        """
        Not derivable from the tenant afterwards: a business that relocates,
        or corrects a wrong timezone, would otherwise silently reinterpret
        every appointment already in the book.
        """
        from tests.conftest import make_calendar_integration
        from app.integrations.calendar import service

        tenant_a.timezone = NY
        await make_calendar_integration(db, tenant_a, "internal")
        await db.commit()

        start = resolve_local(datetime(2026, 6, 16, 15, 0), NY).utc
        result = await service.book(
            db, tenant_a,
            service.BookingRequest(
                tenant_id=tenant_a.id, start=start,
                customer_name="Jane", customer_phone="+15550001111",
            ),
            now=start - timedelta(days=1),
        )
        assert result.outcome.value == "BOOKED"

        from app.db.models import Appointment
        appointment = await db.get(Appointment, __import__("uuid").UUID(result.appointment_id))
        assert appointment.timezone == NY

        # The tenant moves. The appointment must not move with them.
        tenant_a.timezone = LA
        await db.commit()
        await db.refresh(appointment)
        assert appointment.timezone == NY


# ============================================================== helpers ===

class TestFormatting:
    @pytest.mark.parametrize("hour,minute,expected", [
        (9, 0, "9 am"), (9, 30, "9:30 am"), (12, 0, "12 pm"),
        (13, 0, "1 pm"), (15, 45, "3:45 pm"), (0, 0, "12 am"),
    ])
    def test_spoken_times_are_local_and_natural(self, hour, minute, expected):
        moment = resolve_local(datetime(2026, 6, 15, hour, minute), NY).utc
        assert format_spoken(moment, NY) == expected

    def test_spoken_time_uses_the_local_zone_not_utc(self):
        """3 PM in New York is 19:00 UTC. The caller must hear "3 pm"."""
        moment = resolve_local(datetime(2026, 6, 15, 15, 0), NY).utc
        assert format_spoken(moment, NY) == "3 pm"
        assert format_spoken(moment, "UTC") == "7 pm"

    @pytest.mark.parametrize("text,expected", [
        ("3", time(3, 0)), ("3pm", time(15, 0)), ("15:30", time(15, 30)),
        ("10:30 am", time(10, 30)), ("12am", time(0, 0)), ("12pm", time(12, 0)),
    ])
    def test_parse_clock_accepts_the_shapes_a_model_emits(self, text, expected):
        assert parse_clock(text) == expected

    @pytest.mark.parametrize("text", ["25", "half past", "", "9:99", "abc", "13pm"])
    def test_parse_clock_refuses_nonsense_rather_than_guessing(self, text):
        assert parse_clock(text) is None

    def test_combine_local_builds_an_instant_from_a_date_and_a_time(self):
        resolved = combine_local(date(2026, 6, 15), time(9, 0), NY)
        assert resolved.utc == datetime(2026, 6, 15, 13, 0, tzinfo=UTC)


# ============================================ natural language (req 16) ===

class TestNaturalLanguage:
    #: Tuesday 2026-09-01, 10:00 EDT.
    NOW = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)

    def parse(self, text, **kwargs):
        return parse_request(text, timezone_name=NY, now=self.NOW, **kwargs)

    @pytest.mark.parametrize("text,expected", [
        ("tomorrow", date(2026, 9, 2)),
        ("today", date(2026, 9, 1)),
        ("day after tomorrow", date(2026, 9, 3)),
        ("in 3 days", date(2026, 9, 4)),
        ("next Tuesday", date(2026, 9, 8)),
        ("Friday", date(2026, 9, 4)),
        ("this coming Monday", date(2026, 9, 7)),
        ("2026-12-24", date(2026, 12, 24)),
    ])
    def test_relative_days_resolve(self, text, expected):
        assert self.parse(text).day == expected

    def test_a_weekday_matching_today_asks_which_one(self):
        """
        "Tuesday", said on a Tuesday, is genuinely two dates. Picking one
        silently is how someone arrives a week early.
        """
        parsed = self.parse("Tuesday")
        assert parsed.is_ambiguous
        assert "today" in parsed.ambiguity.question.lower()

    @pytest.mark.parametrize("text,expected", [
        ("at 3pm", time(15, 0)),
        ("around 10:30", time(10, 30)),
        ("half past four", time(16, 30)),
        ("quarter to five", time(16, 45)),
        ("quarter past nine", time(9, 15)),
        ("at noon", time(12, 0)),
        ("at midnight", time(0, 0)),
        ("at three in the morning", time(3, 0)),
        ("at 15:30", time(15, 30)),
    ])
    def test_times_resolve_including_spoken_words(self, text, expected):
        """
        Speech-to-text produces words, not digits. A digit-only parser misses
        most of what actually arrives.
        """
        assert self.parse(text).at == expected

    def test_an_unqualified_middling_hour_asks(self):
        parsed = self.parse("at seven")
        assert parsed.is_ambiguous
        assert "morning" in parsed.ambiguity.question

    @pytest.mark.parametrize("text,expected", [
        ("at 5", time(17, 0)),      # nobody books a dentist at 5 AM
        ("at 9", time(9, 0)),       # nobody books one at 9 PM either
    ])
    def test_hours_that_only_make_sense_one_way_are_resolved(self, text, expected):
        assert self.parse(text).at == expected

    def test_part_of_day_disambiguates(self):
        assert self.parse("at seven in the evening").at == time(19, 0)
        assert self.parse("seven in the morning").at == time(7, 0)

    @pytest.mark.parametrize("text,part", [
        ("Friday afternoon", "afternoon"),
        ("tomorrow morning", "morning"),
        ("tonight", "evening"),
    ])
    def test_part_of_day_is_extracted(self, text, part):
        assert self.parse(text).part_of_day == part

    def test_an_iso_date_does_not_leak_digits_into_the_time(self):
        """
        Regression: the time parser used to read the month out of
        "2026-12-24 at 9am" and ask whether "12" meant morning or evening.
        """
        parsed = self.parse("2026-12-24 at 9am")
        assert parsed.day == date(2026, 12, 24)
        assert parsed.at == time(9, 0)
        assert not parsed.is_ambiguous

    def test_a_relative_day_count_is_not_read_as_a_time(self):
        """Regression: "in 3 days" used to book 3 PM."""
        parsed = self.parse("in 3 days")
        assert parsed.day == date(2026, 9, 4)
        assert parsed.at is None

    def test_unparseable_input_returns_nothing_rather_than_today(self):
        """
        Defaulting is how "sometime next month" becomes an appointment this
        afternoon.
        """
        parsed = self.parse("hjkl qwerty")
        assert parsed.day is None
        assert parsed.at is None

    def test_an_explicit_time_argument_wins(self):
        parsed = self.parse("tomorrow", explicit_time="2:15pm")
        assert parsed.day == date(2026, 9, 2)
        assert parsed.at == time(14, 15)

    def test_the_relative_day_is_the_business_local_day(self):
        """
        "Tomorrow" at 23:00 in Dhaka is a different date than the same instant
        interpreted in UTC, which is why this is resolved per tenant.
        """
        late = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)   # 2026-09-02 00:00 Dhaka
        in_dhaka = parse_request("tomorrow", timezone_name=DHAKA, now=late)
        in_ny = parse_request("tomorrow", timezone_name=NY, now=late)
        assert in_dhaka.day == date(2026, 9, 3)
        assert in_ny.day == date(2026, 9, 2)