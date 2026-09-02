"""
Human transfer -- integration tests.

These run the real `transfer_service` against a real database session and a
real `Call` row. The only substituted component is the outbound HTTP call to
Twilio, replaced by `FakeTelephonyProvider`, which records the TwiML we
generated so tests can assert on the actual provider instruction.

No test here asserts merely that a function was called. Every one checks the
resulting database state.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest

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
from app.telephony.transfer_service import TransferError, TransferOutcome
from tests.conftest import make_tenant

HUMAN = "+15557654321"


async def seed_call(db, tenant, *, status=CallStatus.IN_PROGRESS, **kw) -> Call:
    call = Call(
        tenant_id=tenant.id,
        call_sid=f"CA{uuid.uuid4().hex[:14]}",
        from_number="+15551112222",
        to_number=tenant.twilio_number,
        status=status,
        direction=CallDirection.INBOUND,
        started_at=datetime.utcnow(),
        **kw,
    )
    db.add(call)
    await db.commit()
    await db.refresh(call)
    return call


@pytest.fixture
def provider():
    return FakeTelephonyProvider()


async def tenant_with_human(db, number: str = HUMAN, name: str = "Acme"):
    tenant = await make_tenant(db, name)
    tenant.escalation_number = number
    await db.commit()
    await db.refresh(tenant)
    return tenant


# =========================================================== happy path ===

@pytest.mark.asyncio
async def test_valid_escalation_actually_performs_the_transfer(db, provider):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)

    result = await transfer_service.request_transfer(
        db, tenant, call, reason="caller is angry", provider=provider
    )

    assert result.outcome is TransferOutcome.TRANSFER_STARTED
    # The provider was genuinely instructed, exactly once.
    assert provider.call_count == 1
    sid, twiml = provider.redirects[0]
    assert sid == call.call_sid
    assert HUMAN in twiml and "<Dial" in twiml

    await db.refresh(call)
    assert call.status is CallStatus.TRANSFERRED
    assert call.transfer_state is TransferState.DIALING
    assert call.escalated is True
    assert call.transfer_destination == HUMAN
    assert call.transfer_reason == "caller is angry"
    assert call.transfer_attempts == 1
    assert call.transfer_requested_at is not None
    assert call.transfer_started_at is not None
    assert call.transfer_error is None


@pytest.mark.asyncio
async def test_destination_is_normalized_before_dialing(db, provider):
    tenant = await tenant_with_human(db, "+1 (555) 765-4321")
    call = await seed_call(db, tenant)

    await transfer_service.request_transfer(db, tenant, call, provider=provider)
    await db.refresh(call)
    assert call.transfer_destination == "+15557654321"
    assert "+15557654321" in provider.last_twiml()


@pytest.mark.asyncio
async def test_transfer_twiml_carries_an_action_callback(db, provider):
    """Without the action URL the transfer outcome could never be verified."""
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(db, tenant, call, provider=provider)
    assert "/telephony/transfer-status" in provider.last_twiml()


# ============================================================ failures ====

@pytest.mark.asyncio
async def test_missing_destination_fails_safely(db, provider):
    tenant = await make_tenant(db, "No Human Co")
    tenant.escalation_number = None
    await db.commit()
    call = await seed_call(db, tenant)

    result = await transfer_service.request_transfer(db, tenant, call, provider=provider)

    assert result.outcome is TransferOutcome.TRANSFER_FAILED
    assert result.error is TransferError.NO_DESTINATION
    assert provider.call_count == 0, "nobody should have been dialled"

    await db.refresh(call)
    assert call.transfer_state is TransferState.FAILED
    assert call.status is CallStatus.IN_PROGRESS, "the caller is still on the line"
    assert call.transfer_error


@pytest.mark.asyncio
async def test_invalid_destination_fails_safely(db, provider):
    tenant = await tenant_with_human(db, "555-0101")     # no country code
    call = await seed_call(db, tenant)

    result = await transfer_service.request_transfer(db, tenant, call, provider=provider)

    assert result.error is TransferError.INVALID_DESTINATION
    assert provider.call_count == 0
    await db.refresh(call)
    assert call.transfer_state is TransferState.FAILED


@pytest.mark.asyncio
async def test_provider_failure_persists_and_does_not_claim_success(db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    failing = FakeTelephonyProvider(fail_with=("20404", "Call is not in-progress"))

    result = await transfer_service.request_transfer(db, tenant, call, provider=failing)

    assert result.outcome is TransferOutcome.TRANSFER_FAILED
    assert result.error is TransferError.PROVIDER_ERROR
    assert result.ok is False

    await db.refresh(call)
    assert call.transfer_state is TransferState.FAILED
    assert call.status is CallStatus.IN_PROGRESS, "must NOT be marked TRANSFERRED"
    assert call.transfer_failed_at is not None
    assert "20404" in call.transfer_error


@pytest.mark.asyncio
async def test_provider_error_text_never_reaches_the_caller(db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    failing = FakeTelephonyProvider(fail_with=("20404", "AccountSid ACxxx not authorized"))

    result = await transfer_service.request_transfer(db, tenant, call, provider=failing)

    assert "20404" not in result.message
    assert "AccountSid" not in result.message
    tool_result = result.as_tool_result()
    assert "AccountSid" not in str(tool_result)
    assert HUMAN not in str(tool_result), "no phone numbers to the LLM either"


@pytest.mark.asyncio
async def test_already_completed_call_cannot_transfer(db, provider):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant, status=CallStatus.COMPLETED)

    result = await transfer_service.request_transfer(db, tenant, call, provider=provider)

    assert result.error is TransferError.CALL_ALREADY_ENDED
    assert provider.call_count == 0
    await db.refresh(call)
    assert call.status is CallStatus.COMPLETED


@pytest.mark.asyncio
async def test_failed_call_cannot_transfer(db, provider):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant, status=CallStatus.FAILED)
    result = await transfer_service.request_transfer(db, tenant, call, provider=provider)
    assert result.error is TransferError.CALL_ALREADY_ENDED
    assert provider.call_count == 0


# ========================================================= idempotency ====

@pytest.mark.asyncio
async def test_duplicate_transfer_request_dials_only_once(db, provider):
    """The critical one: an LLM retry must not ring a human twice."""
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)

    first = await transfer_service.request_transfer(db, tenant, call, provider=provider)
    second = await transfer_service.request_transfer(db, tenant, call, provider=provider)
    third = await transfer_service.request_transfer(db, tenant, call, provider=provider)

    assert first.outcome is TransferOutcome.TRANSFER_STARTED
    assert second.outcome is TransferOutcome.ALREADY_TRANSFERRED
    assert third.outcome is TransferOutcome.ALREADY_TRANSFERRED
    assert provider.call_count == 1, "the human's phone rang more than once"

    await db.refresh(call)
    assert call.transfer_attempts == 1
    assert call.status is CallStatus.TRANSFERRED


@pytest.mark.asyncio
async def test_already_connected_call_reports_completed_not_a_new_transfer(db, provider):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(db, tenant, call, provider=provider)
    await transfer_service.mark_transfer_connected(db, call)
    await db.commit()

    again = await transfer_service.request_transfer(db, tenant, call, provider=provider)
    assert again.outcome is TransferOutcome.TRANSFER_COMPLETED
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_a_failed_transfer_may_be_retried(db):
    """FAILED is not in-flight, so a genuine second attempt is allowed."""
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)

    failing = FakeTelephonyProvider(fail_with=("500", "boom"))
    await transfer_service.request_transfer(db, tenant, call, provider=failing)
    await db.refresh(call)
    assert call.transfer_state is TransferState.FAILED

    working = FakeTelephonyProvider()
    result = await transfer_service.request_transfer(db, tenant, call, provider=working)
    assert result.outcome is TransferOutcome.TRANSFER_STARTED
    await db.refresh(call)
    assert call.transfer_attempts == 2
    assert call.status is CallStatus.TRANSFERRED


# ======================================================== availability ====

@pytest.mark.asyncio
async def test_transfer_unavailable_without_a_destination(db):
    tenant = await make_tenant(db, "No Human")
    tenant.escalation_number = None
    await db.commit()
    call = await seed_call(db, tenant)
    assert transfer_service.transfer_available(tenant, call) is False


@pytest.mark.asyncio
async def test_transfer_unavailable_for_an_invalid_destination(db):
    tenant = await tenant_with_human(db, "not-a-number")
    call = await seed_call(db, tenant)
    assert transfer_service.transfer_available(tenant, call) is False


@pytest.mark.asyncio
async def test_transfer_unavailable_once_the_call_is_over(db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant, status=CallStatus.COMPLETED)
    assert transfer_service.transfer_available(tenant, call) is False


@pytest.mark.asyncio
async def test_transfer_unavailable_while_one_is_in_flight(db, provider):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    assert transfer_service.transfer_available(tenant, call) is True
    await transfer_service.request_transfer(db, tenant, call, provider=provider)
    assert transfer_service.transfer_available(tenant, call) is False


# =========================================================== callbacks ====

@pytest.mark.asyncio
async def test_connected_callback_records_the_connection(db, provider):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(db, tenant, call, provider=provider)

    changed = await transfer_service.mark_transfer_connected(db, call)
    await db.commit()

    assert changed is True
    await db.refresh(call)
    assert call.transfer_state is TransferState.CONNECTED
    assert call.transfer_completed_at is not None


@pytest.mark.asyncio
async def test_duplicate_connected_callback_changes_nothing(db, provider):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(db, tenant, call, provider=provider)
    await transfer_service.mark_transfer_connected(db, call)
    await db.commit()
    first_time = call.transfer_completed_at

    assert await transfer_service.mark_transfer_connected(db, call) is False
    await db.commit()
    await db.refresh(call)
    assert call.transfer_completed_at == first_time


@pytest.mark.asyncio
async def test_failure_callback_after_connection_is_ignored(db, provider):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(db, tenant, call, provider=provider)
    await transfer_service.mark_transfer_connected(db, call)
    await db.commit()

    assert await transfer_service.mark_transfer_failed(db, call, "busy") is False
    await db.refresh(call)
    assert call.transfer_state is TransferState.CONNECTED


@pytest.mark.asyncio
async def test_no_answer_on_the_human_leg_is_recorded(db, provider):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(db, tenant, call, provider=provider)

    await transfer_service.mark_transfer_failed(db, call, "no-answer")
    await db.commit()
    await db.refresh(call)

    assert call.transfer_state is TransferState.FAILED
    assert call.transfer_error == "no-answer"
    # The customer is still connected to our voicemail fallback.
    assert call.status is CallStatus.TRANSFERRED


# ====================================================== system events =====

async def system_texts(db, call) -> list[str]:
    from sqlalchemy import select
    rows = (await db.execute(
        select(Turn).where(Turn.call_id == call.id, Turn.speaker == Speaker.SYSTEM)
    )).scalars().all()
    return [t.text for t in rows]


@pytest.mark.asyncio
async def test_transfer_writes_system_transcript_events(db, provider):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(db, tenant, call, provider=provider)
    await transfer_service.mark_transfer_connected(db, call)
    await db.commit()

    texts = await system_texts(db, call)
    assert "Human transfer requested." in texts
    assert "Human transfer started." in texts
    assert "Human transfer connected." in texts


@pytest.mark.asyncio
async def test_duplicate_provider_events_do_not_duplicate_system_turns(db, provider):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(db, tenant, call, provider=provider)
    for _ in range(3):
        await transfer_service.mark_transfer_connected(db, call)
        await transfer_service.request_transfer(db, tenant, call, provider=provider)
    await db.commit()

    texts = await system_texts(db, call)
    assert texts.count("Human transfer connected.") == 1
    assert texts.count("Human transfer requested.") == 1
    assert texts.count("Human transfer started.") == 1


@pytest.mark.asyncio
async def test_failed_transfer_writes_a_failure_event_not_a_success(db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    failing = FakeTelephonyProvider(fail_with=("500", "boom"))
    await transfer_service.request_transfer(db, tenant, call, provider=failing)
    await db.commit()

    texts = await system_texts(db, call)
    assert "Human transfer failed." in texts
    assert "Human transfer connected." not in texts


@pytest.mark.asyncio
async def test_system_events_use_the_canonical_speaker_enum(db, provider):
    from sqlalchemy import select
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(db, tenant, call, provider=provider)
    await db.commit()

    rows = (await db.execute(select(Turn).where(Turn.call_id == call.id))).scalars().all()
    assert rows
    assert all(t.speaker is Speaker.SYSTEM for t in rows)


# ============================================================= security ===

@pytest.mark.asyncio
async def test_tenant_mismatch_is_refused(db, provider):
    """A call belonging to tenant B can never be transferred by tenant A."""
    tenant_a = await tenant_with_human(db, "+15551110000", "Tenant A")
    tenant_b = await tenant_with_human(db, "+15552220000", "Tenant B")
    call_b = await seed_call(db, tenant_b)

    result = await transfer_service.request_transfer(db, tenant_a, call_b, provider=provider)

    assert result.error is TransferError.TENANT_MISMATCH
    assert provider.call_count == 0
    await db.refresh(call_b)
    assert call_b.transfer_state is TransferState.NONE
    assert call_b.status is CallStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_destination_always_comes_from_the_tenant_row(db, provider):
    """
    There is no parameter through which a destination can be injected -- the
    number is read from the tenant, so tenant A's call cannot dial tenant B's
    human even if the model asks for it.
    """
    tenant_a = await tenant_with_human(db, "+15551110000", "Tenant A")
    await tenant_with_human(db, "+15552220000", "Tenant B")
    call_a = await seed_call(db, tenant_a)

    await transfer_service.request_transfer(
        db, tenant_a, call_a, reason="please call +15552220000", provider=provider
    )
    await db.refresh(call_a)
    assert call_a.transfer_destination == "+15551110000"
    assert "+15552220000" not in provider.last_twiml()
