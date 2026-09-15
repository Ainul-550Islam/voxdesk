"""Step 6 (scale-compliance): external side effects, exactly-once, retry recovery.

Deterministic tests for the durable exactly-once primitives added in Step 6:

* reminder send leases (no duplicate customer SMS),
* outbound dial attempts (no duplicate phone calls),
* CRM delivery claims (no duplicate provider writes),
* knowledge ingestion claims (no duplicate embedding spend),
* inbound message replay protection (no duplicate turns/replies).

Nothing here touches the network, places a call, or spends money: HTTP
transports are faked at the `httpx` layer (see tests/conftest.FakeTransport)
and Twilio/Stripe are never contacted.
"""
from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.db.models import (
    Appointment,
    AppointmentStatus,
    Call,
    Campaign,
    DocumentStatus,
    KnowledgeDocument,
    Lead,
    LeadStatus,
    Reminder,
    Speaker,
    Turn,
)
from tests.conftest import FakeTransport, make_call, make_integration, make_tenant

# ---------------------------------------------------------------- reminders ===


async def _make_due_reminder(db, tenant, *, send_at=None) -> Reminder:
    appointment = Appointment(
        tenant_id=tenant.id,
        customer_name="Jane",
        customer_phone="+15551234000",
        starts_at=datetime.utcnow() + timedelta(days=2),
        ends_at=datetime.utcnow() + timedelta(days=2, minutes=30),
        status=AppointmentStatus.CONFIRMED,
    )
    db.add(appointment)
    await db.flush()
    reminder = Reminder(
        tenant_id=tenant.id,
        appointment_id=appointment.id,
        channel="sms",
        send_at=send_at or (datetime.utcnow() - timedelta(minutes=1)),
    )
    db.add(reminder)
    await db.commit()
    await db.refresh(reminder)
    return reminder


@pytest.mark.asyncio
async def test_reminder_claim_wins_once_and_loses_within_lease(db):
    from app.integrations import reminders

    tenant = await make_tenant(db, "Lease Clinic")
    reminder = await _make_due_reminder(db, tenant)

    assert await reminders._claim(db, reminder.id, worker="worker-a") is True
    # A second worker claiming within the lease must lose.
    assert await reminders._claim(db, reminder.id, worker="worker-b") is False


@pytest.mark.asyncio
async def test_an_expired_reminder_lease_can_be_reclaimed(db):
    from app.integrations import reminders

    tenant = await make_tenant(db, "Lease Clinic")
    reminder = await _make_due_reminder(db, tenant)

    assert await reminders._claim(db, reminder.id, worker="worker-a") is True
    # The worker died. Once the lease expires, the reaper releases the row and
    # a retry may claim it again.
    released = await reminders.release_stuck_reminders(
        db, now=datetime.utcnow() + timedelta(seconds=reminders.settings.reminder_lease_seconds + 1)
    )
    assert released == 1
    assert await reminders._claim(db, reminder.id, worker="worker-b") is True


@pytest.mark.asyncio
async def test_a_live_lease_is_not_reaped(db):
    from app.integrations import reminders

    tenant = await make_tenant(db, "Lease Clinic")
    reminder = await _make_due_reminder(db, tenant)

    assert await reminders._claim(db, reminder.id, worker="worker-a") is True
    # Now, not after the lease: a healthy in-flight send must be left alone.
    released = await reminders.release_stuck_reminders(db, now=datetime.utcnow())
    assert released == 0


@pytest.mark.asyncio
async def test_reminder_tick_sends_each_due_reminder_once(db, monkeypatch):
    """Two overlapping ticks must produce exactly one SMS."""
    from app.integrations import reminders

    tenant = await make_tenant(db, "Lease Clinic")
    tenant.reminder_enabled = True
    await db.commit()
    reminder = await _make_due_reminder(db, tenant)

    sent: list[str] = []
    async def fake_send_sms(to, body):
        sent.append(to)
        return True

    monkeypatch.setattr(reminders, "send_sms", fake_send_sms)

    first = await reminders.run_reminder_tick(db)
    second = await reminders.run_reminder_tick(db)

    assert first["sent"] == 1
    assert second["sent"] == 0          # already sent, nothing due
    assert len(sent) == 1               # exactly one SMS, ever
    await db.refresh(reminder)
    assert reminder.sent is True


@pytest.mark.asyncio
async def test_reminder_tick_skips_a_leased_reminder(db, monkeypatch):
    from app.integrations import reminders

    tenant = await make_tenant(db, "Lease Clinic")
    tenant.reminder_enabled = True
    await db.commit()
    reminder = await _make_due_reminder(db, tenant)

    sent: list[str] = []
    async def fake_send_sms(to, body):
        sent.append(to)
        return True

    monkeypatch.setattr(reminders, "send_sms", fake_send_sms)
    # Another worker holds the lease.
    assert await reminders._claim(db, reminder.id, worker="worker-a") is True

    result = await reminders.run_reminder_tick(db)

    assert result["sent"] == 0
    assert sent == [], "a leased reminder must not be sent by a second worker"


@pytest.mark.asyncio
async def test_send_reminder_result_redacts_the_number(db, monkeypatch):
    from app.integrations import reminders

    tenant = await make_tenant(db, "Lease Clinic")
    tenant.reminder_enabled = True
    await db.commit()
    reminder = await _make_due_reminder(db, tenant)
    assert await reminders._claim(db, reminder.id, worker="worker-a") is True

    async def fake_send_sms(to, body):
        return True

    monkeypatch.setattr(reminders, "send_sms", fake_send_sms)
    result = await reminders.send_reminder(db, reminder)

    assert result["ok"] is True
    assert result["to"] != "+15551234000"
    assert "*" in result["to"]


# ------------------------------------------------------------------ outbound ===


async def _make_lead(db, tenant) -> tuple[Campaign, Lead]:
    campaign = Campaign(
        tenant_id=tenant.id, name="cold", calls_per_minute=5, is_active=True,
    )
    db.add(campaign)
    await db.flush()
    lead = Lead(
        tenant_id=tenant.id, campaign_id=campaign.id, name="Prospect",
        phone="+15551234000", status=LeadStatus.NEW, attempts=0,
    )
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    return campaign, lead


async def _dialer_tenant(db, name: str):
    """A tenant whose outbound window is always open, so clock time can't
    turn the test into an 'outside_window' result."""
    tenant = await make_tenant(db, name)
    tenant.outbound_enabled = True
    tenant.outbound_window_open = time(0, 0)
    tenant.outbound_window_close = time(23, 59)
    await db.commit()
    return tenant


@pytest.mark.asyncio
async def test_outbound_claim_is_compare_and_set(db):
    from app.telephony import outbound

    tenant = await _dialer_tenant(db, "Dialer Co")
    _, lead = await _make_lead(db, tenant)

    assert await outbound._claim_attempt(db, lead, tenant, now=datetime.utcnow()) is True
    # The in-memory object still reports the old attempt count; the CAS guard
    # must refuse a second claim against the now-incremented row.
    assert await outbound._claim_attempt(db, lead, tenant, now=datetime.utcnow()) is False
    await db.refresh(lead)
    assert lead.attempts == 1


@pytest.mark.asyncio
async def test_a_lost_claim_never_dials(db, monkeypatch):
    from app.telephony import outbound

    tenant = await _dialer_tenant(db, "Dialer Co")
    campaign, lead = await _make_lead(db, tenant)

    monkeypatch.setattr(outbound, "_claim_attempt", _never)

    dialed: list[str] = []
    class FakeTwilio:
        class calls:
            @staticmethod
            def create(**kwargs):
                dialed.append(kwargs["to"])
                return type("C", (), {"sid": "CAxxxx"})()
    monkeypatch.setattr(outbound, "_twilio_client", lambda: FakeTwilio)

    result = await outbound.place_call(db, tenant, campaign, lead)

    assert result["ok"] is False
    assert result["reason"] == "claimed_by_another"
    assert dialed == []


async def _never(*args, **kwargs):
    return False


@pytest.mark.asyncio
async def test_place_call_claims_before_dialing_and_records_the_call(db, monkeypatch):
    from app.telephony import outbound

    tenant = await _dialer_tenant(db, "Dialer Co")
    campaign, lead = await _make_lead(db, tenant)

    dialed: list[str] = []
    class FakeTwilio:
        class calls:
            @staticmethod
            def create(**kwargs):
                dialed.append(kwargs["to"])
                return type("C", (), {"sid": "CA1234567890"})()
    monkeypatch.setattr(outbound, "_twilio_client", lambda: FakeTwilio)

    result = await outbound.place_call(db, tenant, campaign, lead)

    assert result["ok"] is True
    assert dialed == ["+15551234000"]
    await db.refresh(lead)
    assert lead.attempts == 1
    calls = (
        (await db.execute(select(Call).where(Call.lead_id == lead.id)))
        .scalars().all()
    )
    assert len(calls) == 1
    assert calls[0].call_sid == "CA1234567890"


# --------------------------------------------------------------------- CRM ===

GHL_OK = (200, {"contact": {"id": "ghl-contact-1"}})


@pytest.mark.asyncio
async def test_a_duplicate_worker_claim_does_not_double_deliver(
    db, tenant_a, monkeypatch
):
    """
    Two workers holding the same sync row (an overlapping scheduler pass)
    must produce exactly one provider write.
    """
    from app.db.models import CrmEventType, CrmSync
    from app.integrations.crm import events, service

    await make_integration(db, tenant_a, "gohighlevel")
    call = await make_call(db, tenant_a)
    await events.emit(
        db, tenant_id=tenant_a.id, event_type=CrmEventType.CALL_COMPLETED,
        entity_id=call.id, payload=events.call_payload(call, tenant_a),
    )
    await db.commit()
    sync = (
        (await db.execute(select(CrmSync).where(CrmSync.tenant_id == tenant_a.id)))
        .scalars().first()
    )

    transport = FakeTransport(GHL_OK).install(monkeypatch)

    first = await service.process_sync(db, sync)
    assert first.status.value == "synced"
    requests_after_first = len(transport.requests)

    # A second worker with the same (stale) row loses the atomic claim and
    # must not make any further request.
    second = await service.process_sync(db, sync)
    assert second.skipped_reason == "claimed_by_another"
    assert len(transport.requests) == requests_after_first

    upserts = [r for r in transport.requests if "upsert" in r["url"]]
    assert len(upserts) == 1


# ---------------------------------------------------------------- knowledge ===


@pytest.mark.asyncio
async def test_knowledge_claim_is_atomic(db, tenant_a):
    from app.knowledge.ingest import claim_uploaded_document

    document = KnowledgeDocument(
        tenant_id=tenant_a.id, title="price list",
        content_hash=uuid.uuid4().hex, status=DocumentStatus.UPLOADED,
    )
    db.add(document)
    await db.commit()

    assert await claim_uploaded_document(db, document.id) is True
    assert await claim_uploaded_document(db, document.id) is False
    await db.refresh(document)
    assert document.status is DocumentStatus.PROCESSING


# ------------------------------------------------------------- message webhook ===


@pytest.mark.asyncio
async def test_a_redelivered_message_is_answered_only_once(
    client, db, monkeypatch
):
    """A duplicate inbound message must not create a second turn or reply."""
    tenant = await make_tenant(db, "Text Clinic")
    tenant.twilio_number = "+15551234000"
    await db.commit()

    class FakeTextAgent:
        def __init__(self, tenant, handlers, channel):
            pass
        async def reply(self, history, body):
            return {"reply": "pong", "provider": "fake", "model": "fake"}

    monkeypatch.setattr("app.channels.messaging.TextAgent", FakeTextAgent)

    payload = {
        "From": "+15559990000", "To": "+15551234000",
        "Body": "hello", "MessageSid": "SM-redelivered-1",
    }
    first = await client.post("/channels/message", data=payload)
    second = await client.post("/channels/message", data=payload)

    assert first.status_code == 200
    assert "pong" in first.text
    assert second.status_code == 200
    assert "pong" not in second.text              # duplicate answered silently

    thread = (
        (await db.execute(select(Call).where(Call.intent == "chat:sms")))
        .scalars().first()
    )
    turns = (
        (await db.execute(select(Turn).where(Turn.call_id == thread.id)))
        .scalars().all()
    )
    user_turns = [t for t in turns if t.speaker is Speaker.USER]
    assert len(user_turns) == 1, "a redelivery must not append a second turn"


@pytest.mark.asyncio
async def test_message_receipts_are_pruned_by_age(db):
    """Replay records must not grow forever."""
    from app.channels.messaging import prune_receipts
    from app.db.models import MessageWebhookReceipt

    tenant = await make_tenant(db, "Text Clinic")
    old = MessageWebhookReceipt(
        tenant_id=tenant.id, channel="sms", provider_message_id="SM-old",
    )
    old.received_at = datetime.utcnow() - timedelta(days=90)
    fresh = MessageWebhookReceipt(
        tenant_id=tenant.id, channel="sms", provider_message_id="SM-fresh",
    )
    fresh.received_at = datetime.utcnow()
    db.add_all([old, fresh])
    await db.commit()

    removed = await prune_receipts(db, older_than_days=30)

    assert removed == 1
    remaining = (
        (await db.execute(select(MessageWebhookReceipt))).scalars().all()
    )
    assert [r.provider_message_id for r in remaining] == ["SM-fresh"]


# ------------------------------------------------------------------- metrics ===


def test_side_effect_metrics_are_bounded_and_ignored_when_unknown():
    from app.core.metrics import SIDE_EFFECTS, record_side_effect

    before = SIDE_EFFECTS.labels(kind="reminder", outcome="attempt")._value.get()
    record_side_effect("reminder", "attempt")
    assert SIDE_EFFECTS.labels(kind="reminder", outcome="attempt")._value.get() == before + 1

    # Unknown labels are dropped, never registered.
    record_side_effect("not-a-kind", "attempt")
    record_side_effect("reminder", "not-an-outcome")


# ---------------------------------------------------------- tenant isolation ===


@pytest.mark.asyncio
async def test_a_lease_on_one_tenants_reminder_does_not_block_another(db, monkeypatch):
    """Leasing A's reminder must not suppress B's due reminder."""
    from app.integrations import reminders

    tenant_a = await make_tenant(db, "Clinic A")
    tenant_b = await make_tenant(db, "Clinic B")
    tenant_a.reminder_enabled = True
    tenant_b.reminder_enabled = True
    await db.commit()
    reminder_a = await _make_due_reminder(db, tenant_a)
    reminder_b = await _make_due_reminder(db, tenant_b)

    assert await reminders._claim(db, reminder_a.id, worker="worker-a") is True

    sent: list[str] = []
    async def fake_send_sms(to, body):
        sent.append(to)
        return True
    monkeypatch.setattr(reminders, "send_sms", fake_send_sms)

    result = await reminders.run_reminder_tick(db)

    assert result["sent"] == 1
    assert len(sent) == 1                      # only B's reminder went out
    await db.refresh(reminder_b)
    assert reminder_b.sent is True
    await db.refresh(reminder_a)
    assert reminder_a.sent is False            # A's is still leased, unsent
