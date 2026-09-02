"""
STEP 8 phase 2: the agent configuration read endpoint.

`PATCH /api/tenants/{id}/voice` has existed since v0.5, but nothing could read
the configuration back. These cover the new `GET /api/tenants/{id}/agent`:
that it is tenant-scoped, that every role may read it, and above all that it
never serialises a credential.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth_headers

pytestmark = pytest.mark.asyncio


# Columns on `Tenant` that hold, or could hold, a secret. None of these may
# ever appear in the response body -- it is readable by every role, viewer
# included.
FORBIDDEN_FIELDS = (
    "crm_api_key",
    "crm_webhook_url",
    "a2p_brand_sid",
    "a2p_campaign_sid",
    "google_calendar_id",
)


async def test_owner_can_read_the_agent_configuration(client, owner_a, tenant_a):
    headers = await auth_headers(client, owner_a)
    resp = await client.get(f"/api/tenants/{tenant_a.id}/agent", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_name"] == tenant_a.agent_name
    assert body["greeting"] == tenant_a.greeting
    assert body["llm_preset"] == tenant_a.llm_preset
    assert body["timezone"] == tenant_a.timezone


async def test_it_returns_the_fields_the_dashboard_needs(client, owner_a, tenant_a):
    headers = await auth_headers(client, owner_a)
    body = (
        await client.get(f"/api/tenants/{tenant_a.id}/agent", headers=headers)
    ).json()

    # The six writable ones must be readable, or the form cannot be seeded.
    for field in (
        "llm_preset", "temperature", "voice_id", "humanize", "vad_stop_secs",
        "speech_speed",
    ):
        assert field in body, field

    # Plus the read-only context the page displays.
    for field in (
        "agent_name", "greeting", "system_prompt_extra", "language",
        "business_open", "business_close", "appointment_minutes",
        "escalation_number", "record_calls", "recording_disclaimer",
        "sms_enabled", "whatsapp_enabled", "ivr_enabled", "twilio_number",
    ):
        assert field in body, field


async def test_no_credential_is_ever_serialised(client, owner_a, tenant_a, db):
    """The response is readable by every role, so it must carry no secret."""
    tenant_a.crm_api_key = "sk-live-should-never-be-returned"
    tenant_a.a2p_brand_sid = "BN-secret-brand"
    tenant_a.crm_webhook_url = "https://hooks.example.com/secret-path"
    await db.commit()

    headers = await auth_headers(client, owner_a)
    resp = await client.get(f"/api/tenants/{tenant_a.id}/agent", headers=headers)

    body = resp.json()
    for field in FORBIDDEN_FIELDS:
        assert field not in body, f"{field} must not be serialised"

    # Belt and braces: the secret's *value* must not appear anywhere either.
    raw = resp.text
    assert "sk-live-should-never-be-returned" not in raw
    assert "BN-secret-brand" not in raw
    assert "secret-path" not in raw


async def test_every_role_may_read_it(
    client, tenant_a, owner_a, admin_a, manager_a, agent_a, viewer_a
):
    """`tenant:read` is in READ_ONLY_OPERATIONAL, so no role is shut out."""
    # Requested as explicit fixtures rather than `getfixturevalue`, which
    # cannot resolve an async fixture from inside a running loop.
    for user in (owner_a, admin_a, manager_a, agent_a, viewer_a):
        headers = await auth_headers(client, user)
        resp = await client.get(
            f"/api/tenants/{tenant_a.id}/agent", headers=headers
        )
        assert resp.status_code == 200, f"{user.role}: {resp.text}"


async def test_anonymous_access_is_refused(client, tenant_a):
    resp = await client.get(f"/api/tenants/{tenant_a.id}/agent")
    assert resp.status_code in (401, 403)


async def test_another_tenants_id_in_the_path_is_refused(
    client, owner_a, tenant_b
):
    """
    The path parameter is a consistency check, never a selector.

    `scoped_permission` answers 404 rather than 403 here, deliberately: a 403
    would confirm that the other tenant exists. The important half is that no
    data crosses.
    """
    headers = await auth_headers(client, owner_a)
    resp = await client.get(f"/api/tenants/{tenant_b.id}/agent", headers=headers)

    assert resp.status_code == 404
    assert tenant_b.name not in resp.text


async def test_it_returns_the_callers_own_tenant_not_the_path_one(
    client, owner_a, tenant_a
):
    headers = await auth_headers(client, owner_a)
    body = (
        await client.get(f"/api/tenants/{tenant_a.id}/agent", headers=headers)
    ).json()

    assert body["id"] == str(tenant_a.id)


async def test_a_viewer_cannot_write_even_though_they_can_read(
    client, viewer_a, tenant_a
):
    """Read is `tenant:read`; write stays `tenant:update`, admin and above."""
    headers = await auth_headers(client, viewer_a)

    read = await client.get(f"/api/tenants/{tenant_a.id}/agent", headers=headers)
    assert read.status_code == 200

    write = await client.patch(
        f"/api/tenants/{tenant_a.id}/voice",
        json={"temperature": 1.5},
        headers=headers,
    )
    assert write.status_code == 403


async def test_a_saved_change_is_visible_on_the_next_read(
    client, admin_a, tenant_a
):
    """The write endpoint and the new read endpoint agree with each other."""
    headers = await auth_headers(client, admin_a)

    await client.patch(
        f"/api/tenants/{tenant_a.id}/voice",
        json={"temperature": 1.25, "speech_speed": 1.1},
        headers=headers,
    )

    body = (
        await client.get(f"/api/tenants/{tenant_a.id}/agent", headers=headers)
    ).json()
    assert body["temperature"] == pytest.approx(1.25)
    assert body["speech_speed"] == pytest.approx(1.1)
