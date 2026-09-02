"""
Tenant isolation.

Every test here drives the real router stack with a real token. Nothing in the
authorization path is mocked -- if it were, these tests would prove nothing
about whether tenant A can reach tenant B's rows.

The contract under test: cross-tenant access returns 404, never 403, because
403 would confirm that the resource exists.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.db.models import (
    Call,
    CallStatus,
    Campaign,
    Lead,
    Speaker,
    Tenant,
    Turn,
    UserRole,
)
from tests.conftest import auth_headers, make_user


async def seed_call(db, tenant, sid="CA-x"):
    call = Call(
        tenant_id=tenant.id, call_sid=f"{sid}-{uuid.uuid4().hex[:6]}",
        from_number="+15550001", to_number="+15550002",
        direction="inbound", status=CallStatus.COMPLETED,
        started_at=datetime.utcnow(),
    )
    db.add(call)
    await db.commit()
    await db.refresh(call)
    db.add(Turn(call_id=call.id, speaker=Speaker.USER, text="secret medical detail"))
    await db.commit()
    return call


async def seed_lead(db, tenant, phone="+15559999"):
    lead = Lead(tenant_id=tenant.id, phone=f"{phone}{uuid.uuid4().int % 100}", name="Bob")
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    return lead


# ============================================================================
# ATTACK 1 -- swap the tenant_id in the URL for another tenant's id
# ============================================================================
@pytest.mark.asyncio
async def test_attack_1_tenant_id_swap_in_path(client, db, tenant_a, tenant_b, owner_a):
    await seed_call(db, tenant_b, "CA-b")
    headers = await auth_headers(client, owner_a)

    for path in (
        f"/api/tenants/{tenant_b.id}/calls",
        f"/api/tenants/{tenant_b.id}/leads",
        f"/api/tenants/{tenant_b.id}/campaigns",
        f"/api/tenants/{tenant_b.id}/stats",
        f"/api/tenants/{tenant_b.id}/compliance",
        f"/api/tenants/{tenant_b.id}/ivr",
    ):
        r = await client.get(path, headers=headers)
        assert r.status_code == 404, f"{path} leaked with {r.status_code}"
        assert "Tenant B" not in r.text


# ============================================================================
# ATTACK 2 -- fetch another tenant's call/transcript by its real id
# ============================================================================
@pytest.mark.asyncio
async def test_attack_2_cross_tenant_call_id(client, db, tenant_a, tenant_b, owner_a):
    victim_call = await seed_call(db, tenant_b, "CA-victim")
    headers = await auth_headers(client, owner_a)

    r = await client.get(f"/api/calls/{victim_call.id}/transcript", headers=headers)
    assert r.status_code == 404
    assert "secret medical detail" not in r.text


@pytest.mark.asyncio
async def test_own_transcript_is_still_readable(client, db, tenant_a, owner_a):
    own = await seed_call(db, tenant_a, "CA-own")
    headers = await auth_headers(client, owner_a)
    r = await client.get(f"/api/calls/{own.id}/transcript", headers=headers)
    assert r.status_code == 200
    assert "secret medical detail" in r.text


# ============================================================================
# ATTACK 3 -- send tenant_id / owner_id in a request body to reassign a record
# ============================================================================
@pytest.mark.asyncio
async def test_attack_3_body_tenant_id_is_ignored(client, db, tenant_a, tenant_b, owner_a):
    headers = await auth_headers(client, owner_a)
    r = await client.post(
        f"/api/tenants/{tenant_a.id}/leads",
        json={"leads": [{"phone": "+15551230000", "name": "Injected"}],
              "tenant_id": str(tenant_b.id), "owner_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert r.status_code in (200, 201)

    rows = (await db.execute(
        select(Lead).where(Lead.name == "Injected")
    )).scalars().all()
    assert rows and all(l.tenant_id == tenant_a.id for l in rows)


# ============================================================================
# ATTACK 4 -- change your own role by sending role in a body
# ============================================================================
@pytest.mark.asyncio
async def test_attack_4_self_role_escalation_via_body(client, db, tenant_a, agent_a):
    headers = await auth_headers(client, agent_a)

    r = await client.patch(
        f"/api/team/users/{agent_a.id}/role", json={"role": "owner"}, headers=headers
    )
    assert r.status_code == 403

    await db.refresh(agent_a)
    assert agent_a.role == UserRole.AGENT


@pytest.mark.asyncio
async def test_admin_cannot_promote_anyone_to_owner(client, db, tenant_a, admin_a):
    target = await make_user(db, tenant_a, UserRole.AGENT)
    headers = await auth_headers(client, admin_a)
    r = await client.patch(
        f"/api/team/users/{target.id}/role", json={"role": "owner"}, headers=headers
    )
    assert r.status_code == 403
    await db.refresh(target)
    assert target.role == UserRole.AGENT


# ============================================================================
# ATTACK 5 -- PATCH another tenant's configuration
# ============================================================================
@pytest.mark.asyncio
async def test_attack_5_cross_tenant_patch(client, db, tenant_a, tenant_b, owner_a):
    before = tenant_b.llm_preset
    headers = await auth_headers(client, owner_a)

    r = await client.patch(
        f"/api/tenants/{tenant_b.id}/voice", json={"llm_preset": "fast"}, headers=headers
    )
    assert r.status_code == 404

    fresh = await db.get(Tenant, tenant_b.id)
    await db.refresh(fresh)
    assert fresh.llm_preset == before, "another tenant's config was modified"


# ============================================================================
# ATTACK 6 -- a viewer performs a destructive/mutating action
# ============================================================================
@pytest.mark.asyncio
async def test_attack_6_viewer_mutation_is_denied(client, db, tenant_a, viewer_a):
    lead = await seed_lead(db, tenant_a)
    headers = await auth_headers(client, viewer_a)

    assert (await client.post(
        f"/api/tenants/{tenant_a.id}/leads",
        json={"leads": [{"phone": "+15550000123"}]}, headers=headers,
    )).status_code == 403

    assert (await client.post(
        f"/api/tenants/{tenant_a.id}/leads/{lead.id}/do-not-call", headers=headers
    )).status_code == 403

    assert (await client.put(
        f"/api/tenants/{tenant_a.id}/ivr", json={"flow": {}}, headers=headers
    )).status_code == 403


# ============================================================================
# ATTACK 7 -- an agent tries to manage users
# ============================================================================
@pytest.mark.asyncio
async def test_attack_7_agent_manage_users(client, db, tenant_a, agent_a):
    victim = await make_user(db, tenant_a, UserRole.VIEWER)
    headers = await auth_headers(client, agent_a)

    assert (await client.get("/api/team/users", headers=headers)).status_code == 403
    assert (await client.post(
        "/api/team/users",
        json={"email": "new@example.com", "password": "Correct-Horse-9!x",
              "role": "viewer"},
        headers=headers,
    )).status_code == 403
    assert (await client.patch(
        f"/api/team/users/{victim.id}/active", json={"is_active": False}, headers=headers
    )).status_code == 403
    assert (await client.get("/api/team/audit", headers=headers)).status_code == 403


# ============================================================================
# ATTACK 8 -- an admin of tenant A manages a user of tenant B
# ============================================================================
@pytest.mark.asyncio
async def test_attack_8_admin_cross_tenant_user_management(
    client, db, tenant_a, tenant_b, admin_a, owner_b
):
    victim = await make_user(db, tenant_b, UserRole.AGENT)
    headers = await auth_headers(client, admin_a)

    assert (await client.patch(
        f"/api/team/users/{victim.id}/role", json={"role": "viewer"}, headers=headers
    )).status_code == 404
    assert (await client.patch(
        f"/api/team/users/{victim.id}/active", json={"is_active": False}, headers=headers
    )).status_code == 404

    await db.refresh(victim)
    assert victim.role == UserRole.AGENT and victim.is_active is True


@pytest.mark.asyncio
async def test_team_list_only_shows_own_tenant(client, db, tenant_a, tenant_b,
                                               owner_a, owner_b):
    await make_user(db, tenant_b, UserRole.AGENT, email="stranger@example.com")
    headers = await auth_headers(client, owner_a)
    body = (await client.get("/api/team/users", headers=headers)).json()
    emails = {u["email"] for u in body}
    assert owner_a.email in emails
    assert "stranger@example.com" not in emails
    assert owner_b.email not in emails


# ============================================================================
# ATTACK 9 (bonus) -- anonymous access to every protected route
# ============================================================================
@pytest.mark.asyncio
async def test_attack_9_anonymous_access_is_refused_everywhere(client, db, tenant_a):
    call = await seed_call(db, tenant_a)
    lead = await seed_lead(db, tenant_a)
    campaign = Campaign(tenant_id=tenant_a.id, name="C", script_prompt="x")
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)

    requests = [
        ("get", "/api/tenants"),
        ("post", "/api/tenants"),
        ("get", f"/api/tenants/{tenant_a.id}/calls"),
        ("get", f"/api/calls/{call.id}/transcript"),
        ("get", f"/api/tenants/{tenant_a.id}/stats"),
        ("get", f"/api/tenants/{tenant_a.id}/leads"),
        ("post", f"/api/tenants/{tenant_a.id}/leads"),
        ("post", f"/api/tenants/{tenant_a.id}/leads/{lead.id}/do-not-call"),
        ("get", f"/api/tenants/{tenant_a.id}/campaigns"),
        ("post", f"/api/tenants/{tenant_a.id}/campaigns"),
        ("post", f"/api/tenants/{tenant_a.id}/campaigns/{campaign.id}/run"),
        ("patch", f"/api/tenants/{tenant_a.id}/voice"),
        ("get", f"/api/tenants/{tenant_a.id}/ivr"),
        ("put", f"/api/tenants/{tenant_a.id}/ivr"),
        ("get", f"/api/tenants/{tenant_a.id}/compliance"),
        ("get", "/api/team/users"),
        ("post", "/api/team/users"),
        ("get", "/api/team/audit"),
        ("get", "/auth/me"),
    ]
    for method, path in requests:
        kwargs = {} if method == "get" else {"json": {}}
        r = await getattr(client, method)(path, **kwargs)
        assert r.status_code in (401, 403), f"{method.upper()} {path} -> {r.status_code}"


# ---------------------------------------------------- non-attack invariants ---

@pytest.mark.asyncio
async def test_list_tenants_returns_only_your_own(client, tenant_a, tenant_b, owner_a):
    headers = await auth_headers(client, owner_a)
    body = (await client.get("/api/tenants", headers=headers)).json()
    assert [t["id"] for t in body] == [str(tenant_a.id)]


@pytest.mark.asyncio
async def test_lists_never_bleed_rows_across_tenants(client, db, tenant_a, tenant_b, owner_a):
    await seed_call(db, tenant_a, "CA-mine")
    await seed_call(db, tenant_b, "CA-theirs")
    await seed_lead(db, tenant_b, "+15558888")

    headers = await auth_headers(client, owner_a)
    calls = (await client.get(f"/api/tenants/{tenant_a.id}/calls", headers=headers)).json()
    leads = (await client.get(f"/api/tenants/{tenant_a.id}/leads", headers=headers)).json()

    # STEP 8 made the call list a paginated envelope -- a pager needs a total,
    # and adding one later would have broken every client. The isolation
    # guarantee this test exists for is unchanged, and is now asserted on the
    # total as well as the page.
    assert len(calls["calls"]) == 1
    assert calls["total"] == 1
    assert leads == []


@pytest.mark.asyncio
async def test_nonexistent_random_uuid_also_returns_404(client, owner_a):
    headers = await auth_headers(client, owner_a)
    r = await client.get(f"/api/tenants/{uuid.uuid4()}/calls", headers=headers)
    assert r.status_code == 404

