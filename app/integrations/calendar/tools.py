"""
Voice-agent scheduling tools.

Requirement 15's core clause: *"Do NOT let the LLM invent booking success. The
tool result must be the source of truth."*

Three mechanisms enforce that, and they are structural rather than
instructional — a system prompt asking the model not to lie is not a control:

1. **The outcome vocabulary is a closed enum.** `BookingOutcome` has fourteen
   members and the tool payload can only ever carry one of them. There is no
   value the model can receive that means "booked" unless the service wrote it
   after a provider acknowledgement.
2. **The message text is written here, not by the model.** Every result
   carries a `message` composed by the service. The prompt tells the agent to
   read it.
3. **Provider detail never reaches the payload.** `as_tool_payload()` is an
   allowlist: outcome, message, appointment id, slots. No provider name, no
   status code, no error string. The caller cannot be told "Google returned
   401" because the model never learns it.

Every tool is also bounded by `calendar_voice_timeout_seconds`. Requirement 30
says an availability lookup must not hang; on a phone call, three seconds of
silence is already a problem, so the tool returns a safe fallback rather than
waiting for a provider that has stopped answering.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import log
from app.db.models import Appointment, AppointmentStatus, Tenant
from app.integrations.calendar import nlp, service
from app.integrations.calendar.models import BookingOutcome, BookingResult
from app.integrations.calendar.timezones import (
    AmbiguousTimeError,
    NonexistentTimeError,
    format_spoken,
    get_zone,
    now_utc,
    resolve_local,
    to_local,
)

#: What a caller hears when anything unexpected happens. Deliberately the same
#: string for every internal failure mode: variations would leak which one.
GENERIC_FAILURE = (
    "I'm having trouble with the calendar just now. Let me take your details "
    "and someone will call you straight back."
)


async def _bounded(coro, *, what: str) -> object | None:
    """
    Run with the voice deadline, returning None on timeout.

    A separate, tighter bound than the provider timeout: the provider may take
    six seconds, but the agent cannot leave a caller in silence that long, so
    the tool gives up first and says something.
    """
    try:
        return await asyncio.wait_for(
            coro, timeout=settings.calendar_voice_timeout_seconds
        )
    except asyncio.TimeoutError:
        log.warning("calendar.voice_tool_timeout", tool=what)
        return None


class SchedulingTools:
    """
    Bound to one live call, so every tool already knows the tenant.

    Mirrors `FunctionHandlers`' shape so the two compose naturally.
    """

    def __init__(
        self,
        session: AsyncSession,
        tenant: Tenant,
        call=None,
        *,
        now: datetime | None = None,
    ):
        self.session = session
        self.tenant = tenant
        self.call = call
        self.zone = get_zone(tenant.timezone or "UTC")
        self._now = now

    def _clock(self) -> datetime:
        return self._now or now_utc()

    # ------------------------------------------------------------ resolving ---

    def _resolve_when(
        self, when: str, *, at: str | None = None
    ) -> tuple[date | None, datetime | None, str, BookingResult | None]:
        """
        Turn a caller's phrasing into a local date and a UTC instant.

        Returns `(day, utc_start, part_of_day, early_result)`. `early_result`
        is set when the request is ambiguous or impossible, in which case the
        agent asks rather than guessing.
        """
        parsed = nlp.parse_request(
            when or "", timezone_name=str(self.zone), now=self._clock(),
            explicit_time=at,
        )

        if parsed.is_ambiguous:
            return None, None, "any", BookingResult(
                outcome=BookingOutcome.NEEDS_CLARIFICATION,
                message=parsed.ambiguity.question,
                clarification=parsed.ambiguity.question,
            )

        if parsed.day is None:
            return None, None, parsed.part_of_day, BookingResult(
                outcome=BookingOutcome.NEEDS_CLARIFICATION,
                message="Which day would you like?",
                clarification="Which day would you like?",
            )

        if parsed.at is None:
            return parsed.day, None, parsed.part_of_day, None

        # Wall clock -> instant. The two DST cases are asked about rather than
        # resolved silently: requirement 4 says never shift an appointment by
        # an hour, and requirement 16 says ambiguity produces a question.
        try:
            resolved = resolve_local(
                datetime.combine(parsed.day, parsed.at), self.zone
            )
        except AmbiguousTimeError as exc:
            question = (
                f"The clocks go back that night, so "
                f"{format_spoken(exc.first, self.zone)} happens twice. "
                f"Did you mean the earlier one or the later one?"
            )
            return None, None, parsed.part_of_day, BookingResult(
                outcome=BookingOutcome.NEEDS_CLARIFICATION,
                message=question, clarification=question,
            )
        except NonexistentTimeError as exc:
            question = (
                f"The clocks go forward that morning — we jump from "
                f"{exc.gap_start:%-I:%M %p} straight to {exc.gap_end:%-I:%M %p}. "
                f"Shall I look at another time?"
            )
            return None, None, parsed.part_of_day, BookingResult(
                outcome=BookingOutcome.NEEDS_CLARIFICATION,
                message=question, clarification=question,
            )

        return parsed.day, resolved.utc, parsed.part_of_day, None

    # ------------------------------------------------------------- tool: read ---

    async def check_availability(self, when: str, part_of_day: str = "") -> dict:
        """`when` is whatever the caller said: "tomorrow", "next Tuesday"."""
        day, _, parsed_part, early = self._resolve_when(when)
        if early is not None:
            return early.as_tool_payload()

        part = part_of_day or parsed_part
        found = await _bounded(
            service.find_slots(
                self.session, self.tenant, day=day, part_of_day=part,
                now=self._clock(),
            ),
            what="check_availability",
        )
        if found is None:
            return BookingResult(
                outcome=BookingOutcome.FAILED, message=GENERIC_FAILURE
            ).as_tool_payload()

        slots, degraded = found
        if not slots:
            spoken_day = day.strftime("%A")
            if degraded:
                # We could not see the real calendar. Saying "nothing free"
                # would be a claim we cannot support.
                return BookingResult(
                    outcome=BookingOutcome.FAILED,
                    message=(
                        f"I can't see the diary for {spoken_day} at the moment. "
                        f"Let me take your details and someone will call you back."
                    ),
                ).as_tool_payload()
            return BookingResult(
                outcome=BookingOutcome.NO_SLOTS,
                message=f"We've nothing free on {spoken_day}. Shall I try another day?",
            ).as_tool_payload()

        spoken = ", ".join(slot.spoken() for slot in slots)
        return BookingResult(
            outcome=BookingOutcome.AVAILABLE_SLOTS,
            message=f"On {day:%A} we have {spoken}.",
            slots=tuple(slots),
        ).as_tool_payload()

    # ------------------------------------------------------------ tool: book ---

    async def book_appointment(
        self,
        customer_name: str,
        customer_phone: str,
        when: str,
        at: str = "",
        reason: str = "",
        customer_email: str = "",
    ) -> dict:
        invalid = _validate_contact(customer_name, customer_phone)
        if invalid is not None:
            return invalid.as_tool_payload()

        day, start, _, early = self._resolve_when(when, at=at or None)
        if early is not None:
            return early.as_tool_payload()
        if start is None:
            question = "What time on that day would you like?"
            return BookingResult(
                outcome=BookingOutcome.NEEDS_CLARIFICATION,
                message=question, clarification=question,
            ).as_tool_payload()

        request = service.BookingRequest(
            tenant_id=self.tenant.id,
            start=start,
            customer_name=customer_name.strip(),
            customer_phone=customer_phone.strip(),
            customer_email=(customer_email or "").strip() or None,
            reason=(reason or "").strip(),
            call_id=getattr(self.call, "id", None),
        )

        result = await _bounded(
            service.book(self.session, self.tenant, request, now=self._clock()),
            what="book_appointment",
        )
        if result is None:
            # The deadline passed. The service may still be mid-flight, and
            # its own idempotency key means a later retry cannot double-book,
            # so the safe answer is "we'll confirm", never "you're booked".
            return BookingResult(
                outcome=BookingOutcome.FAILED, message=GENERIC_FAILURE
            ).as_tool_payload()

        return result.as_tool_payload()

    # ------------------------------------------------------ tool: reschedule ---

    async def reschedule_appointment(
        self, customer_phone: str, when: str, at: str = "", reason: str = ""
    ) -> dict:
        appointment = await self._find_upcoming(customer_phone)
        if appointment is None:
            return BookingResult(
                outcome=BookingOutcome.NOT_FOUND,
                message="I can't find an upcoming appointment for that number.",
            ).as_tool_payload()

        day, start, _, early = self._resolve_when(when, at=at or None)
        if early is not None:
            return early.as_tool_payload()
        if start is None:
            question = "What time on that day would suit you?"
            return BookingResult(
                outcome=BookingOutcome.NEEDS_CLARIFICATION,
                message=question, clarification=question,
            ).as_tool_payload()

        result = await _bounded(
            service.reschedule(
                self.session, self.tenant, appointment, start,
                reason=reason, now=self._clock(),
            ),
            what="reschedule_appointment",
        )
        if result is None:
            return BookingResult(
                outcome=BookingOutcome.FAILED,
                message=(
                    "I couldn't move that just now, so your original time still "
                    "stands. Someone will call you back."
                ),
            ).as_tool_payload()
        return result.as_tool_payload()

    # ---------------------------------------------------------- tool: cancel ---

    async def cancel_appointment(self, customer_phone: str, reason: str = "") -> dict:
        appointment = await self._find_upcoming(customer_phone)
        if appointment is None:
            return BookingResult(
                outcome=BookingOutcome.NOT_FOUND,
                message="I can't find an upcoming appointment for that number.",
            ).as_tool_payload()

        result = await _bounded(
            service.cancel(
                self.session, self.tenant, appointment,
                reason=reason, cancelled_by="customer",
            ),
            what="cancel_appointment",
        )
        if result is None:
            return BookingResult(
                outcome=BookingOutcome.FAILED, message=GENERIC_FAILURE
            ).as_tool_payload()
        return result.as_tool_payload()

    # --------------------------------------------------------- tool: confirm ---

    async def confirm_appointment(self, customer_phone: str) -> dict:
        """
        Read back an existing booking.

        Never *creates* confirmation state: it reports what the provider
        already accepted. Requirement 19 — do not mark a booking confirmed
        unless provider acceptance has occurred — so a PENDING appointment is
        described honestly as not yet confirmed.
        """
        appointment = await self._find_upcoming(customer_phone)
        if appointment is None:
            return BookingResult(
                outcome=BookingOutcome.NOT_FOUND,
                message="I can't find an upcoming appointment for that number.",
            ).as_tool_payload()

        start = to_local(appointment.starts_at, self.zone)
        when = f"{start:%A} at {format_spoken(appointment.starts_at, self.zone)}"

        if appointment.status is AppointmentStatus.PENDING:
            return BookingResult(
                outcome=BookingOutcome.CONFIRMED,
                message=(
                    f"I have you down for {when}, though it's not fully "
                    f"confirmed yet — someone will be in touch."
                ),
                appointment_id=str(appointment.id),
            ).as_tool_payload()

        return BookingResult(
            outcome=BookingOutcome.CONFIRMED,
            message=f"Yes — you're booked for {when}.",
            appointment_id=str(appointment.id),
        ).as_tool_payload()

    # ----------------------------------------------------------------- lookup ---

    async def _find_upcoming(self, customer_phone: str) -> Appointment | None:
        """
        The caller's next active appointment.

        Matched on a normalized phone, and **always scoped to this tenant** --
        the tenant comes from the bound call, never from an argument, so no
        tool call can reach another business's diary.
        """
        from app.integrations.crm.models import normalize_phone

        wanted = normalize_phone(customer_phone)
        if not wanted:
            return None

        rows = (
            (
                await self.session.execute(
                    select(Appointment)
                    .where(
                        Appointment.tenant_id == self.tenant.id,
                        Appointment.status.in_([
                            AppointmentStatus.PENDING,
                            AppointmentStatus.CONFIRMED,
                            AppointmentStatus.RESCHEDULED,
                        ]),
                        Appointment.starts_at >= self._clock() - timedelta(hours=1),
                    )
                    .order_by(Appointment.starts_at)
                    .limit(25)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if normalize_phone(row.customer_phone) == wanted:
                return row
        return None


# ------------------------------------------------------------- validation ---

def _validate_contact(name: str, phone: str) -> BookingResult | None:
    """
    Requirement 15: tools must validate structured arguments.

    An LLM will happily call a tool with an empty string, and a CRM full of
    nameless, phoneless appointments is worse than a clarifying question.
    """
    from app.integrations.crm.models import normalize_phone

    if not (name or "").strip():
        return BookingResult(
            outcome=BookingOutcome.NEEDS_CLARIFICATION,
            message="Can I take your name?",
            clarification="Can I take your name?",
        )
    digits = normalize_phone(phone)
    if not digits or len(digits) < 7:
        return BookingResult(
            outcome=BookingOutcome.NEEDS_CLARIFICATION,
            message="What's the best number to reach you on?",
            clarification="What's the best number to reach you on?",
        )
    return None


# ------------------------------------------------------------ tool schemas ---

#: OpenAI-style tool definitions. `when` is free text on purpose: the model
#: passes through what the caller said and `nlp.py` resolves it. Asking the
#: model for an ISO date is what the old code did, and it is exactly the
#: "trust the model for DST mathematics" mistake requirement 16 forbids.
SCHEDULING_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": (
                "Find open appointment times. Pass the caller's own words for "
                "the day -- 'tomorrow', 'next Tuesday', 'Friday'. Do NOT "
                "convert to a date yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "when": {
                        "type": "string",
                        "description": "The day, in the caller's words.",
                    },
                    "part_of_day": {
                        "type": "string",
                        "enum": ["any", "morning", "afternoon", "evening"],
                    },
                },
                "required": ["when"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": (
                "Book an appointment. Only say it is booked if this returns "
                "outcome BOOKED -- read the message field back to the caller "
                "exactly as given."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "customer_phone": {"type": "string"},
                    "when": {
                        "type": "string",
                        "description": "The day, in the caller's words.",
                    },
                    "at": {
                        "type": "string",
                        "description": "The time as said: '3pm', '10:30'.",
                    },
                    "reason": {"type": "string"},
                    "customer_email": {"type": "string"},
                },
                "required": ["customer_name", "customer_phone", "when", "at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_appointment",
            "description": "Move the caller's existing appointment to a new time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_phone": {"type": "string"},
                    "when": {"type": "string"},
                    "at": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["customer_phone", "when", "at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": "Cancel the caller's existing appointment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_phone": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["customer_phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_appointment",
            "description": "Read back the caller's existing appointment.",
            "parameters": {
                "type": "object",
                "properties": {"customer_phone": {"type": "string"}},
                "required": ["customer_phone"],
            },
        },
    },
]
