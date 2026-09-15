"""HTTP security headers middleware (OWASP ASVS V14.4).

Applied to every HTTP response. HSTS is only emitted when the deployment is
served over TLS (`settings.uses_https`, driven by the PUBLIC_BASE_URL scheme):
a browser honouring HSTS on plain-HTTP localhost would make local development
painful, while a TLS-terminated staging box gets exactly the same protection
as production. Everything else is always on and deliberately static: no
configuration means no way to accidentally disable a control per-tenant.

Headers set:
    X-Content-Type-Options: nosniff
    X-Frame-Options: DENY            (frame-ancestors is the CSP equivalent)
    Referrer-Policy: strict-origin-when-cross-origin
    Permissions-Policy: camera=(), microphone=(), geolocation=()
    Content-Security-Policy:         (see _CSP below)
    Strict-Transport-Security:       only when served over TLS (settings.uses_https),
                                     max-age=31536000
    Cache-Control: no-store          on authenticated responses only

The CSP is intentionally strict for script and connect sources (self only) but
allows inline styles, which the React dashboard uses. It does NOT allow
'unsafe-inline' for scripts, so a stored-XSS payload in a transcript cannot
execute a classic <script> injection.
"""
from __future__ import annotations

from fastapi import FastAPI, Request

from app.core.config import settings

_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'"
)

_HSTS = "max-age=31536000; includeSubDomains"

# Responses that must never be cached by intermediaries.
_AUTH_PATH_PREFIXES = ("/api/", "/auth/", "/telephony/", "/channels/")


def add_security_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def _security_headers_middleware(request: Request, call_next):
        response = await call_next(request)

        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        headers.setdefault("Content-Security-Policy", _CSP)
        if settings.uses_https:
            headers.setdefault("Strict-Transport-Security", _HSTS)

        if request.url.path.startswith(_AUTH_PATH_PREFIXES):
            headers.setdefault("Cache-Control", "no-store")

        return response
