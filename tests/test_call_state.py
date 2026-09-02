"""
Call lifecycle state machine.

These tests drive the real `Call` model through `app.telephony.call_state`.
Nothing is mocked: the transition rules are pure functions over a row, which is
exactly why they were extracted from the webhook handler.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from app.db.models import Call, CallStatus, TransferState
from app.telephony import call_state


def make_call(status: CallStatus = CallStatus.RINGING, **kw) -> Call:
    return Call(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        call_sid=f"CA{uuid.uuid4().hex[:12]}",
        from_number="+15550000001",
        to_number="+15550000002",
        status=status,
        started_at=datetime.utcnow(),
        duration_seconds=0.0,
        transfer_state=TransferState.NONE,
        transfer_attempts=0,
        **kw,
    )


# ------------------------------------------------------- the required graph ---

@pytest.mark.parametrize("start,target", [
    (CallStatus.RINGING, CallStatus.IN_PROGRESS),
    (CallStatus.RINGING, CallStatus.NO_ANSWER),
    (CallStatus.RINGING, CallStatus.FAILED),
    (CallStatus.IN_PROGRESS, CallStatus.COMPLETED),
    (CallStatus.IN_PROGRESS, CallStatus.TRANSFERRED),
    (CallStatus.IN_PROGRESS, CallStatus.FAILED),
    (CallStatus.TRANSFERRED, CallStatus.COMPLETED),
    (CallStatus.TRANSFERRED, CallStatus.FAILED),
])
def test_legal_transitions_are_applied(start, target):
    call = make_call(start)
    result = call_state.apply_status(call, target)
    assert result.applied is True
    assert call.status is target
    assert result.previous is start


@pytest.mark.parametrize("start,target", [
    (CallStatus.COMPLETED, CallStatus.RINGING),
    (CallStatus.COMPLETED, CallStatus.IN_PROGRESS),
    (CallStatus.FAILED, CallStatus.IN_PROGRESS),
    (CallStatus.FAILED, CallStatus.COMPLETED),
    (CallStatus.NO_ANSWER, CallStatus.IN_PROGRESS),
    (CallStatus.IN_PROGRESS, CallStatus.RINGING),
    (CallStatus.TRANSFERRED, CallStatus.IN_PROGRESS),
    (CallStatus.TRANSFERRED, CallStatus.RINGING),
])
def test_illegal_transitions_are_rejected(start, target):
    call = make_call(start)
    result = call_state.apply_status(call, target)
    assert result.applied is False
    assert call.status is start, "the row must not have been mutated"


def test_terminal_states_are_terminal():
    assert call_state.is_terminal(CallStatus.COMPLETED)
    assert call_state.is_terminal(CallStatus.FAILED)
    assert call_state.is_terminal(CallStatus.NO_ANSWER)
    # A transferred call is still live -- a human is on it.
    assert not call_state.is_terminal(CallStatus.TRANSFERRED)
    assert not call_state.is_terminal(CallStatus.IN_PROGRESS)
    assert not call_state.is_terminal(CallStatus.RINGING)


def test_every_status_has_a_transition_entry():
    assert set(call_state.ALLOWED_TRANSITIONS) == set(CallStatus)


# ------------------------------------------------------------- idempotency ---

def test_duplicate_status_is_a_no_op_but_not_an_error():
    call = make_call(CallStatus.COMPLETED)
    result = call_state.apply_status(call, CallStatus.COMPLETED)
    assert result.applied is False
    assert result.reason == "duplicate"
    assert call.status is CallStatus.COMPLETED


def test_repeated_completed_callbacks_keep_one_ended_at():
    call = make_call(CallStatus.IN_PROGRESS)
    call_state.apply_provider_status(call, "completed", duration_seconds=100)
    first_ended = call.ended_at
    assert first_ended is not None

    call_state.apply_provider_status(call, "completed", duration_seconds=100)
    call_state.apply_provider_status(call, "completed", duration_seconds=100)
    assert call.ended_at == first_ended, "ended_at must be written once"


def test_terminal_state_survives_a_stale_callback():
    """The exact bug: a late 'completed' used to overwrite TRANSFERRED."""
    call = make_call(CallStatus.IN_PROGRESS)
    call_state.apply_status(call, CallStatus.TRANSFERRED)
    call_state.apply_provider_status(call, "completed", duration_seconds=90)
    assert call.status is CallStatus.COMPLETED

    # ... and now a duplicate 'no-answer' arrives for the same SID.
    result = call_state.apply_provider_status(call, "no-answer")
    assert result.applied is False
    assert call.status is CallStatus.COMPLETED


def test_duration_never_shrinks():
    call = make_call(CallStatus.IN_PROGRESS)
    call_state.apply_provider_status(call, "completed", duration_seconds=143)
    call_state.apply_provider_status(call, "completed", duration_seconds=0)
    assert call.duration_seconds == 143


def test_ended_at_can_be_supplied_explicitly():
    call = make_call(CallStatus.IN_PROGRESS)
    when = datetime.utcnow() - timedelta(minutes=5)
    call_state.apply_status(call, CallStatus.COMPLETED, ended_at=when)
    assert call.ended_at == when


def test_non_terminal_transition_does_not_set_ended_at():
    call = make_call(CallStatus.RINGING)
    call_state.apply_status(call, CallStatus.IN_PROGRESS)
    assert call.ended_at is None


# --------------------------------------------------------- provider mapping ---

@pytest.mark.parametrize("raw,expected", [
    ("completed", CallStatus.COMPLETED),
    ("no-answer", CallStatus.NO_ANSWER),
    ("busy", CallStatus.FAILED),
    ("failed", CallStatus.FAILED),
    ("canceled", CallStatus.FAILED),
    ("cancelled", CallStatus.FAILED),
    ("in-progress", CallStatus.IN_PROGRESS),
    ("ringing", CallStatus.RINGING),
    ("queued", CallStatus.RINGING),
    ("initiated", CallStatus.RINGING),
    ("COMPLETED", CallStatus.COMPLETED),      # case-insensitive
    (" busy ", CallStatus.FAILED),            # whitespace tolerant
])
def test_provider_status_mapping(raw, expected):
    assert call_state.map_provider_status(raw) is expected


@pytest.mark.parametrize("raw", ["", None, "wat", "in-progresss", "hangup"])
def test_unknown_provider_status_maps_to_nothing(raw):
    assert call_state.map_provider_status(raw) is None


def test_unknown_provider_status_cannot_terminate_a_live_call():
    """Previously an unrecognised string defaulted to COMPLETED."""
    call = make_call(CallStatus.IN_PROGRESS)
    result = call_state.apply_provider_status(call, "something-new-from-twilio")
    assert result.applied is False
    assert result.reason == "unknown_status"
    assert call.status is CallStatus.IN_PROGRESS


def test_failure_reason_is_recorded_for_bad_endings():
    call = make_call(CallStatus.RINGING)
    call_state.apply_provider_status(call, "busy")
    assert call.status is CallStatus.FAILED
    assert call.failure_reason == "busy"


def test_failure_reason_is_not_set_on_a_clean_completion():
    call = make_call(CallStatus.IN_PROGRESS)
    call_state.apply_provider_status(call, "completed", duration_seconds=30)
    assert call.failure_reason is None


def test_can_transition_allows_self():
    assert call_state.can_transition(CallStatus.COMPLETED, CallStatus.COMPLETED)
