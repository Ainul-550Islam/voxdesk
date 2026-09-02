"""
Billing provider registry.

One dict, and a rule the contract tests enforce: every member of
`BillingProviderType` has an entry, and every entry's `name` equals the enum
value.

Maps to *classes*, never instances. An adapter holds the Stripe secret key, so
a process-wide instance would be a shared mutable holder of a credential;
`build()` constructs one per operation and discards it.
"""
from __future__ import annotations

from app.billing.base import BillingCapability, BillingContextConfig, BillingProvider
from app.billing.providers import ManualBillingProvider, StripeProvider
from app.db.models import BillingProviderType

PROVIDERS: dict[BillingProviderType, type[BillingProvider]] = {
    BillingProviderType.STRIPE: StripeProvider,
    BillingProviderType.MANUAL: ManualBillingProvider,
}


class UnknownBillingProvider(KeyError):
    """No adapter is registered for that provider."""


def provider_class(provider: BillingProviderType) -> type[BillingProvider]:
    try:
        return PROVIDERS[provider]
    except KeyError as exc:
        raise UnknownBillingProvider(
            f"no billing adapter registered for {provider}"
        ) from exc


def build(
    provider: BillingProviderType, config: BillingContextConfig
) -> BillingProvider:
    return provider_class(provider)(config)


def capabilities_of(provider: BillingProviderType) -> frozenset[BillingCapability]:
    return provider_class(provider).capabilities


def supports(provider: BillingProviderType, capability: BillingCapability) -> bool:
    return capability in capabilities_of(provider)


def configured_provider() -> BillingProviderType:
    """
    Which provider this instance uses.

    Falls back to MANUAL when no Stripe key is configured, so a development
    instance has a working billing layer rather than a broken one. Production
    is protected separately: `Settings.validate_security()` refuses to boot
    with `BILLING_PROVIDER=stripe` and no key.
    """
    from app.core.config import settings

    try:
        return BillingProviderType(settings.billing_provider.strip().lower())
    except ValueError:
        return BillingProviderType.MANUAL


def config_from_settings() -> BillingContextConfig:
    from app.core.config import settings

    return BillingContextConfig(
        secret_key=settings.stripe_secret_key,
        webhook_secret=settings.stripe_webhook_secret,
        timeout_seconds=settings.billing_request_timeout_seconds,
        success_url=settings.billing_checkout_success_url,
        cancel_url=settings.billing_checkout_cancel_url,
        return_url=settings.billing_portal_return_url,
    )
