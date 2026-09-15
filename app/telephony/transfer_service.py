"""
Human transfer orchestration.

This is the module that makes `escalate_to_human` mean something. Before it,
the tool set `call.escalated = True`, returned `{"action": "transfer"}` and
told the caller "let me put you through" -- and nothing else happened. The
comment in `functions.py` claimed the pipeline watched for `action ==
"transfer"`; it did not. `transfer.execute_transfer` had no callers at all.

The rule this module exists to enforce:

    A transfer is only "transferred" once the provider has accepted the
    redirect. The AI asking for a transfer is a *request*, not an outcome.

So the flow is deliberately two-phase:

    REQUESTED  -- intent recorded, destination validated, nothing dialled yet
    DIALING    -- provider accepted the redirect; the human's phone is ringing
    CONNECTED  -- the human answered (proved by a provider callback)
    FAILED     -- validation, provider, or the human not picking up

`CallStatus.TRANSFERRED` is only set at DIALING, i.e. after the provider has
actually taken the instruction. It is never set from REQUESTED.

Idempotency is enforced by `Call.transfer_state`. Any second request while a
transfer is in flight returns the existing state instead of placing another
call to the human. That matters: LLMs retry tool calls, and a duplicate here
means a real second phone ringing on somebody's desk.
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    TRANSFER_IN_FLIGHT,
    Call,
    CallStatus,
    Speaker,
    Tenant,
    TransferState,
    Turn,
)
from app.telephony import call_state, phone
from app.telephony.provider import TelephonyProvider, get_provider
from app.telephony.transfer import transfer_twiml

log = structlog.get_logger()


class TransferOutcome(str, enum.Enum):
    """
    The internal result vocabulary the agent layer reasons about.

    These are internal codes. What the caller *hears* is a separate, friendly
    sentence -- raw provider errors never reach a phone line.
    """

    TRANSFER_STARTED = "TRANSFER_STARTED"
    TRANSFER_COMPLETED = "TRANSFER_COMPLETED"
    TRANSFER_FAILED = "TRANSFER_FAILED"
    ALREADY_TRANSFERRED = "ALREADY_TRANSFERRED"


#: Prefix marking a failure we *inferred* rather than one the provider told us
#: about. If the parent call ends while the human's phone is still ringing we
#: assume the transfer failed -- but the authoritative <Dial> callback can
#: arrive afterwards and say otherwise, and the provider always wins over our
#: guess. Provider-reported failures ("busy", "no-answer") carry no prefix and
#: are never overridden.
INFERRED_PREFIX = "inferred: "


#: Why a transfer could not be attempted. Kept separate from the outcome so
#: logs and tests can distinguish "no number configured" from "Twilio 500".
class TransferError(str, enum.Enum):
    NO_DESTINATION = "no_destination"
    INVALID_DESTINATION = "invalid_destination"
    CALL_ALREADY_ENDED = "call_already_ended"
    CALL_NOT_TRANSFERABLE = "call_not_transferable"
    PROVIDER_ERROR = "provider_error"
    TENANT_MISMATCH = "tenant_mismatch"


@dataclass(frozen=True)
class TransferResult:
    outcome: TransferOutcome
    #: Sentence safe to speak to the caller. Never contains provider detail.
    message: str
    state: TransferState
    error: TransferError | None = None
    destination: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in (
            TransferOutcome.TRANSFER_STARTED,
            TransferOutcome.TRANSFER_COMPLETED,
            TransferOutcome.ALREADY_TRANSFERRED,
        )

    def as_tool_result(self) -> dict:
        """
        Shape handed back to the LLM.

        Only the friendly message and the internal outcome code cross this
        boundary -- no provider error strings, no phone numbers.
        """
        return {
            "ok": self.ok,
            "outcome": self.outcome.value,
            "message": self.message,
        }


# What the caller hears. Deliberately generic.
_CALLER_MESSAGE = {
    TransferOutcome.TRANSFER_STARTED:
        "Sure, connecting you to someone now. One moment.",
    TransferOutcome.ALREADY_TRANSFERRED:
        "I'm already connecting you. One moment.",
    TransferOutcome.TRANSFER_FAILED:
        "I'm sorry, I can't reach anyone right now. "
        "I can take a message and have someone call you back.",
}


# --------------------------------------------------------------- utilities ---

def transfer_available(tenant: Tenant, call: Call) -> bool:
    """
    Whether the escalation tool should be offered to the model at all.

    Hiding the tool when it cannot work stops the AI from promising a transfer
    it is about to fail to deliver -- which is worse than never offering it.
    """
    if not phone.is_valid(tenant.escalation_number):
        return False
    if call_state.is_terminal(call.status):
        return False
    # Kept as an explicit ladder rather than a single boolean expression:
    # each clause is a distinct business rule and gets its own line.
    return call.transfer_state not in TRANSFER_IN_FLIGHT


def resolve_destination(tenant: Tenant) -> tuple[str | None, TransferError | None]:
    """
    The destination always comes from the tenant row -- never from the model,
    never from a request body. That is what makes cross-tenant dialling
    impossible rather than merely discouraged.
    """
    raw = tenant.escalation_number
    if not raw or not raw.strip():
        return None, TransferError.NO_DESTINATION
    try:
        return phone.normalize(raw), None
    except phone.InvalidPhoneNumber:
        return None, TransferError.INVALID_DESTINATION


async def add_system_event(session: AsyncSession, call: Call, text: str) -> Turn | None:
    """
    Append a `Speaker.SYSTEM` transcript event, at most once per exact text.

    The de-duplication is what keeps a retried Twilio callback from writing
    "Human transfer connected." into the transcript three times.

    The existence check is an explicit SELECT rather than a walk over
    `call.turns`: the relationship is lazy, and touching it from async code
    raises MissingGreenlet. Flushing first means an event added earlier in this
    same uncommitted transaction is still seen.
    """
    await session.flush()
    existing = (
        await session.execute(
            select(Turn.id).where(
                Turn.call_id == call.id,
                Turn.speaker == Speaker.SYSTEM,
                Turn.text == text,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    turn = Turn(call_id=call.id, speaker=Speaker.SYSTEM, text=text)
    session.add(turn)
    await session.flush()
    return turn


SYSTEM_EVENT = {
    "requested": "Human transfer requested.",
    "started": "Human transfer started.",
    "connected": "Human transfer connected.",
    "failed": "Human transfer failed.",
}


# ------------------------------------------------------------------ request ---

async def request_transfer(
    session: AsyncSession,
    tenant: Tenant,
    call: Call,
    *,
    reason: str = "",
    provider: TelephonyProvider | None = None,
    whisper: str | None = None,
) -> TransferResult:
    """
    Validate, record intent, and actually perform the transfer.

    Never raises: a crash here would drop a live phone call. Every failure path
    returns a `TransferResult` and leaves the row in a consistent state.
    """
    provider = provider or get_provider()
    log_ctx = {
        "call_id": str(call.id),
        "call_sid": call.call_sid,
        "tenant_id": str(call.tenant_id),
        "transfer_attempt": call.transfer_attempts + 1,
    }

    # -- 0. tenant scoping -------------------------------------------------
    # Defence in depth. The caller already loaded both rows, but a mismatch
    # here would mean dialling one tenant's human about another's call.
    if call.tenant_id != tenant.id:
        log.error("invalid_transfer_state", reason="tenant_mismatch", **log_ctx)
        return _fail_result(TransferError.TENANT_MISMATCH, call.transfer_state)

    # -- 1. idempotency ----------------------------------------------------
    if call.transfer_state in TRANSFER_IN_FLIGHT:
        log.info("transfer_duplicate", state=call.transfer_state.value, **log_ctx)
        outcome = (
            TransferOutcome.TRANSFER_COMPLETED
            if call.transfer_state is TransferState.CONNECTED
            else TransferOutcome.ALREADY_TRANSFERRED
        )
        return TransferResult(
            outcome=outcome,
            message=_CALLER_MESSAGE[TransferOutcome.ALREADY_TRANSFERRED],
            state=call.transfer_state,
            destination=call.transfer_destination,
        )

    # -- 2. is the call still transferable? --------------------------------
    if call_state.is_terminal(call.status):
        log.warning("invalid_transfer_state", reason="call_ended",
                    status=call.status.value, **log_ctx)
        return _fail_result(TransferError.CALL_ALREADY_ENDED, call.transfer_state)

    if not call_state.can_transition(call.status, CallStatus.TRANSFERRED):
        log.warning("invalid_transfer_state", reason="not_transferable",
                    status=call.status.value, **log_ctx)
        return _fail_result(TransferError.CALL_NOT_TRANSFERABLE, call.transfer_state)

    # -- 3. destination ----------------------------------------------------
    destination, dest_error = resolve_destination(tenant)
    if dest_error is not None:
        await _record_failure(session, call, dest_error.value, error=dest_error)
        log.warning("transfer_failed", reason=dest_error.value, **log_ctx)
        await session.commit()
        return _fail_result(dest_error, call.transfer_state)

    # -- 4. record intent (REQUESTED) --------------------------------------
    now = datetime.utcnow()
    call.escalated = True
    call.intent = call.intent or "escalation"
    call.transfer_state = TransferState.REQUESTED
    call.transfer_requested_at = call.transfer_requested_at or now
    call.transfer_destination = destination
    call.transfer_reason = (reason or "")[:400] or None
    call.transfer_attempts += 1
    call.transfer_error = None
    await add_system_event(session, call, SYSTEM_EVENT["requested"])
    # Committed before touching the provider so that if the process dies
    # mid-redirect we still know a transfer was attempted.
    await session.commit()

    log.info("transfer_requested", destination=phone.redact(destination),
             reason=(reason or "")[:80], **log_ctx)

    # -- 5. perform the actual transfer ------------------------------------
    twiml = transfer_twiml(
        destination,
        whisper=whisper if whisper is not None else _default_whisper(tenant, reason),
        caller_id=phone.try_normalize(tenant.twilio_number),
        record=bool(tenant.record_calls),
    )
    result = await provider.redirect_call(call.call_sid, twiml)

    if not result.ok:
        await _record_failure(
            session, call,
            f"{result.error_code}: {result.error_message}"[:300],
            error=TransferError.PROVIDER_ERROR,
        )
        await session.commit()
        # Provider detail is logged, not spoken and not returned to the LLM.
        log.error("transfer_failed", reason="provider_error",
                  error_code=result.error_code,
                  error_message=result.error_message, **log_ctx)
        return _fail_result(TransferError.PROVIDER_ERROR, call.transfer_state)

    # -- 6. provider accepted: now, and only now, it is TRANSFERRED --------
    call.transfer_state = TransferState.DIALING
    call.transfer_started_at = datetime.utcnow()
    call_state.apply_status(
        call, CallStatus.TRANSFERRED, source="transfer_service"
    )
    await add_system_event(session, call, SYSTEM_EVENT["started"])
    await session.commit()

    log.info("transfer_started", destination=phone.redact(destination), **log_ctx)
    return TransferResult(
        outcome=TransferOutcome.TRANSFER_STARTED,
        message=_CALLER_MESSAGE[TransferOutcome.TRANSFER_STARTED],
        state=TransferState.DIALING,
        destination=destination,
    )


# ----------------------------------------------------------------- callbacks ---

#: States from which an authoritative "the human answered" callback is accepted.
_CONNECTABLE = (TransferState.REQUESTED, TransferState.DIALING)


async def mark_transfer_connected(session: AsyncSession, call: Call) -> bool:
    """
    The human answered. Driven by a provider callback, never by the AI.

    Returns True if this changed anything, so callers can tell a real event
    from a retried webhook.

    One subtlety: the parent call's "completed" callback can beat the <Dial>
    callback, and when it does we will already have inferred a failure. That
    inference is a guess, and this callback is evidence, so an inferred failure
    is allowed to be corrected here. A failure the provider actually reported
    is not.
    """
    if call.transfer_state is TransferState.CONNECTED:
        log.info("transfer_callback", callback="connected", duplicate=True,
                 call_id=str(call.id), call_sid=call.call_sid)
        return False

    recoverable = (
        call.transfer_state is TransferState.FAILED
        and (call.transfer_error or "").startswith(INFERRED_PREFIX)
    )
    if call.transfer_state not in _CONNECTABLE and not recoverable:
        log.warning("invalid_transfer_state", callback="connected",
                    state=call.transfer_state.value,
                    call_id=str(call.id), call_sid=call.call_sid)
        return False

    if recoverable:
        log.info("transfer_callback", callback="connected",
                 corrects_inference=True,
                 call_id=str(call.id), call_sid=call.call_sid)
        call.transfer_failed_at = None
        call.transfer_error = None

    call.transfer_state = TransferState.CONNECTED
    call.transfer_completed_at = datetime.utcnow()
    await add_system_event(session, call, SYSTEM_EVENT["connected"])
    log.info("transfer_connected", call_id=str(call.id), call_sid=call.call_sid,
             tenant_id=str(call.tenant_id))
    return True


async def mark_transfer_failed(
    session: AsyncSession, call: Call, reason: str
) -> bool:
    """
    The human did not take the call: busy, no answer, rejected, or the leg
    errored. The customer's call itself may still be alive (our TwiML falls
    through to voicemail), so this does NOT terminate the call.
    """
    if call.transfer_state is TransferState.FAILED:
        log.info("transfer_callback", callback="failed", duplicate=True,
                 call_id=str(call.id), call_sid=call.call_sid)
        return False

    if call.transfer_state is TransferState.CONNECTED:
        # It already worked; a late failure callback for an earlier leg must
        # not rewrite that.
        log.warning("invalid_transfer_state", callback="failed_after_connected",
                    call_id=str(call.id), call_sid=call.call_sid)
        return False

    await _record_failure(session, call, reason, error=None)
    log.info("transfer_failed", reason=reason, call_id=str(call.id),
             call_sid=call.call_sid, tenant_id=str(call.tenant_id))
    return True


# ------------------------------------------------------------------ helpers ---

async def _record_failure(
    session: AsyncSession,
    call: Call,
    detail: str,
    *,
    error: TransferError | None,
) -> None:
    call.transfer_state = TransferState.FAILED
    call.transfer_failed_at = datetime.utcnow()
    call.transfer_error = (detail or "")[:300] or None
    call.escalated = True          # it was still attempted; keep it visible
    await add_system_event(session, call, SYSTEM_EVENT["failed"])


def _fail_result(error: TransferError, state: TransferState) -> TransferResult:
    return TransferResult(
        outcome=TransferOutcome.TRANSFER_FAILED,
        message=_CALLER_MESSAGE[TransferOutcome.TRANSFER_FAILED],
        state=state,
        error=error,
    )


#: Anything digit-like in a spoken whisper. See `_safe_whisper_reason`.
_DIGITS = re.compile(r"[\d+]{3,}")
_UNSAFE = re.compile(r"[<>&\"\']")


def _safe_whisper_reason(reason: str) -> str:
    """
    Sanitize free text before it is spoken to the human.

    `reason` originates from the LLM, which in turn is repeating what the
    caller said. Interpolating it raw into <Say> means a caller who says
    "tell them to call five five five..." gets the business's own receptionist
    to read an attacker-supplied number aloud to staff, and lets XML control
    characters into our TwiML. Neither is acceptable, so digit runs are
    dropped, markup characters are stripped, and the whole thing is capped.

    The full, unmodified reason is still stored in `Call.transfer_reason` for
    the dashboard -- it just never becomes speech.
    """
    cleaned = _UNSAFE.sub("", reason or "")
    cleaned = _DIGITS.sub("[number removed]", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:120]


def _default_whisper(tenant: Tenant, reason: str) -> str:
    """Played to the human only, before the caller is bridged in."""
    safe = _safe_whisper_reason(reason)
    if safe:
        return f"Transfer from your A I receptionist. Reason: {safe}"
    return "Transfer from your A I receptionist."