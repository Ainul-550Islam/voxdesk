"""
Natural date and time interpretation.

Requirement 16, and specifically its last line: *"Do not trust the model for
DST/timezone mathematics. Use deterministic date/time libraries."*

The design follows from that. The LLM's job is to pass through what the caller
said — `"next Tuesday"`, `"at three"` — and this module resolves it against the
business's calendar using `zoneinfo`. The model never computes a date, never
applies an offset, and never decides what "tomorrow" means.

Three rules:

**Everything is relative to the business's local now**, not the server's.
"Tomorrow" at 23:00 in Dhaka is a different date than "tomorrow" at 13:00 UTC.

**Ambiguity produces a question, not a guess.** If a caller says "Tuesday" and
today is Tuesday, that is two plausible dates. If they say "at three" with no
meridiem and the business is open at 3 AM and 3 PM, that is two plausible
times. Both return `Ambiguity` and the agent asks — because a silently-picked
time is a missed appointment nobody notices until the customer does not arrive.

**Parsing failure is not a fallback to today.** An unrecognised phrase returns
`None`. The alternative — defaulting — is how "sometime next month" becomes an
appointment this afternoon.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.integrations.calendar.timezones import (
    get_zone,
    now_utc,
    parse_clock,
    to_local,
)

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

#: Callers say "half past four", not "half past 4". A speech-to-text
#: transcript is words, so digit-only patterns miss most of what arrives.
WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

PART_OF_DAY = {
    "morning": "morning",
    "afternoon": "afternoon",
    "evening": "evening",
    "tonight": "evening",
    "midday": "afternoon",
    "noon": "afternoon",
    "lunchtime": "afternoon",
}


@dataclass(frozen=True)
class Ambiguity:
    """Why the request could not be resolved, and what to ask."""

    question: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedRequest:
    """
    What the caller asked for, resolved as far as it can be.

    `day` is a local date. `at` is a local wall-clock time when one was given.
    `part_of_day` narrows without pinning ("Friday afternoon"). Nothing here is
    UTC yet — conversion happens in the service, once, after the business's
    rules have been applied.
    """

    day: date | None = None
    at: time | None = None
    part_of_day: str = "any"
    ambiguity: Ambiguity | None = None

    @property
    def is_ambiguous(self) -> bool:
        return self.ambiguity is not None

    @property
    def has_day(self) -> bool:
        return self.day is not None


# ---------------------------------------------------------------- dates ---

_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_RELATIVE_DAYS = re.compile(r"\bin\s+(\d{1,2})\s+days?\b", re.I)
_THIS_NEXT = re.compile(
    r"\b(this|next|coming|following)\s+(?:coming\s+)?"
    r"(" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")\b",
    re.I,
)
_BARE_WEEKDAY = re.compile(
    r"\b(" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")\b", re.I
)


def parse_request(
    text: str,
    *,
    timezone_name: str,
    now: datetime | None = None,
    explicit_time: str | None = None,
) -> ParsedRequest:
    """
    Resolve a caller's phrasing into a local date, time and part of day.

    `explicit_time` lets a voice tool pass a separately-captured time argument
    without having to embed it in the phrase.
    """
    zone = get_zone(timezone_name)
    local_now = to_local(now or now_utc(), zone)
    today = local_now.date()
    lowered = (text or "").strip().lower()

    part = _part_of_day(lowered)
    day, day_ambiguity = _parse_day_phrase(lowered, today)

    # Parse the time against the text with every date token removed.
    #
    # Without this the time parser reads digits that belong to the date:
    # "2026-12-24 at 9am" produced an ambiguity question about "12" (from the
    # month), and "in 3 days" booked 3 PM. The date parser has already
    # consumed those characters, so the time parser must not see them.
    at, time_ambiguity = _parse_time_phrase(_strip_date_tokens(lowered), explicit_time, part)

    # A day ambiguity is reported first: knowing *which Tuesday* matters more
    # than knowing whether three means morning or afternoon, and asking two
    # questions at once on a phone call does not work.
    ambiguity = day_ambiguity or time_ambiguity
    return ParsedRequest(day=day, at=at, part_of_day=part, ambiguity=ambiguity)


#: Everything the date parser consumes. Removed before time parsing so their
#: digits cannot be mistaken for a clock.
_DATE_TOKENS = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\bin\s+\d{1,2}\s+days?\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
    r"|\bday after tomorrow\b|\btomorrow\b|\btoday\b|\btonight\b",
    re.I,
)


def _strip_date_tokens(text: str) -> str:
    return _DATE_TOKENS.sub(" ", text)


def _words_to_digits(text: str) -> str:
    """Rewrite spoken numbers so the digit patterns can see them."""
    for word, value in WORD_NUMBERS.items():
        text = re.sub(rf"\b{word}\b", str(value), text)
    return text


def _parse_day_phrase(text: str, today: date) -> tuple[date | None, Ambiguity | None]:
    iso = _ISO_DATE.search(text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))), None
        except ValueError:
            return None, Ambiguity("That date does not look right — which day did you mean?")

    # Longest phrase first. "tomorrow" is a substring of "day after
    # tomorrow", so checking the short one first silently loses a day --
    # which is a customer arriving twenty-four hours early.
    if re.search(r"\bday after tomorrow\b", text):
        return today + timedelta(days=2), None
    if re.search(r"\btomorrow\b", text):
        return today + timedelta(days=1), None
    if re.search(r"\btoday\b", text):
        return today, None
    if re.search(r"\btonight\b", text):
        return today, None

    relative = _RELATIVE_DAYS.search(text)
    if relative:
        return today + timedelta(days=int(relative.group(1))), None

    qualified = _THIS_NEXT.search(text)
    if qualified:
        qualifier = qualified.group(1).lower()
        target = WEEKDAYS[qualified.group(2).lower()]
        return _weekday_on_or_after(
            today,
            target,
            # "next Tuesday" said on a Tuesday means the one a week away.
            # "this Tuesday" means the imminent one. English speakers disagree
            # about the first of those, but not enough to be worth a question:
            # "next" almost always means "not today".
            skip_today=qualifier in ("next", "following"),
            force_next_week=qualifier in ("next", "following"),
        ), None

    bare = _BARE_WEEKDAY.search(text)
    if bare:
        target = WEEKDAYS[bare.group(1).lower()]
        if target == today.weekday():
            # "Tuesday" said on a Tuesday. Genuinely two answers.
            spoken = bare.group(1).capitalize()
            return None, Ambiguity(
                f"Did you mean today, or {spoken} next week?",
                options=(today.isoformat(), (today + timedelta(days=7)).isoformat()),
            )
        return _weekday_on_or_after(today, target, skip_today=True), None

    return None, None


def _weekday_on_or_after(
    today: date, weekday: int, *, skip_today: bool, force_next_week: bool = False
) -> date:
    delta = (weekday - today.weekday()) % 7
    if delta == 0 and skip_today:
        delta = 7
    candidate = today + timedelta(days=delta)
    if force_next_week and delta < 7 and candidate.isocalendar()[1] == today.isocalendar()[1]:
        candidate += timedelta(days=7)
    return candidate


# ---------------------------------------------------------------- times ---

_AT_TIME = re.compile(
    r"\b(?:at|around|about|by)?\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*"
    r"(am|pm|a\.m\.|p\.m\.|o'?clock)?\b",
    re.I,
)
_HALF_PAST = re.compile(r"\bhalf\s+past\s+(\d{1,2})\b", re.I)
_QUARTER = re.compile(r"\b(quarter)\s+(past|to)\s+(\d{1,2})\b", re.I)


def _parse_time_phrase(
    text: str, explicit: str | None, part: str
) -> tuple[time | None, Ambiguity | None]:
    if explicit:
        parsed = parse_clock(explicit)
        if parsed is not None:
            return parsed, None
        return None, Ambiguity("What time works for you?")

    # "noon" and "midnight" are exact and must be checked before the word
    # rewriter, which would otherwise leave them untouched and unmatched.
    if re.search(r"\bnoon\b|\bmidday\b", text):
        return time(12, 0), None
    if re.search(r"\bmidnight\b", text):
        return time(0, 0), None

    text = _words_to_digits(text)

    half = _HALF_PAST.search(text)
    if half:
        hour = int(half.group(1))
        return _disambiguate_hour(hour, 30, text, part)

    quarter = _QUARTER.search(text)
    if quarter:
        hour = int(quarter.group(3))
        if quarter.group(2).lower() == "past":
            return _disambiguate_hour(hour, 15, text, part)
        hour = (hour - 1) % 24
        return _disambiguate_hour(hour, 45, text, part)

    for match in _AT_TIME.finditer(text):
        hour_text, minute_text, meridiem = match.groups()
        hour = int(hour_text)
        # A four-digit year or a day-of-month has already been consumed by the
        # date parser; anything above 24 here is not a time.
        if hour > 24:
            continue
        minute = int(minute_text or 0)
        if minute > 59:
            continue

        if meridiem and "o" not in meridiem.lower():
            normalised = meridiem.lower().replace(".", "")[:2]
            if normalised == "pm" and hour != 12:
                hour += 12
            elif normalised == "am" and hour == 12:
                hour = 0
            return time(hour % 24, minute), None

        return _disambiguate_hour(hour, minute, text, part)

    return None, None


def _disambiguate_hour(
    hour: int, minute: int, text: str, part: str
) -> tuple[time | None, Ambiguity | None]:
    """
    Resolve a bare hour with no am/pm.

    A 24-hour reading is unambiguous. Below that, the part of day resolves it
    when the caller gave one ("three in the afternoon"). Otherwise: hours that
    only make sense one way are resolved, and the genuinely ambiguous middle
    is asked about.

    The rule for the resolved cases is business reality rather than arithmetic:
    nobody books a dentist at 5 AM, so "at five" is 17:00. But "at ten" is a
    real morning appointment *and* a real evening one for some businesses, and
    guessing there is how someone arrives twelve hours early.
    """
    if hour == 0 or hour > 12:
        return time(hour % 24, minute), None

    if part == "morning":
        return time(0 if hour == 12 else hour, minute), None
    if part in ("afternoon", "evening"):
        return time(hour if hour == 12 else hour + 12, minute), None

    # Unqualified. 1-6 are overwhelmingly afternoon for an appointment;
    # 8-11 are overwhelmingly morning; 7 and 12 are a coin flip.
    if 1 <= hour <= 6:
        return time(hour + 12, minute), None
    if 8 <= hour <= 11:
        return time(hour, minute), None

    spoken = f"{hour}:{minute:02d}" if minute else str(hour)
    return None, Ambiguity(
        f"Just to be sure — {spoken} in the morning or the evening?",
        options=(f"{hour:02d}:{minute:02d}", f"{(hour + 12) % 24:02d}:{minute:02d}"),
    )


def _part_of_day(text: str) -> str:
    for phrase, part in PART_OF_DAY.items():
        if re.search(rf"\b{re.escape(phrase)}\b", text):
            return part
    return "any"

