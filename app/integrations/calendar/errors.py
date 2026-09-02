"""
Normalized calendar errors.

Same discipline as `app/integrations/crm/errors.py`, and deliberately the same
shape: business logic reads one property, `retryable`, and never inspects a
status code or a vendor exception name.

The distinction that costs money here is different from the CRM one. A CRM
sync failing is invisible to the customer and can be retried for twenty
minutes. A *booking* failing is happening while someone is on the phone, so
the layer must decide in seconds and must never resolve ambiguity by guessing.
`CalendarConflictError` in particular is a first-class outcome rather than an
error condition: it means another booking won the slot, which is normal.
"""
from __future__ import annotations

import re

#: Reused wholesale from the CRM layer. There is exactly one secret scrubber in
#: this codebase, and calendar OAuth tokens are the same shape as CRM tokens.
from app.integrations.crm.errors import safe_message  # noqa: F401


class CalendarError(Exception):
    """Base for everything the calendar layer raises."""

    retryable: bool = False
    code: str = "calendar_error"

    def __init__(self, message: str = "", *, provider: str | None = None):
        super().__init__(message or self.__class__.__name__)
        self.provider = provider

    @property
    def safe_message(self) -> str:
        return safe_message(str(self))


# --------------------------------------------------------------- transient ---

class CalendarTemporaryError(CalendarError):
    """Provider 5xx, connection reset, or an unclassified blip."""
    retryable = True
    code = "temporary"


class CalendarTimeout(CalendarTemporaryError):
    """
    The request exceeded its deadline.

    Retryable, but **only through an idempotent path**. A booking timeout is
    the ambiguous case the brief calls out: the provider may have accepted.
    `service.book()` therefore reconciles before retrying rather than
    re-POSTing.
    """
    code = "timeout"


class CalendarRateLimitError(CalendarTemporaryError):
    code = "rate_limited"

    def __init__(self, message: str = "", *, provider: str | None = None,
                 retry_after: float | None = None):
        super().__init__(message, provider=provider)
        self.retry_after = retry_after


# --------------------------------------------------------------- permanent ---

class CalendarPermanentError(CalendarError):
    retryable = False
    code = "permanent"


class CalendarAuthError(CalendarPermanentError):
    """
    401, or a revoked refresh token.

    Permanent by design. Requirement 26 forbids retrying revoked auth, and a
    revoked Google grant does not come back on its own — the tenant has to
    reconnect. Retrying just burns quota against a dead credential.
    """
    code = "unauthorized"


class CalendarPermissionError(CalendarPermanentError):
    """403 — authenticated, but not allowed to touch this calendar."""
    code = "forbidden"


class CalendarNotFoundError(CalendarPermanentError):
    """404 — the calendar or the event is gone."""
    code = "not_found"


class CalendarConflictError(CalendarPermanentError):
    """
    409, or the provider reporting the slot is taken.

    Not really a failure: it is the correct answer when two callers race. The
    service turns it into a `CONFLICT` result and the agent offers another
    time, which is why it must never be retried.
    """
    code = "conflict"


class CalendarValidationError(CalendarPermanentError):
    """4xx that is not auth, 404, 409 or 429: malformed request, bad slot."""
    code = "validation"


class CalendarUnsupportedError(CalendarPermanentError):
    """
    The adapter does not implement this capability.

    Requirement 12: if a capability is unavailable, return a clear internal
    unsupported error and let the caller present a safe fallback. Not an
    outage, so the service records it without alarming.
    """
    code = "unsupported"


class CalendarConfigurationError(CalendarPermanentError):
    """No credentials, no calendar id, no event type configured."""
    code = "misconfigured"


# ---------------------------------------------------------- classification ---

def classify_status(
    status: int, *, provider: str, body: str = "", retry_after: float | None = None
) -> CalendarError:
    """
    HTTP status to normalized error, in one place for every adapter.

    An adapter overrides only where its provider genuinely deviates — Google
    answering 403 with `rateLimitExceeded`, for instance, which is a throttle
    wearing a permission error's clothes.
    """
    excerpt = _excerpt(body)

    if status == 429:
        return CalendarRateLimitError(
            f"{provider} rate limited the request", provider=provider,
            retry_after=retry_after,
        )
    if status == 401:
        return CalendarAuthError(
            f"{provider} rejected the credentials (HTTP 401)", provider=provider
        )
    if status == 403:
        return CalendarPermissionError(
            f"{provider} refused access to that calendar (HTTP 403)", provider=provider
        )
    if status == 404:
        return CalendarNotFoundError(
            f"{provider} could not find that calendar or event", provider=provider
        )
    if status == 409:
        return CalendarConflictError(
            f"{provider} reports a conflict for that slot", provider=provider
        )
    if status == 410:
        # Google uses 410 Gone for a stale sync token or a deleted event.
        return CalendarNotFoundError(
            f"{provider} reports that resource is gone", provider=provider
        )
    if 400 <= status < 500:
        return CalendarValidationError(
            f"{provider} rejected the request (HTTP {status}){excerpt}",
            provider=provider,
        )
    if status >= 500:
        return CalendarTemporaryError(
            f"{provider} returned HTTP {status}", provider=provider
        )
    return CalendarError(
        f"{provider} returned an unexpected HTTP {status}", provider=provider
    )


#: Google and Microsoft both signal throttling inside a 403 body rather than
#: with a 429. Treating those as permission errors would strand a tenant whose
#: only problem was going too fast.
_THROTTLE_REASONS = re.compile(
    r"(?i)(rateLimitExceeded|userRateLimitExceeded|quotaExceeded|"
    r"ActivityLimitReached|TooManyRequests|throttl)"
)


def refine_403(error: CalendarError, body: str, *, provider: str) -> CalendarError:
    """
    Re-classify a 403 that is actually a throttle.

    Called by adapters after `classify_status`. Kept separate so the mapping
    table above stays a plain reading of the HTTP spec and the vendor
    weirdness is visibly a vendor workaround.
    """
    if isinstance(error, CalendarPermissionError) and _THROTTLE_REASONS.search(body or ""):
        return CalendarRateLimitError(
            f"{provider} is throttling (reported as HTTP 403)", provider=provider
        )
    return error


def _excerpt(body: str) -> str:
    """
    A short scrubbed fragment, for validation errors only.

    Never for auth errors: a 401 body is the most likely place for a token to
    be reflected back, so those carry no excerpt at all.
    """
    if not body:
        return ""
    cleaned = safe_message(body.strip().replace("\n", " "))[:180]
    return f": {cleaned}" if cleaned else ""
