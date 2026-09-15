"""Role-based access control: the permission table and its route enforcement."""
from __future__ import annotations

import pytest

from app.auth import rbac
from app.auth.permissions import Permission
from app.db.models import UserRole
from tests.conftest import auth_headers

# --------------------------------------------------------- pure rbac data ---

def test_every_role_has_an_entry():
    assert set(rbac.ROLE_PERMISSIONS) == set(UserRole)
    assert set(rbac.ROLE_LEVEL) == set(UserRole)


def test_permissions_are_strictly_ordered_owner_down_to_viewer():
    order = [UserRole.OWNER, UserRole.ADMIN, UserRole.MANAGER,
             UserRole.AGENT, UserRole.VIEWER]
    for higher, lower in zip(order, order[1:]):
        assert rbac.ROLE_LEVEL[higher] > rbac.ROLE_LEVEL[lower]


def test_viewer_has_no_write_permission():
    for perm in rbac.ROLE_PERMISSIONS[UserRole.VIEWER]:
        assert not any(
            perm.value.endswith(v)
            for v in (":create", ":update", ":delete", ":write", ":run")
        ), perm


def test_only_owner_and_admin_can_manage_users():
    for role in UserRole:
        allowed = rbac.has_permission(role, Permission.USER_CREATE)
        assert allowed is (role in (UserRole.OWNER, UserRole.ADMIN))


def test_platform_only_permissions_are_denied_to_everyone_including_owner():
    for role in UserRole:
        assert not rbac.has_permission(role, Permission.TENANT_CREATE)
        assert not rbac.has_permission(role, Permission.TENANT_DELETE)


def test_campaign_run_is_not_granted_to_agent_or_viewer():
    assert rbac.has_permission(UserRole.MANAGER, Permission.CAMPAIGN_RUN)
    assert not rbac.has_permission(UserRole.AGENT, Permission.CAMPAIGN_RUN)
    assert not rbac.has_permission(UserRole.VIEWER, Permission.CAMPAIGN_RUN)


@pytest.mark.parametrize("actor,target,ok", [
    (UserRole.OWNER, UserRole.ADMIN, True),
    (UserRole.OWNER, UserRole.OWNER, False),     # cannot clone your own level
    (UserRole.ADMIN, UserRole.MANAGER, True),
    (UserRole.ADMIN, UserRole.ADMIN, False),     # no lateral escalation
    (UserRole.ADMIN, UserRole.OWNER, False),     # no upward escalation
    (UserRole.MANAGER, UserRole.AGENT, False),   # managers do not manage users
])
def test_can_assign_role(actor, target, ok):
    assert rbac.can_assign_role(actor, target) is ok


def test_describe_roles_is_serializable_and_complete():
    described = rbac.describe_roles()
    assert {e["role"] for e in described} == {r.value for r in UserRole}
    for entry in described:
        assert isinstance(entry["permissions"], list) and entry["level"] > 0


# ------------------------------------------------------ route enforcement ---

@pytest.mark.asyncio
async def test_viewer_can_read_calls(client, tenant_a, viewer_a):
    headers = await auth_headers(client, viewer_a)
    r = await client.get(f"/api/tenants/{tenant_a.id}/calls", headers=headers)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_viewer_cannot_change_voice_settings(client, tenant_a, viewer_a):
    headers = await auth_headers(client, viewer_a)
    r = await client.patch(
        f"/api/tenants/{tenant_a.id}/voice", json={"vad_stop_secs": 0.6},
        headers=headers,
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_agent_cannot_run_a_campaign(client, db, tenant_a, agent_a):
    from app.db.models import Campaign
    campaign = Campaign(tenant_id=tenant_a.id, name="Q4", script_prompt="hi")
    db.add(campaign)
    await db.commit()

    headers = await auth_headers(client, agent_a)
    r = await client.post(
        f"/api/tenants/{tenant_a.id}/campaigns/{campaign.id}/run", headers=headers
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_manager_can_run_a_campaign_in_dry_run(client, db, tenant_a, manager_a):
    from app.db.models import Campaign
    campaign = Campaign(tenant_id=tenant_a.id, name="Q4", script_prompt="hi")
    db.add(campaign)
    await db.commit()

    headers = await auth_headers(client, manager_a)
    r = await client.post(
        f"/api/tenants/{tenant_a.id}/campaigns/{campaign.id}/run?dry_run=true",
        headers=headers,
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_agent_cannot_list_team_members(client, agent_a):
    headers = await auth_headers(client, agent_a)
    assert (await client.get("/api/team/users", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_owner_can_list_team_members(client, owner_a):
    headers = await auth_headers(client, owner_a)
    assert (await client.get("/api/team/users", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_denied_requests_are_audited(client, db, viewer_a, tenant_a):
    from sqlalchemy import select

    from app.db.models import AuditAction, AuditLog

    headers = await auth_headers(client, viewer_a)
    await client.patch(
        f"/api/tenants/{tenant_a.id}/voice", json={"vad_stop_secs": 0.6},
        headers=headers,
    )
    rows = (await db.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.AUTHZ_DENIED)
    )).scalars().all()
    assert rows, "an authorization denial must be recorded"


@pytest.mark.asyncio
async def test_no_endpoint_accepts_a_role_claim_from_the_client(client, viewer_a, tenant_a):
    """Escalating by asserting a role in the body must not work."""
    headers = await auth_headers(client, viewer_a)
    r = await client.patch(
        f"/api/tenants/{tenant_a.id}/voice",
        json={"vad_stop_secs": 0.6, "role": "owner"},
        headers=headers,
    )
    assert r.status_code == 403


# ------------------------------------------- machine (non-human) surfaces ---

def test_stream_token_is_bound_to_one_call_and_expires():
    from app.telephony.stream_auth import create_stream_token, verify_stream_token

    token = create_stream_token("CA-real")
    assert verify_stream_token("CA-real", token) is True
    assert verify_stream_token("CA-other", token) is False        # replay
    # Flip the last character to something it is not. `token[:-1] + "0"` was
    # wrong: the digest ends in "0" about 6% of the time, and in those runs the
    # "tampered" token was byte-identical to the real one, so the assertion
    # tested nothing and the suite failed roughly one run in seventeen.
    tampered = token[:-1] + ("1" if token[-1] == "0" else "0")
    assert tampered != token
    assert verify_stream_token("CA-real", tampered) is False      # tamper
    assert verify_stream_token("CA-real", None) is False
    assert verify_stream_token("CA-real", "garbage") is False
    stale = create_stream_token("CA-real", issued_at=1_000)
    assert verify_stream_token("CA-real", stale) is False         # expired


def test_twilio_signature_is_enforced_when_verification_is_enabled(monkeypatch):
    """The messaging webhook used to accept anything. It must not."""
    import asyncio

    from app.core.config import settings
    from app.telephony.stream_auth import verify_twilio_request

    class FakeRequest:
        url = "https://example.com/channels/message"
        headers = {"X-Twilio-Signature": "obviously-wrong"}

        async def form(self):
            return {"From": "+15550001", "Body": "hi"}

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "twilio_skip_webhook_verify", False)
    assert asyncio.run(verify_twilio_request(FakeRequest())) is False