"""
The provider interface.

Requirement 3 is explicit that providers must not be forced to implement
operations their API does not have. That rules out an abstract base class with
seven abstract methods, because then a Jobber adapter has to write five
`raise NotImplementedError` stubs and the caller has to catch them.

So capability is declared, not discovered by calling and failing:

    if provider.supports(Capability.CREATE_APPOINTMENT):
        ...

`CrmProvider` supplies a default for every operation that raises
`CrmUnsupportedOperation`. An adapter overrides what it can do and declares it
in `capabilities`. The contract tests in `tests/test_crm_contract.py` assert
those two things agree — declaring a capability you have not implemented, or
implementing one you have not declared, both fail.

Adapters get one more thing from the base class: `request()`, a single HTTP
entry point that applies the timeout, converts every transport failure into
the normalized error taxonomy, and never lets an `httpx` exception escape. An
adapter that makes a raw `httpx` call bypasses the retry classification, so
there is a contract test for that too.
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.integrations.crm.errors import (
    CrmConnectionError,
    CrmError,
    CrmTimeout,
    CrmUnsupportedOperation,
    classify_status,
    safe_message,
)
from app.integrations.crm.models import (
    CrmResult,
    HealthResult,
    NormalizedActivity,
    NormalizedAppointment,
    NormalizedContact,
)


class Capability(str, enum.Enum):
    """What an adapter can actually do."""

    UPSERT_CONTACT = "upsert_contact"
    CREATE_CONTACT = "create_contact"
    UPDATE_CONTACT = "update_contact"
    GET_CONTACT = "get_contact"
    CREATE_NOTE = "create_note"
    CREATE_ACTIVITY = "create_activity"
    CREATE_APPOINTMENT = "create_appointment"
    CANCEL_APPOINTMENT = "cancel_appointment"
    ADD_TAG = "add_tag"
    ADD_CUSTOM_FIELDS = "add_custom_fields"
    HEALTH_CHECK = "health_check"


@dataclass(frozen=True)
class ProviderContext:
    """
    Everything an adapter is allowed to know.

    Note what is absent: no database session, no `Tenant` row, no request. An
    adapter cannot query, cannot widen its own scope, and cannot accidentally
    read another tenant's anything, because it holds no handle capable of it.
    `tenant_id` is present only so it can be put in a log line and in the
    AES-GCM associated data.

    `credentials` arrives already decrypted and is never logged; `config` is
    the non-secret half and is safe to echo.
    """

    tenant_id: str
    credentials: dict[str, Any]
    config: dict[str, Any]
    field_mappings: dict[str, Any]
    share_transcripts: bool = False
    timeout_seconds: float = 10.0

    def __repr__(self) -> str:
        # A ProviderContext in a traceback must not print the credentials.
        return (
            f"ProviderContext(tenant_id={self.tenant_id!r}, "
            f"credential_keys={sorted(self.credentials)!r}, "
            f"config_keys={sorted(self.config)!r})"
        )


class CrmProvider:
    """
    Base adapter.

    Subclasses set `name` and `capabilities`, then override the operations
    they support.
    """

    #: Matches `CrmProviderType.value`.
    name: str = "base"
    capabilities: frozenset[Capability] = frozenset()

    def __init__(self, context: ProviderContext):
        self.context = context

    # ------------------------------------------------------------ capability ---

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def _require(self, capability: Capability) -> None:
        if not self.supports(capability):
            raise CrmUnsupportedOperation(
                f"{self.name} does not support {capability.value}", provider=self.name
            )

    # ------------------------------------------------------------ operations ---
    #
    # Defaults raise. Nothing here is abstract, so a two-operation adapter is
    # a two-method class.

    async def upsert_contact(self, contact: NormalizedContact) -> CrmResult:
        self._require(Capability.UPSERT_CONTACT)
        raise CrmUnsupportedOperation(f"{self.name}.upsert_contact", provider=self.name)

    async def create_contact(self, contact: NormalizedContact) -> CrmResult:
        self._require(Capability.CREATE_CONTACT)
        raise CrmUnsupportedOperation(f"{self.name}.create_contact", provider=self.name)

    async def update_contact(
        self, external_id: str, contact: NormalizedContact
    ) -> CrmResult:
        self._require(Capability.UPDATE_CONTACT)
        raise CrmUnsupportedOperation(f"{self.name}.update_contact", provider=self.name)

    async def get_contact(
        self, *, external_id: str | None = None, phone: str | None = None,
        email: str | None = None,
    ) -> CrmResult | None:
        self._require(Capability.GET_CONTACT)
        raise CrmUnsupportedOperation(f"{self.name}.get_contact", provider=self.name)

    async def create_note(
        self, external_contact_id: str, activity: NormalizedActivity
    ) -> CrmResult:
        self._require(Capability.CREATE_NOTE)
        raise CrmUnsupportedOperation(f"{self.name}.create_note", provider=self.name)

    async def create_activity(
        self, external_contact_id: str, activity: NormalizedActivity
    ) -> CrmResult:
        self._require(Capability.CREATE_ACTIVITY)
        raise CrmUnsupportedOperation(f"{self.name}.create_activity", provider=self.name)

    async def create_appointment(
        self, appointment: NormalizedAppointment, *, external_contact_id: str | None = None
    ) -> CrmResult:
        self._require(Capability.CREATE_APPOINTMENT)
        raise CrmUnsupportedOperation(
            f"{self.name}.create_appointment", provider=self.name
        )

    async def cancel_appointment(self, external_event_id: str) -> CrmResult:
        self._require(Capability.CANCEL_APPOINTMENT)
        raise CrmUnsupportedOperation(
            f"{self.name}.cancel_appointment", provider=self.name
        )

    async def add_tag(self, external_contact_id: str, tags: list[str]) -> CrmResult:
        self._require(Capability.ADD_TAG)
        raise CrmUnsupportedOperation(f"{self.name}.add_tag", provider=self.name)

    async def add_custom_fields(
        self, external_contact_id: str, fields: dict[str, Any]
    ) -> CrmResult:
        self._require(Capability.ADD_CUSTOM_FIELDS)
        raise CrmUnsupportedOperation(
            f"{self.name}.add_custom_fields", provider=self.name
        )

    async def health_check(self) -> HealthResult:
        """
        Default: report unsupported without pretending to be connected.

        Returning `connected=False` rather than raising, because a health
        check is a diagnostic — a tenant clicking "Test" should get an answer,
        not a 500.
        """
        return HealthResult(
            connected=False, provider=self.name, latency_ms=0.0,
            safe_message=f"{self.name} does not implement a health check",
        )

    # ------------------------------------------------------------- transport ---

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "User-Agent": "VoxDesk/0.5"}

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any | None = None,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200, 201, 202, 204),
    ) -> tuple[int, Any]:
        """
        One HTTP call, with every failure normalized.

        Adapters must route through here. It is what guarantees:

        * the per-request timeout is applied (requirement 14: nothing waits
          on a CRM indefinitely),
        * a `httpx.TimeoutException` becomes `CrmTimeout` and therefore
          retries, while a 422 becomes `CrmValidationError` and therefore
          does not,
        * `Retry-After` is read off a 429 and carried on the exception,
        * no `httpx` type ever escapes into the service layer.

        Returns `(status, parsed_body)`; raises a `CrmError` subclass on
        anything not in `expected`.
        """
        merged = self._headers()
        merged.update(headers or {})

        try:
            async with httpx.AsyncClient(timeout=self.context.timeout_seconds) as client:
                response = await client.request(
                    method, url, json=json_body, params=params, headers=merged
                )
        except httpx.TimeoutException as exc:
            raise CrmTimeout(
                f"{self.name} timed out after {self.context.timeout_seconds:g}s",
                provider=self.name,
            ) from exc
        except httpx.HTTPError as exc:
            # Connection reset, DNS failure, TLS error. `type(exc).__name__`
            # rather than `str(exc)`: httpx puts the full URL in the message,
            # and a URL can carry a token in its query string.
            raise CrmConnectionError(
                f"{self.name} connection failed ({type(exc).__name__})",
                provider=self.name,
            ) from exc

        if response.status_code in expected:
            return response.status_code, _parse(response)

        raise classify_status(
            response.status_code,
            provider=self.name,
            body=response.text if response.status_code not in (401, 403) else "",
            retry_after=_retry_after(response),
        )

    async def _timed_health_check(self, probe) -> HealthResult:
        """
        Shared health-check wrapper: run `probe`, time it, normalize failures.

        Every adapter's health check is "call one cheap read-only endpoint and
        see what happens", so the timing, the error mapping and the guarantee
        that no raw provider body reaches the result live here rather than
        being written out four times.
        """
        started = time.perf_counter()
        try:
            await probe()
        except CrmError as exc:
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
    Read `Retry-After`, defensively.

    Only the delta-seconds form is honoured. The HTTP-date form is legal but
    rare from these providers, and parsing a date supplied by a remote server
    into a sleep duration is a small denial-of-service waiting to happen. An
    unparseable value falls back to our own backoff, which is always safe.
    """
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if not raw:
        return None
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    #: Cap it. A provider claiming a one-hour wait should not pin a worker
    #: slot; our scheduler will pick the row up again later regardless.
    return min(value, 300.0)
