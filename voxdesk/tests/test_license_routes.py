"""Step 9 — licensing endpoints (issue + verify)."""
from __future__ import annotations

import pytest

from app.billing import licensing
from tests.conftest import auth_headers


@pytest.mark.asyncio
async def test_owner_can_issue_a_verifiable_license(client, tenant_a, owner_a):
    headers = await auth_headers(client, owner_a)
    r = await client.post(
        "/api/billing/license/issue",
        json={"plan": "pro", "seats": 5, "expires_in_days": 30},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == str(tenant_a.id)
    assert body["plan"] == "pro"
    assert body["seats"] == 5

    info = licensing.verify_license(body["license"])
    assert info is not None
    assert info.tenant_id == str(tenant_a.id)
    assert info.plan == "pro"
    assert info.seats == 5


@pytest.mark.asyncio
async def test_issue_rejects_out_of_range_payloads(client, tenant_a, owner_a):
    headers = await auth_headers(client, owner_a)
    # seats must be >= 1
    r = await client.post(
        "/api/billing/license/issue",
        json={"plan": "pro", "seats": 0, "expires_in_days": 30},
        headers=headers,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_verify_endpoint_is_public_and_honest(client, tenant_a, owner_a):
    headers = await auth_headers(client, owner_a)
    token = (
        await client.post(
            "/api/billing/license/issue",
            json={"plan": "pro", "seats": 1, "expires_in_days": 30},
            headers=headers,
        )
    ).json()["license"]

    ok = await client.post("/api/billing/license/verify", json={"license": token})
    assert ok.status_code == 200
    assert ok.json()["valid"] is True

    bad = await client.post(
        "/api/billing/license/verify", json={"license": "not-a-token"}
    )
    assert bad.status_code == 200
    assert bad.json()["valid"] is False


@pytest.mark.asyncio
async def test_issue_requires_owner(client, tenant_a, admin_a):
    headers = await auth_headers(client, admin_a)
    r = await client.post(
        "/api/billing/license/issue",
        json={"plan": "pro", "seats": 1, "expires_in_days": 30},
        headers=headers,
    )
    assert r.status_code == 403
