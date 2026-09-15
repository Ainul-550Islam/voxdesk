"""RFC 9116 security.txt (Step 9 hardening).

Serves /.well-known/security.txt (with /security.txt as a convenience) from the
operator's security contact and policy URLs. Returns 404 when no contact is
configured: a security.txt without a real, monitored contact is worse than none
because scanners treat a present file as authoritative.

Contact is an email or a https:// URL, per RFC 9116. Set SECURITY_CONTACT in
production (see .env.example).
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import FastAPI, Response

from app.core.config import settings

_BODY = """Contact: {contact}
Expires: {expires}
Preferred-Languages: en
Canonical: {base}/.well-known/security.txt
"""


def _render() -> str | None:
    contact = (settings.security_contact or "").strip()
    if not contact:
        return None
    expires = (date.today() + timedelta(days=365)).isoformat()
    base = settings.public_base_url.rstrip("/")
    return _BODY.format(contact=contact, expires=expires, base=base)


def add_security_txt(app: FastAPI) -> None:
    @app.get("/.well-known/security.txt", include_in_schema=False)
    @app.get("/security.txt", include_in_schema=False)
    async def security_txt() -> Response:
        body = _render()
        if body is None:
            return Response(
                status_code=404, content="no security contact configured"
            )
        return Response(content=body, media_type="text/plain; charset=utf-8")
