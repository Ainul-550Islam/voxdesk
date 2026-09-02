"""
Tenant isolation and API security for CRM integrations.

Requirement 22's list, one test each, plus the cases that list implies.

The premise throughout: **an id is not authorization.** Every test here gives
Tenant B a genuine, authenticated session and a correct id belonging to Tenant
A, and asserts that being right about the id changes nothing.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.db.models import (
    AuditLog,
    CrmEvent,
    CrmEventType,
    CrmIntegration,
    CrmProviderType,
    CrmSync,
)
from tests.conftest import auth_headers, make_integration

TOKEN_A = "pit-tenant-a-secret-token-1234567890"
TOKEN_B = "pit-tenant-b-secret-token-0987654321"


@pytest.fixture
async def integration_a(db, tenant_a):
    return await make_integration(
        db, tenant_a, "gohighlevel",
        credentials={"access_token": TOKEN_A},
        config={"location_id": "loc-A", "base_url": "https://ghl.test"},
    )


@pytest.fixture
async def integration_b(db, tenant_b):
    return await make_integration(
        db, tenant_b, "gohighlevel",
        credentials={"access_token": TOKEN_B},
        config={"location_id": "loc-B", "base_url": "https://ghl.test"},
    )


# ================================================== cross-tenant reads ===

async def test_tenant_b_cannot_read_tenant_a_integration(
    client, db, owner_b, integration_a, integration_b
):
    headers = await auth_headers(client, owner_b)
    response = await client.get("/api/integrations/crm/gohighlevel", headers=headers)

    assert response.status_code == 200
    body = response.json()
    # B sees B's own integration, never A's.
    assert body["config"]["location_id"] == "loc-B"
    assert "loc-A" not in json.dumps(body)


async def test_tenant_b_listing_never_includes_tenant_a(
    client, db, owner_b, integration_a, integration_b
):
    headers = await auth_headers(client, owner_b)
    response = await client.get("/api/integrations/crm", headers=headers)

    assert response.status_code == 200
    body = json.dumps(response.json())
    assert "loc-B" in body
    assert "loc-A" not in body


async def test_a_tenant_with_no_integration_gets_404_not_someone_elses(
    client, db, owner_b, integration_a
):
    """
    A 404 here, and a 404 for a provider nobody has connected, must be
    indistinguishable — otherwise the endpoint reports whether *another*
    tenant has GoHighLevel connected.
    """
    headers = await auth_headers(client, owner_b)

    theirs = await client.get("/api/integrations/crm/gohighlevel", headers=headers)
    nobodys = await client.get("/api/integrations/crm/jobber", headers=headers)

    assert theirs.status_code == nobodys.status_code == 404
    assert theirs.json() == nobodys.json()


async def test_no_credential_reaches_any_api_response(
    client, db, owner_a, integration_a
):
    headers = await auth_headers(client, owner_a)

    for path in (
        "/api/integrations/crm",
        "/api/integrations/crm/gohighlevel",
        "/api/integrations/crm/providers",
        "/api/integrations/crm/syncs",
    ):
        response = await client.get(path, headers=headers)
        assert response.status_code == 200, path
        body = response.text
        assert TOKEN_A not in body, path
        assert integration_a.credentials_encrypted not in body, path


# ================================================= cross-tenant writes ===

async def test_tenant_b_cannot_modify_tenant_a_integration(
    client, db, owner_b, integration_a, integration_b
):
    headers = await auth_headers(client, owner_b)
    response = await client.put(
        "/api/integrations/crm/gohighlevel",
        headers=headers,
        json={"config": {"location_id": "HIJACKED"}, "is_enabled": True},
    )
    assert response.status_code == 200

    await db.refresh(integration_a)
    await db.refresh(integration_b)
    assert integration_a.config["location_id"] == "loc-A"      # untouched
    assert integration_b.config["location_id"] == "HIJACKED"   # B changed B's


async def test_tenant_b_cannot_delete_tenant_a_integration(
    client, db, owner_b, integration_a
):
    headers = await auth_headers(client, owner_b)
    response = await client.delete(
        "/api/integrations/crm/gohighlevel", headers=headers
    )
    assert response.status_code == 404

    still_there = await db.get(CrmIntegration, integration_a.id)
    assert still_there is not None


async def test_tenant_b_cannot_disconnect_tenant_a_integration(
    client, db, owner_b, integration_a
):
    headers = await auth_headers(client, owner_b)
    response = await client.post(
        "/api/integrations/crm/gohighlevel/disconnect", headers=headers
    )
    assert response.status_code == 404

    await db.refresh(integration_a)
    assert integration_a.credentials_encrypted is not None


async def test_tenant_b_cannot_trigger_a_test_against_tenant_a_credentials(
    client, db, owner_b, integration_a
):
    """
    A connection test spends Tenant A's provider rate limit and would confirm
    whether their token is live. Both are A's business.
    """
    headers = await auth_headers(client, owner_b)
    response = await client.post(
        "/api/integrations/crm/gohighlevel/test", headers=headers
    )
    assert response.status_code == 404


# ================================================== tenant_id injection ===

async def test_a_tenant_id_in_the_body_is_ignored(
    client, db, owner_b, tenant_a, integration_b
):
    """
    Requirement 6: do not accept an arbitrary tenant_id and trust it.

    The model has no such field, so it is dropped. Asserting the *effect* --
    that A's row is untouched and B's changed -- rather than the mechanism,
    because the mechanism could change while the guarantee must not.
    """
    headers = await auth_headers(client, owner_b)
    response = await client.put(
        "/api/integrations/crm/gohighlevel",
        headers=headers,
        json={
            "tenant_id": str(tenant_a.id),
            "config": {"location_id": "injected"},
        },
    )
    assert response.status_code == 200

    rows = (
        (
            await db.execute(
                select(CrmIntegration).where(CrmIntegration.tenant_id == tenant_a.id)
            )
        )
        .scalars()
        .all()
    )
    assert all(r.config.get("location_id") != "injected" for r in rows)

    await db.refresh(integration_b)
    assert integration_b.config["location_id"] == "injected"


async def test_a_tenant_id_query_parameter_is_ignored(
    client, db, owner_b, tenant_a, integration_a, integration_b
):
    headers = await auth_headers(client, owner_b)
    response = await client.get(
        f"/api/integrations/crm/gohighlevel?tenant_id={tenant_a.id}", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["config"]["location_id"] == "loc-B"


# ============================================== cross-tenant sync access ===

@pytest.fixture
async def sync_a(db, tenant_a, integration_a):
    from app.integrations.crm import events
    from tests.conftest import make_call

    call = await make_call(db, tenant_a)
    await events.emit(
        db, tenant_id=tenant_a.id, event_type=CrmEventType.CALL_COMPLETED,
        entity_id=call.id, payload={"call_id": str(call.id), "secret": "A-ONLY"},
    )
    await db.commit()
    return call


async def test_tenant_b_cannot_see_tenant_a_syncs(client, db, owner_b, sync_a):
    headers = await auth_headers(client, owner_b)
    response = await client.get("/api/integrations/crm/syncs", headers=headers)

    assert response.status_code == 200
    assert response.json()["total"] == 0


async def test_tenant_b_cannot_see_tenant_a_external_ids(
    client, db, tenant_a, owner_b, sync_a
):
    """Requirement 5: Tenant A must never inspect Tenant B's external IDs."""
    syncs = (
        (await db.execute(select(CrmSync).where(CrmSync.tenant_id == tenant_a.id)))
        .scalars()
        .all()
    )
    assert syncs
    syncs[0].external_id = "GHL-CONTACT-SECRET-99"
    await db.commit()

    headers = await auth_headers(client, owner_b)
    response = await client.get("/api/integrations/crm/syncs", headers=headers)
    assert "GHL-CONTACT-SECRET-99" not in response.text


async def test_an_event_is_only_queued_for_its_own_tenants_integrations(
    db, tenant_a, tenant_b, integration_a, integration_b
):
    """
    The fan-out itself must be tenant-scoped. If `_subscribed_integrations`
    ever dropped its tenant predicate, Tenant A's call would be pushed into
    Tenant B's CRM -- the worst outcome in this whole step, and it would look
    like a working feature.
    """
    from app.integrations.crm import events
    from tests.conftest import make_call

    call = await make_call(db, tenant_a)
    event = await events.emit(
        db, tenant_id=tenant_a.id, event_type=CrmEventType.CALL_COMPLETED,
        entity_id=call.id, payload={"call_id": str(call.id)},
    )
    await db.commit()

    syncs = (
        (await db.execute(select(CrmSync).where(CrmSync.event_id == event.id)))
        .scalars()
        .all()
    )
    assert len(syncs) == 1
    assert syncs[0].integration_id == integration_a.id
    assert syncs[0].tenant_id == tenant_a.id


async def test_a_sync_whose_integration_belongs_elsewhere_is_refused(
    db, tenant_a, tenant_b, integration_a, integration_b
):
    """
    Defensive depth. Even if a mismatched row were somehow written, the
    service must refuse rather than run Tenant A's event against Tenant B's
    credentials.
    """
    from app.db.models import CrmEntityType, CrmSyncStatus
    from app.integrations.crm import service
    from tests.conftest import make_call

    call = await make_call(db, tenant_a)
    event = CrmEvent(
        tenant_id=tenant_a.id, event_type=CrmEventType.CALL_COMPLETED,
        entity_type=CrmEntityType.CALL, entity_id=call.id,
        idempotency_key=f"manual:{uuid.uuid4()}", payload={},
    )
    db.add(event)
    await db.flush()

    rogue = CrmSync(
        tenant_id=tenant_a.id, event_id=event.id,
        integration_id=integration_b.id,          # <-- another tenant's
        provider=CrmProviderType.GOHIGHLEVEL,
        entity_type=CrmEntityType.CALL, entity_id=call.id,
        status=CrmSyncStatus.PENDING,
    )
    db.add(rogue)
    await db.commit()

    outcome = await service.process_sync(db, rogue)
    assert outcome.status is CrmSyncStatus.PERMANENT_FAILURE
    assert outcome.error_code == "misconfigured"


# =========================================================== permissions ===

async def test_a_viewer_cannot_write_an_integration(client, db, viewer_a):
    headers = await auth_headers(client, viewer_a)
    response = await client.put(
        "/api/integrations/crm/webhook",
        headers=headers,
        json={"config": {"url": "https://evil.example.com/steal"}},
    )
    assert response.status_code == 403


async def test_an_agent_cannot_read_integrations(client, db, agent_a, integration_a):
    """
    Integration config is a sensitive setting, not operational data. An agent
    handling calls has no reason to see which CRM is wired up or where its
    webhook points.
    """
    headers = await auth_headers(client, agent_a)
    response = await client.get("/api/integrations/crm", headers=headers)
    assert response.status_code == 403


async def test_the_routes_use_the_permission_enum_not_role_strings():
    """
    Requirement 6 forbids `if user.role == "admin"`. Asserted against the
    source, because this is the kind of shortcut that gets added under time
    pressure and reads as harmless.
    """
    import pathlib

    for path in ("app/api/integration_routes.py", "app/api/crm_webhook_routes.py"):
        source = pathlib.Path(path).read_text()
        assert '== "admin"' not in source, path
        assert "== 'admin'" not in source, path
        assert ".role ==" not in source, path


async def test_unauthenticated_requests_are_rejected(client, integration_a):
    for method, path in (
        ("get", "/api/integrations/crm"),
        ("get", "/api/integrations/crm/gohighlevel"),
        ("put", "/api/integrations/crm/gohighlevel"),
        ("delete", "/api/integrations/crm/gohighlevel"),
        ("post", "/api/integrations/crm/gohighlevel/test"),
    ):
        kwargs = {"json": {}} if method in ("put", "post") else {}
        response = await getattr(client, method)(path, **kwargs)
        assert response.status_code in (401, 403), f"{method} {path}"


# ============================================== no secrets in audit rows ===

async def test_connecting_an_integration_writes_no_secret_to_the_audit_log(
    client, db, owner_a
):
    """Requirement 22: CRM secret absent from audit records."""
    headers = await auth_headers(client, owner_a)
    response = await client.put(
        "/api/integrations/crm/hubspot",
        headers=headers,
        json={"credentials": {"access_token": "pat-na1-AUDIT-LEAK-TEST"}},
    )
    assert response.status_code == 200

    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.tenant_id == owner_a.tenant_id)))
        .scalars()
        .all()
    )
    assert rows
    everything = json.dumps([r.detail for r in rows])
    assert "pat-na1-AUDIT-LEAK-TEST" not in everything
    # The *names* of the supplied fields are recorded, which is what makes the
    # audit trail useful without making it dangerous.
    assert "access_token" in everything


async def test_no_secret_reaches_the_structured_log(
    client, db, owner_a, monkeypatch
):
    """Requirement 22: CRM secret absent from logs."""
    captured: list[tuple] = []

    from app.core import logging as app_logging

    for level in ("info", "warning", "error"):
        original = getattr(app_logging.log, level)

        def spy(event, _level=level, _orig=original, **kw):
            captured.append((event, kw))
            return _orig(event, **kw)

        monkeypatch.setattr(app_logging.log, level, spy)

    headers = await auth_headers(client, owner_a)
    await client.put(
        "/api/integrations/crm/hubspot",
        headers=headers,
        json={"credentials": {"access_token": "pat-na1-LOG-LEAK-TEST"}},
    )

    assert captured, "nothing was logged, so the assertion would be vacuous"
    assert "pat-na1-LOG-LEAK-TEST" not in json.dumps(captured, default=str)
