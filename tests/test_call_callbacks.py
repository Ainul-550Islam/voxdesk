"""
Provider callback handling: signatures, idempotency, and ordering races.

These drive the real FastAPI webhook routes over the real router stack against
a real database. Twilio itself is not involved -- signature verification is
bypassed in APP_ENV=development, which is exactly the configuration these
routes run under in tests -- but every state change is asserted in the DB.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import (
    Call,
    CallDirection,
    CallStatus,
    Speaker,
    TransferState,
    Turn,
)
from app.telephony import transfer_service
from app.telephony.provider import FakeTelephonyProvider
from tests.conftest import make_tenant
from tests.test_transfer import seed_call, tenant_with_human


async def post_status(client, sid, status, duration="60"):
    return await client.post(
        "/telephony/status",
        data={"CallSid": sid, "CallStatus": status, "CallDuration": duration},
    )


async def post_dial(client, sid, dial_status, duration="30"):
    return await client.post(
        "/telephony/transfer-status",
        data={"CallSid": sid, "DialCallStatus": dial_status,
              "DialCallDuration": duration},
    )


async def system_texts(db, call):
    rows = (await db.execute(
        select(Turn).where(Turn.call_id == call.id, Turn.speaker == Speaker.SYSTEM)
    )).scalars().all()
    return [t.text for t in rows]


# ================================================================ security ===

@pytest.mark.asyncio
async def test_status_webhook_rejects_a_bad_signature_outside_development(
    client, db, monkeypatch
):
    """This route previously had no signature check at all."""
    tenant = await make_tenant(db, "Sig Co")
    call = await seed_call(db, tenant)
    monkeypatch.setattr(settings, "app_env", "production")

    resp = await post_status(client, call.call_sid, "completed")

    assert resp.status_code == 403
    await db.refresh(call)
    assert call.status is CallStatus.IN_PROGRESS, "state changed without a signature"


@pytest.mark.asyncio
async def test_transfer_status_webhook_rejects_a_bad_signature(client, db, monkeypatch):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    monkeypatch.setattr(settings, "app_env", "production")
    assert (await post_dial(client, call.call_sid, "answered")).status_code == 403


@pytest.mark.asyncio
async def test_unknown_call_sid_is_acknowledged_but_changes_nothing(client, db):
    resp = await post_status(client, f"CA{uuid.uuid4().hex}", "completed")
    assert resp.status_code == 200


# ============================================================ basic status ===

@pytest.mark.asyncio
async def test_completed_callback_finishes_the_call(client, db):
    tenant = await make_tenant(db, "Basic Co")
    call = await seed_call(db, tenant)

    await post_status(client, call.call_sid, "completed", "125")
    await db.refresh(call)

    assert call.status is CallStatus.COMPLETED
    assert call.duration_seconds == 125
    assert call.ended_at is not None


@pytest.mark.asyncio
async def test_no_answer_and_busy_map_correctly(client, db):
    tenant = await make_tenant(db, "Map Co")
    a = await seed_call(db, tenant, status=CallStatus.RINGING)
    b = await seed_call(db, tenant, status=CallStatus.RINGING)

    await post_status(client, a.call_sid, "no-answer", "0")
    await post_status(client, b.call_sid, "busy", "0")
    await db.refresh(a)
    await db.refresh(b)

    assert a.status is CallStatus.NO_ANSWER
    assert b.status is CallStatus.FAILED
    assert b.failure_reason == "busy"


@pytest.mark.asyncio
async def test_unknown_provider_status_does_not_terminate_the_call(client, db):
    tenant = await make_tenant(db, "Weird Co")
    call = await seed_call(db, tenant)
    await post_status(client, call.call_sid, "brand-new-twilio-status")
    await db.refresh(call)
    assert call.status is CallStatus.IN_PROGRESS


# ============================================================ idempotency ====

@pytest.mark.asyncio
async def test_duplicate_completed_callbacks_bill_the_minutes_once(client, db):
    tenant = await make_tenant(db, "Billing Co")
    tenant.minutes_used = 0.0
    await db.commit()
    call = await seed_call(db, tenant)

    for _ in range(4):
        await post_status(client, call.call_sid, "completed", "120")

    await db.refresh(tenant)
    assert tenant.minutes_used == pytest.approx(2.0), "minutes were double-billed"


@pytest.mark.asyncio
async def test_duplicate_completed_callbacks_are_otherwise_no_ops(client, db):
    tenant = await make_tenant(db, "Dup Co")
    call = await seed_call(db, tenant)

    await post_status(client, call.call_sid, "completed", "90")
    await db.refresh(call)
    first_ended = call.ended_at

    for _ in range(3):
        assert (await post_status(client, call.call_sid, "completed", "90")).status_code == 200
    await db.refresh(call)
    assert call.ended_at == first_ended
    assert call.status is CallStatus.COMPLETED


# ================================================================= races =====

@pytest.mark.asyncio
async def test_case_a_transfer_then_stream_close_then_completed_then_dial_callback(
    client, db
):
    """
    Case A: transfer requested -> stream closes -> completed arrives ->
    the dial callback arrives last.

    The late dial callback must not resurrect the finished call.
    """
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    provider = FakeTelephonyProvider()
    await transfer_service.request_transfer(db, tenant, call, provider=provider)
    await db.refresh(call)
    assert call.status is CallStatus.TRANSFERRED

    await post_status(client, call.call_sid, "completed", "200")
    await post_dial(client, call.call_sid, "answered")

    await db.refresh(call)
    assert call.status is CallStatus.COMPLETED, "final status must stay terminal"
    assert call.transfer_state is TransferState.CONNECTED
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_case_b_dial_callback_then_completed(client, db):
    """Case B: the transfer connects, then the call completes normally."""
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(
        db, tenant, call, provider=FakeTelephonyProvider()
    )

    await post_dial(client, call.call_sid, "answered")
    await db.refresh(call)
    assert call.transfer_state is TransferState.CONNECTED
    assert call.status is CallStatus.TRANSFERRED

    await post_status(client, call.call_sid, "completed", "310")
    await db.refresh(call)
    assert call.status is CallStatus.COMPLETED
    assert call.transfer_state is TransferState.CONNECTED, "transfer history kept"


@pytest.mark.asyncio
async def test_case_c_no_answer_after_another_terminal_callback(client, db):
    """Case C: a stale no-answer arrives after the call already completed."""
    tenant = await make_tenant(db, "Race C")
    call = await seed_call(db, tenant)

    await post_status(client, call.call_sid, "completed", "75")
    await post_status(client, call.call_sid, "no-answer", "0")

    await db.refresh(call)
    assert call.status is CallStatus.COMPLETED
    assert call.duration_seconds == 75


@pytest.mark.asyncio
async def test_case_d_duplicate_completed_callbacks(client, db):
    """Case D: pure duplicates. Deterministic final state, one set of events."""
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(
        db, tenant, call, provider=FakeTelephonyProvider()
    )

    for _ in range(3):
        await post_dial(client, call.call_sid, "answered")
    for _ in range(3):
        await post_status(client, call.call_sid, "completed", "150")

    await db.refresh(call)
    assert call.status is CallStatus.COMPLETED
    assert call.transfer_state is TransferState.CONNECTED

    texts = await system_texts(db, call)
    assert texts.count("Human transfer connected.") == 1
    assert texts.count("Human transfer started.") == 1


@pytest.mark.asyncio
async def test_call_ending_while_still_dialing_marks_the_transfer_failed(client, db):
    """The human never picked up before the caller hung up."""
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(
        db, tenant, call, provider=FakeTelephonyProvider()
    )

    await post_status(client, call.call_sid, "completed", "40")
    await db.refresh(call)

    assert call.status is CallStatus.COMPLETED
    assert call.transfer_state is TransferState.FAILED
    assert "dialing" in (call.transfer_error or "")


@pytest.mark.asyncio
async def test_busy_human_leg_leaves_the_customer_call_alive(client, db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(
        db, tenant, call, provider=FakeTelephonyProvider()
    )

    await post_dial(client, call.call_sid, "busy")
    await db.refresh(call)

    assert call.transfer_state is TransferState.FAILED
    assert call.transfer_error == "busy"
    # Our TwiML falls through to voicemail, so the call is not over.
    assert call.status is CallStatus.TRANSFERRED
    texts = await system_texts(db, call)
    assert "Human transfer failed." in texts
    assert "Human transfer connected." not in texts


@pytest.mark.asyncio
async def test_out_of_order_dial_callback_before_any_transfer_is_ignored(client, db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)

    await post_dial(client, call.call_sid, "answered")
    await db.refresh(call)

    assert call.transfer_state is TransferState.NONE
    assert await system_texts(db, call) == []


# ============================================================ lead grading ===

@pytest.mark.asyncio
async def test_duplicate_callbacks_do_not_regrade_a_lead(client, db):
    from app.db.models import Lead, LeadStatus

    tenant = await make_tenant(db, "Lead Co")
    lead = Lead(tenant_id=tenant.id, phone="+15550001111", name="Bob",
                attempts=1, status=LeadStatus.QUEUED)
    db.add(lead)
    await db.commit()
    await db.refresh(lead)

    call = Call(
        tenant_id=tenant.id, call_sid=f"CA{uuid.uuid4().hex[:14]}",
        from_number=tenant.twilio_number, to_number=lead.phone,
        status=CallStatus.IN_PROGRESS, direction=CallDirection.OUTBOUND,
        started_at=datetime.utcnow(), lead_id=lead.id, lead_score=80,
    )
    db.add(call)
    await db.commit()

    await post_status(client, call.call_sid, "completed", "45")
    await db.refresh(lead)
    assert lead.status is LeadStatus.QUALIFIED

    lead.status = LeadStatus.DNC        # caller opted out afterwards
    await db.commit()
    await post_status(client, call.call_sid, "completed", "45")
    await db.refresh(lead)
    assert lead.status is LeadStatus.DNC, "a duplicate webhook overwrote DNC"


@pytest.mark.asyncio
async def test_provider_reported_failure_is_not_overridden_by_a_later_connect(
    client, db
):
    """
    The inference-correction path must be narrow: only *our guess* may be
    corrected. A busy signal reported by the provider is evidence and stands.
    """
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(
        db, tenant, call, provider=FakeTelephonyProvider()
    )

    await post_dial(client, call.call_sid, "busy")
    await post_dial(client, call.call_sid, "answered")

    await db.refresh(call)
    assert call.transfer_state is TransferState.FAILED
    assert call.transfer_error == "busy"


@pytest.mark.asyncio
async def test_outbound_answer_webhook_requires_a_signature(client, monkeypatch):
    """The last telephony route that was still unauthenticated."""
    monkeypatch.setattr(settings, "app_env", "production")
    resp = await client.post(
        "/telephony/outbound-answer",
        data={"CallSid": "CA-whatever", "AnsweredBy": "human"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_outbound_answer_works_in_development(client):
    resp = await client.post(
        "/telephony/outbound-answer",
        data={"CallSid": "CA-dev", "AnsweredBy": "human"},
    )
    assert resp.status_code == 200
    assert "<Stream" in resp.text

