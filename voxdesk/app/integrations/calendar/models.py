"""
The provider-neutral calendar domain.

Everything above the adapters speaks these types; everything below speaks one
vendor's JSON. Frozen dataclasses rather than ORM rows or Pydantic models,
for the same reason as the CRM layer: they cross a boundary, they must be
constructible in a test without a database, and they must not drag a session
into an adapter.

**Every datetime in this module is UTC and aware.** Not by convention — by
assertion, in `__post_init__`. A naive datetime reaching an adapter is how an
appointment ends up five hours out, and the cheapest place to catch that is
the moment the value is constructed.
"""
from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from app.integrations.calendar.timezones import UTC, as_utc


def _safe_meeting_url(value: str | None) -> str | None:
    """
    Drop a `meeting_url` we would refuse to link to.

    Providers hand us this string verbatim -- Google's `hangoutLink`,
    Microsoft's `joinUrl`, and for Cal.com `meetingUrl` **or the free-text
    `location` field**, which is not required to be a URL at all. A value of
    `javascript:...` rendered as an href in the dashboard is stored XSS, so
    the unusable ones are normalised away here, at the single point every
    provider's response passes through on its way into an Appointment.

    https only: every real provider issues https join links, and there is no
    case for sending a user to a plaintext meeting URL. Anything else -- a
    non-URL location string like "Office, 2nd floor", a protocol-relative
    `//host`, or a dangerous scheme -- becomes None, which the model already
    treats as "no meeting link".

    This is a normalisation, not a validation: it never raises, because a bad
    URL must not fail an otherwise valid booking. The dashboard applies the
    same allowlist independently -- neither layer trusts the other.
    """
    if not isinstance(value, str):
        return None
    # Browsers ignore leading control characters in a URL, so strip them
    # before inspecting the scheme.
    cleaned = value.strip().strip("\x00\t\n\r\x0b\x0c")
    if not cleaned or cleaned.startswith("//"):
        return None
    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return None
    return cleaned


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(
            f"{name} must be timezone-aware; naive datetimes are how "
            f"appointments end up in the wrong hour"
        )
    return value.astimezone(UTC)


@dataclass(frozen=True)
class TimeWindow:
    """A half-open interval [start, end) in UTC."""

    start: datetime
    end: datetime

    def __post_init__(self):
        object.__setattr__(self, "start", _require_utc(self.start, "start"))
        object.__setattr__(self, "end", _require_utc(self.end, "end"))
        if self.end <= self.start:
            raise ValueError("a time window must end after it starts")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def overlaps(self, other: "TimeWindow") -> bool:
        """
        Half-open comparison, so back-to-back appointments do not collide.

        `<` and `>` rather than `<=` and `>=`: a 09:00–09:30 and a 09:30–10:00
        booking share an instant but not an interval. Using closed comparison
        here would make every consecutive slot conflict with its neighbour,
        which silently halves a business's capacity.
        """
        return self.start < other.end and other.start < self.end

    def contains(self, moment: datetime) -> bool:
        return self.start <= as_utc(moment) < self.end

    def expanded(self, *, before: timedelta, after: timedelta) -> "TimeWindow":
        """This window plus its buffers — what actually has to be free."""
        return TimeWindow(start=self.start - before, end=self.end + after)


@dataclass(frozen=True)
class AvailabilitySlot:
    """One bookable opening."""

    start: datetime
    end: datetime
    timezone: str
    provider: str | None = None
    calendar_reference: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "start", _require_utc(self.start, "start"))
        object.__setattr__(self, "end", _require_utc(self.end, "end"))

    @property
    def window(self) -> TimeWindow:
        return TimeWindow(start=self.start, end=self.end)

    def local_start(self) -> datetime:
        from app.integrations.calendar.timezones import to_local
        return to_local(self.start, self.timezone)

    def spoken(self) -> str:
        from app.integrations.calendar.timezones import format_spoken
        return format_spoken(self.start, self.timezone)


@dataclass(frozen=True)
class BusyPeriod:
    """A block of time the provider says is not free."""

    start: datetime
    end: datetime
    source: str = "provider"

    def __post_init__(self):
        object.__setattr__(self, "start", _require_utc(self.start, "start"))
        object.__setattr__(self, "end", _require_utc(self.end, "end"))

    @property
    def window(self) -> TimeWindow:
        return TimeWindow(start=self.start, end=self.end)


@dataclass(frozen=True)
class Attendee:
    name: str = ""
    phone: str | None = None
    email: str | None = None

    @property
    def display_name(self) -> str:
        return self.name or self.phone or self.email or "Caller"


@dataclass(frozen=True)
class EventRequest:
    """
    What we ask a provider to put on a calendar.

    `idempotency_key` is passed to providers that accept one (Google's
    client-supplied event id, Cal.com's booking uid) and used to reconcile
    after a timeout for those that do not.
    """

    title: str
    start: datetime
    end: datetime
    timezone: str
    attendee: Attendee
    description: str = ""
    calendar_reference: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "start", _require_utc(self.start, "start"))
        object.__setattr__(self, "end", _require_utc(self.end, "end"))


@dataclass(frozen=True)
class CalendarEvent:
    """
    What a provider says exists.

    `external_id` is the whole point: the service refuses to mark an
    appointment CONFIRMED without one, so a provider returning 200 and no id
    is a failure to record rather than a success.
    """

    external_id: str
    start: datetime
    end: datetime
    title: str = ""
    status: str = "confirmed"
    calendar_reference: str | None = None
    meeting_url: str | None = None
    #: True when the provider matched an existing event rather than creating.
    already_existed: bool = False
    raw_reference: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "start", _require_utc(self.start, "start"))
        object.__setattr__(self, "end", _require_utc(self.end, "end"))
        object.__setattr__(self, "meeting_url", _safe_meeting_url(self.meeting_url))


@dataclass(frozen=True)
class HealthResult:
    connected: bool
    provider: str
    latency_ms: float
    safe_message: str = ""


# ------------------------------------------------------------ tool results ---

class BookingOutcome(str, enum.Enum):
    """
    The vocabulary a voice tool may return.

    A closed set, because requirement 15 says the tool result is the source of
    truth and the LLM must not invent booking success. If the agent can only
    ever see one of these, "the model said it was booked" stops being a
    possible state.
    """
    AVAILABLE_SLOTS = "AVAILABLE_SLOTS"
    NO_SLOTS = "NO_SLOTS"
    BOOKED = "BOOKED"
    ALREADY_BOOKED = "ALREADY_BOOKED"
    CONFLICT = "CONFLICT"
    RESCHEDULED = "RESCHEDULED"
    CANCELLED = "CANCELLED"
    CONFIRMED = "CONFIRMED"
    NOT_FOUND = "NOT_FOUND"
    OUTSIDE_HOURS = "OUTSIDE_HOURS"
    TOO_SOON = "TOO_SOON"
    TOO_FAR = "TOO_FAR"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    FAILED = "FAILED"


@dataclass(frozen=True)
class BookingResult:
    """
    A structured tool result.

    `message` is what the agent may say. It is written here, not by the model,
    and it never contains a provider error — requirement 15 and the STEP 5
    failure-UX rule both apply: the caller must never hear "Google returned
    401".
    """

    outcome: BookingOutcome
    message: str
    appointment_id: str | None = None
    slots: tuple[AvailabilitySlot, ...] = ()
    #: Present only when the outcome is NEEDS_CLARIFICATION.
    clarification: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome in {
            BookingOutcome.AVAILABLE_SLOTS, BookingOutcome.BOOKED,
            BookingOutcome.RESCHEDULED, BookingOutcome.CANCELLED,
            BookingOutcome.CONFIRMED, BookingOutcome.ALREADY_BOOKED,
        }

    def as_tool_payload(self) -> dict:
        """
        What the LLM actually sees.

        Deliberately narrow. No provider name, no error code, no internal id
        beyond the appointment's — anything else is something the model could
        repeat to the caller.
        """
        payload: dict[str, Any] = {
            "outcome": self.outcome.value,
            # Derived from `outcome`, never set independently. Present because
            # every other tool in this codebase returns it, so the agent sees
            # one convention -- but it cannot disagree with the outcome, so it
            # adds no way for the model to infer success.
            "ok": self.ok,
            "message": self.message,
        }
        if self.appointment_id:
            payload["appointment_id"] = self.appointment_id
        if self.slots:
            payload["slots"] = [
                {"spoken": s.spoken(), "iso": s.start.isoformat()} for s in self.slots
            ]
        if self.clarification:
            payload["ask"] = self.clarification
        return payload


# -------------------------------------------------------------- identity ---

def slot_key(tenant_id: str, start: datetime, end: datetime) -> str:
    """
    The value that makes a slot lockable.

    Deterministic from the tenant and the exact UTC interval, so two callers
    racing for Tuesday 3 PM compute the same string and the unique constraint
    on `(tenant_id, slot_key)` decides between them — at the database, not in
    application logic that can interleave.

    Hashed rather than concatenated only to bound the column width; it is not
    a secret and it does not need to be.
    """
    start_utc = _require_utc(start, "start")
    end_utc = _require_utc(end, "end")
    basis = f"{tenant_id}|{start_utc.isoformat()}|{end_utc.isoformat()}"
    return hashlib.sha256(basis.encode()).hexdigest()[:48]


def booking_idempotency_key(
    tenant_id: str,
    start: datetime,
    *,
    phone: str | None = None,
    email: str | None = None,
    request_id: str | None = None,
) -> str:
    """
    Stable identity for one booking *request*.

    Derived from who is booking and when, not generated per attempt.
    Requirement 9 is explicit that a fresh UUID per retry is not duplicate
    protection — it is the opposite, because every retry then looks like a new
    booking.

    `request_id` lets an API client supply its own; without one the natural
    key is (tenant, caller identity, requested start), which is exactly what
    "the same person asking for the same slot again" means.
    """
    from app.integrations.crm.models import normalize_email, normalize_phone

    who = (
        request_id
        or normalize_phone(phone)
        or normalize_email(email)
        or "anonymous"
    )
    start_utc = _require_utc(start, "start")
    basis = f"{tenant_id}|{who}|{start_utc.isoformat()}"
    return hashlib.sha256(basis.encode()).hexdigest()[:48]