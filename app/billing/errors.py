"""
Normalized billing errors.

Same discipline as the CRM and calendar layers: business logic reads one
property, `retryable`, and never inspects a status code or a vendor exception
name.

What is different here is the cost of getting the transient/permanent split
wrong. A CRM sync retried too eagerly wastes quota. A *billing* write retried
without idempotency creates a second subscription and charges a customer
twice, and the customer finds out before we do. So every retryable error in
this module is only retryable through a path that carries an idempotency key.
"""
from __future__ import annotations

#: One scrubber for the whole codebase. Stripe keys (`sk_live_...`,
#: `whsec_...`) are the same shape as the tokens it already redacts.
from app.integrations.crm.errors import safe_message  # noqa: F401


class BillingError(Exception):
    """Base for everything the billing layer raises."""

    retryable: bool = False
    code: str = "billing_error"

    def __init__(self, message: str = "", *, provider: str | None = None):
        super().__init__(message or self.__class__.__name__)
        self.provider = provider

    @property
    def safe_message(self) -> str:
        return safe_message(str(self))


# --------------------------------------------------------------- transient ---

class BillingTemporaryError(BillingError):
    """Provider 5xx, connection reset, or an unclassified blip."""
    retryable = True
    code = "temporary"


class BillingTimeout(BillingTemporaryError):
    """
    The request exceeded its deadline.

    **The dangerous one.** A timeout on `create_subscription` means the
    provider may have created it. Retrying blindly is how a tenant ends up
    with two subscriptions and two charges, so `service.py` reconciles by
    listing the provider's subscriptions for that customer before it retries.
    """
    code = "timeout"


class BillingRateLimited(BillingTemporaryError):
    code = "rate_limited"

    def __init__(self, message: str = "", *, provider: str | None = None,
                 retry_after: float | None = None):
        super().__init__(message, provider=provider)
        self.retry_after = retry_after


# --------------------------------------------------------------- permanent ---

class BillingPermanentError(BillingError):
    retryable = False
    code = "permanent"


class BillingAuthError(BillingPermanentError):
    """
    401, or a revoked API key.

    Permanent: a rotated Stripe key does not come back on its own, and
    hammering the API with a dead one gets an account flagged.
    """
    code = "unauthorized"


class BillingValidationError(BillingPermanentError):
    """4xx that is not auth or rate limiting: bad price, bad customer id."""
    code = "validation"


class BillingNotFoundError(BillingPermanentError):
    code = "not_found"


class BillingCardError(BillingPermanentError):
    """
    The card was declined.

    Permanent for *this attempt* -- retrying the same card in ten seconds
    changes nothing. The provider's own dunning schedule handles the retry,
    which is why this must not be retried locally.
    """
    code = "card_declined"


class BillingUnsupportedError(BillingPermanentError):
    """The adapter does not implement this capability."""
    code = "unsupported"


class BillingConfigurationError(BillingPermanentError):
    """No API key, no webhook secret, no price id for an active plan."""
    code = "misconfigured"


# ------------------------------------------------------- domain (not HTTP) ---

class PlanNotFound(BillingPermanentError):
    """No such plan code, or the plan is inactive."""
    code = "plan_not_found"


class EntitlementDenied(BillingPermanentError):
    """
    The tenant's plan does not permit this action.

    Carries the machine-readable detail a caller needs to explain itself
    without re-deriving it: which metric, what the limit was, where they are.
    """
    code = "entitlement_denied"

    def __init__(
        self, message: str = "", *, feature: str = "", limit: int | None = None,
        used: int | None = None,
    ):
        super().__init__(message)
        self.feature = feature
        self.limit = limit
        self.used = used


class UsageConflict(BillingPermanentError):
    """
    A usage event collided with an existing one.

    Not an error in normal operation -- it is idempotency working. Raised only
    where a caller genuinely needs to distinguish "recorded" from "already
    recorded"; `metering.record_usage` returns rather than raising.
    """
    code = "usage_conflict"


# ---------------------------------------------------------- classification ---

def classify_status(
    status: int, *, provider: str, body: str = "", retry_after: float | None = None
) -> BillingError:
    """HTTP status to normalized error, in one place for every adapter."""
    excerpt = _excerpt(body)

    if status == 429:
        return BillingRateLimited(
            f"{provider} rate limited the request", provider=provider,
            retry_after=retry_after,
        )
    if status in (401, 403):
        return BillingAuthError(
            f"{provider} rejected the API credentials (HTTP {status})",
            provider=provider,
        )
    if status == 404:
        return BillingNotFoundError(
            f"{provider} could not find that object", provider=provider
        )
    if status == 402:
        # Stripe's "Request Failed" -- the parameters were valid but the
        # operation failed, which for us is almost always a declined card.
        return BillingCardError(
            f"{provider} could not complete the payment{excerpt}", provider=provider
        )
    if 400 <= status < 500:
        return BillingValidationError(
            f"{provider} rejected the request (HTTP {status}){excerpt}",
            provider=provider,
        )
    if status >= 500:
        return BillingTemporaryError(
            f"{provider} returned HTTP {status}", provider=provider
        )
    return BillingError(
        f"{provider} returned an unexpected HTTP {status}", provider=provider
    )


def _excerpt(body: str) -> str:
    """
    A short scrubbed fragment, for validation and card errors only.

    Never for auth errors: a 401 body is the most likely place for an API key
    to be reflected back.
    """
    if not body:
        return ""
    cleaned = safe_message(body.strip().replace("\n", " "))[:180]
    return f": {cleaned}" if cleaned else ""
