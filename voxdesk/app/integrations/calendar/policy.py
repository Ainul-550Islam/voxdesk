"""
Business hours and slot generation.

The distinction requirement 5 insists on, and which the pre-STEP-6 code did not
make: **business policy is not provider availability.** A dentist's Google
calendar being empty at 3 AM does not make 3 AM bookable. Both are checked, in
that order, because the cheap local filter should run before the network call.

The pipeline:

    requested window
      → business hours (weekday intervals, breaks, holidays, blocked periods)
      → minimum notice / booking horizon
      → candidate slot grid
      → provider free/busy            (network)
      → VoxDesk's own appointments    (database)
      → buffers
      → offered slots

Everything here is pure except the two lookups the service injects, so the
whole policy is testable without a database or a provider.

All reasoning is done on **local wall-clock** and resolved to UTC at the
boundary. That ordering is not stylistic: "09:00 to 17:00 on Tuesday" is a
statement about a wall clock, and doing the arithmetic in UTC means a business
in a DST zone opens an hour early for half the year.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable

from app.integrations.calendar.models import AvailabilitySlot, BusyPeriod, TimeWindow
from app.integrations.calendar.timezones import (
    NonexistentTimeError,
    get_zone,
    now_utc,
    resolve_local,
    to_local,
)

#: Index 0 is Monday, matching `datetime.weekday()`.
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class PolicyError(ValueError):
    """The stored policy is malformed. Raised on write, never during a call."""


@dataclass(frozen=True)
class SchedulingRules:
    """
    A tenant's booking policy, resolved.

    Built either from a `SchedulingPolicy` row or — for a tenant that has not
    configured one — from the legacy `Tenant.business_open` / `business_close`
    / `appointment_minutes` columns, so behaviour is unchanged for anyone who
    has not opted in.
    """

    timezone: str
    weekly_hours: dict[str, list[tuple[time, time]]]
    holidays: frozenset[date] = frozenset()
    blocked: tuple[TimeWindow, ...] = ()
    slot_minutes: int = 30
    slot_interval_minutes: int = 30
    buffer_before_minutes: int = 0
    buffer_after_minutes: int = 0
    minimum_notice_minutes: int = 60
    booking_horizon_days: int = 60
    max_slots_offered: int = 3
    allow_outside_business_hours: bool = False
    require_provider_confirmation: bool = True

    @property
    def duration(self) -> timedelta:
        return timedelta(minutes=self.slot_minutes)

    @property
    def buffer_before(self) -> timedelta:
        return timedelta(minutes=self.buffer_before_minutes)

    @property
    def buffer_after(self) -> timedelta:
        return timedelta(minutes=self.buffer_after_minutes)

    def intervals_for(self, day: date) -> list[tuple[time, time]]:
        """Opening intervals on a given local date. Empty means closed."""
        if day in self.holidays:
            return []
        return list(self.weekly_hours.get(WEEKDAY_KEYS[day.weekday()], []))

    def is_open_on(self, day: date) -> bool:
        return bool(self.intervals_for(day))


# ------------------------------------------------------------ construction ---

def _parse_time(raw) -> time:
    if isinstance(raw, time):
        return raw
    if not isinstance(raw, str):
        raise PolicyError(f"{raw!r} is not a time")
    text = raw.strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise PolicyError(f"{raw!r} is not a HH:MM time")


def parse_weekly_hours(raw: dict | None) -> dict[str, list[tuple[time, time]]]:
    """
    Validate `{"mon": [["09:00", "17:00"], ...]}`.

    Intervals are sorted and checked for overlap. An overlapping pair is
    rejected rather than merged, because it usually means someone meant to
    write a break and wrote the wrong boundary — silently merging would
    produce a schedule the tenant did not ask for and cannot see is wrong.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise PolicyError("weekly_hours must be an object keyed by weekday")

    parsed: dict[str, list[tuple[time, time]]] = {}
    for key, value in raw.items():
        day = str(key).strip().lower()[:3]
        if day not in WEEKDAY_KEYS:
            raise PolicyError(
                f"{key!r} is not a weekday; use {', '.join(WEEKDAY_KEYS)}"
            )
        if value in (None, []):
            parsed[day] = []
            continue
        if not isinstance(value, list):
            raise PolicyError(f"{key!r} must be a list of [open, close] pairs")

        intervals: list[tuple[time, time]] = []
        for entry in value:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                raise PolicyError(
                    f"{key!r} entries must be [open, close] pairs, got {entry!r}"
                )
            opens, closes = _parse_time(entry[0]), _parse_time(entry[1])
            if closes <= opens:
                raise PolicyError(
                    f"{key!r} interval {entry!r} closes before it opens"
                )
            intervals.append((opens, closes))

        intervals.sort()
        for earlier, later in zip(intervals, intervals[1:]):
            if later[0] < earlier[1]:
                raise PolicyError(
                    f"{key!r} has overlapping intervals {earlier} and {later}"
                )
        parsed[day] = intervals
    return parsed


def parse_holidays(raw: list | None) -> frozenset[date]:
    if not raw:
        return frozenset()
    if not isinstance(raw, list):
        raise PolicyError("holidays must be a list of YYYY-MM-DD strings")
    days = set()
    for entry in raw:
        if isinstance(entry, date):
            days.add(entry)
            continue
        try:
            days.add(date.fromisoformat(str(entry).strip()))
        except ValueError:
            raise PolicyError(f"{entry!r} is not a YYYY-MM-DD date")
    return frozenset(days)


def parse_blocked(raw: list | None, zone_name: str) -> tuple[TimeWindow, ...]:
    """
    Parse `[{"start": "...", "end": "..."}]` local wall-clock closures.

    Stored as local because that is how a human writes "closed 1–14 July", and
    resolved here so a vacation does not shift by an hour if it spans a DST
    boundary.
    """
    if not raw:
        return ()
    if not isinstance(raw, list):
        raise PolicyError("blocked_periods must be a list of {start, end} objects")

    windows = []
    for entry in raw:
        if not isinstance(entry, dict) or "start" not in entry or "end" not in entry:
            raise PolicyError(f"blocked period {entry!r} needs start and end")
        try:
            start_local = datetime.fromisoformat(str(entry["start"]))
            end_local = datetime.fromisoformat(str(entry["end"]))
        except ValueError:
            raise PolicyError(f"blocked period {entry!r} has an unparseable datetime")

        start = resolve_local(
            start_local, zone_name, on_ambiguous="earliest", on_nonexistent="shift"
        ).utc
        end = resolve_local(
            end_local, zone_name, on_ambiguous="latest", on_nonexistent="shift"
        ).utc
        if end <= start:
            raise PolicyError(f"blocked period {entry!r} ends before it starts")
        windows.append(TimeWindow(start=start, end=end))
    return tuple(windows)


def rules_from(tenant, policy=None) -> SchedulingRules:
    """
    Build the effective rules for a tenant.

    With no `SchedulingPolicy` row, this reproduces the pre-STEP-6 behaviour
    exactly — one open/close pair, seven days a week — except for one
    deliberate change: `minimum_notice_minutes` defaults to 60 rather than 0.
    The old code offered a slot starting one second in the future, which is
    not a booking, it is a surprise.
    """
    zone_name = getattr(tenant, "timezone", None) or "UTC"
    get_zone(zone_name)  # validate early; a bad zone must not reach slot math

    if policy is None:
        opens = getattr(tenant, "business_open", time(9, 0))
        closes = getattr(tenant, "business_close", time(17, 0))
        minutes = int(getattr(tenant, "appointment_minutes", 30) or 30)
        weekly = {day: [(opens, closes)] for day in WEEKDAY_KEYS}
        return SchedulingRules(
            timezone=zone_name,
            weekly_hours=weekly,
            slot_minutes=minutes,
            slot_interval_minutes=minutes,
        )

    return SchedulingRules(
        timezone=zone_name,
        weekly_hours=parse_weekly_hours(policy.weekly_hours),
        holidays=parse_holidays(policy.holidays),
        blocked=parse_blocked(policy.blocked_periods, zone_name),
        slot_minutes=policy.slot_minutes,
        slot_interval_minutes=policy.slot_interval_minutes or policy.slot_minutes,
        buffer_before_minutes=policy.buffer_before_minutes,
        buffer_after_minutes=policy.buffer_after_minutes,
        minimum_notice_minutes=policy.minimum_notice_minutes,
        booking_horizon_days=policy.booking_horizon_days,
        max_slots_offered=policy.max_slots_offered,
        allow_outside_business_hours=policy.allow_outside_business_hours,
        require_provider_confirmation=policy.require_provider_confirmation,
    )


# --------------------------------------------------------- business hours ---

def business_windows(rules: SchedulingRules, day: date) -> list[TimeWindow]:
    """
    The open intervals on a local date, as UTC windows.

    A nonexistent local time (the business opens at 02:00 and the clocks jump
    over it) is shifted forward to the first real instant rather than raising:
    a DST transition should not close a business for the day.
    """
    zone = get_zone(rules.timezone)
    windows: list[TimeWindow] = []

    for opens, closes in rules.intervals_for(day):
        try:
            start = resolve_local(
                datetime.combine(day, opens), zone,
                on_ambiguous="earliest", on_nonexistent="shift",
            ).utc
            end = resolve_local(
                datetime.combine(day, closes), zone,
                on_ambiguous="latest", on_nonexistent="shift",
            ).utc
        except NonexistentTimeError:      # pragma: no cover - shift handles it
            continue
        if end > start:
            windows.append(TimeWindow(start=start, end=end))
    return windows


def is_within_business_hours(rules: SchedulingRules, window: TimeWindow) -> bool:
    """
    Does this appointment fit entirely inside one opening interval?

    Entirely, and inside *one* — an appointment spanning the lunch break is
    not "mostly open", it is a customer sitting in an empty waiting room.
    """
    if rules.allow_outside_business_hours:
        return True

    zone = get_zone(rules.timezone)
    local_start = to_local(window.start, zone)

    # Check the local date of the start and the day either side, so an
    # appointment crossing local midnight is still evaluated correctly.
    for offset in (-1, 0, 1):
        for opening in business_windows(rules, local_start.date() + timedelta(days=offset)):
            if opening.start <= window.start and window.end <= opening.end:
                return True
    return False


def is_blocked(rules: SchedulingRules, window: TimeWindow) -> bool:
    return any(window.overlaps(blocked) for blocked in rules.blocked)


# ------------------------------------------------------------ slot grid ---

def candidate_slots(
    rules: SchedulingRules,
    day: date,
    *,
    now: datetime | None = None,
) -> list[AvailabilitySlot]:
    """
    Every slot the business *policy* allows on a local date.

    Provider availability and existing appointments are not consulted here —
    that is `filter_slots`. Separating them keeps this function pure and makes
    "why was this slot not offered" answerable one layer at a time.
    """
    moment = now or now_utc()
    earliest = moment + timedelta(minutes=rules.minimum_notice_minutes)
    horizon = moment + timedelta(days=rules.booking_horizon_days)

    step = timedelta(minutes=rules.slot_interval_minutes or rules.slot_minutes)
    if step <= timedelta(0):
        raise PolicyError("slot interval must be positive")

    slots: list[AvailabilitySlot] = []
    for opening in business_windows(rules, day):
        cursor = opening.start
        while cursor + rules.duration <= opening.end:
            window = TimeWindow(start=cursor, end=cursor + rules.duration)
            cursor += step

            if window.start < earliest or window.start > horizon:
                continue
            if is_blocked(rules, window):
                continue
            slots.append(AvailabilitySlot(
                start=window.start, end=window.end, timezone=rules.timezone
            ))
    return slots


def filter_slots(
    slots: Iterable[AvailabilitySlot],
    *,
    rules: SchedulingRules,
    busy: Iterable[BusyPeriod] = (),
    booked: Iterable[TimeWindow] = (),
) -> list[AvailabilitySlot]:
    """
    Remove slots that collide with anything, buffers included.

    The buffer is applied to the *candidate*, not to the existing bookings.
    Expanding the candidate by (before, after) and testing that against the
    unexpanded conflicts is equivalent for overlap purposes and does the
    expansion once per slot rather than once per conflict.
    """
    conflicts = [period.window for period in busy] + list(booked)
    if not conflicts:
        return list(slots)

    surviving = []
    for slot in slots:
        guarded = slot.window.expanded(
            before=rules.buffer_before, after=rules.buffer_after
        )
        if not any(guarded.overlaps(conflict) for conflict in conflicts):
            surviving.append(slot)
    return surviving


def part_of_day_filter(
    slots: Iterable[AvailabilitySlot], part: str, timezone_name: str
) -> list[AvailabilitySlot]:
    """
    "Morning" / "afternoon" / "evening", judged in **local** time.

    The pre-STEP-6 code compared `cursor.hour < 12` on a zone-aware datetime,
    which happened to work only because the datetime was already local. Doing
    it on a UTC value — which is what the new pipeline carries — would call
    9 AM in Los Angeles an afternoon slot.
    """
    part = (part or "any").strip().lower()
    if part in ("", "any"):
        return list(slots)

    bounds = {
        "morning": (0, 12),
        "afternoon": (12, 17),
        "evening": (17, 24),
    }.get(part)
    if bounds is None:
        return list(slots)

    low, high = bounds
    zone = get_zone(timezone_name)
    return [slot for slot in slots if low <= to_local(slot.start, zone).hour < high]


def describe_hours(rules: SchedulingRules, day: date) -> str:
    """A human sentence about a day's opening hours, for the voice agent."""
    intervals = rules.intervals_for(day)
    if not intervals:
        return "closed"
    return ", ".join(
        f"{_spoken_time(opens)} to {_spoken_time(closes)}"
        for opens, closes in intervals
    )


def _spoken_time(value: time) -> str:
    hour = value.hour % 12 or 12
    meridiem = "am" if value.hour < 12 else "pm"
    return f"{hour}:{value.minute:02d} {meridiem}" if value.minute else f"{hour} {meridiem}"