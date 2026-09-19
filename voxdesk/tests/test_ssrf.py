"""SSRF guard (Step 9) — deterministic tests, no network.

The outbound-URL validator must reject every destination class the brief
names: localhost, loopback, RFC 1918 private ranges, link-local, the cloud
metadata endpoint, internal/special-use DNS names, IPv6 local addresses, and
non-http(s) schemes — while still accepting the official provider hosts and
the ``*.test`` stubs the rest of the suite uses.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.ssrf import (
    OutboundUrlError,
    is_safe_outbound_url,
    validate_outbound_url,
)
from app.db.models import CalendarProviderType, CrmProviderType
from app.integrations.calendar.providers.google import GoogleCalendarProvider
from app.integrations.crm.providers.ghl import GoHighLevelProvider


# ------------------------------------------------------------------ validator ---


@pytest.mark.parametrize(
    "url",
    [
        "https://api.hubapi.com/crm/v3/objects/contacts",
        "https://services.leadconnectorhq.com/contacts/",
        "https://api.getjobber.com/api/graphql",
        "https://www.googleapis.com/calendar/v3",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "https://api.cal.com/v2",
        "https://ghl.test",
        "https://hubspot.test",
        "https://jobber.test/graphql",
        "https://gcal.test/v3",
        "https://oauth.test/token",
        "https://example.com:8443/join/room?token=abc",
    ],
)
def test_public_https_urls_are_accepted(url):
    validate_outbound_url(url, require_https=True)
    assert is_safe_outbound_url(url, require_https=True) is True


@pytest.mark.parametrize(
    "url",
    [
        "ftp://files.example.com/x",
        "file:///etc/passwd",
        "gopher://example.com",
        "javascript:alert(1)",
        "vbscript:msgbox(1)",
        "data:text/html,<script>alert(1)</script>",
    ],
)
def test_non_http_schemes_are_rejected(url):
    with pytest.raises(OutboundUrlError):
        validate_outbound_url(url)


def test_userinfo_is_rejected():
    with pytest.raises(OutboundUrlError):
        validate_outbound_url("https://admin:secret@example.com/x")


def test_empty_and_malformed_urls_are_rejected():
    for url in ("", "   ", "https:///path", "not a url", "https://"):
        with pytest.raises(OutboundUrlError):
            validate_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://127.0.0.1:8000/health",
        "http://0.0.0.0",
        "http://[::1]",
        "http://localhost",
        "https://localhost",
        "http://localhost:5432",
        "http://sub.localhost",
    ],
)
def test_loopback_is_rejected(url):
    with pytest.raises(OutboundUrlError):
        validate_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1",
        "http://172.16.0.1",
        "http://192.168.1.1",
        "http://[::ffff:192.168.1.1]",  # IPv4-mapped IPv6
        "http://[fc00::1]",  # RFC 4193 unique-local
        "http://[fec0::1]",  # site-local (deprecated but reserved)
    ],
)
def test_private_addresses_are_rejected(url):
    with pytest.raises(OutboundUrlError):
        validate_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (AWS/GCP/Azure)
        "http://[fe80::1]",  # link-local IPv6
    ],
)
def test_link_local_and_metadata_are_rejected(url):
    with pytest.raises(OutboundUrlError):
        validate_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://metadata.google.internal",
        "http://metadata",
        "http://instance-data",
        "http://instance-data.ec2.internal",
        "http://foo.local",
        "http://foo.internal",
        "http://a.localhost",
    ],
)
def test_internal_and_special_use_hostnames_are_rejected(url):
    with pytest.raises(OutboundUrlError):
        validate_outbound_url(url)


def test_require_https_blocks_plain_http_but_not_vice_versa():
    # Plain http to a public host is fine for a generic fetch...
    validate_outbound_url("http://example.com/x", require_https=False)
    # ...but not when the caller declares that credentials/PII travel there.
    with pytest.raises(OutboundUrlError):
        validate_outbound_url("http://example.com/x", require_https=True)


# ------------------------------------------------------------ API boundary ---


def test_crm_base_url_private_ip_is_rejected_at_the_api():
    from app.api.integration_routes import _validated_config

    with pytest.raises(HTTPException) as exc:
        _validated_config(CrmProviderType.HUBSPOT, {"base_url": "http://192.168.1.10"})
    assert exc.value.status_code == 422


def test_crm_base_url_https_public_is_accepted():
    from app.api.integration_routes import _validated_config

    cleaned = _validated_config(CrmProviderType.HUBSPOT, {"base_url": "https://hubspot.test"})
    assert cleaned["base_url"] == "https://hubspot.test"


def test_webhook_url_private_target_is_rejected_at_the_api():
    from app.api.integration_routes import _validated_config

    with pytest.raises(HTTPException) as exc:
        _validated_config(CrmProviderType.WEBHOOK, {"url": "https://169.254.169.254/x"})
    assert exc.value.status_code == 422


def test_calendar_token_url_localhost_is_rejected_at_the_api():
    from app.api.appointment_routes import _validated_config

    with pytest.raises(HTTPException) as exc:
        _validated_config(CalendarProviderType.GOOGLE, {"token_url": "http://127.0.0.1/token"})
    assert exc.value.status_code == 422


def test_calendar_base_url_metadata_host_is_rejected_at_the_api():
    from app.api.appointment_routes import _validated_config

    with pytest.raises(HTTPException) as exc:
        _validated_config(
            CalendarProviderType.GOOGLE,
            {"base_url": "https://metadata.google.internal"},
        )
    assert exc.value.status_code == 422


# ------------------------------------------------------- provider-level guard ---


def _crm_ctx(**config):
    from app.integrations.crm.base import ProviderContext

    return ProviderContext(
        tenant_id="t-1",
        credentials={"access_token": "tok-secret"},
        config=config,
        field_mappings={},
    )


def test_ghl_provider_rejects_private_base_url_at_use():
    from app.integrations.crm.errors import CrmConfigurationError

    provider = GoHighLevelProvider(_crm_ctx(location_id="loc-1", base_url="http://10.0.0.5"))
    with pytest.raises(CrmConfigurationError):
        provider._base()


def test_ghl_provider_accepts_public_https_base_url():
    provider = GoHighLevelProvider(_crm_ctx(location_id="loc-1", base_url="https://ghl.test"))
    assert provider._base() == "https://ghl.test"


def _calendar_ctx(**config):
    from app.integrations.calendar.base import CalendarContext

    return CalendarContext(
        tenant_id="t-1",
        credentials={
            "access_token": "tok",
            "refresh_token": "ref",
            "client_id": "cid",
            "client_secret": "csec",
        },
        config=config,
        timezone="America/New_York",
    )


def test_google_provider_rejects_loopback_base_url_at_use():
    from app.integrations.calendar.errors import CalendarConfigurationError

    provider = GoogleCalendarProvider(_calendar_ctx(calendar_id="primary", base_url="http://[::1]"))
    with pytest.raises(CalendarConfigurationError):
        provider._base()


def test_google_provider_accepts_public_https_base_url():
    provider = GoogleCalendarProvider(
        _calendar_ctx(calendar_id="primary", base_url="https://gcal.test/v3")
    )
    assert provider._base() == "https://gcal.test/v3"
