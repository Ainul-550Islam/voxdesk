"""
Cal.com adapter (API v2).

Contract:

* Base ``https://api.cal.com/v2``
* ``Authorization: Bearer cal_live_...`` and ``cal-api-version: 2024-08-13``
* Slots:      ``GET  /slots``
* Book:       ``POST /bookings``
* Reschedule: ``POST /bookings/{uid}/reschedule``  (a real endpoint, not
  cancel-then-rebook)
* Cancel:     ``POST /bookings/{uid}/cancel``
* Read:       ``GET  /bookings/{uid}``
* Responses are wrapped: ``{"status": "success", "data": {...}}``

**Cal.com is a booking system, not a calendar.** That distinction drives the
capability set, and pretending otherwise is exactly what requirement 12
forbids:

* **No free/busy.** Cal.com exposes *bookable slots* for an event type, having
  already applied the owner's schedule, buffers and connected calendars. There
  is no "here are the busy blocks on the underlying calendar" endpoint, so
  `FREE_BUSY` is **not** declared. `GET_AVAILABILITY` is, and the service uses
  slots directly instead of subtracting busy periods.
* **No arbitrary event creation.** Everything is a booking against an
  `eventTypeId`. `CREATE_EVENT` is declared because booking *is* the create
  operation here, but a tenant with no `event_type_id` configured gets a clear
  configuration error rather than a confusing 400.
* **`RESCHEDULE` is a first-class capability** — the one provider where it is
  not emulated by updating an event. `UPDATE_EVENT` is deliberately absent:
  Cal.com's PATCH does not move a booking's time, so declaring it would let
  the service call something that silently does not do what it means.

**Idempotency.** Cal.com accepts arbitrary `metadata` on a booking, so the key
travels there and `find_event_by_key` scans the affected window for it. There
is no server-side dedupe, which is why the database-level slot lock in
`service.book` is the primary defence for this provider rather than a backstop.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.ssrf import OutboundUrlError, validate_outbound_url
from app.integrations.calendar.base import (
    CalendarCapability,
    CalendarProvider,
)
from app.integrations.calendar.errors import (
    CalendarConfigurationError,
    CalendarConflictError,
    CalendarError,
    CalendarNotFoundError,
    CalendarValidationError,
)
from app.integrations.calendar.models import (
    AvailabilitySlot,
    CalendarEvent,
    EventRequest,
    HealthResult,
    TimeWindow,
)
from app.integrations.calendar.timezones import UTC

BASE_URL = "https://api.cal.com/v2"
API_VERSION = "2024-08-13"


class CalComProvider(CalendarProvider):
    name = "calcom"
    capabilities = frozenset({
        CalendarCapability.GET_AVAILABILITY,
        CalendarCapability.CREATE_EVENT,
        CalendarCapability.CANCEL_EVENT,
        CalendarCapability.GET_EVENT,
        CalendarCapability.RESCHEDULE,
        CalendarCapability.INVITE_ATTENDEE,
        CalendarCapability.CONFERENCING,
        CalendarCapability.HEALTH_CHECK,
    })

    # ------------------------------------------------------------- plumbing ---

    @property
    def _api_key(self) -> str:
        key = (self.context.credentials or {}).get("api_key") or ""
        if not key:
            raise CalendarConfigurationError(
                "Cal.com API key is not configured", provider=self.name
            )
        return key

    @property
    def _event_type_id(self) -> int:
        raw = (self.context.config or {}).get("event_type_id")
        if raw in (None, ""):
            raise CalendarConfigurationError(
                "Cal.com requires an event_type_id; every booking is made "
                "against an event type",
                provider=self.name,
            )
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise CalendarConfigurationError(
                f"Cal.com event_type_id must be an integer, got {raw!r}",
                provider=self.name,
            )

    def _base(self) -> str:
        # SSRF guard (Step 9): this URL receives the Cal.com API key.
        base = (self.context.config or {}).get("base_url") or BASE_URL
        try:
            validate_outbound_url(base, require_https=True)
        except OutboundUrlError as exc:
            raise CalendarConfigurationError(str(exc), provider=self.name)
        return base

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["Authorization"] = f"Bearer {self._api_key}"
        headers["cal-api-version"] = (
            (self.context.config or {}).get("api_version") or API_VERSION
        )
        return headers

    @staticmethod
    def _unwrap(data: Any) -> Any:
        """
        Cal.com wraps everything in `{"status": ..., "data": ...}`.

        A `status` of anything but success at HTTP 200 is a rejection — the
        same trap as Jobber's `userErrors` in the CRM layer, and it has to be
        checked or a refused booking is reported as a success.
        """
        if not isinstance(data, dict):
            return data
        status = str(data.get("status") or "").lower()
        if status and status != "success":
            error = data.get("error") or {}
            message = (
                error.get("message") if isinstance(error, dict) else str(error)
            ) or "rejected"
            raise CalendarValidationError(
                f"Cal.com rejected the request: {str(message)[:200]}",
                provider="calcom",
            )
        return data.get("data", data)

    # ----------------------------------------------------------- operations ---

    async def get_slots(
        self, window: TimeWindow, *, timezone_name: str = "UTC"
    ) -> list[AvailabilitySlot]:
        """
        Bookable slots for the configured event type.

        Cal.com has already applied the owner's schedule, buffers, minimum
        notice and connected-calendar conflicts, so these are genuine
        openings. VoxDesk still intersects them with its own business hours
        and its own appointments -- requirement 5 says business policy and
        provider availability are different things and both must be checked.
        """
        _, raw = await self.request(
            "GET", f"{self._base()}/slots",
            params={
                "eventTypeId": self._event_type_id,
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "timeZone": "UTC",
            },
        )
        data = self._unwrap(raw)
        return [
            AvailabilitySlot(
                start=start,
                end=_slot_end(start, entry),
                timezone=timezone_name,
                provider=self.name,
                calendar_reference=str(self._event_type_id),
            )
            for start, entry in _iter_slots(data)
        ]

    async def create_event(self, request: EventRequest) -> CalendarEvent:
        attendee = request.attendee
        if not attendee.email:
            # Cal.com requires an attendee email. Refusing here with a clear
            # message beats a 400 the tenant cannot interpret; the service
            # turns this into a caller-safe "we need an email address".
            raise CalendarValidationError(
                "Cal.com requires an attendee email address to book",
                provider=self.name,
            )

        body: dict[str, Any] = {
            "eventTypeId": self._event_type_id,
            "start": request.start.astimezone(UTC).isoformat(),
            "attendee": {
                "name": attendee.display_name,
                "email": attendee.email,
                "timeZone": request.timezone,
                "language": (self.context.config or {}).get("language") or "en",
            },
        }
        if attendee.phone:
            body["attendee"]["phoneNumber"] = attendee.phone
        if request.idempotency_key:
            # No server-side dedupe; this is what `find_event_by_key` reads.
            body["metadata"] = {
                **(request.metadata or {}),
                "voxdesk_key": request.idempotency_key,
            }
        elif request.metadata:
            body["metadata"] = dict(request.metadata)

        try:
            _, raw = await self.request(
                "POST", f"{self._base()}/bookings", json_body=body
            )
        except CalendarValidationError as exc:
            # Cal.com answers "no longer available" with a 400 rather than a
            # 409. Classifying that as validation would make it permanent and
            # unhelpful; it is a conflict, and the agent should offer another
            # time.
            if _looks_like_conflict(str(exc)):
                raise CalendarConflictError(
                    "that time is no longer available", provider=self.name
                ) from exc
            raise

        return self._to_event(self._unwrap(raw))

    async def reschedule(
        self, external_id: str, new_start: datetime, *, reason: str = ""
    ) -> CalendarEvent:
        """
        The dedicated reschedule endpoint.

        Cal.com handles cancelling the old booking and creating the new one
        atomically on its side, which is strictly better than doing it in two
        calls from here — a failure between them would leave the customer with
        no booking at all.
        """
        body: dict[str, Any] = {"start": new_start.astimezone(UTC).isoformat()}
        if reason:
            body["reschedulingReason"] = reason

        try:
            _, raw = await self.request(
                "POST", f"{self._base()}/bookings/{external_id}/reschedule",
                json_body=body,
            )
        except CalendarValidationError as exc:
            if _looks_like_conflict(str(exc)):
                raise CalendarConflictError(
                    "that time is no longer available", provider=self.name
                ) from exc
            raise
        return self._to_event(self._unwrap(raw))

    async def cancel_event(
        self, external_id: str, *, reason: str = "", calendar_reference: str | None = None
    ) -> None:
        try:
            await self.request(
                "POST", f"{self._base()}/bookings/{external_id}/cancel",
                json_body={"cancellationReason": reason or "Cancelled by VoxDesk"},
            )
        except CalendarNotFoundError:
            return                                  # already gone; idempotent
        except CalendarValidationError as exc:
            # Cal.com returns 400 for "already cancelled". Requirement 18 says
            # cancelling twice must not error repeatedly, and the end state we
            # want already holds.
            if _looks_like_already_cancelled(str(exc)):
                return
            raise

    async def get_event(
        self, external_id: str, *, calendar_reference: str | None = None
    ) -> CalendarEvent | None:
        try:
            _, raw = await self.request(
                "GET", f"{self._base()}/bookings/{external_id}"
            )
        except CalendarNotFoundError:
            return None
        return self._to_event(self._unwrap(raw))

    async def find_event_by_key(
        self, idempotency_key: str, window: TimeWindow
    ) -> CalendarEvent | None:
        """
        Scan the affected window for our metadata key.

        Bounded to one appointment's window, so it is a small query. A failure
        here re-raises rather than returning `None`: "I could not check" must
        not be mistaken for "it is definitely not there", or the service would
        book a duplicate.
        """
        _, raw = await self.request(
            "GET", f"{self._base()}/bookings",
            params={
                "afterStart": window.start.isoformat(),
                "beforeEnd": window.end.isoformat(),
                "take": 50,
            },
        )
        data = self._unwrap(raw)
        bookings = data if isinstance(data, list) else (data or {}).get("bookings") or []

        for booking in bookings:
            if not isinstance(booking, dict):
                continue
            metadata = booking.get("metadata") or {}
            if metadata.get("voxdesk_key") != idempotency_key:
                continue
            if str(booking.get("status") or "").lower() in ("cancelled", "rejected"):
                continue
            return self._to_event(booking)
        return None

    async def health_check(self) -> HealthResult:
        async def probe():
            # Listing event types proves the key works and the account is
            # reachable, without creating anything.
            await self.request("GET", f"{self._base()}/event-types")

        return await self._timed_health_check(probe)

    # ----------------------------------------------------------- conversion ---

    def _to_event(self, data: Any) -> CalendarEvent:
        if isinstance(data, dict) and "booking" in data:
            data = data["booking"]
        if not isinstance(data, dict):
            raise CalendarValidationError(
                "Cal.com returned an unrecognised booking payload", provider=self.name
            )

        uid = data.get("uid") or data.get("id")
        if not uid:
            raise CalendarValidationError(
                "Cal.com accepted the booking but returned no uid",
                provider=self.name,
            )

        status = str(data.get("status") or "accepted").lower()
        return CalendarEvent(
            external_id=str(uid),
            start=_parse_dt(data.get("start") or data.get("startTime")),
            end=_parse_dt(data.get("end") or data.get("endTime")),
            title=str(data.get("title") or ""),
            status="cancelled" if status in ("cancelled", "rejected") else "confirmed",
            calendar_reference=str(data.get("eventTypeId") or ""),
            meeting_url=data.get("meetingUrl") or data.get("location") or None,
        )


# ------------------------------------------------------------------ helpers ---

def _iter_slots(data: Any):
    """
    Cal.com has shipped several slot shapes across v2 minor versions:

      {"2026-03-02": [{"start": "..."}, ...]}     (grouped by date)
      {"slots": {"2026-03-02": [...]}}
      [{"start": "..."}, ...]                     (flat)

    All three are accepted rather than pinning one, because a tenant on a
    different `cal-api-version` should not silently get zero availability.
    """
    if isinstance(data, dict) and "slots" in data:
        data = data["slots"]

    if isinstance(data, list):
        for entry in data:
            parsed = _slot_start(entry)
            if parsed:
                yield parsed, entry
        return

    if isinstance(data, dict):
        for entries in data.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                parsed = _slot_start(entry)
                if parsed:
                    yield parsed, entry


def _slot_start(entry: Any) -> datetime | None:
    raw = entry.get("start") or entry.get("time") if isinstance(entry, dict) else entry
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return _parse_dt(raw)
    except CalendarError:
        return None


def _slot_end(start: datetime, entry: Any) -> datetime:
    if isinstance(entry, dict):
        for key in ("end", "endTime"):
            if entry.get(key):
                try:
                    return _parse_dt(entry[key])
                except CalendarError:
                    pass
    # Cal.com often omits the end; the event type's length is authoritative
    # and the service re-derives duration from policy anyway.
    from datetime import timedelta
    return start + timedelta(minutes=30)


def _parse_dt(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise CalendarValidationError(
            "Cal.com returned a booking with no timestamp", provider="calcom"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalendarValidationError(
            "Cal.com returned an unparseable timestamp", provider="calcom"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _looks_like_conflict(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "no longer available", "not available", "already booked",
            "slot", "conflict", "fully booked",
        )
    )


def _looks_like_already_cancelled(message: str) -> bool:
    lowered = message.lower()
    return "already cancelled" in lowered or "already canceled" in lowered