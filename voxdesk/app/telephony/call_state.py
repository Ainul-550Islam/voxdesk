"""
The single source of truth for call lifecycle state.

Before this module, call status was written from four different places --
`incoming_call`, `media_stream`'s exception handler, `call_status`, and
`place_call` -- each with its own idea of what was legal. The status webhook in
particular did:

    call.status = _TERMINAL_STATUS.get(CallStatus_, CallStatus.COMPLETED)

which means a duplicate or late Twilio callback would happily rewrite a
TRANSFERRED call as COMPLETED, and an unrecognised status string would silently
be recorded as a successful completion. Twilio retries webhooks, and callbacks
from a parent call and a transfer leg can arrive out of order, so both of those
things happen in production rather than in theory.

Everything here is pure state-machine logic over a `Call` row. There are no
HTTP concerns and no provider SDK imports, so the rules can be tested directly.

Terminology
-----------
*Terminal* states are COMPLETED, FAILED and NO_ANSWER. Once a call reaches one,
nothing may move it anywhere else.

TRANSFERRED is deliberately **not** terminal: a transferred call is still up,
just with a human on it, and it must still be allowed to reach COMPLETED (or
FAILED) when the human hangs up.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog

from app.core import observability
from app.db.models import Call, CallStatus

log = structlog.get_logger()


# --------------------------------------------------------------- the graph ---

#: Every transition this application considers legal.
ALLOWED_TRANSITIONS: dict[CallStatus, frozenset[CallStatus]] = {
    CallStatus.RINGING: frozenset({
        CallStatus.IN_PROGRESS,
        CallStatus.NO_ANSWER,
        CallStatus.FAILED,
        # Twilio can report a call as completed straight from ringing when the
        # caller hangs up during ringback.
        CallStatus.COMPLETED,
    }),
    CallStatus.IN_PROGRESS: frozenset({
        CallStatus.TRANSFERRED,
        CallStatus.COMPLETED,
        CallStatus.FAILED,
    }),
    CallStatus.TRANSFERRED: frozenset({
        # The human hung up, or the transfer leg died after connecting.
        CallStatus.COMPLETED,
        CallStatus.FAILED,
    }),
    # Terminal.
    CallStatus.COMPLETED: frozenset(),
    CallStatus.FAILED: frozenset(),
    CallStatus.NO_ANSWER: frozenset(),
}

TERMINAL_STATUSES: frozenset[CallStatus] = frozenset({
    CallStatus.COMPLETED,
    CallStatus.FAILED,
    CallStatus.NO_ANSWER,
})

#: Provider status string -> our enum. Twilio's vocabulary, lowercased.
PROVIDER_STATUS_MAP: dict[str, CallStatus] = {
    "queued": CallStatus.RINGING,
    "initiated": CallStatus.RINGING,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.IN_PROGRESS,
    "in_progress": CallStatus.IN_PROGRESS,
    "answered": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "no-answer": CallStatus.NO_ANSWER,
    "no_answer": CallStatus.NO_ANSWER,
    "busy": CallStatus.FAILED,
    "failed": CallStatus.FAILED,
    "canceled": CallStatus.FAILED,
    "cancelled": CallStatus.FAILED,
}

#: Provider statuses that explain *why* a call ended badly. Stored verbatim so
#: support can tell "nobody picked up" apart from "the number is disconnected".
FAILURE_REASONS = {"busy", "failed", "canceled", "cancelled", "no-answer", "no_answer"}


def is_terminal(status: CallStatus) -> bool:
    return status in TERMINAL_STATUSES


def can_transition(current: CallStatus, target: CallStatus) -> bool:
    """A transition to the same state is always allowed (it is a no-op)."""
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def map_provider_status(raw: str | None) -> CallStatus | None:
    """
    Translate a provider status string. Returns None for anything unknown.

    Returning None rather than defaulting to COMPLETED is the whole point: an
    unrecognised string must not be able to terminate a live call.
    """
    if not raw:
        return None
    return PROVIDER_STATUS_MAP.get(raw.strip().lower())


# ------------------------------------------------------------------ result ---

@dataclass(frozen=True)
class TransitionResult:
    """What `apply_status` actually did. Never raises; always reports."""

    applied: bool
    previous: CallStatus
    current: CallStatus
    reason: str

    @property
    def ignored(self) -> bool:
        return not self.applied


# ------------------------------------------------------------------- apply ---

def apply_status(
    call: Call,
    target: CallStatus,
    *,
    reason: str = "",
    duration_seconds: float | None = None,
    ended_at: datetime | None = None,
    source: str = "unknown",
) -> TransitionResult:
    """
    Move `call` to `target` if that is legal. Idempotent and non-raising.

    The caller is responsible for committing the session. This function only
    mutates the in-memory row so it can be composed inside a larger unit of
    work (for example: transition + write a transcript event + bill minutes,
    all in one transaction).

    Outcomes:
        applied=True   the status changed
        applied=False  duplicate (same status), terminal, or illegal
    """
    previous = call.status

    # 1. Duplicate callback. Twilio retries; this is the common case, not an
    #    error. We still refresh duration/ended_at because a retry sometimes
    #    carries a more complete payload than the first delivery.
    if previous == target:
        _stamp(call, duration_seconds, ended_at, target)
        log.debug(
            "call_state.duplicate",
            call_id=str(call.id), call_sid=call.call_sid,
            status=target.value, source=source,
        )
        return TransitionResult(False, previous, previous, "duplicate")

    # 2. Already finished. A late webhook must not resurrect or rewrite it.
    if is_terminal(previous):
        log.warning(
            "call_state.terminal_protected",
            call_id=str(call.id), call_sid=call.call_sid,
            current=previous.value, rejected=target.value, source=source,
        )
        return TransitionResult(False, previous, previous, "terminal")

    # 3. Illegal edge, e.g. COMPLETED -> RINGING or FAILED -> IN_PROGRESS.
    if not can_transition(previous, target):
        log.warning(
            "call_state.illegal_transition",
            call_id=str(call.id), call_sid=call.call_sid,
            current=previous.value, rejected=target.value, source=source,
        )
        return TransitionResult(False, previous, previous, "illegal")

    call.status = target
    _stamp(call, duration_seconds, ended_at, target)

    # Step 7 observability: count the call-shaped signals once, at the exact
    # transition that produced them. Bounded labels only — the call id/SID
    # stay in the log line below, never in a Prometheus label.
    if target is CallStatus.IN_PROGRESS and previous is CallStatus.RINGING:
        observability.record_call_answered()
    if is_terminal(target):
        observability.record_call_outcome(target.value)
        observability.observe_call_duration(call.duration_seconds)

    if reason and target in (CallStatus.FAILED, CallStatus.NO_ANSWER):
        # Only record a failure reason where one is meaningful. `summary` is
        # the caller-visible outcome field and is left alone.
        call.failure_reason = reason[:120]

    log.info(
        "call_state.transition",
        call_id=str(call.id), call_sid=call.call_sid,
        previous=previous.value, current=target.value,
        reason=reason or None, source=source,
    )
    return TransitionResult(True, previous, target, "applied")


def _stamp(
    call: Call,
    duration_seconds: float | None,
    ended_at: datetime | None,
    target: CallStatus,
) -> None:
    """
    Update timestamps and duration without ever losing information.

    Duration only ever grows: a retry that reports 0 seconds must not erase a
    previously recorded 143 seconds. `ended_at` is written once and then left
    alone, so the first authoritative end time wins.
    """
    if duration_seconds is not None and duration_seconds > (call.duration_seconds or 0):
        call.duration_seconds = duration_seconds

    if is_terminal(target) and call.ended_at is None:
        call.ended_at = ended_at or datetime.utcnow()


def apply_provider_status(
    call: Call,
    raw_status: str | None,
    *,
    duration_seconds: float | None = None,
    source: str = "provider",
) -> TransitionResult:
    """
    Apply a raw provider status string.

    An unknown string is ignored rather than guessed at, because guessing here
    means terminating somebody's live call on a typo.
    """
    target = map_provider_status(raw_status)
    if target is None:
        log.warning(
            "call_state.unknown_provider_status",
            call_id=str(call.id), call_sid=call.call_sid,
            raw=raw_status, source=source,
        )
        return TransitionResult(False, call.status, call.status, "unknown_status")

    normalized = (raw_status or "").strip().lower()
    reason = normalized if normalized in FAILURE_REASONS else ""
    return apply_status(
        call, target,
        reason=reason,
        duration_seconds=duration_seconds,
        source=source,
    )