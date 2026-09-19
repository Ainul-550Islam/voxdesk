"""Team management: creation, role change, deactivation, owner protection."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import AuditAction, AuditLog, User, UserRole
from tests.conftest import auth_headers, login, make_user

NEW = {"email": "new.hire@example.com", "password": "Strong-Passphrase-7!",
       "full_name": "New Hire", "role": "agent"}


@pytest.mark.asyncio
async def test_owner_can_create_a_user_who_can_then_log_in(client, owner_a):
    headers = await auth_headers(client, owner_a)
    r = await client.post("/api/team/users", json=NEW, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["email"] == "new.hire@example.com"

    ok = await login(client, "new.hire@example.com", NEW["password"])
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_created_user_lands_in_the_creators_tenant_only(client, db, owner_a, tenant_b):
    headers = await auth_headers(client, owner_a)
    payload = dict(NEW, tenant_id=str(tenant_b.id))     # attempted injection
    await client.post("/api/team/users", json=payload, headers=headers)

    user = (await db.execute(
        select(User).where(User.email == NEW["email"])
    )).scalar_one()
    assert user.tenant_id == owner_a.tenant_id


@pytest.mark.asyncio
async def test_create_response_never_exposes_the_hash(client, owner_a):
    headers = await auth_headers(client, owner_a)
    r = await client.post("/api/team/users", json=NEW, headers=headers)
    assert "password_hash" not in r.text and "$2b$" not in r.text
    assert NEW["password"] not in r.text


@pytest.mark.asyncio
async def test_weak_password_is_rejected(client, owner_a):
    headers = await auth_headers(client, owner_a)
    r = await client.post(
        "/api/team/users", json=dict(NEW, password="password"), headers=headers
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_email_is_a_conflict(client, owner_a):
    headers = await auth_headers(client, owner_a)
    await client.post("/api/team/users", json=NEW, headers=headers)
    r = await client.post("/api/team/users", json=NEW, headers=headers)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_email_is_normalized_before_storage(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    await client.post(
        "/api/team/users", json=dict(NEW, email="  MiXeD.Case@Example.COM "),
        headers=headers,
    )
    user = (await db.execute(
        select(User).where(User.email == "mixed.case@example.com")
    )).scalar_one_or_none()
    assert user is not None


@pytest.mark.asyncio
async def test_admin_cannot_create_an_owner(client, admin_a):
    headers = await auth_headers(client, admin_a)
    r = await client.post("/api/team/users", json=dict(NEW, role="owner"), headers=headers)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_owner_can_change_a_role(client, db, tenant_a, owner_a):
    target = await make_user(db, tenant_a, UserRole.AGENT)
    headers = await auth_headers(client, owner_a)
    r = await client.patch(
        f"/api/team/users/{target.id}/role", json={"role": "manager"}, headers=headers
    )
    assert r.status_code == 200
    await db.refresh(target)
    assert target.role == UserRole.MANAGER


@pytest.mark.asyncio
async def test_role_change_forces_reauthentication(client, db, tenant_a, owner_a):
    target = await make_user(db, tenant_a, UserRole.MANAGER)
    stale = await auth_headers(client, target)
    owner_headers = await auth_headers(client, owner_a)

    await client.patch(
        f"/api/team/users/{target.id}/role", json={"role": "viewer"},
        headers=owner_headers,
    )
    # The old token still claims 'manager', so it must be rejected outright.
    assert (await client.get("/auth/me", headers=stale)).status_code == 401


@pytest.mark.asyncio
async def test_the_last_owner_cannot_be_demoted(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    r = await client.patch(
        f"/api/team/users/{owner_a.id}/role", json={"role": "admin"}, headers=headers
    )
    assert r.status_code in (403, 409)
    await db.refresh(owner_a)
    assert owner_a.role == UserRole.OWNER


@pytest.mark.asyncio
async def test_the_last_owner_cannot_be_deactivated(client, db, owner_a):
    headers = await auth_headers(client, owner_a)
    r = await client.patch(
        f"/api/team/users/{owner_a.id}/active", json={"is_active": False}, headers=headers
    )
    assert r.status_code in (403, 409)
    await db.refresh(owner_a)
    assert owner_a.is_active is True


@pytest.mark.asyncio
async def test_a_second_owner_makes_demotion_possible(client, db, tenant_a, owner_a):
    second = await make_user(db, tenant_a, UserRole.OWNER)
    headers = await auth_headers(client, owner_a)
    r = await client.patch(
        f"/api/team/users/{second.id}/role", json={"role": "admin"}, headers=headers
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_admin_cannot_deactivate_an_owner(client, db, tenant_a, admin_a, owner_a):
    headers = await auth_headers(client, admin_a)
    r = await client.patch(
        f"/api/team/users/{owner_a.id}/active", json={"is_active": False}, headers=headers
    )
    assert r.status_code == 403
    await db.refresh(owner_a)
    assert owner_a.is_active is True


@pytest.mark.asyncio
async def test_deactivated_user_cannot_log_in_again(client, db, tenant_a, owner_a):
    target = await make_user(db, tenant_a, UserRole.AGENT)
    headers = await auth_headers(client, owner_a)
    await client.patch(
        f"/api/team/users/{target.id}/active", json={"is_active": False}, headers=headers
    )
    assert (await login(client, target.email)).status_code == 401


@pytest.mark.asyncio
async def test_user_list_is_free_of_sensitive_fields(client, db, tenant_a, owner_a):
    await make_user(db, tenant_a, UserRole.AGENT)
    headers = await auth_headers(client, owner_a)
    r = await client.get("/api/team/users", headers=headers)
    assert r.status_code == 200
    assert "$2b$" not in r.text
    for user in r.json():
        assert set(user) <= {
            "id", "tenant_id", "email", "full_name", "role", "is_active",
            "created_at", "updated_at", "last_login_at",
        }, user


@pytest.mark.asyncio
async def test_management_actions_are_audited(client, db, tenant_a, owner_a):
    headers = await auth_headers(client, owner_a)
    created = (await client.post("/api/team/users", json=NEW, headers=headers)).json()
    await client.patch(
        f"/api/team/users/{created['id']}/role", json={"role": "manager"}, headers=headers
    )
    await client.patch(
        f"/api/team/users/{created['id']}/active", json={"is_active": False},
        headers=headers,
    )

    actions = {
        r.action for r in
        (await db.execute(select(AuditLog))).scalars().all()
    }
    assert AuditAction.USER_CREATED in actions
    assert AuditAction.ROLE_CHANGED in actions
    assert AuditAction.USER_DEACTIVATED in actions


@pytest.mark.asyncio
async def test_audit_feed_is_scoped_to_the_tenant(client, db, tenant_a, tenant_b,
                                                  owner_a, owner_b):
    await login(client, owner_b.email)          # generates tenant B audit rows
    headers = await auth_headers(client, owner_a)
    rows = (await client.get("/api/team/audit", headers=headers)).json()
    assert all(r.get("actor_email") != owner_b.email for r in rows)