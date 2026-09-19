"""Self-hosted / white-label licensing (Step 9 business domain).

License tokens are HMAC-SHA256 signed, URL-safe strings that bind a plan, seat
count and expiry to a tenant id. They let a self-hosted or white-label
deployment verify entitlement offline (no Stripe account required), which is
the licensing model resellers expect.

Security properties:

* HS256 over a compact payload; tampering, truncation and expiry all fail
  closed to ``verify_license`` returning ``None``.
* The signing key is ``LICENSE_SECRET`` (falls back to ``JWT_SECRET``), so a
  licence minted for one deployment is invalid elsewhere.
* Verification is constant-time via ``hmac.compare_digest``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

from app.core.config import settings

_LICENSE_PREFIX = "voxdesk-license-v1"


@dataclass(frozen=True)
class LicenseInfo:
    tenant_id: str
    plan: str
    seats: int
    expires_at: int
    issued_at: int


def _signing_key() -> str:
    return settings.license_secret or settings.jwt_secret


def _payload_to_string(payload: dict[str, Any]) -> str:
    # Deterministic, compact: "prefix.plan.seats.expiry.issued.tenant"
    return ".".join(
        [
            _LICENSE_PREFIX,
            str(payload["plan"]),
            str(payload["seats"]),
            str(payload["expires_at"]),
            str(payload["issued_at"]),
            str(payload["tenant_id"]),
        ]
    )


def _sign(payload: str) -> str:
    return hmac.new(
        _signing_key().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def issue_license(
    tenant_id: str,
    plan: str,
    seats: int,
    expires_at: int,
    *,
    issued_at: int | None = None,
) -> str:
    """Mint a signed license token (returned to the operator/tenant)."""
    payload = {
        "tenant_id": tenant_id,
        "plan": plan,
        "seats": seats,
        "expires_at": expires_at,
        "issued_at": int(issued_at if issued_at is not None else time.time()),
    }
    body = base64.urlsafe_b64encode(_payload_to_string(payload).encode()).decode()
    return f"{body}.{_sign(_payload_to_string(payload))}"


def verify_license(token: str, *, now: int | None = None) -> LicenseInfo | None:
    """Validate a license token. Returns LicenseInfo or None on any failure."""
    if not token:
        return None
    body_b64, _, signature = token.rpartition(".")
    if not body_b64 or not signature:
        return None
    try:
        raw = base64.urlsafe_b64decode(body_b64.encode()).decode()
    except Exception:
        return None

    parts = raw.split(".")
    if len(parts) != 6 or parts[0] != _LICENSE_PREFIX:
        return None

    if not hmac.compare_digest(_sign(raw), signature):
        return None

    try:
        plan, seats_s, expires_s, issued_s, tenant_id = parts[1], parts[2], parts[3], parts[4], parts[5]
        seats = int(seats_s)
        expires_at = int(expires_s)
        issued_at = int(issued_s)
    except ValueError:
        return None

    current = int(now if now is not None else time.time())
    if current >= expires_at:
        return None
    if seats < 1:
        return None

    return LicenseInfo(
        tenant_id=tenant_id,
        plan=plan,
        seats=seats,
        expires_at=expires_at,
        issued_at=issued_at,
    )
