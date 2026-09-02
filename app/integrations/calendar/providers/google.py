"""
Google Calendar adapter (Calendar API v3, per-tenant OAuth).

Contract:

* Base ``https://www.googleapis.com/calendar/v3``
* ``Authorization: Bearer <access token>``
* Free/busy: ``POST /freeBusy`` with ``timeMin``/``timeMax``/``items``
* Events: ``POST|GET|PATCH|DELETE /calendars/{calendarId}/events[/{eventId}]``
* Token refresh: ``POST https://oauth2.googleapis.com/token`` with
  ``grant_type=refresh_token``

**The idempotency mechanism is a client-supplied event id.** Google lets the
caller set ``id`` on the event body, and returns **409** if that id already
exists. That turns the ambiguous-timeout problem into a solved one: we derive
the id from the booking's idempotency key, so a retry after a timeout either
creates the event (it never landed) or gets a 409 (it did) — and a 409 on our
own key means "already booked", not "someone else took it".

Google's id charset is base32hex: lowercase ``a-v`` and ``0-9``, 5–1024 chars.
Our keys are sha256 hex, which is a strict subset, but `_event_id()` filters
anyway so a future key format cannot silently produce 400s.

**Two Google quirks that are handled explicitly:**

* A **403** may be a throttle rather than a permission problem
  (``rateLimitExceeded``, ``userRateLimitExceeded``). `base.request` calls
  `refine_403` so those retry instead of stranding the tenant.
* A **410 Gone** means the event was deleted. For a cancel, that is success,
  not an error — see `cancel_event`.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.integrations.calendar.base import (
    CalendarCapability,
    CalendarProvider,
)
from app.integrations.calendar.errors import (
    CalendarAuthError,
    CalendarConfigurationError,
    CalendarConflictError,
    CalendarNotFoundError,
    CalendarValidationError,
)
from app.integrations.calendar.models import (
    BusyPeriod,
    CalendarEvent,
    EventRequest,
    HealthResult,
    TimeWindow,
)
from app.integrations.calendar.timezones import UTC

BASE_URL = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Google event ids are base32hex: a-v and 0-9 only.
_ID_ALLOWED = re.compile(r"[^a-v0-9]")


class GoogleCalendarProvider(CalendarProvider):
    name = "google"
    capabilities = frozenset({
        CalendarCapability.GET_AVAILABILITY,
        CalendarCapability.FREE_BUSY,
        CalendarCapability.CREATE_EVENT,
        CalendarCapability.UPDATE_EVENT,
        CalendarCapability.CANCEL_EVENT,
        CalendarCapability.GET_EVENT,
        CalendarCapability.RESCHEDULE,
        CalendarCapability.INVITE_ATTENDEE,
        CalendarCapability.EVENT_REMINDERS,
        CalendarCapability.HEALTH_CHECK,
    })
    # CONFERENCING is absent: creating a Meet link needs
    # `conferenceDataVersion=1` plus a request id, and silently not creating
    # one when a tenant expects it is worse than saying we cannot.

    # ------------------------------------------------------------- plumbing ---

    @property
    def _token(self) -> str:
        token = (self.context.credentials or {}).get("access_token") or ""
        if not token:
            raise CalendarConfigurationError(
                "Google access token is not configured", provider=self.name
            )
        return token

    @property
    def _calendar_id(self) -> str:
        calendar = (self.context.config or {}).get("calendar_id") or "primary"
        return calendar

    def _base(self) -> str:
        return (self.context.config or {}).get("base_url") or BASE_URL

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["Authorization"] = f"Bearer {self._token}"
        headers["Accept"] = "application/json"
        return headers

    # ---------------------------------------------------------------- OAuth ---

    async def refresh_access_token(self) -> dict[str, Any]:
        """
        Exchange the refresh token for a new access token.

        Returns the new credential bundle for the service to re-encrypt. Note
        that Google omits `refresh_token` from a refresh response, so the
        existing one is carried forward — dropping it is how an integration
        works for an hour and then dies permanently.

        `invalid_grant` means the user revoked access or the token expired
        beyond recovery. That is `CalendarAuthError`, which is *not* retryable:
        requirement 26 forbids retrying revoked auth, and hammering Google's
        token endpoint with a dead grant gets an app flagged.
        """
        credentials = self.context.credentials or {}
        refresh_token = credentials.get("refresh_token")
        client_id = credentials.get("client_id")
        client_secret = credentials.get("client_secret")

        if not (refresh_token and client_id and client_secret):
            raise CalendarConfigurationError(
                "Google refresh requires refresh_token, client_id and client_secret",
                provider=self.name,
            )

        token_url = (self.context.config or {}).get("token_url") or TOKEN_URL
        try:
            _, data = await self.request(
                "POST", token_url,
                form={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                # No bearer header: the access token is what we are replacing.
                authenticated=False,
            )
        except CalendarValidationError as exc:
            # Google answers a dead grant with 400 invalid_grant.
            raise CalendarAuthError(
                "Google refused the refresh token; the tenant must reconnect",
                provider=self.name,
            ) from exc

        if not isinstance(data, dict) or not data.get("access_token"):
            raise CalendarAuthError(
                "Google returned no access token", provider=self.name
            )

        return {
            **credentials,
            "access_token": data["access_token"],
            # Google does not resend the refresh token; keep ours.
            "refresh_token": data.get("refresh_token") or refresh_token,
            "expires_in": data.get("expires_in"),
        }

    # -------------------------------------------------------------- mapping ---

    @staticmethod
    def _event_id(idempotency_key: str | None) -> str | None:
        """
        Derive a Google event id from our idempotency key.

        Filtered to Google's base32hex charset and length-checked, so a future
        change to key format produces `None` (Google generates an id, and we
        fall back to reconciliation by search) rather than a stream of 400s.
        """
        if not idempotency_key:
            return None
        cleaned = _ID_ALLOWED.sub("", idempotency_key.lower())
        return cleaned if 5 <= len(cleaned) <= 1024 else None

    def event_payload(self, request: EventRequest) -> dict[str, Any]:
        """
        Normalized request -> Google event body.

        Public and pure, so payload mapping is testable without HTTP.

        Times are sent as UTC with an explicit ``timeZone``. Google accepts an
        offset in the string, but sending UTC plus the zone name means the
        event still renders in the business's local time in the Google UI while
        the instant we transmit is unambiguous. Sending a local wall clock with
        a bare offset is what breaks across DST.
        """
        body: dict[str, Any] = {
            "summary": request.title,
            "description": request.description,
            "start": {
                "dateTime": request.start.astimezone(UTC).isoformat(),
                "timeZone": request.timezone,
            },
            "end": {
                "dateTime": request.end.astimezone(UTC).isoformat(),
                "timeZone": request.timezone,
            },
        }

        event_id = self._event_id(request.idempotency_key)
        if event_id:
            body["id"] = event_id

        if request.attendee.email:
            body["attendees"] = [{
                "email": request.attendee.email,
                "displayName": request.attendee.name or None,
            }]

        # Phone numbers are not calendar attendees; they belong in the body so
        # whoever opens the event can call back.
        extras = []
        if request.attendee.phone:
            extras.append(f"Phone: {request.attendee.phone}")
        if extras:
            body["description"] = "\n".join(
                part for part in (body["description"], *extras) if part
            )

        return body

    # ----------------------------------------------------------- operations ---

    async def get_busy(self, window: TimeWindow) -> list[BusyPeriod]:
        _, data = await self.request(
            "POST", f"{self._base()}/freeBusy",
            json_body={
                "timeMin": window.start.isoformat(),
                "timeMax": window.end.isoformat(),
                "items": [{"id": self._calendar_id}],
            },
        )
        calendars = (data or {}).get("calendars") or {}
        entry = calendars.get(self._calendar_id) or {}

        # A per-calendar `errors` array means Google could not read *that*
        # calendar even though the HTTP call succeeded. Returning [] here would
        # reproduce the exact bug the audit found: an unreadable calendar
        # looking completely free.
        if entry.get("errors"):
            reason = str(entry["errors"][0].get("reason", "unknown"))
            if reason in ("notFound", "deleted"):
                raise CalendarNotFoundError(
                    f"Google cannot find calendar {self._calendar_id!r}",
                    provider=self.name,
                )
            raise CalendarAuthError(
                f"Google refused free/busy for that calendar ({reason})",
                provider=self.name,
            )

        return [
            BusyPeriod(
                start=_parse_dt(period["start"]),
                end=_parse_dt(period["end"]),
                source="google",
            )
            for period in entry.get("busy", [])
        ]

    async def create_event(self, request: EventRequest) -> CalendarEvent:
        calendar = request.calendar_reference or self._calendar_id
        body = self.event_payload(request)

        try:
            _, data = await self.request(
                "POST", f"{self._base()}/calendars/{calendar}/events",
                json_body=body,
            )
        except CalendarConflictError:
            # 409 on *our own* derived id means this exact booking already
            # landed -- almost certainly our own earlier attempt that timed
            # out. Fetch it and report success, because it is one.
            event_id = body.get("id")
            if event_id:
                existing = await self.get_event(
                    event_id, calendar_reference=calendar
                )
                if existing is not None:
                    return CalendarEvent(
                        external_id=existing.external_id,
                        start=existing.start, end=existing.end,
                        title=existing.title, status=existing.status,
                        calendar_reference=calendar, already_existed=True,
                    )
            raise

        return self._to_event(data, calendar)

    async def update_event(
        self, external_id: str, request: EventRequest
    ) -> CalendarEvent:
        calendar = request.calendar_reference or self._calendar_id
        body = self.event_payload(request)
        # The id is immutable; PATCHing it is a 400.
        body.pop("id", None)

        _, data = await self.request(
            "PATCH", f"{self._base()}/calendars/{calendar}/events/{external_id}",
            json_body=body,
        )
        return self._to_event(data, calendar)

    async def cancel_event(
        self, external_id: str, *, reason: str = "", calendar_reference: str | None = None
    ) -> None:
        calendar = calendar_reference or self._calendar_id
        try:
            await self.request(
                "DELETE", f"{self._base()}/calendars/{calendar}/events/{external_id}",
                expected=(200, 204),
            )
        except CalendarNotFoundError:
            # Already gone (404, or 410 Gone mapped to the same class).
            # Requirement 18 says cancellation must be idempotent, and the
            # desired end state -- no event on the calendar -- already holds.
            return

    async def get_event(
        self, external_id: str, *, calendar_reference: str | None = None
    ) -> CalendarEvent | None:
        calendar = calendar_reference or self._calendar_id
        try:
            _, data = await self.request(
                "GET", f"{self._base()}/calendars/{calendar}/events/{external_id}"
            )
        except CalendarNotFoundError:
            return None

        if isinstance(data, dict) and data.get("status") == "cancelled":
            # Google keeps tombstones. A cancelled event is not a bookable one.
            return CalendarEvent(
                external_id=str(data.get("id") or external_id),
                start=_parse_dt(data.get("start") or {}),
                end=_parse_dt(data.get("end") or {}),
                status="cancelled", calendar_reference=calendar,
            )
        return self._to_event(data, calendar)

    async def find_event_by_key(
        self, idempotency_key: str, window: TimeWindow
    ) -> CalendarEvent | None:
        """
        Reconciliation by deterministic id — no search needed.

        Because the event id *is* the key, "did my booking land?" is a direct
        GET rather than a listing scan.
        """
        event_id = self._event_id(idempotency_key)
        if not event_id:
            return None
        found = await self.get_event(event_id)
        if found is None or found.status == "cancelled":
            return None
        return found

    async def health_check(self) -> HealthResult:
        async def probe():
            # Cheap and read-only, and it proves the three things that break:
            # the token works, the scope covers calendar, and the calendar id
            # is real.
            await self.request(
                "GET", f"{self._base()}/calendars/{self._calendar_id}"
            )

        return await self._timed_health_check(probe)

    # ----------------------------------------------------------- conversion ---

    def _to_event(self, data: Any, calendar: str) -> CalendarEvent:
        if not isinstance(data, dict) or not data.get("id"):
            raise CalendarValidationError(
                "Google accepted the request but returned no event id",
                provider=self.name,
            )
        return CalendarEvent(
            external_id=str(data["id"]),
            start=_parse_dt(data.get("start") or {}),
            end=_parse_dt(data.get("end") or {}),
            title=str(data.get("summary") or ""),
            status=str(data.get("status") or "confirmed"),
            calendar_reference=calendar,
            meeting_url=(data.get("hangoutLink") or None),
        )


def _parse_dt(value: Any) -> datetime:
    """
    Google returns `{"dateTime": "...", "timeZone": "..."}` for timed events
    and `{"date": "YYYY-MM-DD"}` for all-day ones. Free/busy returns a bare
    string.
    """
    if isinstance(value, str):
        raw = value
    elif isinstance(value, dict):
        raw = value.get("dateTime") or value.get("date") or ""
    else:
        raw = ""

    if not raw:
        # Never fabricate "now": a missing time would silently become a real
        # instant and land in the database as a real appointment.
        raise CalendarValidationError(
            "Google returned an event with no start or end time", provider="google"
        )

    text = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # All-day: "2026-03-01"
        try:
            parsed = datetime.fromisoformat(f"{text}T00:00:00+00:00")
        except ValueError as exc:
            raise CalendarValidationError(
                "Google returned an unparseable timestamp", provider="google"
            ) from exc

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
