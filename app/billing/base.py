"""
The billing provider interface.

Same shape as the CRM and calendar layers: capability is declared, not
discovered by calling and failing; one HTTP entry point normalizes every
transport failure; and an adapter holds no database handle, so it *cannot*
reach another tenant.

One difference specific to billing: **every mutating call carries an
idempotency key.** Stripe supports `Idempotency-Key` natively and it is the
difference between a timeout costing a retry and a timeout costing a customer
a second subscription. `request()` takes it as a first-class argument rather
than leaving each adapter to remember.
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from app.billing.errors import (
    BillingError,
    BillingTemporaryError,
    BillingTimeout,
    BillingUnsupportedError,
    classify_status,
    safe_message,
)
from app.db.models import BillingInterval, InvoiceStatus, SubscriptionStatus


class BillingCapability(str, enum.Enum):
    CREATE_CUSTOMER = "create_customer"
    GET_CUSTOMER = "get_customer"
    CREATE_SUBSCRIPTION = "create_subscription"
    UPDATE_SUBSCRIPTION = "update_subscription"
    CANCEL_SUBSCRIPTION = "cancel_subscription"
    GET_SUBSCRIPTION = "get_subscription"
    LIST_SUBSCRIPTIONS = "list_subscriptions"
    LIST_INVOICES = "list_invoices"
    GET_INVOICE = "get_invoice"
    CHECKOUT_SESSION = "create_checkout_session"
    PORTAL_SESSION = "create_customer_portal_session"
    VERIFY_WEBHOOK = "verify_webhook"
    HEALTH_CHECK = "health_check"


# ------------------------------------------------------- normalized models ---
#
# Provider request/response shapes stay inside the adapter (requirement 5).
# These are what crosses the boundary.

@dataclass(frozen=True)
class RemoteCustomer:
    external_id: str
    email: str | None = None
    name: str | None = None
    #: True when the provider matched an existing customer rather than creating.
    already_existed: bool = False


@dataclass(frozen=True)
class RemoteSubscription:
    """
    A provider subscription, already normalized.

    `status` is a `SubscriptionStatus`, not a provider string — the mapping
    happens inside the adapter and nowhere else.
    """

    external_id: str
    status: SubscriptionStatus
    external_customer_id: str | None = None
    external_price_id: str | None = None
    interval: BillingInterval = BillingInterval.MONTH
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    trial_start: datetime | None = None
    trial_end: datetime | None = None
    cancel_at_period_end: bool = False
    canceled_at: datetime | None = None
    #: The provider's own clock for this state. Ordering authority for
    #: out-of-order webhooks (requirement 23).
    updated_at: datetime | None = None


@dataclass(frozen=True)
class RemoteInvoice:
    external_id: str
    status: InvoiceStatus
    currency: str = "usd"
    amount_due_cents: int = 0
    amount_paid_cents: int = 0
    period_start: datetime | None = None
    period_end: datetime | None = None
    hosted_invoice_url: str | None = None
    external_subscription_id: str | None = None
    external_customer_id: str | None = None


@dataclass(frozen=True)
class RemoteSession:
    """A checkout or portal session. `url` is where the browser is sent."""

    session_id: str
    url: str


@dataclass(frozen=True)
class WebhookEvent:
    """
    A verified provider event.

    Only ever produced by `verify_webhook`, which means an unverified body
    cannot be turned into one of these by accident.
    """

    event_id: str
    event_type: str
    created_at: datetime | None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HealthResult:
    connected: bool
    provider: str
    latency_ms: float
    safe_message: str = ""


@dataclass(frozen=True)
class BillingContextConfig:
    """
    Everything an adapter is allowed to know.

    No database session, no `Tenant` row, no request. `secret_key` is never
    logged and never rendered — see `__repr__`.
    """

    secret_key: str = ""
    webhook_secret: str = ""
    timeout_seconds: float = 15.0
    #: Where Stripe returns the browser after checkout.
    success_url: str = ""
    cancel_url: str = ""
    return_url: str = ""
    base_url: str = ""

    def __repr__(self) -> str:
        # A config in a traceback must not print an API key. Tracebacks get
        # pasted into issue trackers.
        return (
            f"BillingContextConfig(has_secret={bool(self.secret_key)}, "
            f"has_webhook_secret={bool(self.webhook_secret)}, "
            f"timeout={self.timeout_seconds})"
        )


class BillingProvider:
    """
    Base adapter. Subclasses set `name` and `capabilities`, then override only
    what they support. Nothing here is abstract.
    """

    name: str = "base"
    capabilities: frozenset[BillingCapability] = frozenset()

    def __init__(self, config: BillingContextConfig):
        self.config = config

    def supports(self, capability: BillingCapability) -> bool:
        return capability in self.capabilities

    def _require(self, capability: BillingCapability) -> None:
        if not self.supports(capability):
            raise BillingUnsupportedError(
                f"{self.name} does not support {capability.value}", provider=self.name
            )

    # ----------------------------------------------------------- operations ---

    async def create_customer(
        self, *, tenant_id: str, email: str | None, name: str | None,
        idempotency_key: str,
    ) -> RemoteCustomer:
        self._require(BillingCapability.CREATE_CUSTOMER)
        raise BillingUnsupportedError(f"{self.name}.create_customer", provider=self.name)

    async def get_customer(self, external_id: str) -> RemoteCustomer | None:
        self._require(BillingCapability.GET_CUSTOMER)
        raise BillingUnsupportedError(f"{self.name}.get_customer", provider=self.name)

    async def find_customer_by_tenant(self, tenant_id: str) -> RemoteCustomer | None:
        """
        Reconciliation: did a previous attempt already create this customer?

        Default `None` means "I cannot tell". The service treats that as
        genuinely unknown and does not blind-retry — silence is not evidence
        of absence, and here the cost of guessing wrong is a duplicate
        customer record.
        """
        return None

    async def create_subscription(
        self, *, external_customer_id: str, price_id: str, trial_days: int,
        idempotency_key: str, metadata: dict[str, str] | None = None,
    ) -> RemoteSubscription:
        self._require(BillingCapability.CREATE_SUBSCRIPTION)
        raise BillingUnsupportedError(
            f"{self.name}.create_subscription", provider=self.name
        )

    async def update_subscription(
        self, external_id: str, *, price_id: str | None = None,
        cancel_at_period_end: bool | None = None, proration: str = "none",
        idempotency_key: str = "",
    ) -> RemoteSubscription:
        self._require(BillingCapability.UPDATE_SUBSCRIPTION)
        raise BillingUnsupportedError(
            f"{self.name}.update_subscription", provider=self.name
        )

    async def cancel_subscription(
        self, external_id: str, *, at_period_end: bool = True,
        idempotency_key: str = "",
    ) -> RemoteSubscription:
        self._require(BillingCapability.CANCEL_SUBSCRIPTION)
        raise BillingUnsupportedError(
            f"{self.name}.cancel_subscription", provider=self.name
        )

    async def get_subscription(self, external_id: str) -> RemoteSubscription | None:
        self._require(BillingCapability.GET_SUBSCRIPTION)
        raise BillingUnsupportedError(
            f"{self.name}.get_subscription", provider=self.name
        )

    async def list_subscriptions(
        self, external_customer_id: str
    ) -> list[RemoteSubscription]:
        """
        Every subscription for a customer.

        The reconciliation primitive for requirement 32: after a timeout on
        `create_subscription`, this answers "did it land?" without creating a
        second one.
        """
        self._require(BillingCapability.LIST_SUBSCRIPTIONS)
        raise BillingUnsupportedError(
            f"{self.name}.list_subscriptions", provider=self.name
        )

    async def list_invoices(
        self, external_customer_id: str, *, limit: int = 20
    ) -> list[RemoteInvoice]:
        self._require(BillingCapability.LIST_INVOICES)
        raise BillingUnsupportedError(f"{self.name}.list_invoices", provider=self.name)

    async def get_invoice(self, external_id: str) -> RemoteInvoice | None:
        self._require(BillingCapability.GET_INVOICE)
        raise BillingUnsupportedError(f"{self.name}.get_invoice", provider=self.name)

    async def create_checkout_session(
        self, *, external_customer_id: str, price_id: str, tenant_id: str,
        trial_days: int = 0, idempotency_key: str = "",
    ) -> RemoteSession:
        self._require(BillingCapability.CHECKOUT_SESSION)
        raise BillingUnsupportedError(
            f"{self.name}.create_checkout_session", provider=self.name
        )

    async def create_portal_session(self, *, external_customer_id: str) -> RemoteSession:
        self._require(BillingCapability.PORTAL_SESSION)
        raise BillingUnsupportedError(
            f"{self.name}.create_portal_session", provider=self.name
        )

    def verify_webhook(self, *, raw_body: bytes, signature_header: str) -> WebhookEvent:
        """
        Verify a raw request body and return a normalized event.

        **Takes bytes, deliberately.** Requirement 21: signature verification
        must use the exact raw body. Accepting a parsed dict here would make
        the signature meaningless, and the type signature is what stops a
        future caller from re-serializing.
        """
        self._require(BillingCapability.VERIFY_WEBHOOK)
        raise BillingUnsupportedError(f"{self.name}.verify_webhook", provider=self.name)

    async def health_check(self) -> HealthResult:
        return HealthResult(
            connected=False, provider=self.name, latency_ms=0.0,
            safe_message=f"{self.name} does not implement a health check",
        )

    # ------------------------------------------------------------ transport ---

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": "VoxDesk/0.7"}

    async def request(
        self,
        method: str,
        url: str,
        *,
        form: dict | None = None,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        expected: tuple[int, ...] = (200, 201, 202, 204),
    ) -> tuple[int, Any]:
        """
        One HTTP call, every failure normalized.

        `idempotency_key` is first-class: Stripe honours `Idempotency-Key` on
        every POST, and a mutating billing call without one turns a network
        blip into a double charge.
        """
        merged = self._headers()
        if idempotency_key:
            merged["Idempotency-Key"] = idempotency_key
        merged.update(headers or {})

        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                response = await client.request(
                    method, url, data=form, params=params, headers=merged
                )
        except httpx.TimeoutException as exc:
            raise BillingTimeout(
                f"{self.name} timed out after {self.config.timeout_seconds:g}s",
                provider=self.name,
            ) from exc
        except httpx.HTTPError as exc:
            # `type(exc).__name__`, not `str(exc)`: httpx puts the full URL in
            # the message and a URL can carry a key in its query string.
            raise BillingTemporaryError(
                f"{self.name} connection failed ({type(exc).__name__})",
                provider=self.name,
            ) from exc

        if response.status_code in expected:
            return response.status_code, _parse(response)

        raise classify_status(
            response.status_code,
            provider=self.name,
            # A 401 body is the most likely place for an API key to be
            # reflected back, so it is excluded entirely rather than scrubbed.
            body=response.text if response.status_code not in (401, 403) else "",
            retry_after=_retry_after(response),
        )

    async def _timed_health_check(self, probe) -> HealthResult:
        started = time.perf_counter()
        try:
            await probe()
        except BillingError as exc:
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
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if not raw:
        return None
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return None
    return min(value, 300.0) if value >= 0 else None

