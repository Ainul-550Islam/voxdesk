"""
Normalized CRM errors.

Every provider fails differently. GoHighLevel returns 422 with a JSON body,
HubSpot returns 400 with a `category` field, Jobber is GraphQL and answers 200
with an `errors` array, and a generic webhook returns whatever the tenant's
server felt like. Retry logic must not have to know any of that.

So each adapter's only job on failure is to raise one of these. The retry
policy then reads exactly one property — `retryable` — and never inspects a
status code or a provider message.

The important distinction is **transient versus permanent**, and getting it
wrong is expensive in both directions:

* Treating a permanent error as transient means five pointless requests, an
  angry rate limiter, and a sync that reports FAILED for twenty minutes before
  admitting it was never going to work.
* Treating a transient error as permanent means a lead silently lost because
  the provider had a bad thirty seconds.

`safe_message()` exists because these strings are shown to tenants and written
to the database. A provider's raw response body is exactly the sort of thing
that echoes a submitted token back at you, so nothing raw ever gets stored.
"""
from __future__ import annotations

import re


class CrmError(Exception):
    """Base for everything the CRM layer raises."""

    #: Whether the retry policy should try again.
    retryable: bool = False
    #: Stable machine-readable class, stored in `CrmSync.last_error_code`.
    code: str = "crm_error"

    def __init__(self, message: str = "", *, provider: str | None = None):
        super().__init__(message or self.__class__.__name__)
        self.provider = provider

    @property
    def safe_message(self) -> str:
        """A message safe to persist and show. See `safe_message()` below."""
        return safe_message(str(self))


# --------------------------------------------------------------- transient ---

class CrmTransientError(CrmError):
    """Worth trying again."""
    retryable = True
    code = "transient"


class CrmTimeout(CrmTransientError):
    code = "timeout"


class CrmConnectionError(CrmTransientError):
    code = "connection"


class CrmServerError(CrmTransientError):
    """Provider returned 5xx. Their problem, and usually a brief one."""
    code = "server_error"

    def __init__(self, message: str = "", *, provider: str | None = None,
                 status: int | None = None):
        super().__init__(message, provider=provider)
        self.status = status


class CrmRateLimited(CrmTransientError):
    """
    429. Carries `retry_after` when the provider told us how long to wait.

    We honour the provider's number over our own backoff when it is larger,
    because arguing with a rate limiter is how a token gets suspended.
    """
    code = "rate_limited"

    def __init__(self, message: str = "", *, provider: str | None = None,
                 retry_after: float | None = None):
        super().__init__(message, provider=provider)
        self.retry_after = retry_after


# --------------------------------------------------------------- permanent ---

class CrmPermanentError(CrmError):
    """Retrying cannot change the outcome."""
    retryable = False
    code = "permanent"


class CrmAuthError(CrmPermanentError):
    """
    401/403. Permanent on purpose.

    A revoked token does not come back on its own, and hammering an auth
    endpoint with a dead credential is how an account gets locked. The tenant
    has to reconnect, so the sync should say so immediately.
    """
    code = "unauthorized"


class CrmValidationError(CrmPermanentError):
    """
    4xx that is not auth and not 429: malformed payload, missing required
    field, bad id. Requirement 7 calls this out specifically — the old code
    got it right and it is preserved here.
    """
    code = "validation"

    def __init__(self, message: str = "", *, provider: str | None = None,
                 status: int | None = None, field: str | None = None):
        super().__init__(message, provider=provider)
        self.status = status
        self.field = field


class CrmNotFound(CrmPermanentError):
    code = "not_found"


class CrmUnsupportedOperation(CrmPermanentError):
    """
    The adapter does not implement this capability.

    Permanent, and not really an error at all — requirement 3 says providers
    must not be forced to implement operations their API lacks. The service
    layer treats it as "nothing to do" rather than a failure to alarm on.
    """
    code = "unsupported"


class CrmConfigurationError(CrmPermanentError):
    """Missing credentials, absent location id, no webhook URL configured."""
    code = "misconfigured"


# ---------------------------------------------------------- classification ---

def classify_status(
    status: int, *, provider: str, body: str = "", retry_after: float | None = None
) -> CrmError:
    """
    Turn an HTTP status into the right exception.

    Shared by every HTTP adapter so the transient/permanent split is decided
    in one place. An adapter overrides only where its provider genuinely
    deviates — Jobber's GraphQL 200-with-errors, for instance.
    """
    excerpt = _excerpt(body)

    if status == 429:
        return CrmRateLimited(
            f"{provider} rate limited the request", provider=provider,
            retry_after=retry_after,
        )
    if status in (401, 403):
        return CrmAuthError(
            f"{provider} rejected the credentials (HTTP {status})", provider=provider
        )
    if status == 404:
        return CrmNotFound(f"{provider} returned 404", provider=provider)
    if 400 <= status < 500:
        return CrmValidationError(
            f"{provider} rejected the payload (HTTP {status}){excerpt}",
            provider=provider, status=status,
        )
    if status >= 500:
        return CrmServerError(
            f"{provider} returned HTTP {status}", provider=provider, status=status
        )
    return CrmError(f"{provider} returned an unexpected HTTP {status}", provider=provider)


def _excerpt(body: str) -> str:
    """
    A short, scrubbed fragment of a provider response.

    Included only for validation errors, where the provider is telling us
    which field we got wrong and that is genuinely the fastest route to a fix.
    Scrubbed first, and never for auth errors — a 401 body is the single most
    likely place for a token to be echoed back.
    """
    if not body:
        return ""
    cleaned = safe_message(body.strip().replace("\n", " "))[:180]
    return f": {cleaned}" if cleaned else ""


# ------------------------------------------------------------- redaction ---

#: Substrings that mark a value as secret regardless of where it appeared.
_SECRET_KEYS = (
    "authorization", "api_key", "apikey", "access_token", "refresh_token",
    "client_secret", "private_key", "signing_secret", "password", "bearer",
    "x-api-key", "token",
)

#: `"access_token": "abc123"` / `api_key=abc123` / `Bearer abc123`
#:
#: The `["']?` after the key name is load-bearing and was missing at first:
#: in a JSON body the key is *quoted*, so the text is `"access_token":"v"` and
#: there is a closing quote between the name and the colon. Without it the
#: scrubber matched shell-style `api_key=v` and missed every real provider
#: response — which is the only case it exists for. A contract test caught it.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(" + "|".join(re.escape(k) for k in _SECRET_KEYS) + r")\b"
    r"([\"']?\s*[:=]\s*|\s+)"
    # Skip an auth scheme word if one is present. Without this,
    # `Authorization: Bearer <token>` redacts the word "Bearer" and leaves the
    # token: the regex is non-overlapping, so consuming "Bearer" as the value
    # means the separate `bearer <token>` rule never gets a turn.
    r"(?:(?:bearer|token)\s+)?"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;}\)]+)"
)

#: Long opaque strings that look like credentials even without a label:
#: GHL `pit-...`, HubSpot `pat-na1-...`, Jobber/Stripe-style prefixes, JWTs.
_TOKEN_SHAPED = re.compile(
    r"(?i)\b("
    r"pit-[A-Za-z0-9\-]{8,}"
    r"|pat-[A-Za-z0-9\-]{8,}"
    # Underscores and hyphens are inside these tokens, not delimiters around
    # them: `sk_live_abc...` truncated to `sk_` + 4 chars under the old
    # `[A-Za-z0-9]` class and fell below the length floor, so it survived.
    r"|sk_[A-Za-z0-9_\-]{8,}"
    r"|xoxb-[A-Za-z0-9\-]{8,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.?[A-Za-z0-9_\-]*"
    r")\b"
)

REDACTED = "[redacted]"


def safe_message(text: str, *, limit: int = 400) -> str:
    """
    Scrub anything credential-shaped out of a string.

    Applied on every path where provider output could reach durable storage:
    `CrmSync.last_error`, `CrmIntegration.last_error`, health-check messages,
    and the structured log helper.

    Two passes, because both failure modes are real. Labelled assignments
    catch `{"access_token": "..."}` echoed from a provider. The shape-based
    pass catches a bare token pasted into a message with no label, which is
    what a misconfigured tenant tends to produce.

    This is defence in depth, not the primary control: the primary control is
    that adapters do not put response bodies into messages in the first place.
    """
    if not text:
        return ""

    scrubbed = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    scrubbed = _TOKEN_SHAPED.sub(REDACTED, scrubbed)

    if len(scrubbed) > limit:
        scrubbed = scrubbed[: limit - 1].rstrip() + "…"
    return scrubbed
