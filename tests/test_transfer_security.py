"""
Tenant isolation for the transfer surface.

STEP 2 established that a tenant may never touch another tenant's rows. The
new transfer metadata and the new /calls/{id}/transfer endpoint are part of
that surface, so the same guarantees are re-asserted here rather than assumed.
"""
from __future__ import annotations

import pytest

from app.db.models import CallStatus, TransferState, UserRole
from app.telephony import transfer_service
from app.telephony.provider import FakeTelephonyProvider
from tests.conftest import auth_headers, make_user
from tests.test_transfer import seed_call, tenant_with_human


@pytest.mark.asyncio
async def test_tenant_a_cannot_trigger_a_transfer_on_tenant_b_call(db):
    tenant_a = await tenant_with_human(db, "+15551110000", "Tenant A")
    tenant_b = await tenant_with_human(db, "+15552220000", "Tenant B")
    call_b = await seed_call(db, tenant_b)
    provider = FakeTelephonyProvider()

    result = await transfer_service.request_transfer(db, tenant_a, call_b, provider=provider)

    assert result.ok is False
    assert provider.call_count == 0
    await db.refresh(call_b)
    assert call_b.transfer_state is TransferState.NONE
    assert call_b.status is CallStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_tenant_a_cannot_use_tenant_b_destination(db):
    """The destination is read from the tenant row and cannot be supplied."""
    tenant_a = await tenant_with_human(db, "+15551110000", "Tenant A")
    tenant_b = await tenant_with_human(db, "+15552220000", "Tenant B")

    dest_a, _ = transfer_service.resolve_destination(tenant_a)
    dest_b, _ = transfer_service.resolve_destination(tenant_b)
    assert dest_a == "+15551110000"
    assert dest_b == "+15552220000"

    call_a = await seed_call(db, tenant_a)
    provider = FakeTelephonyProvider()
    await transfer_service.request_transfer(db, tenant_a, call_a, provider=provider)
    assert "+15552220000" not in provider.last_twiml()


@pytest.mark.asyncio
async def test_tenant_a_cannot_read_tenant_b_transfer_metadata(client, db):
    tenant_a = await tenant_with_human(db, "+15551110000", "Tenant A")
    tenant_b = await tenant_with_human(db, "+15552220000", "Tenant B")
    owner_a = await make_user(db, tenant_a, UserRole.OWNER)

    call_b = await seed_call(db, tenant_b)
    await transfer_service.request_transfer(
        db, tenant_b, call_b, reason="B private reason",
        provider=FakeTelephonyProvider(),
    )

    headers = await auth_headers(client, owner_a)
    resp = await client.get(f"/api/calls/{call_b.id}/transfer", headers=headers)

    assert resp.status_code == 404, "cross-tenant read must 404, not 403"
    assert "B private reason" not in resp.text
    assert "+15552220000" not in resp.text


@pytest.mark.asyncio
async def test_owner_can_read_their_own_transfer_metadata(client, db):
    tenant = await tenant_with_human(db, "+15551110000", "Tenant A")
    owner = await make_user(db, tenant, UserRole.OWNER)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(
        db, tenant, call, reason="angry caller", provider=FakeTelephonyProvider()
    )

    headers = await auth_headers(client, owner)
    body = (await client.get(f"/api/calls/{call.id}/transfer", headers=headers)).json()

    assert body["transfer_state"] == "dialing"
    assert body["transfer_reason"] == "angry caller"
    assert body["transfer_attempts"] == 1


@pytest.mark.asyncio
async def test_transfer_endpoint_redacts_the_destination_number(client, db):
    """The dashboard shows that a transfer happened, not a staff directory."""
    tenant = await tenant_with_human(db, "+15551110000", "Tenant A")
    owner = await make_user(db, tenant, UserRole.OWNER)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(
        db, tenant, call, provider=FakeTelephonyProvider()
    )

    headers = await auth_headers(client, owner)
    body = (await client.get(f"/api/calls/{call.id}/transfer", headers=headers)).json()

    assert body["transfer_destination"] != "+15551110000"
    assert "5551110" not in body["transfer_destination"]


@pytest.mark.asyncio
async def test_transfer_endpoint_requires_authentication(client, db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    resp = await client.get(f"/api/calls/{call.id}/transfer")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_viewer_may_read_transfer_metadata_but_agent_scoping_holds(client, db):
    tenant = await tenant_with_human(db)
    viewer = await make_user(db, tenant, UserRole.VIEWER)
    call = await seed_call(db, tenant)

    headers = await auth_headers(client, viewer)
    resp = await client.get(f"/api/calls/{call.id}/transfer", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["transfer_state"] == "none"


@pytest.mark.asyncio
async def test_call_list_exposes_transfer_state_only_for_own_tenant(client, db):
    tenant_a = await tenant_with_human(db, "+15551110000", "Tenant A")
    tenant_b = await tenant_with_human(db, "+15552220000", "Tenant B")
    owner_a = await make_user(db, tenant_a, UserRole.OWNER)

    await seed_call(db, tenant_a)
    await seed_call(db, tenant_b)

    headers = await auth_headers(client, owner_a)
    body = (await client.get(f"/api/tenants/{tenant_a.id}/calls", headers=headers)).json()

    # STEP 8 made this a paginated envelope; the guarantee is unchanged.
    rows = body["calls"]
    assert len(rows) == 1
    assert body["total"] == 1
    assert rows[0]["transfer_state"] == "none"


# ------------------------------------------------------------ observability ---

@pytest.mark.asyncio
async def test_transfer_logs_carry_correlation_ids_and_no_secrets(db):
    """
    Structured logs must be usable for support (call_id / call_sid /
    tenant_id) without becoming a leak of numbers or credentials.
    """
    from structlog.testing import capture_logs

    tenant = await tenant_with_human(db, "+15551110000", "Log Co")
    call = await seed_call(db, tenant)

    with capture_logs() as logs:
        await transfer_service.request_transfer(
            db, tenant, call, reason="angry", provider=FakeTelephonyProvider()
        )

    events = {entry["event"] for entry in logs}
    assert "transfer_requested" in events
    assert "transfer_started" in events

    requested = next(e for e in logs if e["event"] == "transfer_requested")
    assert requested["call_id"] == str(call.id)
    assert requested["call_sid"] == call.call_sid
    assert requested["tenant_id"] == str(tenant.id)
    assert requested["transfer_attempt"] == 1

    # The destination is redacted, and no credential ever appears.
    blob = str(logs)
    assert "+15551110000" not in blob
    assert requested["destination"].endswith("00")
    assert "5551110" not in requested["destination"]

    from app.core.config import settings
    assert settings.jwt_secret not in blob
    assert settings.twilio_auth_token not in blob or not settings.twilio_auth_token

