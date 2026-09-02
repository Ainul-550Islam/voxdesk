"""
Calendar provider registry.

One dict, and a rule the contract tests enforce: every member of
`CalendarProviderType` has an entry, and every entry's `name` equals the enum
value. Adding a provider is a class plus a line here.

Maps to *classes*, never instances. An adapter holds one tenant's decrypted
OAuth tokens, so a process-wide instance would be a cross-tenant credential
leak waiting for a concurrency bug. `build()` constructs a fresh adapter per
operation and it is discarded afterwards.
"""
from __future__ import annotations

from app.db.models import CalendarProviderType
from app.integrations.calendar.base import (
    CalendarCapability,
    CalendarContext,
    CalendarProvider,
)
from app.integrations.calendar.providers import (
    CalComProvider,
    GoogleCalendarProvider,
    GoogleServiceAccountProvider,
    InternalCalendarProvider,
    MicrosoftCalendarProvider,
)

PROVIDERS: dict[CalendarProviderType, type[CalendarProvider]] = {
    CalendarProviderType.GOOGLE: GoogleCalendarProvider,
    CalendarProviderType.GOOGLE_SERVICE_ACCOUNT: GoogleServiceAccountProvider,
    CalendarProviderType.MICROSOFT: MicrosoftCalendarProvider,
    CalendarProviderType.CALCOM: CalComProvider,
    CalendarProviderType.INTERNAL: InternalCalendarProvider,
}


class UnknownCalendarProvider(KeyError):
    """No adapter is registered for that provider."""


def provider_class(provider: CalendarProviderType) -> type[CalendarProvider]:
    try:
        return PROVIDERS[provider]
    except KeyError as exc:
        raise UnknownCalendarProvider(
            f"no calendar adapter registered for {provider}"
        ) from exc


def build(provider: CalendarProviderType, context: CalendarContext) -> CalendarProvider:
    """Construct an adapter for one operation on one tenant."""
    return provider_class(provider)(context)


def capabilities_of(provider: CalendarProviderType) -> frozenset[CalendarCapability]:
    """
    What a provider can do, without constructing it.

    Served by the API so a dashboard can grey out "reschedule" for a
    service-account tenant instead of offering it and failing.
    """
    return provider_class(provider).capabilities


def supports(provider: CalendarProviderType, capability: CalendarCapability) -> bool:
    return capability in capabilities_of(provider)

