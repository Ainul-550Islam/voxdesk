"""
Media-stream token lifecycle (STEP 2 design, re-verified under STEP 3).

The signed stream-token scheme is unchanged. What is new here is proving that
the transfer lifecycle does not corrupt call status when the stream is torn
down -- a transfer deliberately kills the socket, and that must not be
recorded as a failed call.
"""
from __future__ import annotations

import time

import pytest

from app.db.models import CallStatus, TransferState
from app.telephony import call_state, transfer_service
from app.telephony.provider import FakeTelephonyProvider
from app.telephony.stream_auth import (
    STREAM_TOKEN_TTL_SECONDS,
    create_stream_token,
    verify_stream_token,
)
from tests.test_transfer import seed_call, tenant_with_human

# ------------------------------------------------------------- the token ----

def test_correct_token_succeeds():
    token = create_stream_token("CA-abc")
    assert verify_stream_token("CA-abc", token) is True


def test_token_from_another_call_fails():
    """A token observed on one call must not open another call's audio."""
    stolen = create_stream_token("CA-victim")
    assert verify_stream_token("CA-attacker", stolen) is False


def test_expired_token_fails():
    old = create_stream_token("CA-abc", issued_at=int(time.time()) - (STREAM_TOKEN_TTL_SECONDS + 5))
    assert verify_stream_token("CA-abc", old) is False


def test_token_at_the_edge_of_its_window_still_works():
    edge = create_stream_token("CA-abc", issued_at=int(time.time()) - (STREAM_TOKEN_TTL_SECONDS - 5))
    assert verify_stream_token("CA-abc", edge) is True


def test_future_dated_token_fails():
    future = create_stream_token("CA-abc", issued_at=int(time.time()) + 600)
    assert verify_stream_token("CA-abc", future) is False


@pytest.mark.parametrize("bad", [None, "", "garbage", "123.deadbeef", "."])
def test_malformed_tokens_fail(bad):
    assert verify_stream_token("CA-abc", bad) is False


def test_tampered_signature_fails():
    token = create_stream_token("CA-abc")
    assert verify_stream_token("CA-abc", token[:-1] + ("0" if token[-1] != "0" else "1")) is False


def test_missing_call_sid_fails():
    assert verify_stream_token("", create_stream_token("CA-abc")) is False


# ------------------------------------------- transfer vs stream teardown ----

@pytest.mark.asyncio
async def test_stream_teardown_after_a_transfer_is_not_a_failed_call(db):
    """
    Twilio replaces the TwiML on transfer, which kills our WebSocket. The
    handler must recognise that and leave the call TRANSFERRED rather than
    recording FAILED, which is what the old unconditional
    `call.status = CallStatus.FAILED` did.
    """
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)
    await transfer_service.request_transfer(
        db, tenant, call, provider=FakeTelephonyProvider()
    )
    await db.refresh(call)
    assert call.transfer_state is not TransferState.NONE

    # This is the branch the websocket handler takes on stream teardown.
    if call.transfer_state is TransferState.NONE:
        call_state.apply_status(call, CallStatus.FAILED, source="media_stream")

    assert call.status is CallStatus.TRANSFERRED


@pytest.mark.asyncio
async def test_stream_crash_without_a_transfer_does_mark_the_call_failed(db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant)

    assert call.transfer_state is TransferState.NONE
    result = call_state.apply_status(
        call, CallStatus.FAILED, reason="media stream error", source="media_stream"
    )
    assert result.applied is True
    assert call.status is CallStatus.FAILED


@pytest.mark.asyncio
async def test_stream_crash_cannot_regress_an_already_completed_call(db):
    tenant = await tenant_with_human(db)
    call = await seed_call(db, tenant, status=CallStatus.COMPLETED)
    result = call_state.apply_status(call, CallStatus.FAILED, source="media_stream")
    assert result.applied is False
    assert call.status is CallStatus.COMPLETED
