"""
Microsoft Outlook adapter (Microsoft Graph v1.0).

Contract:

* Base ``https://graph.microsoft.com/v1.0``
* ``Authorization: Bearer <access token>``
* Free/busy: ``POST /me/calendar/getSchedule`` with ``schedules``,
  ``startTime``/``endTime`` as ``{dateTime, timeZone}``, and
  ``availabilityViewInterval``
* Events: ``POST /me/events``, ``GET|PATCH|DELETE /me/events/{id}``
* Token refresh: ``POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token``

**The idempotency mechanism is ``transactionId``.** Graph documents it on
create-event as a client-supplied value used to avoid duplicates when a
request is retried. That is exactly the ambiguous-timeout case, so every
create carries one derived from the booking's idempotency key.

**The timezone decision, which is the trap in this API.** Graph historically
speaks *Windows* timezone names — ``"Pacific Standard Time"``, not
``"America/Los_Angeles"``. Shipping an IANA↔Windows mapping table would be a
second source of truth for DST and a permanent maintenance liability.

So this adapter sends **UTC instants with ``timeZone: "UTC"``** and asks for
responses in UTC via ``Prefer: outlook.timezone="UTC"``. Graph accepts UTC
under both naming schemes, the instant transmitted is unambiguous, and all
local rendering stays in `timezones.py` where there is exactly one
implementation of it. The cost is that the Outlook UI shows the event in the
user's own zone rather than the business's — which is what an Outlook user
expects anyway.

**Cancel vs delete.** Graph has both: ``POST /events/{id}/cancel`` sends a
cancellation notice and only works when the caller is the organiser, while
``DELETE`` removes it. Booked appointments are organised by the connected
mailbox, so cancel is tried first and delete is the fallback — a cancellation
that silently fails is worse than one that removes the event without notifying.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.core.ssrf import OutboundUrlError, validate_outbound_url
from app.integrations.calendar.base import (
    CalendarCapability,
    CalendarProvider,
)
from app.integrations.calendar.errors import (
    CalendarAuthError,
    CalendarConfigurationError,
    CalendarError,
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

BASE_URL = "https://graph.microsoft.com/v1.0"
TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

#: Graph's `availabilityView` string is one character per interval:
#: 0 free, 1 tentative, 2 busy, 3 out of office, 4 working elsewhere.
#: Only 0 and 4 leave the person genuinely bookable.
_FREE_CODES = {"0", "4"}

_UUID_SAFE = re.compile(r"[^A-Za-z0-9\-]")


class MicrosoftCalendarProvider(CalendarProvider):
    name = "microsoft"
    capabilities = frozenset({
        CalendarCapability.GET_AVAILABILITY,
        CalendarCapability.FREE_BUSY,
        CalendarCapability.CREATE_EVENT,
        CalendarCapability.UPDATE_EVENT,
        CalendarCapability.CANCEL_EVENT,
        CalendarCapability.GET_EVENT,
        CalendarCapability.RESCHEDULE,
        CalendarCapability.INVITE_ATTENDEE,
        CalendarCapability.CONFERENCING,
        CalendarCapability.EVENT_REMINDERS,
        CalendarCapability.HEALTH_CHECK,
    })

    # ------------------------------------------------------------- plumbing ---

    @property
    def _token(self) -> str:
        token = (self.context.credentials or {}).get("access_token") or ""
        if not token:
            raise CalendarConfigurationError(
                "Microsoft access token is not configured", provider=self.name
            )
        return token

    def _base(self) -> str:
        # SSRF guard (Step 9): this URL receives the bearer access token.
        base = (self.context.config or {}).get("base_url") or BASE_URL
        try:
            validate_outbound_url(base, require_https=True)
        except OutboundUrlError as exc:
            raise CalendarConfigurationError(str(exc), provider=self.name)
        return base

    def _mailbox(self) -> str:
        """
        Whose calendar. `/me` for a delegated user token; `/users/{upn}` for
        an application token acting on a shared mailbox.
        """
        mailbox = (self.context.config or {}).get("mailbox")
        return f"/users/{mailbox}" if mailbox else "/me"

    def _calendar_path(self) -> str:
        calendar_id = (self.context.config or {}).get("calendar_id")
        if calendar_id:
            return f"{self._mailbox()}/calendars/{calendar_id}"
        return f"{self._mailbox()}/calendar"

    def _events_path(self) -> str:
        calendar_id = (self.context.config or {}).get("calendar_id")
        if calendar_id:
            return f"{self._mailbox()}/calendars/{calendar_id}/events"
        return f"{self._mailbox()}/events"

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["Authorization"] = f"Bearer {self._token}"
        # Ask for UTC back. Without this Graph answers in the mailbox's zone
        # and the offsets have to be inferred, which is where hours go missing.
        headers["Prefer"] = 'outlook.timezone="UTC"'
        return headers

    # ---------------------------------------------------------------- OAuth ---

    async def refresh_access_token(self) -> dict[str, Any]:
        """
        Refresh via the Microsoft identity platform.

        Microsoft *does* return a rotated refresh token, unlike Google, so the
        new one must be stored — keeping the old one works until it is
        invalidated and then fails at the worst possible moment.
        """
        credentials = self.context.credentials or {}
        refresh_token = credentials.get("refresh_token")
        client_id = credentials.get("client_id")
        client_secret = credentials.get("client_secret")
        if not (refresh_token and client_id and client_secret):
            raise CalendarConfigurationError(
                "Microsoft refresh requires refresh_token, client_id and client_secret",
                provider=self.name,
            )

        directory = (self.context.config or {}).get("directory_tenant") or "common"
        token_url = (self.context.config or {}).get("token_url") or (
            TOKEN_URL_TEMPLATE.format(tenant=directory)
        )
        # SSRF guard (Step 9): the refresh body carries client_secret and the
        # refresh token, so the token endpoint must be https and non-private.
        try:
            validate_outbound_url(token_url, require_https=True)
        except OutboundUrlError as exc:
            raise CalendarConfigurationError(str(exc), provider=self.name)

        try:
            _, data = await self.request(
                "POST", token_url,
                form={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": credentials.get("scope")
                    or "https://graph.microsoft.com/.default offline_access",
                },
                authenticated=False,
            )
        except CalendarValidationError as exc:
            raise CalendarAuthError(
                "Microsoft refused the refresh token; the tenant must reconnect",
                provider=self.name,
            ) from exc

        if not isinstance(data, dict) or not data.get("access_token"):
            raise CalendarAuthError(
                "Microsoft returned no access token", provider=self.name
            )

        return {
            **credentials,
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or refresh_token,
            "expires_in": data.get("expires_in"),
        }

    # -------------------------------------------------------------- mapping ---

    @staticmethod
    def _transaction_id(idempotency_key: str | None) -> str | None:
        """Graph caps transactionId at 256 characters."""
        if not idempotency_key:
            return None
        cleaned = _UUID_SAFE.sub("", idempotency_key)[:256]
        return cleaned or None

    def event_payload(self, request: EventRequest) -> dict[str, Any]:
        """
        Normalized request -> Graph event body.

        `dateTime` is a *naive* ISO string with `timeZone` alongside — Graph
        rejects an offset inside `dateTime` when `timeZone` is also given, so
        the offset is stripped after converting to UTC.
        """
        body: dict[str, Any] = {
            "subject": request.title,
            "body": {"contentType": "text", "content": request.description or ""},
            "start": _graph_time(request.start),
            "end": _graph_time(request.end),
        }

        transaction_id = self._transaction_id(request.idempotency_key)
        if transaction_id:
            body["transactionId"] = transaction_id

        if request.attendee.email:
            body["attendees"] = [{
                "emailAddress": {
                    "address": request.attendee.email,
                    "name": request.attendee.name or request.attendee.email,
                },
                "type": "required",
            }]

        if request.attendee.phone:
            existing = body["body"]["content"]
            body["body"]["content"] = "\n".join(
                part for part in (existing, f"Phone: {request.attendee.phone}") if part
            )

        return body

    # ----------------------------------------------------------- operations ---

    async def get_busy(self, window: TimeWindow) -> list[BusyPeriod]:
        schedule_id = (
            (self.context.config or {}).get("schedule_id")
            or (self.context.config or {}).get("mailbox")
        )
        if not schedule_id:
            raise CalendarConfigurationError(
                "Microsoft free/busy needs a mailbox or schedule_id to query",
                provider=self.name,
            )

        # 15-minute granularity: fine enough for any realistic appointment
        # grid, and it keeps the availabilityView string short.
        interval = int((self.context.config or {}).get("availability_interval") or 15)

        _, data = await self.request(
            "POST", f"{self._base()}{self._calendar_path()}/getSchedule",
            json_body={
                "schedules": [schedule_id],
                "startTime": _graph_time(window.start),
                "endTime": _graph_time(window.end),
                "availabilityViewInterval": interval,
            },
        )

        entries = (data or {}).get("value") or []
        if not entries:
            raise CalendarValidationError(
                "Microsoft returned no schedule for that mailbox", provider=self.name
            )

        entry = entries[0]
        if entry.get("error"):
            message = str((entry["error"] or {}).get("message", "unknown"))
            raise CalendarAuthError(
                f"Microsoft refused the schedule for that mailbox: {message[:120]}",
                provider=self.name,
            )

        busy: list[BusyPeriod] = []
        for item in entry.get("scheduleItems") or []:
            status = str(item.get("status") or "busy").lower()
            if status == "free":
                continue
            busy.append(BusyPeriod(
                start=_parse_graph_time(item.get("start")),
                end=_parse_graph_time(item.get("end")),
                source="microsoft",
            ))

        # `scheduleItems` is omitted when the caller lacks detail permission,
        # but `availabilityView` is always present. Falling back to it means a
        # restricted mailbox still yields correct busy blocks rather than
        # looking completely free.
        if not busy and entry.get("availabilityView"):
            busy = _busy_from_view(
                str(entry["availabilityView"]), window.start, interval
            )
        return busy

    async def create_event(self, request: EventRequest) -> CalendarEvent:
        _, data = await self.request(
            "POST", f"{self._base()}{self._events_path()}",
            json_body=self.event_payload(request),
        )
        return self._to_event(data)

    async def update_event(
        self, external_id: str, request: EventRequest
    ) -> CalendarEvent:
        body = self.event_payload(request)
        # transactionId is create-only; PATCHing it is rejected.
        body.pop("transactionId", None)
        _, data = await self.request(
            "PATCH", f"{self._base()}{self._mailbox()}/events/{external_id}",
            json_body=body,
        )
        return self._to_event(data)

    async def cancel_event(
        self, external_id: str, *, reason: str = "", calendar_reference: str | None = None
    ) -> None:
        path = f"{self._base()}{self._mailbox()}/events/{external_id}"
        try:
            # Organiser path: notifies the attendee.
            await self.request(
                "POST", f"{path}/cancel",
                json_body={"Comment": reason or "Cancelled"},
                expected=(200, 202, 204),
            )
            return
        except CalendarNotFoundError:
            return                      # already gone; idempotent
        except CalendarError:
            # Not the organiser, or a single non-meeting event -- Graph
            # rejects /cancel for those. Fall through to delete rather than
            # leaving the event on the calendar.
            pass

        try:
            await self.request("DELETE", path, expected=(200, 204))
        except CalendarNotFoundError:
            return

    async def get_event(
        self, external_id: str, *, calendar_reference: str | None = None
    ) -> CalendarEvent | None:
        try:
            _, data = await self.request(
                "GET", f"{self._base()}{self._mailbox()}/events/{external_id}"
            )
        except CalendarNotFoundError:
            return None
        return self._to_event(data)

    async def find_event_by_key(
        self, idempotency_key: str, window: TimeWindow
    ) -> CalendarEvent | None:
        """
        Reconcile by scanning the window for our transactionId.

        Graph has no "get by transactionId" endpoint, so this filters events in
        the affected window. Bounded by the window, which is one appointment
        wide, so it is a small query rather than a mailbox scan.
        """
        transaction_id = self._transaction_id(idempotency_key)
        if not transaction_id:
            return None

        try:
            _, data = await self.request(
                "GET", f"{self._base()}{self._mailbox()}/calendarView",
                params={
                    "startDateTime": window.start.isoformat(),
                    "endDateTime": window.end.isoformat(),
                    "$select": "id,subject,start,end,transactionId,isCancelled,onlineMeeting",
                    "$top": "50",
                },
            )
        except CalendarError:
            # Could not tell. Returning None here would mean "definitely not
            # there", which would license a duplicate booking -- so re-raise
            # and let the service treat it as unknown.
            raise

        for item in (data or {}).get("value") or []:
            if item.get("transactionId") == transaction_id and not item.get("isCancelled"):
                return self._to_event(item)
        return None

    async def health_check(self) -> HealthResult:
        async def probe():
            await self.request("GET", f"{self._base()}{self._calendar_path()}")

        return await self._timed_health_check(probe)

    # ----------------------------------------------------------- conversion ---

    def _to_event(self, data: Any) -> CalendarEvent:
        if not isinstance(data, dict) or not data.get("id"):
            raise CalendarValidationError(
                "Microsoft accepted the request but returned no event id",
                provider=self.name,
            )
        online = data.get("onlineMeeting") or {}
        return CalendarEvent(
            external_id=str(data["id"]),
            start=_parse_graph_time(data.get("start")),
            end=_parse_graph_time(data.get("end")),
            title=str(data.get("subject") or ""),
            status="cancelled" if data.get("isCancelled") else "confirmed",
            meeting_url=online.get("joinUrl") or None,
        )


# ------------------------------------------------------------------ helpers ---

def _graph_time(moment: datetime) -> dict[str, str]:
    """
    `{"dateTime": "<naive UTC ISO>", "timeZone": "UTC"}`.

    The offset is stripped deliberately: Graph rejects a body that carries both
    an offset inside `dateTime` and a separate `timeZone`.
    """
    utc = moment.astimezone(UTC).replace(tzinfo=None)
    return {"dateTime": utc.isoformat(timespec="seconds"), "timeZone": "UTC"}


def _parse_graph_time(value: Any) -> datetime:
    if not isinstance(value, dict):
        raise CalendarValidationError(
            "Microsoft returned an event with no usable time", provider="microsoft"
        )
    raw = value.get("dateTime") or ""
    if not raw:
        raise CalendarValidationError(
            "Microsoft returned an event with no dateTime", provider="microsoft"
        )

    # Graph sends 7 fractional digits; `fromisoformat` accepts at most 6 on
    # older Pythons, so it is truncated rather than risking a parse failure.
    text = raw.replace("Z", "")
    if "." in text:
        head, _, fraction = text.partition(".")
        text = f"{head}.{fraction[:6]}"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CalendarValidationError(
            "Microsoft returned an unparseable timestamp", provider="microsoft"
        ) from exc

    if parsed.tzinfo is None:
        # We always ask for UTC via the Prefer header, and `timeZone` in the
        # response confirms it.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _busy_from_view(view: str, start: datetime, interval_minutes: int) -> list[BusyPeriod]:
    """
    Decode `availabilityView` into busy periods.

    One character per interval. Consecutive non-free characters are merged so
    a three-hour meeting is one block rather than twelve.
    """
    from datetime import timedelta

    periods: list[BusyPeriod] = []
    run_start: datetime | None = None

    for index, code in enumerate(view):
        moment = start + timedelta(minutes=interval_minutes * index)
        if code not in _FREE_CODES:
            if run_start is None:
                run_start = moment
        elif run_start is not None:
            periods.append(BusyPeriod(start=run_start, end=moment, source="microsoft"))
            run_start = None

    if run_start is not None:
        end = start + timedelta(minutes=interval_minutes * len(view))
        periods.append(BusyPeriod(start=run_start, end=end, source="microsoft"))
    return periods