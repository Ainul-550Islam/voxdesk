"""
Provider registry.

One dict, and a rule enforced by a contract test: every member of
`CrmProviderType` has an entry, and every entry's `name` equals the enum
value. Adding Salesforce is a class plus a line here.

The registry deliberately maps to *classes*, not instances. An adapter holds
a `ProviderContext` containing one tenant's decrypted credentials, so a
process-wide instance would be a cross-tenant credential leak waiting for a
concurrency bug. `build()` constructs a fresh adapter per operation and it is
discarded afterwards.
"""
from __future__ import annotations

from app.db.models import CrmProviderType
from app.integrations.crm.base import Capability, CrmProvider, ProviderContext
from app.integrations.crm.providers import (
    GoHighLevelProvider,
    HubSpotProvider,
    JobberProvider,
    WebhookProvider,
)

PROVIDERS: dict[CrmProviderType, type[CrmProvider]] = {
    CrmProviderType.GOHIGHLEVEL: GoHighLevelProvider,
    CrmProviderType.HUBSPOT: HubSpotProvider,
    CrmProviderType.JOBBER: JobberProvider,
    CrmProviderType.WEBHOOK: WebhookProvider,
}


class UnknownProvider(KeyError):
    """No adapter is registered for that provider."""


def provider_class(provider: CrmProviderType) -> type[CrmProvider]:
    try:
        return PROVIDERS[provider]
    except KeyError as exc:
        raise UnknownProvider(f"no adapter registered for {provider}") from exc


def build(provider: CrmProviderType, context: ProviderContext) -> CrmProvider:
    """Construct an adapter for one operation on one tenant."""
    return provider_class(provider)(context)


def capabilities_of(provider: CrmProviderType) -> frozenset[Capability]:
    """
    What a provider can do, without constructing it.

    Used by the integrations API so a dashboard can grey out an option rather
    than offering it and failing.
    """
    return provider_class(provider).capabilities


def supports(provider: CrmProviderType, capability: Capability) -> bool:
    return capability in capabilities_of(provider)
