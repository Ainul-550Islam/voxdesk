"""Step 9 — GDPR data-subject rights endpoints (export / erasure / status)."""
from __future__ import annotations

import pytest

from tests.conftest import auth_headers
from tests.test_transfer import seed_call


@pytest.mark.asyncio
async def test_export_returns_own_data_structured(client, db, tenant_a, owner_a):
    headers = await auth_headers(client, owner_a)
    await seed_call(db, tenant_a)

    r = await client.get("/api/gdpr/export", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"exported_at", "tenant", "calls", "transcripts", "leads"}
    assert len(body["calls"]) == 1
    assert body["calls"][0]["from"] == "+15551112222"


@pytest.mark.asyncio
async def test_status_reports_consent_state(client, tenant_a, owner_a):
    headers = await auth_headers(client, owner_a)
    r = await client.get("/api/gdpr/status", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["erasure_requested_at"] is None
    assert body["data_consent_recorded_at"] is None
    assert "ai_disclosure" in body


@pytest.mark.asyncio
async def test_erasure_deletes_data_and_marks_the_tenant(
    client, db, tenant_a, owner_a
):
    headers = await auth_headers(client, owner_a)
    await seed_call(db, tenant_a)

    r = await client.post("/api/gdpr/erasure", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["purged_calls"] == 1

    # The tenant row is retained (billing/audit obligations) but marked.
    status = (await client.get("/api/gdpr/status", headers=headers)).json()
    assert status["erasure_requested_at"] is not None

    # And the data is actually gone.
    export = (await client.get("/api/gdpr/export", headers=headers)).json()
    assert export["calls"] == []


@pytest.mark.asyncio
async def test_non_owner_is_forbidden(client, tenant_a, admin_a):
    """Data-subject rights are owner-gated: an admin of the same tenant
    cannot export or erase, so a hijacked lower-privilege account cannot
    exfiltrate or destroy data."""
    headers = await auth_headers(client, admin_a)
    assert (await client.get("/api/gdpr/export", headers=headers)).status_code == 403
    assert (await client.post("/api/gdpr/erasure", headers=headers)).status_code == 403
    assert (await client.get("/api/gdpr/status", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_anonymous_is_refused(client):
    assert (await client.get("/api/gdpr/export")).status_code == 401
    assert (await client.post("/api/gdpr/erasure")).status_code == 401
    assert (await client.get("/api/gdpr/status")).status_code == 401
