"""Provider error taxonomy for the live voice layer.

The voice pipeline talks to three families of external systems — STT
(Deepgram), TTS (ElevenLabs) and LLMs (OpenAI / Anthropic / Google). Each can
fail in a small, well-understood set of ways, and the rest of the system needs
to answer three questions about any failure:

* is it *safe to show / log* (never a secret)?
* is it *retryable* (transient) or permanent?
* what *category* does it fall into, so state handling, metrics and logs agree?

This mirrors the taxonomy already used by the CRM adapters
(``app/integrations/crm/errors.py``) — one small hierarchy, each class carrying
a ``retryable`` flag and a ``category`` label — but is deliberately independent
of it: CRM errors wrap HTTP responses, while these wrap *live provider
behaviour* before and during a call.

Every message is constructed by the raising code from fixed, safe text (no
provider payloads, no keys, no customer data). ``ProviderError`` subclasses
``RuntimeError`` so that existing callers which treat provider failures as
ordinary runtime failures keep working unchanged.
"""
from __future__ import annotations

from typing import Any

#: Bounded set of categories. Metrics and dashboards group by these, so the
#: set is deliberately closed — a new category is a deliberate change.
CATEGORIES = (
    "configuration_error",
    "authentication_error",
    "authorization_error",
    "rate_limit",
    "timeout",
    "unavailable",
    "invalid_request",
    "unsupported_feature",
    "provider_error",
)


class ProviderError(RuntimeError):
    """Base class for every voice-provider failure.

    ``category`` is one of :data:`CATEGORIES`. ``retryable`` is True only for
    transient failures where a bounded retry is safe (never for invalid
    credentials, invalid requests or unsupported features).
    """

    category: str = "provider_error"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        provider: str = "unknown",
        category: str | None = None,
        retryable: bool | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.detail = detail
        if category is not None:
            self.category = category
        if retryable is not None:
            self.retryable = retryable

    @property
    def safe_message(self) -> str:
        """The message, guaranteed by construction to be safe to log/show."""
        return self.message

    def __str__(self) -> str:
        return f"{self.provider}/{self.category}: {self.message}"


class ProviderConfigurationError(ProviderError):
    """The provider is not configured (missing key, empty URL, bad setting).

    Permanent by definition: retrying without a config change cannot help.
    """

    category = "configuration_error"
    retryable = False


class ProviderAuthenticationError(ProviderError):
    """The provider rejected our credentials (401 / bad key)."""

    category = "authentication_error"
    retryable = False


class ProviderAuthorizationError(ProviderError):
    """The credentials are valid but lack permission for the request (403)."""

    category = "authorization_error"
    retryable = False


class ProviderRateLimitedError(ProviderError):
    """The provider rate-limited us (429). Carries an optional retry delay."""

    category = "rate_limit"
    retryable = True

    def __init__(
        self,
        message: str = "Provider rate limited",
        *,
        provider: str = "unknown",
        retry_after: float | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message, provider=provider, detail=detail)
        self.retry_after = retry_after


class ProviderTimeoutError(ProviderError):
    """A provider call exceeded its bounded time budget."""

    category = "timeout"
    retryable = True


class ProviderUnavailableError(ProviderError):
    """The provider is unreachable or returned 5xx. Transient."""

    category = "unavailable"
    retryable = True


class ProviderInvalidRequestError(ProviderError):
    """We sent something the provider rejected as malformed. Permanent."""

    category = "invalid_request"
    retryable = False


class UnsupportedProviderFeatureError(ProviderError):
    """We asked for something the provider (or our SDK) cannot express.

    Raised instead of silently ignoring an unsupported setting — an
    unsupported parameter must fail clearly, never disappear.
    """

    category = "unsupported_feature"
    retryable = False


class ProviderRuntimeError(ProviderError):
    """Any other provider failure with no more specific category."""

    category = "provider_error"
    retryable = False
