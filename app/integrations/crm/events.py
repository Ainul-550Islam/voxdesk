"""
Normalized events and idempotency.

The central rule of this step: **a business fact is recorded once, delivery is
attempted many times.** The old code conflated the two — `call.crm_synced` was
set before the POST, so a crash in between lost the event permanently while
still claiming it was sent.

Here, `emit()` writes a `CrmEvent` and a `CrmSync` row per enabled
integration, inside the caller's transaction. Nothing is sent. A worker picks
the syncs up afterwards. If the process dies immediately after the commit, the
event survives and is delivered late; if it dies before, the business fact was
never committed either, so there is nothing to lose.

**Idempotency keys are derived, not generated.** A random uuid per emit would
make every duplicate call a new event, which is exactly the failure mode
requirement 12 lists: duplicate webhooks, worker restarts, and API timeouts
after the provider already accepted. The key is a pure function of the
business fact, so the same fact computes the same key forever, and the unique
constraint on `(tenant_id, idempotency_key)` turns a duplicate emit into a
no-op at the database level rather than a race in Python.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import log
from app.db.models import (
    CrmEntityType,
    CrmEvent,
    CrmEventType,
    CrmIntegration,
    CrmSync,
    CrmSyncStatus,
)

#: Bumped when the shape of `CrmEvent.payload` changes incompatibly, so an
#: adapter can tell an old queued row from a new one after a deploy.
PAYLOAD_VERSION = 1

#: Which entity each event type is about. Derived rather than passed in, so a
#: caller cannot file a `lead.created` against a call id.
_ENTITY_FOR_EVENT: dict[CrmEventType, CrmEntityType] = {
    CrmEventType.CALL_COMPLETED: CrmEntityType.CALL,
    CrmEventType.CALL_MISSED: CrmEntityType.CALL,
    CrmEventType.TRANSFER_COMPLETED: CrmEntityType.CALL,
    CrmEventType.LEAD_CREATED: CrmEntityType.LEAD,
    CrmEventType.LEAD_UPDATED: CrmEntityType.LEAD,
    CrmEventType.APPOINTMENT_BOOKED: CrmEntityType.APPOINTMENT,
    CrmEventType.APPOINTMENT_CANCELLED: CrmEntityType.APPOINTMENT,
}


def entity_type_for(event_type: CrmEventType) -> CrmEntityType:
    return _ENTITY_FOR_EVENT[event_type]


def idempotency_key(
    event_type: CrmEventType, entity_id: uuid.UUID | str, *, discriminator: str = ""
) -> str:
    """
    The stable identity of one business fact.

    `"{event}:{entity}"` and nothing else — deliberately not hashed, not
    timestamped, not random. Two properties follow, and both are load-bearing:

    * **Deterministic.** The same call completing twice (a retried Twilio
      webhook) computes the same key, so the second emit collides and is
      dropped.
    * **Legible.** `call.completed:3f2a...` in a log or a support query says
      what it is. A sha256 would need a lookup to mean anything.

    `discriminator` exists for facts that genuinely can recur for one entity:
    an appointment rescheduled twice is two real events on the same row, so
    the timestamp of the change distinguishes them.
    """
    base = f"{event_type.value}:{entity_id}"
    return f"{base}:{discriminator}" if discriminator else base


async def emit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_type: CrmEventType,
    entity_id: uuid.UUID,
    payload: dict[str, Any],
    discriminator: str = "",
) -> CrmEvent | None:
    """
    Record a business event and queue it for every enabled integration.

    Returns the event, or `None` when this exact fact was already recorded.
    `None` is a success: it means idempotency did its job.

    Does **not** commit. The caller owns the transaction, which is the point —
    the event and the thing that caused it land together or not at all.
    """
    key = idempotency_key(event_type, entity_id, discriminator=discriminator)

    existing = (
        await session.execute(
            select(CrmEvent).where(
                CrmEvent.tenant_id == tenant_id,
                CrmEvent.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        log.info(
            "crm.event_duplicate", tenant_id=str(tenant_id),
            event_type=event_type.value, idempotency_key=key,
        )
        return None

    event = CrmEvent(
        tenant_id=tenant_id,
        event_type=event_type,
        entity_type=entity_type_for(event_type),
        entity_id=entity_id,
        idempotency_key=key,
        payload=payload,
        payload_version=PAYLOAD_VERSION,
    )
    session.add(event)

    try:
        # Materialise the event id so the sync rows can reference it, and let
        # the unique constraint arbitrate if a concurrent request got here
        # first. The SELECT above is an optimisation; *this* is the guarantee.
        await session.flush()
    except IntegrityError:
        await session.rollback()
        log.info(
            "crm.event_duplicate_race", tenant_id=str(tenant_id),
            event_type=event_type.value, idempotency_key=key,
        )
        return None

    integrations = await _subscribed_integrations(session, tenant_id, event_type)
    for integration in integrations:
        session.add(
            CrmSync(
                tenant_id=tenant_id,
                event_id=event.id,
                integration_id=integration.id,
                provider=integration.provider,
                entity_type=event.entity_type,
                entity_id=entity_id,
                status=CrmSyncStatus.PENDING,
            )
        )

    log.info(
        "crm.event_emitted", tenant_id=str(tenant_id), event_type=event_type.value,
        entity_id=str(entity_id), idempotency_key=key, queued=len(integrations),
    )
    return event


async def _subscribed_integrations(
    session: AsyncSession, tenant_id: uuid.UUID, event_type: CrmEventType
) -> list[CrmIntegration]:
    """
    Enabled integrations for this tenant that want this event.

    The `tenant_id` predicate is the isolation boundary. An empty
    `subscribed_events` means "everything", because the alternative — a tenant
    connecting a CRM and receiving nothing until they also tick seven boxes —
    is a support ticket rather than a feature.
    """
    rows = (
        (
            await session.execute(
                select(CrmIntegration).where(
                    CrmIntegration.tenant_id == tenant_id,
                    CrmIntegration.is_enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        integration for integration in rows
        if not integration.subscribed_events
        or event_type.value in integration.subscribed_events
    ]


# ------------------------------------------------------------- payloads ---

def call_payload(call: Any, tenant: Any, *, include_transcript_reference: bool = True) -> dict:
    """
    Safe metadata for a call event (requirement 18).

    Note the shape of the transcript field: a **reference**, never the
    transcript. The brief is explicit that full transcripts must not be
    blindly sent to every provider, and a call transcript is the most
    sensitive thing this product holds — a caller reciting a card number, a
    diagnosis, an address. Providers that should receive the text get it via
    the per-integration `share_transcripts` flag, applied in `service.py`
    where the integration is in scope. This function never has it.
    """
    payload = {
        "call_id": str(call.id),
        "call_sid": call.call_sid,
        "direction": getattr(call.direction, "value", str(call.direction)),
        "status": getattr(call.status, "value", str(call.status)),
        "from_number": call.from_number,
        "to_number": call.to_number,
        "duration_seconds": round(call.duration_seconds or 0.0, 1),
        "intent": call.intent,
        "summary": call.summary or "",
        "booked": bool(call.booked),
        "transferred": bool(call.escalated),
        "lead_score": call.lead_score,
        "recording_url": call.recording_url,
        "business": tenant.name if tenant else "",
    }
    if include_transcript_reference:
        payload["transcript_reference"] = f"voxdesk:call:{call.id}:turns"
    return payload


def lead_payload(lead: Any) -> dict:
    return {
        "lead_id": str(lead.id),
        "name": lead.name or "",
        "phone": lead.phone,
        "email": lead.email,
        "company": lead.company,
        "status": getattr(lead.status, "value", str(lead.status)),
        "score": lead.score,
        "notes": lead.notes or "",
        "campaign_id": str(lead.campaign_id) if lead.campaign_id else None,
        "custom_fields": dict(lead.custom_fields or {}),
    }


def appointment_payload(appointment: Any) -> dict:
    return {
        "appointment_id": str(appointment.id),
        "call_id": str(appointment.call_id) if appointment.call_id else None,
        "customer_name": appointment.customer_name,
        "customer_phone": appointment.customer_phone,
        "reason": appointment.reason or "",
        "starts_at": appointment.starts_at.isoformat() if appointment.starts_at else None,
        "ends_at": appointment.ends_at.isoformat() if appointment.ends_at else None,
    }

