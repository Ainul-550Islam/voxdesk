"""Step 9 — RFC 9116 security.txt endpoint."""
from __future__ import annotations

import pytest

from app.core.config import settings


@pytest.mark.asyncio
async def test_security_txt_404_without_a_configured_contact(client, monkeypatch):
    monkeypatch.setattr(settings, "security_contact", "")
    r = await client.get("/.well-known/security.txt")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_security_txt_serves_the_configured_contact(client, monkeypatch):
    monkeypatch.setattr(settings, "security_contact", "security@example.com")
    r = await client.get("/.well-known/security.txt")
    assert r.status_code == 200
    assert "Contact: security@example.com" in r.text
    assert "Expires: " in r.text

    # Convenience alias.
    r2 = await client.get("/security.txt")
    assert r2.status_code == 200
    assert "Contact: security@example.com" in r2.text
