"""
Timezone handling for scheduling.

The rule this module enforces, everywhere, without exception:

    **Store UTC. Reason in the tenant's zone. Never let the two blur.**

A voice agent is unusually exposed to timezone bugs. "Tomorrow at three"
carries no offset, arrives at a server whose clock is UTC, and refers to a
business that might be in Los Angeles. Get it wrong by an hour twice a year and
the customer arrives to a locked door — and the failure is invisible in
testing, because it only reproduces on two Sundays.

The three cases that actually break, and what this module does about them:

**DST is not a fixed offset.** `America/New_York` is −05:00 in January and
−04:00 in July. Adding `timedelta(days=1)` to an aware datetime adds exactly
24 hours of *elapsed time*, which across a spring-forward boundary lands on a
different wall-clock hour than the caller said. So date arithmetic is done on
naive wall-clock values and localised afterwards — never the other way round.

**Ambiguous times.** When clocks go back, 01:30 happens twice. `zoneinfo`
resolves this with `fold`: `fold=0` is the first (DST) occurrence, `fold=1` the
second (standard). Defaulting silently would put an appointment an hour off
half the time, so `resolve_local()` reports the ambiguity and the caller
decides.

**Nonexistent times.** When clocks go forward, 02:30 never happens.
`zoneinfo` does not raise — it happily produces a datetime whose UTC round trip
lands somewhere else entirely. That silent wrongness is worse than an error, so
this module detects it by round-tripping and reports it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

#: Used only when a tenant has no timezone at all, which the schema prevents
#: for new rows. Explicitly *not* the server's local zone: inheriting the
#: server's zone is how a deployment move silently rewrites every schedule.
FALLBACK_TIMEZONE = "UTC"


class TimezoneError(ValueError):
    """The zone name is not a valid IANA identifier."""


class AmbiguousTimeError(ValueError):
    """
    The wall-clock time occurs twice on that date (clocks going back).

    Carries both candidates so a caller can offer them: "did you mean 1:30 AM
    EDT or 1:30 AM EST?"
    """

    def __init__(self, local: datetime, first: datetime, second: datetime):
        super().__init__(
            f"{local.isoformat()} occurs twice in {local.tzinfo}; "
            f"clocks go back that day"
        )
        self.local = local
        self.first = first
        self.second = second


class NonexistentTimeError(ValueError):
    """
    The wall-clock time does not occur on that date (clocks going forward).

    Carries the gap so a caller can suggest the nearest real time.
    """

    def __init__(self, local: datetime, gap_start: datetime, gap_end: datetime):
        super().__init__(
            f"{local.isoformat()} does not exist in {local.tzinfo}; "
            f"clocks jump from {gap_start.time()} to {gap_end.time()} that day"
        )
        self.local = local
        self.gap_start = gap_start
        self.gap_end = gap_end


def get_zone(name: str | None) -> ZoneInfo:
    """
    Resolve an IANA identifier.

    Raises rather than falling back, because a typo'd zone silently becoming
    UTC is exactly the class of bug this module exists to prevent. The caller
    decides whether a fallback is appropriate; this function does not decide
    for them.
    """
    if not name:
        raise TimezoneError("no timezone given")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise TimezoneError(f"{name!r} is not a known IANA timezone") from exc


def is_valid_zone(name: str | None) -> bool:
    try:
        get_zone(name)
        return True
    except TimezoneError:
        return False


def tenant_zone(tenant) -> ZoneInfo:
    """
    The zone a tenant's business runs in.

    Falls back to UTC *and says so in the exception-free path* rather than
    raising, because a tenant row with a bad timezone should degrade to
    something defensible instead of taking every call down. Validation happens
    at write time, in the API.
    """
    try:
        return get_zone(getattr(tenant, "timezone", None))
    except TimezoneError:
        return get_zone(FALLBACK_TIMEZONE)


# --------------------------------------------------------------- resolving ---

@dataclass(frozen=True)
class ResolvedTime:
    """
    A wall-clock time successfully pinned to a real instant.

    `utc` is what gets stored; `local` is what gets spoken back to the caller.
    Both are kept because deriving one from the other at the point of use is
    where offsets get dropped.
    """

    local: datetime
    utc: datetime
    zone_name: str
    #: True when the wall-clock time occurred twice and a choice was made.
    was_ambiguous: bool = False


def resolve_local(
    naive_local: datetime,
    zone: ZoneInfo | str,
    *,
    on_ambiguous: str = "raise",
    on_nonexistent: str = "raise",
) -> ResolvedTime:
    """
    Turn a naive wall-clock datetime into a real instant.

    `on_ambiguous`:
      * ``"raise"``   (default) -- `AmbiguousTimeError` with both candidates
      * ``"earliest"`` -- the first occurrence (still DST)
      * ``"latest"``   -- the second occurrence (standard time)

    `on_nonexistent`:
      * ``"raise"``  (default) -- `NonexistentTimeError` with the gap
      * ``"shift"``  -- move forward to the first real instant after the gap

    Defaults raise on purpose. Requirement 16 says an ambiguous request must
    produce a clarification question rather than a silent choice, and the only
    way to guarantee that is to make silence impossible to get by accident.
    """
    zone = zone if isinstance(zone, ZoneInfo) else get_zone(zone)
    if naive_local.tzinfo is not None:
        naive_local = naive_local.replace(tzinfo=None)

    gap = _nonexistent_gap(naive_local, zone)
    if gap is not None:
        if on_nonexistent == "shift":
            shifted = gap[1]
            return ResolvedTime(
                local=shifted,
                utc=shifted.astimezone(UTC),
                zone_name=str(zone),
            )
        raise NonexistentTimeError(naive_local.replace(tzinfo=zone), *gap)

    first = naive_local.replace(tzinfo=zone, fold=0)
    second = naive_local.replace(tzinfo=zone, fold=1)
    ambiguous = first.utcoffset() != second.utcoffset()

    if ambiguous:
        if on_ambiguous == "earliest":
            chosen = first
        elif on_ambiguous == "latest":
            chosen = second
        else:
            raise AmbiguousTimeError(naive_local.replace(tzinfo=zone), first, second)
        return ResolvedTime(
            local=chosen, utc=chosen.astimezone(UTC), zone_name=str(zone),
            was_ambiguous=True,
        )

    return ResolvedTime(
        local=first, utc=first.astimezone(UTC), zone_name=str(zone)
    )


def _nonexistent_gap(
    naive_local: datetime, zone: ZoneInfo
) -> tuple[datetime, datetime] | None:
    """
    Detect a spring-forward gap by round-tripping.

    `zoneinfo` does not signal nonexistent times: it produces a datetime that
    converts to UTC and back to a *different* wall clock. That round-trip
    mismatch is the detection — there is no supported API for asking directly.

    Returns `(gap_start_local, gap_end_local)` — the wall-clock time the
    clocks jump *from* and the one they jump *to*. Those two values are what
    makes a useful clarification question ("we skip from 2 to 3 that
    morning"), so they are found properly rather than approximated from the
    requested time: a binary search over UTC instants for the moment the
    offset changes.
    """
    attached = naive_local.replace(tzinfo=zone)
    round_tripped = attached.astimezone(UTC).astimezone(zone)
    if round_tripped.replace(tzinfo=None) == naive_local:
        return None

    # Bracket the transition. Every DST shift in the IANA database is under
    # three hours, so the requested time is within this window of it.
    low = (naive_local - timedelta(hours=6)).replace(tzinfo=zone).astimezone(UTC)
    high = (naive_local + timedelta(hours=6)).replace(tzinfo=zone).astimezone(UTC)
    low_offset = low.astimezone(zone).utcoffset()

    # Narrow to the second. ~15 iterations for a 12-hour window.
    while (high - low) > timedelta(seconds=1):
        middle = low + (high - low) / 2
        if middle.astimezone(zone).utcoffset() == low_offset:
            low = middle
        else:
            high = middle

    gap_start = low.astimezone(zone)      # last wall clock before the jump
    gap_end = high.astimezone(zone)       # first wall clock after it
    return gap_start, gap_end


# ----------------------------------------------------------------- helpers ---

def to_utc(value: datetime, zone: ZoneInfo | str | None = None) -> datetime:
    """
    Normalise anything to an aware UTC datetime.

    A naive value is interpreted in `zone`; without a zone that is an error
    rather than an assumption, because "assume UTC" and "assume server local"
    are both wrong roughly half the time.
    """
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    if zone is None:
        raise TimezoneError(
            "cannot convert a naive datetime to UTC without knowing its zone"
        )
    return resolve_local(value, zone, on_ambiguous="earliest", on_nonexistent="shift").utc


def to_local(value: datetime, zone: ZoneInfo | str) -> datetime:
    """
    Render an instant in a zone.

    A naive value is assumed to already be UTC. That assumption is safe *here*
    and only here: everything this application stores is UTC, and SQLite hands
    back naive datetimes even for `DateTime(timezone=True)` columns. Without
    this, every read from the test database would need a manual `replace`.
    """
    zone = zone if isinstance(zone, ZoneInfo) else get_zone(zone)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(zone)


def as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive value read back from the database."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def now_utc() -> datetime:
    """
    The current instant, aware.

    Exists so that no scheduling code ever calls `datetime.utcnow()`, which
    returns a *naive* value that then compares wrongly against aware ones. The
    pre-STEP-6 reminder scheduler did exactly that and silently dropped every
    reminder due within five hours.
    """
    return datetime.now(UTC)


def combine_local(day: date, at: time, zone: ZoneInfo | str, **kwargs) -> ResolvedTime:
    """Wall-clock date + time in a zone, resolved to an instant."""
    return resolve_local(datetime.combine(day, at), zone, **kwargs)


def add_days_local(moment: datetime, days: int, zone: ZoneInfo | str) -> datetime:
    """
    Add calendar days, preserving wall-clock time across DST.

    `moment + timedelta(days=1)` adds 24 hours of elapsed time, so "same time
    tomorrow" lands an hour early or late across a transition. This adds the
    day to the *local calendar* and re-resolves, which is what a human means.
    """
    zone = zone if isinstance(zone, ZoneInfo) else get_zone(zone)
    local = to_local(moment, zone) if moment.tzinfo else moment.replace(tzinfo=zone)
    target = (local + timedelta(days=days)).replace(tzinfo=None)
    return resolve_local(
        target, zone, on_ambiguous="earliest", on_nonexistent="shift"
    ).utc


_TIME_PATTERN = re.compile(r"^(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?$", re.I)


def parse_clock(text: str) -> time | None:
    """
    Parse a bare clock time: `3`, `3pm`, `15:30`, `10:30 am`.

    Deliberately small and strict. It handles the shapes an LLM produces after
    being told to emit a time, and refuses everything else rather than
    guessing — a parser that silently accepts nonsense is how "at half past"
    becomes 00:00.
    """
    match = _TIME_PATTERN.match((text or "").strip())
    if not match:
        return None

    hour = int(match.group("h"))
    minute = int(match.group("m") or 0)
    meridiem = (match.group("ap") or "").lower()

    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    elif not 0 <= hour <= 23:
        return None

    return time(hour, minute)


def format_spoken(moment: datetime, zone: ZoneInfo | str) -> str:
    """How a time is read back to a caller. Local, never UTC."""
    local = to_local(moment, zone)
    hour = local.hour % 12 or 12
    meridiem = "am" if local.hour < 12 else "pm"
    if local.minute:
        return f"{hour}:{local.minute:02d} {meridiem}"
    return f"{hour} {meridiem}"


def format_spoken_date(moment: datetime, zone: ZoneInfo | str) -> str:
    local = to_local(moment, zone)
    return f"{local:%A} {local:%B} {local.day}"

