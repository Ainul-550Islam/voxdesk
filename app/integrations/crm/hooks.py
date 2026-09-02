"""
Where VoxDesk business events become CRM events.

One module, imported by the telephony and agent layers, so that "which
lifecycle moments reach a CRM" is a question with a single answer rather than
seven `emit()` calls scattered across the codebase.

Every function here obeys the same three rules:

* **It never raises.** Requirement 28: a CRM problem is a backend integration
  problem and the caller — a live phone call — must not hear about it or be
  delayed by it.
* **It never sends anything.** It writes rows. The worker sends. That is what
  makes a crash between "the call ended" and "the CRM knows" recoverable.
* **It does not commit.** The caller owns the transaction, so the event and
  the business change it describes land together.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import log
from app.db.models import CrmEventType
from app.integrations.crm import events


async def _safe_emit(
    session: AsyncSession, *, tenant_id, event_type: CrmEventType,
    entity_id: uuid.UUID, payload: dict, discriminator: str = "",
) -> bool:
    """
    Emit, swallowing everything.

    A bare `except Exception` is usually a smell. Here it is the requirement:
    this runs inside the Twilio status webhook and inside the voice pipeline's
    function handlers, and there is no CRM-shaped problem that should turn a
    completed call into a 500 or leave a caller listening to silence.
    """
    try:
        event = await events.emit(
            session, tenant_id=tenant_id, event_type=event_type,
            entity_id=entity_id, payload=payload, discriminator=discriminator,
        )
        return event is not None
    except Exception as exc:
        log.error(
            "crm.emit_failed", event_type=event_type.value,
            entity_id=str(entity_id), error=type(exc).__name__,
        )
        return False


# --------------------------------------------------------------------- call ---

async def on_call_completed(session: AsyncSession, tenant, call) -> bool:
    """
    A call finished normally.

    The idempotency key is derived from the call id, so a retried Twilio
    status callback — which is routine, not exceptional — produces no second
    event and therefore no second CRM contact.
    """
    return await _safe_emit(
        session, tenant_id=call.tenant_id, event_type=CrmEventType.CALL_COMPLETED,
        entity_id=call.id, payload=events.call_payload(call, tenant),
    )


async def on_call_missed(session: AsyncSession, tenant, call) -> bool:
    """
    Nobody was reached: no answer, busy, or failed.

    Arguably the most commercially valuable event in the product. A missed
    call at a home-service business is a job that went to a competitor, and
    the point of pushing it to the CRM within seconds is that somebody can
    call back before that happens.
    """
    return await _safe_emit(
        session, tenant_id=call.tenant_id, event_type=CrmEventType.CALL_MISSED,
        entity_id=call.id, payload=events.call_payload(call, tenant),
    )


async def on_transfer_completed(session: AsyncSession, tenant, call) -> bool:
    """A human picked up the transfer."""
    payload = events.call_payload(call, tenant)
    payload["transfer_state"] = getattr(
        call.transfer_state, "value", str(call.transfer_state)
    )
    return await _safe_emit(
        session, tenant_id=call.tenant_id,
        event_type=CrmEventType.TRANSFER_COMPLETED, entity_id=call.id,
        payload=payload,
    )


# --------------------------------------------------------------------- lead ---

async def on_lead_created(session: AsyncSession, lead) -> bool:
    return await _safe_emit(
        session, tenant_id=lead.tenant_id, event_type=CrmEventType.LEAD_CREATED,
        entity_id=lead.id, payload=events.lead_payload(lead),
    )


async def on_lead_updated(session: AsyncSession, lead, *, reason: str = "") -> bool:
    """
    A lead changed.

    Unlike creation, an update genuinely can happen many times for one lead,
    so the idempotency key needs a discriminator or the second update would be
    swallowed as a duplicate. `reason` is that discriminator: the caller
    passes something stable and meaningful for the change ("status:qualified",
    "score:80"), which makes the key both unique per real change and idempotent
    across retries of the same change.
    """
    discriminator = reason or f"status:{getattr(lead.status, 'value', lead.status)}"
    return await _safe_emit(
        session, tenant_id=lead.tenant_id, event_type=CrmEventType.LEAD_UPDATED,
        entity_id=lead.id, payload=events.lead_payload(lead),
        discriminator=discriminator,
    )


# -------------------------------------------------------------- appointment ---

async def on_appointment_booked(session: AsyncSession, appointment) -> bool:
    return await _safe_emit(
        session, tenant_id=appointment.tenant_id,
        event_type=CrmEventType.APPOINTMENT_BOOKED, entity_id=appointment.id,
        payload=events.appointment_payload(appointment),
    )


async def on_appointment_cancelled(session: AsyncSession, appointment) -> bool:
    return await _safe_emit(
        session, tenant_id=appointment.tenant_id,
        event_type=CrmEventType.APPOINTMENT_CANCELLED, entity_id=appointment.id,
        payload=events.appointment_payload(appointment),
    )


async def on_appointment_rescheduled(
    session: AsyncSession, appointment, *, previous_start: Any = None
) -> bool:
    """
    A booking moved.

    The current model has no distinct "rescheduled" state, so this is emitted
    as a fresh `appointment.booked` discriminated by the new start time. Two
    reschedules produce two events; the same reschedule retried produces one.
    """
    return await _safe_emit(
        session, tenant_id=appointment.tenant_id,
        event_type=CrmEventType.APPOINTMENT_BOOKED, entity_id=appointment.id,
        payload={
            **events.appointment_payload(appointment),
            "rescheduled_from": (
                previous_start.isoformat() if hasattr(previous_start, "isoformat")
                else previous_start
            ),
        },
        discriminator=f"rescheduled:{appointment.starts_at.isoformat()}",
    )

