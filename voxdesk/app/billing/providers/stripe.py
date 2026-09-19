"""
Stripe adapter.

Contract:

* Base ``https://api.stripe.com/v1``
* ``Authorization: Bearer sk_...``, requests **form-encoded**, responses JSON
* Nested parameters use bracket syntax: ``items[0][price]=price_FAKE``
* ``Idempotency-Key`` is honoured on every POST

Written against the REST API directly rather than the ``stripe`` SDK. Three
reasons, and the third is the one that matters: the SDK is synchronous and
this codebase is async throughout; adding it pulls a large dependency for
roughly a dozen endpoints; and requirement 5 says core billing logic must not
depend on Stripe SDK classes, which is far easier to guarantee when they are
not importable.

**Everything Stripe-shaped stops here.** Status strings, form encoding, the
`data` envelope, the object-type sniffing in `verify_webhook` — none of it
leaves this file. What leaves is `RemoteSubscription`, `RemoteInvoice` and
friends, with a `SubscriptionStatus` enum member rather than a vendor string.

**Two version-drift defences.** Stripe moved `current_period_start`/`_end`
from the subscription onto its items in a 2025 API version, so `_period()`
reads the root and falls back to the first item. And `Stripe-Signature` can
carry several `v1` values during a secret rotation plus schemes we do not
know (`v0`), so verification checks *every* `v1` and ignores the rest.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from typing import Any

from app.billing.base import (
    BillingCapability,
    BillingProvider,
    HealthResult,
    RemoteCustomer,
    RemoteInvoice,
    RemoteSession,
    RemoteSubscription,
    WebhookEvent,
)
from app.billing.errors import (
    BillingAuthError,
    BillingConfigurationError,
    BillingNotFoundError,
    BillingValidationError,
)
from app.db.models import BillingInterval, InvoiceStatus, SubscriptionStatus

BASE_URL = "https://api.stripe.com/v1"

#: Stripe's documented default. Rejecting anything older is what stops a
#: captured request being replayed a week later.
SIGNATURE_TOLERANCE_SECONDS = 300

#: Stripe status -> ours. The **only** place these strings appear.
#:
#: `unpaid` maps to PAST_DUE rather than a state of its own: it means dunning
#: has been exhausted, which is operationally the same conversation, and
#: adding a state the UI would render identically buys nothing.
_STATUS_MAP: dict[str, SubscriptionStatus] = {
    "trialing": SubscriptionStatus.TRIALING,
    "active": SubscriptionStatus.ACTIVE,
    "past_due": SubscriptionStatus.PAST_DUE,
    "unpaid": SubscriptionStatus.PAST_DUE,
    "canceled": SubscriptionStatus.CANCELED,
    "incomplete": SubscriptionStatus.INCOMPLETE,
    "incomplete_expired": SubscriptionStatus.INCOMPLETE_EXPIRED,
    "paused": SubscriptionStatus.PAUSED,
}

_INVOICE_STATUS_MAP: dict[str, InvoiceStatus] = {
    "draft": InvoiceStatus.DRAFT,
    "open": InvoiceStatus.OPEN,
    "paid": InvoiceStatus.PAID,
    "uncollectible": InvoiceStatus.UNCOLLECTIBLE,
    "void": InvoiceStatus.VOID,
}


class StripeProvider(BillingProvider):
    name = "stripe"
    capabilities = frozenset({
        BillingCapability.CREATE_CUSTOMER,
        BillingCapability.GET_CUSTOMER,
        BillingCapability.CREATE_SUBSCRIPTION,
        BillingCapability.UPDATE_SUBSCRIPTION,
        BillingCapability.CANCEL_SUBSCRIPTION,
        BillingCapability.GET_SUBSCRIPTION,
        BillingCapability.LIST_SUBSCRIPTIONS,
        BillingCapability.LIST_INVOICES,
        BillingCapability.GET_INVOICE,
        BillingCapability.CHECKOUT_SESSION,
        BillingCapability.PORTAL_SESSION,
        BillingCapability.VERIFY_WEBHOOK,
        BillingCapability.HEALTH_CHECK,
    })

    # ------------------------------------------------------------- plumbing ---

    @property
    def _secret(self) -> str:
        if not self.config.secret_key:
            raise BillingConfigurationError(
                "Stripe secret key is not configured", provider=self.name
            )
        return self.config.secret_key

    def _base(self) -> str:
        return self.config.base_url or BASE_URL

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["Authorization"] = f"Bearer {self._secret}"
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        return headers

    # -------------------------------------------------------------- customer ---

    async def create_customer(
        self, *, tenant_id: str, email: str | None, name: str | None,
        idempotency_key: str,
    ) -> RemoteCustomer:
        form: dict[str, Any] = {"metadata[voxdesk_tenant_id]": tenant_id}
        if email:
            form["email"] = email
        if name:
            form["name"] = name

        _, data = await self.request(
            "POST", f"{self._base()}/customers", form=form,
            idempotency_key=idempotency_key,
        )
        return self._to_customer(data)

    async def get_customer(self, external_id: str) -> RemoteCustomer | None:
        try:
            _, data = await self.request(
                "GET", f"{self._base()}/customers/{external_id}"
            )
        except BillingNotFoundError:
            return None
        if isinstance(data, dict) and data.get("deleted"):
            # Stripe tombstones deleted customers rather than 404ing.
            return None
        return self._to_customer(data)

    async def find_customer_by_tenant(self, tenant_id: str) -> RemoteCustomer | None:
        """
        Reconciliation after an ambiguous create.

        Uses the search API on the metadata we stamped at creation. Stripe's
        search index is eventually consistent (roughly a minute), so a `None`
        here is genuinely "I cannot confirm" rather than "definitely absent" —
        which is why `service.py` treats it as unknown and refuses to create a
        second customer on that basis alone.
        """
        try:
            _, data = await self.request(
                "GET", f"{self._base()}/customers/search",
                params={
                    "query": f"metadata['voxdesk_tenant_id']:'{tenant_id}'",
                    "limit": 1,
                },
            )
        except (BillingValidationError, BillingNotFoundError):
            return None

        rows = (data or {}).get("data") or []
        if not rows:
            return None
        customer = self._to_customer(rows[0])
        return RemoteCustomer(
            external_id=customer.external_id, email=customer.email,
            name=customer.name, already_existed=True,
        )

    def _to_customer(self, data: Any) -> RemoteCustomer:
        if not isinstance(data, dict) or not data.get("id"):
            raise BillingValidationError(
                "Stripe accepted the request but returned no customer id",
                provider=self.name,
            )
        return RemoteCustomer(
            external_id=str(data["id"]),
            email=data.get("email"),
            name=data.get("name"),
        )

    # ---------------------------------------------------------- subscription ---

    async def create_subscription(
        self, *, external_customer_id: str, price_id: str, trial_days: int,
        idempotency_key: str, metadata: dict[str, str] | None = None,
    ) -> RemoteSubscription:
        form: dict[str, Any] = {
            "customer": external_customer_id,
            "items[0][price]": price_id,
            # Without this a card that needs 3DS silently leaves the
            # subscription `incomplete` and nobody finds out until the
            # customer asks why nothing works.
            "payment_behavior": "default_incomplete",
            "expand[0]": "latest_invoice.payment_intent",
        }
        if trial_days > 0:
            form["trial_period_days"] = trial_days
        for key, value in (metadata or {}).items():
            form[f"metadata[{key}]"] = value

        _, data = await self.request(
            "POST", f"{self._base()}/subscriptions", form=form,
            idempotency_key=idempotency_key,
        )
        return self._to_subscription(data)

    async def update_subscription(
        self, external_id: str, *, price_id: str | None = None,
        cancel_at_period_end: bool | None = None, proration: str = "none",
        idempotency_key: str = "",
    ) -> RemoteSubscription:
        form: dict[str, Any] = {}

        if price_id is not None:
            # Changing the price needs the existing item's id, so the current
            # state is read first. Sending `items[0][price]` without an id
            # *adds* a second item and bills the customer for both.
            current = await self._raw_subscription(external_id)
            items = ((current or {}).get("items") or {}).get("data") or []
            if not items:
                raise BillingValidationError(
                    "Stripe subscription has no items to update",
                    provider=self.name,
                )
            form["items[0][id]"] = items[0]["id"]
            form["items[0][price]"] = price_id
            form["proration_behavior"] = proration

        if cancel_at_period_end is not None:
            form["cancel_at_period_end"] = "true" if cancel_at_period_end else "false"

        if not form:
            existing = await self.get_subscription(external_id)
            if existing is None:
                raise BillingNotFoundError(
                    "Stripe subscription not found", provider=self.name
                )
            return existing

        _, data = await self.request(
            "POST", f"{self._base()}/subscriptions/{external_id}", form=form,
            idempotency_key=idempotency_key or None,
        )
        return self._to_subscription(data)

    async def cancel_subscription(
        self, external_id: str, *, at_period_end: bool = True,
        idempotency_key: str = "",
    ) -> RemoteSubscription:
        if at_period_end:
            return await self.update_subscription(
                external_id, cancel_at_period_end=True,
                idempotency_key=idempotency_key,
            )
        try:
            _, data = await self.request(
                "DELETE", f"{self._base()}/subscriptions/{external_id}",
                idempotency_key=idempotency_key or None,
            )
        except BillingNotFoundError:
            # Already gone. Requirement: cancellation is idempotent, and the
            # end state we want already holds.
            return RemoteSubscription(
                external_id=external_id, status=SubscriptionStatus.CANCELED,
                canceled_at=datetime.now(timezone.utc),
            )
        return self._to_subscription(data)

    async def get_subscription(self, external_id: str) -> RemoteSubscription | None:
        data = await self._raw_subscription(external_id)
        return self._to_subscription(data) if data else None

    async def _raw_subscription(self, external_id: str) -> dict | None:
        try:
            _, data = await self.request(
                "GET", f"{self._base()}/subscriptions/{external_id}"
            )
        except BillingNotFoundError:
            return None
        return data if isinstance(data, dict) else None

    async def list_subscriptions(
        self, external_customer_id: str
    ) -> list[RemoteSubscription]:
        _, data = await self.request(
            "GET", f"{self._base()}/subscriptions",
            params={
                "customer": external_customer_id,
                # `all` on purpose: reconciliation after a timeout needs to
                # see an `incomplete` subscription too, and the default filter
                # hides exactly the state a half-finished create leaves behind.
                "status": "all",
                "limit": 20,
            },
        )
        return [
            self._to_subscription(row) for row in ((data or {}).get("data") or [])
        ]

    def _to_subscription(self, data: Any) -> RemoteSubscription:
        if not isinstance(data, dict) or not data.get("id"):
            raise BillingValidationError(
                "Stripe accepted the request but returned no subscription id",
                provider=self.name,
            )

        raw_status = str(data.get("status") or "").lower()
        status = _STATUS_MAP.get(raw_status)
        if status is None:
            # An unmapped status is a Stripe change we have not caught up
            # with. INCOMPLETE is the safe reading: it does not grant service
            # and it does not silently cancel anyone.
            status = SubscriptionStatus.INCOMPLETE

        # `cancel_at_period_end` plus `active` is Stripe's way of saying
        # "winding down". That distinction matters to every UI, so it becomes
        # a state of its own here rather than a boolean callers must remember.
        cancel_at_period_end = bool(data.get("cancel_at_period_end"))
        if cancel_at_period_end and status is SubscriptionStatus.ACTIVE:
            status = SubscriptionStatus.CANCELING

        start, end, price_id, interval = self._period(data)

        return RemoteSubscription(
            external_id=str(data["id"]),
            status=status,
            external_customer_id=_id_of(data.get("customer")),
            external_price_id=price_id,
            interval=interval,
            current_period_start=start,
            current_period_end=end,
            trial_start=_ts(data.get("trial_start")),
            trial_end=_ts(data.get("trial_end")),
            cancel_at_period_end=cancel_at_period_end,
            canceled_at=_ts(data.get("canceled_at")),
            # Stripe has no `updated` on a subscription. `created` is a poor
            # ordering key, so the event's own timestamp is used instead by
            # the webhook layer; this is the fallback for a direct read.
            updated_at=datetime.now(timezone.utc),
        )

    def _period(
        self, data: dict
    ) -> tuple[datetime | None, datetime | None, str | None, BillingInterval]:
        """
        Extract period bounds, price and interval.

        Stripe moved `current_period_start`/`_end` from the subscription root
        onto its items in a 2025 API version. Reading the root first and
        falling back to the first item means the adapter works on both, which
        matters because the API version is set per-account and we do not
        control it.
        """
        items = (data.get("items") or {}).get("data") or []
        first = items[0] if items else {}

        start = _ts(data.get("current_period_start")) or _ts(
            first.get("current_period_start")
        )
        end = _ts(data.get("current_period_end")) or _ts(
            first.get("current_period_end")
        )

        price = first.get("price") or {}
        price_id = _id_of(price) if price else None

        recurring = (price or {}).get("recurring") or {}
        interval = (
            BillingInterval.YEAR
            if str(recurring.get("interval") or "").lower() == "year"
            else BillingInterval.MONTH
        )
        return start, end, price_id, interval

    # -------------------------------------------------------------- invoices ---

    async def list_invoices(
        self, external_customer_id: str, *, limit: int = 20
    ) -> list[RemoteInvoice]:
        _, data = await self.request(
            "GET", f"{self._base()}/invoices",
            params={"customer": external_customer_id, "limit": min(limit, 100)},
        )
        return [self._to_invoice(row) for row in ((data or {}).get("data") or [])]

    async def get_invoice(self, external_id: str) -> RemoteInvoice | None:
        try:
            _, data = await self.request(
                "GET", f"{self._base()}/invoices/{external_id}"
            )
        except BillingNotFoundError:
            return None
        return self._to_invoice(data)

    def _to_invoice(self, data: Any) -> RemoteInvoice:
        if not isinstance(data, dict) or not data.get("id"):
            raise BillingValidationError(
                "Stripe returned an invoice with no id", provider=self.name
            )
        raw_status = str(data.get("status") or "").lower()
        lines = (data.get("lines") or {}).get("data") or []
        period = (lines[0].get("period") if lines else None) or {}

        return RemoteInvoice(
            external_id=str(data["id"]),
            # An unrecognised status becomes DRAFT rather than an invented
            # member: requirement 20 says do not invent invoice status.
            status=_INVOICE_STATUS_MAP.get(raw_status, InvoiceStatus.DRAFT),
            currency=str(data.get("currency") or "usd"),
            amount_due_cents=int(data.get("amount_due") or 0),
            amount_paid_cents=int(data.get("amount_paid") or 0),
            period_start=_ts(period.get("start")) or _ts(data.get("period_start")),
            period_end=_ts(period.get("end")) or _ts(data.get("period_end")),
            hosted_invoice_url=data.get("hosted_invoice_url"),
            external_subscription_id=_id_of(data.get("subscription")),
            external_customer_id=_id_of(data.get("customer")),
        )

    # -------------------------------------------------------------- sessions ---

    async def create_checkout_session(
        self, *, external_customer_id: str, price_id: str, tenant_id: str,
        trial_days: int = 0, idempotency_key: str = "",
    ) -> RemoteSession:
        if not (self.config.success_url and self.config.cancel_url):
            raise BillingConfigurationError(
                "checkout requires success_url and cancel_url", provider=self.name
            )

        form: dict[str, Any] = {
            "mode": "subscription",
            "customer": external_customer_id,
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": 1,
            "success_url": self.config.success_url,
            "cancel_url": self.config.cancel_url,
            # Stamped on both the session and the subscription it creates, so
            # a webhook can be attributed to a tenant even if the local row is
            # not written yet (requirement 23's ordering problem).
            "metadata[voxdesk_tenant_id]": tenant_id,
            "subscription_data[metadata][voxdesk_tenant_id]": tenant_id,
        }
        if trial_days > 0:
            form["subscription_data[trial_period_days]"] = trial_days

        _, data = await self.request(
            "POST", f"{self._base()}/checkout/sessions", form=form,
            idempotency_key=idempotency_key or None,
        )
        return self._to_session(data)

    async def create_portal_session(self, *, external_customer_id: str) -> RemoteSession:
        if not self.config.return_url:
            raise BillingConfigurationError(
                "the customer portal requires a return_url", provider=self.name
            )
        _, data = await self.request(
            "POST", f"{self._base()}/billing_portal/sessions",
            form={
                "customer": external_customer_id,
                "return_url": self.config.return_url,
            },
        )
        return self._to_session(data)

    def _to_session(self, data: Any) -> RemoteSession:
        if not isinstance(data, dict) or not data.get("url"):
            raise BillingValidationError(
                "Stripe returned a session with no URL", provider=self.name
            )
        return RemoteSession(
            session_id=str(data.get("id") or ""), url=str(data["url"])
        )

    # --------------------------------------------------------------- webhook ---

    def verify_webhook(self, *, raw_body: bytes, signature_header: str) -> WebhookEvent:
        """
        Verify a raw body against `Stripe-Signature`.

        Hand-rolled rather than using the SDK, and therefore written to the
        published scheme exactly:

        * signed payload is ``{t}.{raw_body}`` — the **bytes as received**,
          never a re-serialized dict;
        * HMAC-SHA256, hex, keyed on the `whsec_` endpoint secret;
        * **every** `v1` value is checked, because Stripe sends several during
          a signing-secret rotation and accepting only the first would drop
          valid events for the whole rotation window;
        * schemes we do not understand (`v0`) are ignored rather than failing;
        * the timestamp is inside the signed payload and is rejected outside
          the tolerance, which is what stops a captured request being replayed;
        * `compare_digest`, because `==` leaks the signature prefix through
          timing.
        """
        if not self.config.webhook_secret:
            raise BillingConfigurationError(
                "Stripe webhook secret is not configured", provider=self.name
            )
        if not signature_header:
            raise BillingAuthError("missing Stripe-Signature header", provider=self.name)

        timestamp, signatures = _parse_signature_header(signature_header)
        if timestamp is None or not signatures:
            raise BillingAuthError(
                "malformed Stripe-Signature header", provider=self.name
            )

        age = abs(time.time() - timestamp)
        if age > SIGNATURE_TOLERANCE_SECONDS:
            raise BillingAuthError(
                f"Stripe signature timestamp is {int(age)}s old; tolerance is "
                f"{SIGNATURE_TOLERANCE_SECONDS}s",
                provider=self.name,
            )

        signed = b"%d." % timestamp + raw_body
        expected = hmac.new(
            self.config.webhook_secret.encode(), signed, hashlib.sha256
        ).hexdigest()

        if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
            raise BillingAuthError("Stripe signature mismatch", provider=self.name)

        return _to_event(raw_body)

    async def health_check(self) -> HealthResult:
        async def probe():
            # Cheap, read-only, and it proves the one thing that breaks: the
            # key works and is for the right mode.
            await self.request("GET", f"{self._base()}/prices", params={"limit": 1})

        return await self._timed_health_check(probe)


# ------------------------------------------------------------------ helpers ---

def _parse_signature_header(header: str) -> tuple[int | None, list[str]]:
    """
    Split `t=...,v1=...,v1=...,v0=...` into a timestamp and every v1 value.

    Tolerant of whitespace and of unknown schemes, strict about the two things
    that matter: there must be a numeric `t` and at least one `v1`.
    """
    timestamp: int | None = None
    signatures: list[str] = []

    for part in (header or "").split(","):
        key, _, value = part.strip().partition("=")
        key, value = key.strip(), value.strip()
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                return None, []
        elif key == "v1" and value:
            signatures.append(value)
    return timestamp, signatures


def _to_event(raw_body: bytes) -> WebhookEvent:
    """Parse a *verified* body. Only reachable after the signature passed."""
    import json

    try:
        payload = json.loads(raw_body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BillingValidationError(
            "Stripe event body is not valid JSON", provider="stripe"
        ) from exc

    if not isinstance(payload, dict) or not payload.get("id"):
        raise BillingValidationError(
            "Stripe event has no id", provider="stripe"
        )

    return WebhookEvent(
        event_id=str(payload["id"]),
        event_type=str(payload.get("type") or ""),
        created_at=_ts(payload.get("created")),
        data=payload,
    )


def _ts(value: Any) -> datetime | None:
    """Stripe sends Unix seconds. Always returns an aware UTC datetime."""
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _id_of(value: Any) -> str | None:
    """
    Stripe returns a related object as either a bare id or an expanded object,
    depending on the `expand` parameter and the endpoint. Accept both rather
    than depending on which.
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        found = value.get("id")
        return str(found) if found else None
    return None