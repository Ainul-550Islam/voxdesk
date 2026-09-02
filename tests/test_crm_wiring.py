"""
Call / lead / appointment wiring, the configuration API, and inbound webhooks.

Requirements 6, 18, 19, 20, 21, 24, 28.

The wiring tests use the real database and the real lifecycle functions. The
question they answer is not "does `emit()` work" — that is covered elsewhere —
but "does a completed call actually reach the CRM layer, and does a CRM
failure stay invisible to the caller".
"""
from __future__ import annotations

import json
import time

import httpx
import pytest
from sqlalchemy import func, select

from app.db.models import (
    CallStatus,
    CrmEvent,
    CrmEventType,
    CrmProviderType,
    CrmSync,
    CrmSyncStatus,
    CrmWebhookReceipt,
)
from app.integrations.crm import hooks, service
from tests.conftest import (
    FakeTransport,
    auth_headers,
    make_appointment,
    make_call,
    make_integration,
    make_lead,
)


async def _events_for(db, tenant):
    return (
        (
            await db.execute(
                select(CrmEvent)
                .where(CrmEvent.tenant_id == tenant.id)
                .order_by(CrmEvent.created_at)
            )
        )
        .scalars()
        .all()
    )


# ======================================================= call -> CRM (18) ===

class TestCallWiring:
    async def test_a_completed_call_emits_an_event(self, db, tenant_a):
        call = await make_call(db, tenant_a)
        assert await hooks.on_call_completed(db, tenant_a, call)
        await db.commit()

        rows = await _events_for(db, tenant_a)
        assert len(rows) == 1
        assert rows[0].event_type is CrmEventType.CALL_COMPLETED
        assert rows[0].entity_id == call.id

    async def test_a_missed_call_emits_an_event(self, db, tenant_a):
        call = await make_call(
            db, tenant_a, status=CallStatus.NO_ANSWER, booked=False,
            duration_seconds=0.0, summary="",
        )
        assert await hooks.on_call_missed(db, tenant_a, call)
        await db.commit()

        rows = await _events_for(db, tenant_a)
        assert rows[0].event_type is CrmEventType.CALL_MISSED

    async def test_a_completed_transfer_emits_an_event(self, db, tenant_a):
        call = await make_call(db, tenant_a, escalated=True)
        assert await hooks.on_transfer_completed(db, tenant_a, call)
        await db.commit()

        rows = await _events_for(db, tenant_a)
        assert rows[0].event_type is CrmEventType.TRANSFER_COMPLETED
        assert "transfer_state" in rows[0].payload

    async def test_the_payload_carries_the_safe_call_metadata(self, db, tenant_a):
        call = await make_call(db, tenant_a)
        await hooks.on_call_completed(db, tenant_a, call)
        await db.commit()

        payload = (await _events_for(db, tenant_a))[0].payload
        for key in (
            "call_id", "call_sid", "direction", "duration_seconds", "intent",
            "summary", "booked", "transferred", "lead_score",
        ):
            assert key in payload, key
        assert payload["booked"] is True
        assert payload["duration_seconds"] == 92.5

    async def test_the_payload_carries_a_transcript_reference_not_a_transcript(
        self, db, tenant_a
    ):
        """
        Requirement 18: do not blindly send full transcripts to all providers.
        A call transcript is the most sensitive thing this product holds.
        """
        from app.db.models import Speaker, Turn

        call = await make_call(db, tenant_a)
        db.add(Turn(
            call_id=call.id, speaker=Speaker.USER,
            text="My card number is 4111 1111 1111 1111",
        ))
        await db.commit()

        await hooks.on_call_completed(db, tenant_a, call)
        await db.commit()

        payload = (await _events_for(db, tenant_a))[0].payload
        assert payload["transcript_reference"] == f"voxdesk:call:{call.id}:turns"
        assert "4111" not in json.dumps(payload)
        assert "turns" not in {k for k in payload if k != "transcript_reference"}

    async def test_the_transcript_reference_is_withheld_unless_opted_in(
        self, db, tenant_a
    ):
        """
        Requirement 18: transcript sharing must be configurable. The reference
        only reaches a provider whose integration has `share_transcripts`.
        """
        from app.integrations.crm.service import _sources_for

        call = await make_call(db, tenant_a)
        await hooks.on_call_completed(db, tenant_a, call)
        await db.commit()
        event = (await _events_for(db, tenant_a))[0]

        opted_out = await make_integration(db, tenant_a, "webhook")
        assert "transcript_reference" not in _sources_for(
            event, event.payload, opted_out
        )

        opted_in = await make_integration(
            db, tenant_a, "hubspot", share_transcripts=True
        )
        assert "transcript_reference" in _sources_for(
            event, event.payload, opted_in
        )

    async def test_a_crm_failure_never_reaches_the_caller(self, db, tenant_a):
        """
        Requirement 28. Simulated by making the emit path explode; the hook
        must swallow it and report False rather than raising into the voice
        pipeline.
        """
        import app.integrations.crm.events as events_module

        call = await make_call(db, tenant_a)

        async def boom(*args, **kwargs):
            raise RuntimeError("the CRM layer is on fire")

        original = events_module.emit
        events_module.emit = boom
        try:
            assert await hooks.on_call_completed(db, tenant_a, call) is False
        finally:
            events_module.emit = original

    async def test_the_hook_does_not_commit(self, db, tenant_a):
        """
        The event must land in the same transaction as the business change,
        so a hook that commits early would break atomicity.
        """
        call = await make_call(db, tenant_a)
        await hooks.on_call_completed(db, tenant_a, call)

        # Capture the id first: rollback expires every loaded object, and
        # reading `tenant_a.id` afterwards would lazy-load and raise
        # MissingGreenlet rather than testing anything.
        tenant_id = tenant_a.id
        await db.rollback()

        remaining = (
            (
                await db.execute(
                    select(CrmEvent).where(CrmEvent.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []


# ======================================================= lead -> CRM (19) ===

class TestLeadWiring:
    async def test_lead_creation_emits(self, db, tenant_a):
        lead = await make_lead(db, tenant_a)
        assert await hooks.on_lead_created(db, lead)
        await db.commit()

        rows = await _events_for(db, tenant_a)
        assert rows[0].event_type is CrmEventType.LEAD_CREATED
        assert rows[0].payload["phone"] == "+15557778888"

    async def test_lead_updates_are_distinguished_by_reason(self, db, tenant_a):
        """
        Unlike creation, an update genuinely recurs. Without a discriminator
        the second real change would be swallowed as a duplicate.
        """
        lead = await make_lead(db, tenant_a)

        assert await hooks.on_lead_updated(db, lead, reason="status:qualified")
        assert await hooks.on_lead_updated(db, lead, reason="status:do_not_call")
        # ...but the same change retried is still one event.
        assert not await hooks.on_lead_updated(db, lead, reason="status:qualified")
        await db.commit()

        rows = await _events_for(db, tenant_a)
        assert len(rows) == 2

    async def test_bulk_import_emits_one_event_per_lead(
        self, client, db, owner_a, tenant_a
    ):
        headers = await auth_headers(client, owner_a)
        response = await client.post(
            f"/api/tenants/{tenant_a.id}/leads",
            headers=headers,
            json={"leads": [
                {"name": "A One", "phone": "+15550000001"},
                {"name": "B Two", "phone": "+15550000002"},
                {"name": "C Three", "phone": "+15550000003"},
            ]},
        )
        assert response.status_code in (200, 201), response.text
        assert response.json()["created"] == 3

        rows = await _events_for(db, tenant_a)
        assert len(rows) == 3
        assert all(r.event_type is CrmEventType.LEAD_CREATED for r in rows)

    async def test_the_external_contact_id_is_stored(
        self, db, tenant_a, monkeypatch
    ):
        from app.db.models import CrmContactLink

        FakeTransport((200, {"contact": {"id": "ghl-99"}})).install(monkeypatch)
        await make_integration(db, tenant_a, "gohighlevel")

        lead = await make_lead(db, tenant_a)
        await hooks.on_lead_created(db, lead)
        await db.commit()
        await service.run_sync_tick(db)

        link = (
            (
                await db.execute(
                    select(CrmContactLink).where(
                        CrmContactLink.tenant_id == tenant_a.id
                    )
                )
            )
            .scalars()
            .one()
        )
        assert link.external_contact_id == "ghl-99"
        assert link.lead_id == lead.id

        sync = (
            (await db.execute(select(CrmSync).where(CrmSync.tenant_id == tenant_a.id)))
            .scalars()
            .one()
        )
        assert sync.external_id == "ghl-99"
        assert sync.status is CrmSyncStatus.SYNCED


# ================================================ appointment -> CRM (20) ===

class TestAppointmentWiring:
    async def test_booking_emits(self, db, tenant_a):
        appointment = await make_appointment(db, tenant_a)
        assert await hooks.on_appointment_booked(db, appointment)
        await db.commit()

        rows = await _events_for(db, tenant_a)
        assert rows[0].event_type is CrmEventType.APPOINTMENT_BOOKED
        assert rows[0].payload["customer_phone"] == "+15557778888"

    async def test_cancellation_emits(self, db, tenant_a):
        appointment = await make_appointment(db, tenant_a)
        assert await hooks.on_appointment_cancelled(db, appointment)
        await db.commit()

        rows = await _events_for(db, tenant_a)
        assert rows[0].event_type is CrmEventType.APPOINTMENT_CANCELLED

    async def test_a_duplicate_booking_event_is_safe(self, db, tenant_a):
        appointment = await make_appointment(db, tenant_a)
        assert await hooks.on_appointment_booked(db, appointment)
        assert not await hooks.on_appointment_booked(db, appointment)
        await db.commit()

        assert len(await _events_for(db, tenant_a)) == 1

    async def test_rescheduling_produces_a_distinct_event(self, db, tenant_a):
        from datetime import timedelta

        appointment = await make_appointment(db, tenant_a)
        original = appointment.starts_at
        await hooks.on_appointment_booked(db, appointment)

        appointment.starts_at = original + timedelta(hours=2)
        assert await hooks.on_appointment_rescheduled(
            db, appointment, previous_start=original
        )
        await db.commit()

        rows = await _events_for(db, tenant_a)
        assert len(rows) == 2
        assert rows[1].payload["rescheduled_from"] == original.isoformat()

    async def test_a_provider_without_a_calendar_is_skipped_cleanly(
        self, db, tenant_a, monkeypatch
    ):
        """
        Jobber has no appointment API. That must be a clean "unsupported",
        not a retried failure that looks like an outage.
        """
        FakeTransport((200, {})).install(monkeypatch)
        await make_integration(db, tenant_a, "jobber")

        appointment = await make_appointment(db, tenant_a)
        await hooks.on_appointment_booked(db, appointment)
        await db.commit()

        await service.run_sync_tick(db)

        sync = (
            (await db.execute(select(CrmSync).where(CrmSync.tenant_id == tenant_a.id)))
            .scalars()
            .one()
        )
        assert sync.status is CrmSyncStatus.PERMANENT_FAILURE
        assert sync.last_error_code == "unsupported"
        assert sync.attempt_count == 1        # not retried five times


# ====================================================== configuration API ===

class TestIntegrationApi:
    async def test_connecting_a_provider(self, client, db, owner_a):
        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/integrations/crm/gohighlevel",
            headers=headers,
            json={
                "credentials": {"access_token": "pit-abc-123"},
                "config": {"location_id": "loc-9"},
            },
        )
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["provider"] == "gohighlevel"
        assert body["connected"] is True
        assert body["config"]["location_id"] == "loc-9"
        assert "pit-abc-123" not in response.text

    async def test_updating_without_credentials_keeps_the_existing_token(
        self, client, db, owner_a, tenant_a
    ):
        """
        A tenant editing their location id must not have to re-paste a token
        they cannot read back.
        """
        integration = await make_integration(db, tenant_a, "gohighlevel")
        original = integration.credentials_encrypted

        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/integrations/crm/gohighlevel",
            headers=headers,
            json={"config": {"location_id": "loc-changed"}},
        )
        assert response.status_code == 200
        assert response.json()["connected"] is True

        await db.refresh(integration)
        assert integration.credentials_encrypted == original
        assert integration.config["location_id"] == "loc-changed"

    async def test_an_unknown_credential_field_is_rejected(self, client, owner_a):
        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/integrations/crm/hubspot",
            headers=headers,
            json={"credentials": {"access_token": "t", "admin_override": "x"}},
        )
        assert response.status_code == 422
        assert "admin_override" in response.text

    async def test_an_unknown_config_field_is_rejected(self, client, owner_a):
        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/integrations/crm/hubspot",
            headers=headers, json={"config": {"internal_flag": "x"}},
        )
        assert response.status_code == 422

    async def test_a_webhook_needs_an_https_url(self, client, owner_a):
        headers = await auth_headers(client, owner_a)

        missing = await client.put(
            "/api/integrations/crm/webhook", headers=headers, json={"config": {}}
        )
        assert missing.status_code == 422

        insecure = await client.put(
            "/api/integrations/crm/webhook", headers=headers,
            json={"config": {"url": "http://plain.example.com/x"}},
        )
        assert insecure.status_code == 422
        assert "https" in insecure.text

    async def test_a_webhook_gets_a_generated_signing_secret(
        self, client, db, owner_a, tenant_a
    ):
        """
        We generate it rather than asking the tenant to, which is what makes
        an unsigned webhook integration impossible to create.
        """
        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/integrations/crm/webhook", headers=headers,
            json={"config": {"url": "https://hooks.example.com/x"}},
        )
        assert response.status_code == 200
        assert response.json()["connected"] is True

        integration = await service.get_integration(
            db, tenant_id=tenant_a.id, provider=CrmProviderType.WEBHOOK
        )
        assert integration.credentials_encrypted
        # ...and it is not in the response.
        assert "signing_secret" not in response.text

    async def test_an_invalid_field_mapping_is_rejected_at_configuration_time(
        self, client, owner_a
    ):
        """
        Requirement 17. A bad mapping should be a 422 when it is saved, not a
        mystery at three in the morning when a call ends.
        """
        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/integrations/crm/hubspot",
            headers=headers,
            json={"field_mappings": {"my_field": "internal_secret_column"}},
        )
        assert response.status_code == 422
        assert "not a mappable" in response.text

    async def test_too_many_mappings_are_rejected(self, client, owner_a):
        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/integrations/crm/hubspot",
            headers=headers,
            json={"field_mappings": {f"f{i}": "lead_score" for i in range(40)}},
        )
        assert response.status_code == 422

    async def test_an_unknown_event_subscription_is_rejected(self, client, owner_a):
        headers = await auth_headers(client, owner_a)
        response = await client.put(
            "/api/integrations/crm/hubspot",
            headers=headers, json={"subscribed_events": ["call.completed", "nope"]},
        )
        assert response.status_code == 422

    async def test_an_unknown_provider_is_404(self, client, owner_a):
        headers = await auth_headers(client, owner_a)
        response = await client.get("/api/integrations/crm/salesforce", headers=headers)
        assert response.status_code == 404

    async def test_the_provider_catalogue_lists_capabilities(self, client, owner_a):
        headers = await auth_headers(client, owner_a)
        response = await client.get("/api/integrations/crm/providers", headers=headers)

        assert response.status_code == 200
        catalogue = {p["provider"]: p for p in response.json()["providers"]}
        assert set(catalogue) == {p.value for p in CrmProviderType}
        # A dashboard can grey out appointment sync for Jobber.
        assert "create_appointment" not in catalogue["jobber"]["capabilities"]
        assert "create_appointment" in catalogue["gohighlevel"]["capabilities"]

    async def test_disconnect_drops_credentials_but_keeps_config(
        self, client, db, owner_a, tenant_a
    ):
        integration = await make_integration(db, tenant_a, "gohighlevel")
        headers = await auth_headers(client, owner_a)

        response = await client.post(
            "/api/integrations/crm/gohighlevel/disconnect", headers=headers
        )
        assert response.status_code == 200

        body = response.json()
        assert body["connected"] is False
        assert body["is_enabled"] is False
        assert body["config"]["location_id"] == "loc_test_123"

        await db.refresh(integration)
        assert integration.credentials_encrypted is None

    async def test_delete_removes_the_integration_but_keeps_sync_history(
        self, client, db, owner_a, tenant_a
    ):
        """
        Sync rows are the record of what was sent to a customer's CRM.
        Deleting them because someone unplugged the connector would destroy
        the audit trail exactly when it matters.
        """
        from app.db.models import CrmIntegration

        integration = await make_integration(db, tenant_a, "webhook")
        tenant_id = tenant_a.id
        call = await make_call(db, tenant_a)
        await hooks.on_call_completed(db, tenant_a, call)
        await db.commit()

        before = (
            await db.execute(
                select(func.count(CrmSync.id)).where(CrmSync.tenant_id == tenant_a.id)
            )
        ).scalar()
        assert before == 1

        headers = await auth_headers(client, owner_a)
        response = await client.delete(
            "/api/integrations/crm/webhook", headers=headers
        )
        assert response.status_code == 204

        # Re-query rather than `db.get`: the identity map would hand back the
        # cached object and the assertion would pass for the wrong reason.
        db.expunge_all()
        gone = (
            await db.execute(
                select(CrmIntegration).where(CrmIntegration.id == integration.id)
            )
        ).scalar_one_or_none()
        assert gone is None

        # ...but the sync history survives.
        after = (
            await db.execute(
                select(func.count(CrmSync.id)).where(CrmSync.tenant_id == tenant_id)
            )
        ).scalar()
        assert after == before

    async def test_the_sync_feed_shows_safe_status(
        self, client, db, owner_a, tenant_a, monkeypatch
    ):
        """Requirement 15."""
        FakeTransport((401, {"detail": "token pit-LEAKED-999 rejected"})).install(
            monkeypatch
        )
        await make_integration(db, tenant_a, "gohighlevel")
        call = await make_call(db, tenant_a)
        await hooks.on_call_completed(db, tenant_a, call)
        await db.commit()
        await service.run_sync_tick(db)

        headers = await auth_headers(client, owner_a)
        response = await client.get("/api/integrations/crm/syncs", headers=headers)

        assert response.status_code == 200
        entry = response.json()["syncs"][0]
        assert entry["provider"] == "gohighlevel"
        assert entry["status"] == "permanent_failure"
        assert entry["attempt_count"] == 1
        assert entry["last_error_code"] == "unauthorized"
        assert entry["event_type"] == "call.completed"
        # The operator sees *that* it failed, never the token.
        assert "pit-LEAKED-999" not in response.text

    async def test_the_sync_feed_can_be_filtered(
        self, client, db, owner_a, tenant_a
    ):
        await make_integration(db, tenant_a, "webhook")
        call = await make_call(db, tenant_a)
        await hooks.on_call_completed(db, tenant_a, call)
        await db.commit()

        headers = await auth_headers(client, owner_a)
        pending = await client.get(
            "/api/integrations/crm/syncs?status=pending", headers=headers
        )
        synced = await client.get(
            "/api/integrations/crm/syncs?status=synced", headers=headers
        )
        assert pending.json()["total"] == 1
        assert synced.json()["total"] == 0

        bad = await client.get(
            "/api/integrations/crm/syncs?status=invented", headers=headers
        )
        assert bad.status_code == 422


# ============================================================ health (24) ===

class TestHealthCheck:
    async def test_a_working_connection_reports_connected(
        self, client, db, owner_a, tenant_a, monkeypatch
    ):
        FakeTransport((200, {"contacts": []})).install(monkeypatch)
        await make_integration(db, tenant_a, "gohighlevel")

        headers = await auth_headers(client, owner_a)
        response = await client.post(
            "/api/integrations/crm/gohighlevel/test", headers=headers
        )

        assert response.status_code == 200
        body = response.json()
        assert body == {
            "connected": True, "provider": "gohighlevel",
            "latency_ms": body["latency_ms"], "safe_message": "connected",
        }
        assert body["latency_ms"] >= 0

    async def test_a_bad_token_reports_a_diagnosis_not_a_500(
        self, client, db, owner_a, tenant_a, monkeypatch
    ):
        FakeTransport((401, {"token": "pit-SECRET-1234567890"})).install(monkeypatch)
        await make_integration(db, tenant_a, "gohighlevel")

        headers = await auth_headers(client, owner_a)
        response = await client.post(
            "/api/integrations/crm/gohighlevel/test", headers=headers
        )

        assert response.status_code == 200
        assert response.json()["connected"] is False
        assert "pit-SECRET-1234567890" not in response.text

    async def test_a_health_check_records_its_result(
        self, client, db, owner_a, tenant_a, monkeypatch
    ):
        FakeTransport((500, {})).install(monkeypatch)
        integration = await make_integration(db, tenant_a, "gohighlevel")

        headers = await auth_headers(client, owner_a)
        await client.post("/api/integrations/crm/gohighlevel/test", headers=headers)

        await db.refresh(integration)
        assert integration.last_health_ok is False
        assert integration.last_health_check_at is not None
        assert integration.last_error

    async def test_a_timeout_reports_cleanly(
        self, client, db, owner_a, tenant_a, monkeypatch
    ):
        FakeTransport(httpx.ReadTimeout("slow")).install(monkeypatch)
        await make_integration(db, tenant_a, "hubspot")

        headers = await auth_headers(client, owner_a)
        response = await client.post(
            "/api/integrations/crm/hubspot/test", headers=headers
        )
        assert response.status_code == 200
        assert response.json()["connected"] is False
        assert "timed out" in response.json()["safe_message"]


# ================================================== inbound webhooks (21) ===

class TestInboundWebhooks:
    @pytest.fixture
    async def inbound(self, db, tenant_a):
        from app.api.crm_webhook_routes import routing_token

        integration = await make_integration(
            db, tenant_a, "webhook",
            credentials={"signing_secret": "inbound-secret-" + "x" * 30},
        )
        return integration, routing_token(integration)

    def _signed(self, body: str, secret: str, *, timestamp: int | None = None):
        from app.integrations.crm.providers.webhook import sign

        ts = timestamp if timestamp is not None else int(time.time())
        return {
            "X-VoxDesk-Signature": sign(secret, timestamp=ts, body=body),
            "X-VoxDesk-Timestamp": str(ts),
            "Content-Type": "application/json",
        }

    async def test_a_correctly_signed_event_is_accepted(self, client, inbound):
        integration, token = inbound
        secret = "inbound-secret-" + "x" * 30
        body = json.dumps({"id": "evt-1", "type": "contact.updated"})

        response = await client.post(
            f"/api/integrations/crm/inbound/webhook/{token}",
            content=body, headers=self._signed(body, secret),
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True, "duplicate": False}

    async def test_an_invalid_signature_is_rejected(self, client, inbound):
        integration, token = inbound
        body = json.dumps({"id": "evt-2"})

        response = await client.post(
            f"/api/integrations/crm/inbound/webhook/{token}",
            content=body, headers=self._signed(body, "the-wrong-secret"),
        )
        assert response.status_code == 401

    async def test_a_missing_signature_is_rejected(self, client, inbound):
        integration, token = inbound
        response = await client.post(
            f"/api/integrations/crm/inbound/webhook/{token}",
            json={"id": "evt-3"},
        )
        assert response.status_code == 401

    async def test_a_forged_routing_token_is_rejected(self, client, inbound):
        body = json.dumps({"id": "evt-4"})
        response = await client.post(
            "/api/integrations/crm/inbound/webhook/" + "f" * 40,
            content=body,
            headers=self._signed(body, "inbound-secret-" + "x" * 30),
        )
        assert response.status_code == 401

    async def test_a_replayed_event_is_ignored(self, client, db, inbound, tenant_a):
        integration, token = inbound
        secret = "inbound-secret-" + "x" * 30
        body = json.dumps({"id": "evt-replay", "type": "contact.updated"})
        headers = self._signed(body, secret)

        first = await client.post(
            f"/api/integrations/crm/inbound/webhook/{token}",
            content=body, headers=headers,
        )
        second = await client.post(
            f"/api/integrations/crm/inbound/webhook/{token}",
            content=body, headers=headers,
        )

        assert first.json() == {"ok": True, "duplicate": False}
        # 200, not 409: a provider that sees an error just retries.
        assert second.status_code == 200
        assert second.json()["duplicate"] is True

        count = (
            await db.execute(
                select(func.count(CrmWebhookReceipt.id)).where(
                    CrmWebhookReceipt.tenant_id == tenant_a.id
                )
            )
        ).scalar()
        assert count == 1

    async def test_an_old_timestamp_is_rejected(self, client, inbound):
        """
        The signature is still valid; the timestamp is not. This is what makes
        a captured request unusable an hour later.
        """
        integration, token = inbound
        secret = "inbound-secret-" + "x" * 30
        body = json.dumps({"id": "evt-old"})

        response = await client.post(
            f"/api/integrations/crm/inbound/webhook/{token}",
            content=body,
            headers=self._signed(body, secret, timestamp=int(time.time()) - 7200),
        )
        assert response.status_code == 401

    async def test_a_tenant_id_in_the_body_does_not_route(
        self, client, db, inbound, tenant_b
    ):
        """
        Requirement 21: never accept `tenant_id` from an untrusted webhook
        body as the tenant selector. The path token decides; the body is data.
        """
        integration, token = inbound
        secret = "inbound-secret-" + "x" * 30
        body = json.dumps({"id": "evt-inject", "tenant_id": str(tenant_b.id)})

        response = await client.post(
            f"/api/integrations/crm/inbound/webhook/{token}",
            content=body, headers=self._signed(body, secret),
        )
        assert response.status_code == 200

        receipts = (
            (await db.execute(select(CrmWebhookReceipt))).scalars().all()
        )
        assert len(receipts) == 1
        assert receipts[0].tenant_id == integration.tenant_id
        assert receipts[0].tenant_id != tenant_b.id

    async def test_providers_without_verifiable_signatures_fail_closed(
        self, client, db, tenant_a
    ):
        """
        GoHighLevel signs with Ed25519 against HighLevel's public key, which
        this step does not provision. An unverified inbound endpoint is worse
        than none, so it is refused rather than accepted-without-checking.
        """
        from app.api.crm_webhook_routes import routing_token

        integration = await make_integration(db, tenant_a, "gohighlevel")
        token = routing_token(integration)

        response = await client.post(
            f"/api/integrations/crm/inbound/gohighlevel/{token}",
            json={"webhookId": "ghl-1", "type": "ContactCreate"},
        )
        assert response.status_code == 401

    async def test_an_unknown_provider_is_rejected_identically(self, client):
        response = await client.post(
            "/api/integrations/crm/inbound/salesforce/" + "a" * 40, json={}
        )
        assert response.status_code == 401

    async def test_every_rejection_looks_the_same(self, client, inbound):
        """
        The endpoint must not become an oracle for which routing tokens or
        providers are live.
        """
        body = json.dumps({"id": "x"})
        bad_token = await client.post(
            "/api/integrations/crm/inbound/webhook/" + "0" * 40,
            content=body, headers=self._signed(body, "s"),
        )
        bad_provider = await client.post(
            "/api/integrations/crm/inbound/salesforce/" + "0" * 40,
            content=body, headers=self._signed(body, "s"),
        )
        _, real_token = inbound
        bad_signature = await client.post(
            f"/api/integrations/crm/inbound/webhook/{real_token}",
            content=body, headers=self._signed(body, "wrong"),
        )

        bodies = {r.text for r in (bad_token, bad_provider, bad_signature)}
        statuses = {r.status_code for r in (bad_token, bad_provider, bad_signature)}
        assert statuses == {401}
        assert len(bodies) == 1

    async def test_receipts_can_be_pruned(self, db, tenant_a):
        from datetime import datetime, timedelta

        from app.api.crm_webhook_routes import prune_receipts

        db.add(CrmWebhookReceipt(
            tenant_id=tenant_a.id, provider=CrmProviderType.WEBHOOK,
            provider_event_id="old", received_at=datetime.utcnow() - timedelta(days=90),
        ))
        db.add(CrmWebhookReceipt(
            tenant_id=tenant_a.id, provider=CrmProviderType.WEBHOOK,
            provider_event_id="new",
        ))
        await db.commit()

        assert await prune_receipts(db, older_than_days=30) == 1
        remaining = (await db.execute(select(CrmWebhookReceipt))).scalars().all()
        assert [r.provider_event_id for r in remaining] == ["new"]


# ============================================ observability prep (29) ===

class TestStructuredLogging:
    """
    Requirement 29: emit structured events carrying tenant_id, provider,
    event_type, entity_id, sync_id, attempt, duration_ms and outcome — the
    fields a later observability step will aggregate on. Not building the
    observability system, just making sure the data exists and is clean.
    """

    @pytest.fixture
    def captured(self, monkeypatch):
        from app.core import logging as app_logging

        records: list[tuple[str, dict]] = []
        for level in ("info", "warning", "error"):
            original = getattr(app_logging.log, level)

            def spy(event, _orig=original, **kw):
                records.append((event, kw))
                return _orig(event, **kw)

            monkeypatch.setattr(app_logging.log, level, spy)
        return records

    async def test_a_successful_sync_logs_the_required_fields(
        self, db, tenant_a, captured, monkeypatch
    ):
        FakeTransport((200, {"contact": {"id": "ghl-1"}})).install(monkeypatch)
        await make_integration(db, tenant_a, "gohighlevel")
        call = await make_call(db, tenant_a)
        await hooks.on_call_completed(db, tenant_a, call)
        await db.commit()

        await service.run_sync_tick(db)

        entry = next(kw for name, kw in captured if name == "crm.sync_ok")
        for field in (
            "tenant_id", "provider", "entity_id", "sync_id", "attempt",
            "duration_ms", "outcome",
        ):
            assert field in entry, field
        assert entry["outcome"] == "synced"
        assert entry["attempt"] == 1
        assert entry["duration_ms"] >= 0

    async def test_a_failed_sync_logs_an_error_code_and_no_secret(
        self, db, tenant_a, captured, monkeypatch
    ):
        FakeTransport(
            (500, {"detail": "access_token pit-LEAK-777 is bad"})
        ).install(monkeypatch)
        await make_integration(db, tenant_a, "gohighlevel")
        call = await make_call(db, tenant_a)
        await hooks.on_call_completed(db, tenant_a, call)
        await db.commit()

        await service.run_sync_tick(db)

        entry = next(kw for name, kw in captured if name == "crm.sync_retry")
        assert entry["outcome"] == "retry"
        assert entry["error_code"] == "server_error"
        assert "retry_in_seconds" in entry
        assert "pit-LEAK-777" not in json.dumps(captured, default=str)

    async def test_event_emission_is_logged_with_the_idempotency_key(
        self, db, tenant_a, captured
    ):
        await make_integration(db, tenant_a, "webhook")
        call = await make_call(db, tenant_a)
        await hooks.on_call_completed(db, tenant_a, call)
        await db.commit()

        entry = next(kw for name, kw in captured if name == "crm.event_emitted")
        assert entry["event_type"] == "call.completed"
        assert entry["idempotency_key"] == f"call.completed:{call.id}"
        assert entry["queued"] == 1

    async def test_a_suppressed_duplicate_is_visible_in_the_logs(
        self, db, tenant_a, captured
    ):
        """
        Idempotency working silently is indistinguishable from events going
        missing. It has to say so.
        """
        call = await make_call(db, tenant_a)
        await hooks.on_call_completed(db, tenant_a, call)
        await hooks.on_call_completed(db, tenant_a, call)
        await db.commit()

        assert any(name == "crm.event_duplicate" for name, _ in captured)
