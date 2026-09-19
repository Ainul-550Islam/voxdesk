"""Outbound-URL validation (SSRF guard).

Several integrations accept a destination from tenant configuration:

* ``base_url`` for GoHighLevel / HubSpot / Jobber (CRM) and Google /
  Microsoft / Cal.com (calendar) — every request to these carries a bearer
  access token in the ``Authorization`` header.
* ``token_url`` for Google / Microsoft OAuth refresh — the POST body carries
  the tenant's ``client_secret`` and ``refresh_token``.
* ``url`` for the generic signed-webhook CRM provider — the POST body carries
  customer phone numbers and call summaries.

That makes them an SSRF surface: a tenant who points one of these at
``http://169.254.169.254`` (cloud metadata), loopback, or a private RFC 1918
address makes the VoxDesk server place an authenticated request against a
target the tenant could not otherwise reach, and the credentials travel with
it.

The guard here is **static and deterministic** — it classifies the URL by its
literal components and never resolves DNS:

* scheme must be ``http`` or ``https`` (and ``https`` when the caller says so,
  because credentials or PII travel to these endpoints);
* no ``userinfo`` component (a URL like ``https://admin:secret@host`` smuggles
  a credential into the destination);
* a host that is an IP literal is rejected when it is loopback, private
  (RFC 1918 / RFC 4193 ULA), link-local (RFC 3927 / ``fe80::/10``), reserved,
  multicast, unspecified, or the metadata address ``169.254.169.254``;
* a hostname of ``localhost`` / ``*.localhost``, or one ending in ``.local``
  (mDNS) / ``.internal`` / ``.localhost``, or the well-known cloud metadata
  names, is rejected.

What it deliberately does **not** do:

* **No DNS resolution.** A hostname that resolves to a private address
  (DNS rebinding, split-horizon DNS) is invisible to a static check. The
  complementary control is egress network policy (the API container must not
  be able to reach link-local/metadata/private ranges at the network layer);
  that is an operational requirement documented in ``docs/SECURITY.md``, not
  something a string check can promise.
* **No redirect following.** Each HTTP client here is configured with a
  timeout and the adapters treat a redirect as a response to validate, not a
  destination to chase with credentials replayed. A future client that
  auto-follows redirects must re-validate every hop.

Failure raises :class:`OutboundUrlError` with a message safe to show an
operator (it never echoes the rejected URL's credentials).
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = ("http", "https")

#: Hostnames that are, by definition, not routable public endpoints.
_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

#: Suffixes that identify non-public / special-use names.
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


class OutboundUrlError(ValueError):
    """The URL must not be used as an outbound destination."""


def _ip_classification(host: str) -> str | None:
    """Return a non-empty reason when ``host`` is an IP literal we refuse."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None

    if addr.is_unspecified:
        return "unspecified address"
    if addr.is_loopback:
        return "loopback address"
    if addr.is_link_local:
        return "link-local address"
    if addr.is_multicast:
        return "multicast address"
    if addr.is_reserved:
        return "reserved address"
    if addr.is_private:
        return "private address"
    if host == "169.254.169.254":
        return "cloud metadata address"
    return None


def _hostname_reason(host: str) -> str | None:
    lowered = host.rstrip(".").lower()

    if lowered in _METADATA_HOSTS:
        return "cloud metadata endpoint"
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return "localhost"
    if lowered.endswith(_BLOCKED_SUFFIXES):
        return "non-public hostname"
    return None


def validate_outbound_url(url: str, *, require_https: bool = False) -> None:
    """Raise :class:`OutboundUrlError` when ``url`` is an unsafe destination.

    ``require_https`` should be set for every destination that receives
    credentials or PII (token refresh, CRM base URLs, outbound webhooks).
    """
    if not isinstance(url, str) or not url.strip():
        raise OutboundUrlError("destination URL is empty")

    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise OutboundUrlError("destination URL is malformed") from exc

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise OutboundUrlError("destination URL must use http or https")
    if require_https and parts.scheme.lower() != "https":
        raise OutboundUrlError(
            "destination URL must use https — credentials or customer data " "are sent to it"
        )
    if parts.username or parts.password:
        raise OutboundUrlError("destination URL must not embed credentials")

    host = parts.hostname
    if not host:
        raise OutboundUrlError("destination URL has no host")

    # IP literals are classified directly; IPv4-mapped IPv6 (::ffff:127.0.0.1)
    # is caught by ip_address() classifying it as loopback/private.
    reason = _ip_classification(host)
    if reason is None and _HOST_RE.fullmatch(host) is None:
        # Not an IP and not a plain hostname (e.g. bracket forms leaking
        # through). Refuse anything we cannot classify cleanly.
        reason = "unrecognised host format"
    if reason is None:
        reason = _hostname_reason(host)

    if reason:
        raise OutboundUrlError(
            f"destination URL resolves to a non-public or reserved target ({reason})"
        )


def is_safe_outbound_url(url: str, *, require_https: bool = False) -> bool:
    """Boolean form of :func:`validate_outbound_url`, for callers that prefer it."""
    try:
        validate_outbound_url(url, require_https=require_https)
    except OutboundUrlError:
        return False
    return True
