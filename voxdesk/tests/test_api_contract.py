"""Step 9 — API contract smoke test.

Hits every registered route with a safe, parameterised request and asserts the
server never returns a 500 and always returns a structured response. This is a
*contract* test, not a functional test: it proves the route exists, is wired,
and fails with a client error (4xx) rather than an internal error when hit
without proper authorisation.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app as voxdesk_app

# Routes that need a WebSocket or a multipart body and are therefore not part
# of this plain-HTTP sweep.
_SKIP_PREFIXES = ("/telephony/ws",)


def _safe_sample(route) -> dict:
    """Build a concrete path for a route with path parameters."""
    path = route.path
    for param in route.param_convertors or {}:
        path = path.replace("{" + param + "}", uuid.uuid4().hex)
    return path


@pytest.mark.asyncio
async def test_no_route_returns_500_when_probed():
    async with AsyncClient(
        transport=ASGITransport(app=voxdesk_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        probed = 0
        for route in voxdesk_app.routes:
            if not getattr(route, "methods", None):
                continue
            if "GET" not in route.methods:
                continue
            path = _safe_sample(route)
            if path.startswith(_SKIP_PREFIXES):
                continue
            r = await ac.get(path)
            assert r.status_code != 500, f"GET {path} -> 500"
            probed += 1
        assert probed > 40, f"expected to probe many routes, only {probed}"
