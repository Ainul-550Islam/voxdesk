"""Real-provider checks that reuse the existing integration adapters.

Calendar, CRM and billing already ship a per-provider, read-only
``health_check()`` — with a ``HEALTH_CHECK`` capability, normalized error
taxonomies and secret-safe ``safe_message`` strings. These checks construct
one adapter from environment variables (the same shape a tenant's stored,
decrypted connection would produce) and call that existing probe. Nothing
here invents a new provider call.

The tenant-scoped providers have no global setting — their credentials are
stored per tenant, encrypted, in the database — so the validation harness
reads them from dedicated ``VOXDESK_REAL_*`` environment variables. Absent
variables mean SKIPPED, never a fake PASS.

Two adapters (Google and Microsoft calendar) need an access token but are
authorized by a refresh token; when only the refresh half is supplied the
check performs the same read-only token refresh the calendar service does,
then re-runs the probe with the fresh token.
"""
from __future__ import annotations

import time

from app.core.config import settings
from app.integrations.validation.status import CheckOutcome, CheckStatus, env_or


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


async def _calendar_check(name, provider_type, required, credentials, config) -> CheckOutcome:
    """Run one calendar adapter's existing ``health_check``.

    ``required`` maps credential keys to the environment variables that would
    provide them, so the SKIP reason names variables, never values.
    """
    missing = [
        env for key, env in required.items() if not (credentials.get(key) or "").strip()
    ]
    if missing:
        return CheckOutcome(
            name, CheckStatus.SKIPPED,
            f"not configured (missing: {', '.join(missing)})",
        )

    from app.integrations.calendar.base import CalendarContext
    from app.integrations.calendar.errors import CalendarError
    from app.integrations.calendar.registry import build as calendar_build

    def _build(creds):
        return calendar_build(
            provider_type,
            CalendarContext(
                tenant_id="validation", credentials=creds, config=config,
                timezone="UTC", timeout_seconds=10.0,
            ),
        )

    started = time.perf_counter()
    try:
        adapter = _build(credentials)
        if not credentials.get("access_token") and credentials.get("refresh_token"):
            refresh = getattr(adapter, "refresh_access_token", None)
            if refresh is not None:
                refreshed = await refresh()
                adapter = _build({**credentials, **refreshed})
        result = await adapter.health_check()
    except CalendarError as exc:
        return CheckOutcome(name, CheckStatus.FAIL, exc.safe_message,
                            latency_ms=_ms(started))
    except Exception as exc:
        return CheckOutcome(name, CheckStatus.FAIL,
                            f"unexpected error ({type(exc).__name__})",
                            latency_ms=_ms(started))
    if getattr(result, "connected", False):
        return CheckOutcome(name, CheckStatus.PASS,
                            result.safe_message or "connected",
                            latency_ms=getattr(result, "latency_ms", None))
    return CheckOutcome(name, CheckStatus.FAIL,
                        result.safe_message or "provider reported not connected",
                        latency_ms=getattr(result, "latency_ms", None))


async def _crm_check(name, provider_type, cred_required, config_required,
                     credentials, config) -> CheckOutcome:
    """Run one CRM adapter's existing ``health_check``."""
    missing = [
        env for key, env in cred_required.items()
        if not (credentials.get(key) or "").strip()
    ] + [
        env for key, env in config_required.items()
        if not (config.get(key) or "").strip()
    ]
    if missing:
        return CheckOutcome(
            name, CheckStatus.SKIPPED,
            f"not configured (missing: {', '.join(missing)})",
        )

    from app.integrations.crm.base import ProviderContext
    from app.integrations.crm.errors import CrmError
    from app.integrations.crm.registry import build as crm_build

    started = time.perf_counter()
    try:
        adapter = crm_build(
            provider_type,
            ProviderContext(
                tenant_id="validation", credentials=credentials, config=config,
                field_mappings={}, timeout_seconds=10.0,
            ),
        )
        result = await adapter.health_check()
    except CrmError as exc:
        return CheckOutcome(name, CheckStatus.FAIL, exc.safe_message,
                            latency_ms=_ms(started))
    except Exception as exc:
        return CheckOutcome(name, CheckStatus.FAIL,
                            f"unexpected error ({type(exc).__name__})",
                            latency_ms=_ms(started))
    if getattr(result, "connected", False):
        return CheckOutcome(name, CheckStatus.PASS,
                            result.safe_message or "connected",
                            latency_ms=getattr(result, "latency_ms", None))
    return CheckOutcome(name, CheckStatus.FAIL,
                        result.safe_message or "provider reported not connected",
                        latency_ms=getattr(result, "latency_ms", None))


# --------------------------------------------------------------- calendar ---

async def check_google_calendar() -> CheckOutcome:
    from app.db.models import CalendarProviderType

    return await _calendar_check(
        "Google Calendar", CalendarProviderType.GOOGLE,
        required={
            "refresh_token": "VOXDESK_REAL_GOOGLE_CALENDAR_REFRESH_TOKEN",
            "client_id": "VOXDESK_REAL_GOOGLE_CALENDAR_CLIENT_ID",
            "client_secret": "VOXDESK_REAL_GOOGLE_CALENDAR_CLIENT_SECRET",
        },
        credentials={
            "refresh_token": env_or("VOXDESK_REAL_GOOGLE_CALENDAR_REFRESH_TOKEN"),
            "client_id": env_or("VOXDESK_REAL_GOOGLE_CALENDAR_CLIENT_ID"),
            "client_secret": env_or("VOXDESK_REAL_GOOGLE_CALENDAR_CLIENT_SECRET"),
            "access_token": env_or("VOXDESK_REAL_GOOGLE_CALENDAR_ACCESS_TOKEN"),
        },
        config={"calendar_id": env_or(
            "VOXDESK_REAL_GOOGLE_CALENDAR_CALENDAR_ID", "primary")},
    )


async def check_microsoft_calendar() -> CheckOutcome:
    from app.db.models import CalendarProviderType

    return await _calendar_check(
        "Microsoft Calendar", CalendarProviderType.MICROSOFT,
        required={
            "refresh_token": "VOXDESK_REAL_MICROSOFT_REFRESH_TOKEN",
            "client_id": "VOXDESK_REAL_MICROSOFT_CLIENT_ID",
            "client_secret": "VOXDESK_REAL_MICROSOFT_CLIENT_SECRET",
        },
        credentials={
            "refresh_token": env_or("VOXDESK_REAL_MICROSOFT_REFRESH_TOKEN"),
            "client_id": env_or("VOXDESK_REAL_MICROSOFT_CLIENT_ID"),
            "client_secret": env_or("VOXDESK_REAL_MICROSOFT_CLIENT_SECRET"),
            "access_token": env_or("VOXDESK_REAL_MICROSOFT_ACCESS_TOKEN"),
        },
        config={"calendar_id": env_or("VOXDESK_REAL_MICROSOFT_CALENDAR_ID")},
    )


async def check_calcom() -> CheckOutcome:
    from app.db.models import CalendarProviderType

    return await _calendar_check(
        "Cal.com", CalendarProviderType.CALCOM,
        required={"api_key": "VOXDESK_REAL_CALCOM_API_KEY"},
        credentials={"api_key": env_or("VOXDESK_REAL_CALCOM_API_KEY")},
        config={"base_url": env_or("VOXDESK_REAL_CALCOM_BASE_URL")},
    )


# --------------------------------------------------------------------- crm ---

async def check_hubspot() -> CheckOutcome:
    from app.db.models import CrmProviderType

    return await _crm_check(
        "HubSpot", CrmProviderType.HUBSPOT,
        cred_required={"access_token": "VOXDESK_REAL_HUBSPOT_TOKEN"},
        config_required={},
        credentials={"access_token": env_or("VOXDESK_REAL_HUBSPOT_TOKEN")},
        config={},
    )


async def check_gohighlevel() -> CheckOutcome:
    from app.db.models import CrmProviderType

    return await _crm_check(
        "GoHighLevel", CrmProviderType.GOHIGHLEVEL,
        cred_required={"access_token": "VOXDESK_REAL_GHL_ACCESS_TOKEN"},
        config_required={"location_id": "VOXDESK_REAL_GHL_LOCATION_ID"},
        credentials={"access_token": env_or("VOXDESK_REAL_GHL_ACCESS_TOKEN")},
        config={"location_id": env_or("VOXDESK_REAL_GHL_LOCATION_ID")},
    )


async def check_jobber() -> CheckOutcome:
    from app.db.models import CrmProviderType

    return await _crm_check(
        "Jobber", CrmProviderType.JOBBER,
        cred_required={"access_token": "VOXDESK_REAL_JOBBER_ACCESS_TOKEN"},
        config_required={},
        credentials={"access_token": env_or("VOXDESK_REAL_JOBBER_ACCESS_TOKEN")},
        config={},
    )


# ------------------------------------------------------------------ stripe ---

async def check_stripe() -> CheckOutcome:
    """Verify the Stripe key with a read-only request. Never a charge.

    The only call is the adapter's existing health check (``GET /prices``).
    A live-mode key is validated exactly the same read-only way; no mutation
    path exists in this check, so production-mode writes are impossible by
    construction.
    """
    key = (settings.stripe_secret_key or "").strip()
    if not key:
        return CheckOutcome(
            "Stripe", CheckStatus.SKIPPED, "STRIPE_SECRET_KEY not configured"
        )
    webhook = (settings.stripe_webhook_secret or "").strip()
    if webhook and not webhook.startswith("whsec_"):
        return CheckOutcome(
            "Stripe", CheckStatus.FAIL,
            "STRIPE_WEBHOOK_SECRET is malformed (must start with whsec_)",
        )
    if key.startswith("sk_test_"):
        mode = "test"
    elif key.startswith("sk_live_"):
        mode = "live"
    else:
        mode = "unknown"

    from app.billing.base import BillingContextConfig
    from app.billing.errors import BillingError
    from app.billing.providers.stripe import StripeProvider

    started = time.perf_counter()
    try:
        adapter = StripeProvider(
            BillingContextConfig(
                secret_key=key, webhook_secret=webhook, timeout_seconds=15.0
            )
        )
        result = await adapter.health_check()
    except BillingError as exc:
        return CheckOutcome("Stripe", CheckStatus.FAIL, exc.safe_message,
                            latency_ms=_ms(started))
    except Exception as exc:
        return CheckOutcome("Stripe", CheckStatus.FAIL,
                            f"unexpected error ({type(exc).__name__})",
                            latency_ms=_ms(started))
    detail = f"read-only validation ({mode} key); no charge or subscription created"
    if getattr(result, "connected", False):
        return CheckOutcome("Stripe", CheckStatus.PASS, detail,
                            latency_ms=getattr(result, "latency_ms", None))
    return CheckOutcome("Stripe", CheckStatus.FAIL,
                        result.safe_message or "provider reported not connected",
                        latency_ms=getattr(result, "latency_ms", None))
