"""
The calendar provider interface.

Same shape as the CRM layer's `base.py`, deliberately: one codebase, one way
of writing an adapter. Capability is *declared*, not discovered by calling and
failing, so the service can ask "can this provider reschedule?" before it tries.

Two things every adapter gets and must use:

* `request()` — the single HTTP entry point. It applies the timeout, converts
  every transport failure into the normalized taxonomy, reads `Retry-After`,
  and lets no `httpx` exception escape. An adapter making a raw call bypasses
  retry classification, so there is a contract test for it.
* `CalendarContext` — everything the adapter is allowed to know. No database
  session, no `Tenant` row. An adapter *cannot* reach another tenant because
  it holds nothing capable of it.
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.integrations.calendar.errors import (
    CalendarError,
    CalendarTemporaryError,
    CalendarTimeout,
    CalendarUnsupportedError,
    classify_status,
    refine_403,
    safe_message,
)
from app.integrations.calendar.models import (
    BusyPeriod,
    CalendarEvent,
    EventRequest,
    HealthResult,
    TimeWindow,
)


class CalendarCapability(str, enum.Enum):
    GET_AVAILABILITY = "get_availability"
    FREE_BUSY = "free_busy"
    CREATE_EVENT = "create_event"
    UPDATE_EVENT = "update_event"
    CANCEL_EVENT = "cancel_event"
    GET_EVENT = "get_event"
    RESCHEDULE = "reschedule"
    INVITE_ATTENDEE = "invite_attendee"
    CONFERENCING = "conferencing"
    EVENT_REMINDERS = "event_reminders"
    HEALTH_CHECK = "health_check"


@dataclass(frozen=True)
class CalendarContext:
    """
    Everything an adapter may know about one tenant's connection.

    `credentials` arrives already decrypted and is never logged. `config` is
    the non-secret half and is safe to echo. `tenant_id` is here only so it can
    go in a log line and in the AES-GCM associated data — not so anything can
    be looked up with it.
    """

    tenant_id: str
    credentials: dict[str, Any]
    config: dict[str, Any]
    timezone: str = "UTC"
    timeout_seconds: float = 10.0

    def __repr__(self) -> str:
        # A context in a traceback must not print an OAuth token. Tracebacks
        # get pasted into issue trackers.
        return (
            f"CalendarContext(tenant_id={self.tenant_id!r}, "
            f"credential_keys={sorted(self.credentials)!r}, "
            f"config_keys={sorted(self.config)!r})"
        )


class CalendarProvider:
    """
    Base adapter. Subclasses set `name` and `capabilities`, then override only
    what they support. Nothing here is abstract, so a three-operation provider
    is a three-method class.
    """

    name: str = "base"
    capabilities: frozenset[CalendarCapability] = frozenset()

    def __init__(self, context: CalendarContext):
        self.context = context

    # ----------------------------------------------------------- capability ---

    def supports(self, capability: CalendarCapability) -> bool:
        return capability in self.capabilities

    def _require(self, capability: CalendarCapability) -> None:
        if not self.supports(capability):
            raise CalendarUnsupportedError(
                f"{self.name} does not support {capability.value}", provider=self.name
            )

    # ----------------------------------------------------------- operations ---

    async def get_busy(self, window: TimeWindow) -> list[BusyPeriod]:
        """
        Periods the calendar is not free.

        Note the contract: this **raises** on failure rather than returning an
        empty list. The pre-STEP-6 code returned `[]` on any exception, which
        made "the calendar is empty" and "the calendar is unreachable"
        indistinguishable — and therefore made an outage look like total
        availability. Emptiness must mean emptiness.
        """
        self._require(CalendarCapability.FREE_BUSY)
        raise CalendarUnsupportedError(f"{self.name}.get_busy", provider=self.name)

    async def create_event(self, request: EventRequest) -> CalendarEvent:
        self._require(CalendarCapability.CREATE_EVENT)
        raise CalendarUnsupportedError(f"{self.name}.create_event", provider=self.name)

    async def update_event(
        self, external_id: str, request: EventRequest
    ) -> CalendarEvent:
        self._require(CalendarCapability.UPDATE_EVENT)
        raise CalendarUnsupportedError(f"{self.name}.update_event", provider=self.name)

    async def cancel_event(
        self, external_id: str, *, reason: str = "", calendar_reference: str | None = None
    ) -> None:
        self._require(CalendarCapability.CANCEL_EVENT)
        raise CalendarUnsupportedError(f"{self.name}.cancel_event", provider=self.name)

    async def get_event(
        self, external_id: str, *, calendar_reference: str | None = None
    ) -> CalendarEvent | None:
        """
        Look up one event. `None` means "definitely not there".

        This is the reconciliation primitive: after an ambiguous timeout the
        service asks the provider whether the event exists before considering
        a retry, which is what stops one network blip becoming two bookings.
        """
        self._require(CalendarCapability.GET_EVENT)
        raise CalendarUnsupportedError(f"{self.name}.get_event", provider=self.name)

    async def find_event_by_key(
        self, idempotency_key: str, window: TimeWindow
    ) -> CalendarEvent | None:
        """
        Reconciliation for providers with no client-supplied event id.

        Default is `None` — "I cannot tell you". The service treats that as
        genuinely unknown and refuses to blind-retry, rather than assuming
        absence. Silence is not evidence.
        """
        return None

    async def health_check(self) -> HealthResult:
        """
        Default reports not-connected rather than raising: a tenant pressing
        "Test" deserves an answer, not a 500.
        """
        return HealthResult(
            connected=False, provider=self.name, latency_ms=0.0,
            safe_message=f"{self.name} does not implement a health check",
        )

    # ------------------------------------------------------------ transport ---

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "User-Agent": "VoxDesk/0.6"}

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any | None = None,
        form: dict | None = None,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200, 201, 202, 204),
        authenticated: bool = True,
    ) -> tuple[int, Any]:
        """
        One HTTP call, every failure normalized.

        Guarantees, all of which a raw `httpx` call in an adapter would break:
        the timeout is applied; a `TimeoutException` becomes `CalendarTimeout`
        and therefore reconciles rather than blind-retrying; a 409 becomes
        `CalendarConflictError` and therefore does not retry at all; a 403
        that is really a throttle is re-classified; and no `httpx` type
        escapes into the service.
        """
        # `authenticated=False` skips the subclass's header builder.
        #
        # Needed for OAuth token endpoints: they are exactly the case where the
        # access token is dead or absent, so calling `_headers()` there raises
        # `CalendarConfigurationError` and a tenant whose token expired could
        # never refresh it. A test caught this.
        merged = self._headers() if authenticated else CalendarProvider._headers(self)
        if form is not None:
            # OAuth token endpoints require form encoding, not JSON. Microsoft
            # rejects a JSON body outright; Google's documented contract is
            # form as well.
            merged["Content-Type"] = "application/x-www-form-urlencoded"
        merged.update(headers or {})

        try:
            async with httpx.AsyncClient(timeout=self.context.timeout_seconds) as client:
                response = await client.request(
                    method, url, json=json_body, data=form, params=params,
                    headers=merged,
                )
        except httpx.TimeoutException as exc:
            raise CalendarTimeout(
                f"{self.name} timed out after {self.context.timeout_seconds:g}s",
                provider=self.name,
            ) from exc
        except httpx.HTTPError as exc:
            # `type(exc).__name__`, not `str(exc)`: httpx puts the full URL in
            # the message and a URL can carry a token in its query string.
            raise CalendarTemporaryError(
                f"{self.name} connection failed ({type(exc).__name__})",
                provider=self.name,
            ) from exc

        if response.status_code in expected:
            return response.status_code, _parse(response)

        body = response.text if response.status_code != 401 else ""
        error = classify_status(
            response.status_code, provider=self.name, body=body,
            retry_after=_retry_after(response),
        )
        raise refine_403(error, body, provider=self.name)

    async def _timed_health_check(self, probe) -> HealthResult:
        """Run a cheap read-only probe, time it, normalize the failure."""
        started = time.perf_counter()
        try:
            await probe()
        except CalendarError as exc:
            return HealthResult(
                connected=False, provider=self.name,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                safe_message=exc.safe_message,
            )
        except Exception as exc:
            return HealthResult(
                connected=False, provider=self.name,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                safe_message=safe_message(f"unexpected error: {type(exc).__name__}"),
            )
        return HealthResult(
            connected=True, provider=self.name,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            safe_message="connected",
        )


def _parse(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _retry_after(response: httpx.Response) -> float | None:
    """
    Delta-seconds only, capped.

    The HTTP-date form is legal but rare from these providers, and parsing a
    remote-supplied date into a sleep duration is a small denial of service
    waiting to happen. An unparseable value falls back to our own backoff,
    which is always safe.
    """
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if not raw:
        return None
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return None
    return min(value, 300.0) if value >= 0 else None

