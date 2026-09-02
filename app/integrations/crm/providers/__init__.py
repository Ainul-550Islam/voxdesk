"""Provider adapters. Registered in `app.integrations.crm.registry`."""
from app.integrations.crm.providers.ghl import GoHighLevelProvider
from app.integrations.crm.providers.hubspot import HubSpotProvider
from app.integrations.crm.providers.jobber import JobberProvider
from app.integrations.crm.providers.webhook import WebhookProvider

__all__ = [
    "GoHighLevelProvider",
    "HubSpotProvider",
    "JobberProvider",
    "WebhookProvider",
]

