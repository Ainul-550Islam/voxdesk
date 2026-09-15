"""
Sync orchestration.

This is the only module that holds all the pieces at once: a tenant's
integration row, its decrypted credentials, an adapter, the retry policy, the
rate limiter, and the database. Everything else is deliberately unable to
reach across those boundaries.

The shape of one delivery:

    claim the sync row (PENDING/FAILED -> PROCESSING, conditionally)
      -> decrypt credentials for this (tenant, provider)
      -> build a fresh adapter
      -> resolve the contact: reuse the stored external id, or upsert
      -> perform the operation the event calls for
      -> persist external id + SYNCED, atomically
    on error: classify, decide retry, schedule or give up

Three rules that shaped it:

* **Never claim success without proof.** `_mark_synced` requires an
  `external_id`. A provider that returns 200 with no id is a failure to
  record, not a success.
* **Never let a CRM failure reach the caller.** Requirement 28. The public
  entry points return a result object and log; they do not raise into the
  voice pipeline.
* **Never widen scope from an id.** Every query here filters on `tenant_id`
  even when the id alone would be unique, because an id is not authorization.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import log
from app.core.metrics import record_side_effect
from app.db.models import (
    CrmContactLink,
    CrmEntityType,
    CrmEvent,
    CrmEventType,
    CrmIntegration,
    CrmProviderType,
    CrmSync,
    CrmSyncStatus,
    RETRYABLE_SYNC_STATUSES,
)
from app.integrations.crm import crypto, mapping
from app.integrations.crm.base import Capability, ProviderContext
from app.integrations.crm.errors import (
    CrmConfigurationError,
    CrmError,
    CrmUnsupportedOperation,
    safe_message,
)
from app.integrations.crm.models import (
    CrmResult,
    HealthResult,
    NormalizedActivity,
    NormalizedAppointment,
    NormalizedContact,
)
from app.integrations.crm.registry import build as build_provider
from app.integrations.crm.retry import limiter, policy_from_settings


@dataclass(frozen=True)
class SyncOutcome:
    """What happened to one sync attempt. Never raises; always reports."""

    sync_id: uuid.UUID
    status: CrmSyncStatus
    external_id: str | None = None
    error_code: str | None = None
    safe_error: str | None = None
    duration_ms: float = 0.0
    skipped_reason: str | None = None


# ---------------------------------------------------------------- context ---

def build_context(integration: CrmIntegration) -> ProviderContext:
    """
    Decrypt this integration's credentials and package them for an adapter.

    The AES-GCM associated data is `(tenant_id, provider)`, so a row whose
    ciphertext was copied from another tenant's integration fails to decrypt
    here rather than producing a usable token. That is the last line of the
    isolation guarantee and it lives in the cipher, not in a `WHERE` clause.
    """
    from app.core.config import settings

    credentials: dict[str, Any] = {}
    if integration.credentials_encrypted:
        key_ring = crypto.key_ring_from_settings()
        if key_ring is None:
            raise CrmConfigurationError(
                "CRM credential encryption is not configured on this instance, "
                "so stored credentials cannot be read"
            )
        credentials = crypto.decrypt_credentials(
            integration.credentials_encrypted,
            tenant_id=str(integration.tenant_id),
            provider=integration.provider.value,
            key_ring=key_ring,
        )

    return ProviderContext(
        tenant_id=str(integration.tenant_id),
        credentials=credentials,
        config=dict(integration.config or {}),
        field_mappings=dict(integration.field_mappings or {}),
        share_transcripts=bool(integration.share_transcripts),
        timeout_seconds=settings.crm_request_timeout_seconds,
    )


async def get_integration(
    session: AsyncSession, *, tenant_id: uuid.UUID, provider: CrmProviderType
) -> CrmIntegration | None:
    """
    The only sanctioned lookup.

    Keyed on `(tenant_id, provider)`, never on provider alone — requirement 5
    states that explicitly, and the unique constraint makes the pair a real
    key rather than a filter someone might forget.
    """
    return (
        await session.execute(
            select(CrmIntegration).where(
                CrmIntegration.tenant_id == tenant_id,
                CrmIntegration.provider == provider,
            )
        )
    ).scalar_one_or_none()


# ------------------------------------------------------------ contact link ---

async def _linked_contact_id(
    session: AsyncSession, *, tenant_id: uuid.UUID, provider: CrmProviderType,
    contact: NormalizedContact,
) -> tuple[str | None, str | None]:
    """
    Look up a previously-synced external contact id.

    Returns `(external_id, identity_hash)`. This is the mechanism that stops
    the tenth call from the same number becoming the tenth CRM contact: we
    remember what the provider gave us the first time and update that record
    instead of creating another.
    """
    fingerprint = contact.identity_hash(str(tenant_id))
    if fingerprint is None:
        return None, None

    link = (
        await session.execute(
            select(CrmContactLink).where(
                CrmContactLink.tenant_id == tenant_id,
                CrmContactLink.provider == provider,
                CrmContactLink.identity_hash == fingerprint,
            )
        )
    ).scalar_one_or_none()
    return (link.external_contact_id if link else None), fingerprint


async def _remember_contact(
    session: AsyncSession, *, tenant_id: uuid.UUID, provider: CrmProviderType,
    fingerprint: str | None, external_id: str, lead_id: uuid.UUID | None = None,
) -> None:
    if not fingerprint or not external_id:
        return

    existing = (
        await session.execute(
            select(CrmContactLink).where(
                CrmContactLink.tenant_id == tenant_id,
                CrmContactLink.provider == provider,
                CrmContactLink.identity_hash == fingerprint,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.external_contact_id = external_id
        existing.last_seen_at = datetime.utcnow()
        if lead_id and not existing.lead_id:
            existing.lead_id = lead_id
        return

    session.add(
        CrmContactLink(
            tenant_id=tenant_id, provider=provider, identity_hash=fingerprint,
            external_contact_id=external_id, lead_id=lead_id,
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        # Another worker linked the same identity first. Its id is as good as
        # ours; the unique constraint is doing its job.
        await session.rollback()


# ---------------------------------------------------------------- delivery ---

async def process_sync(session: AsyncSession, sync: CrmSync) -> SyncOutcome:
    """
    Attempt one delivery. Commits its own state transitions.

    Never raises. A CRM integration failing is a normal operating condition,
    and the worker loop must keep going.
    """
    started = time.perf_counter()
    policy = policy_from_settings()

    integration = await session.get(CrmIntegration, sync.integration_id)
    if integration is None or integration.tenant_id != sync.tenant_id:
        # Defensive: the FK makes the first half unlikely and the second
        # impossible, but a sync must never run against a mismatched tenant.
        return await _mark_permanent(
            session, sync, code="misconfigured",
            message="the integration for this sync no longer exists",
            started=started,
        )
    if not integration.is_enabled:
        return await _skip(session, sync, "integration is disabled", started)

    if not limiter.allow(str(sync.tenant_id), sync.provider.value):
        # Not a failure: reschedule and let another tenant's work through.
        delay = limiter.retry_after(str(sync.tenant_id), sync.provider.value)
        sync.next_attempt_at = datetime.utcnow() + timedelta(seconds=max(delay, 1.0))
        sync.status = CrmSyncStatus.PENDING
        await session.commit()
        return SyncOutcome(
            sync_id=sync.id, status=CrmSyncStatus.PENDING,
            skipped_reason="rate_limited_locally",
            duration_ms=_ms(started),
        )

    event = await session.get(CrmEvent, sync.event_id)
    if event is None or event.tenant_id != sync.tenant_id:
        return await _mark_permanent(
            session, sync, code="misconfigured",
            message="the event for this sync no longer exists", started=started,
        )

    # Step 6 (scale-compliance): claim the row atomically. Two workers that
    # both selected this sync from an overlapping pass must not both deliver;
    # a single conditional UPDATE lets exactly one win. All the pre-checks
    # above (integration, rate limit, event) already ran, so a lost claim here
    # simply means another worker owns the row.
    now = datetime.utcnow()
    claimed = await session.execute(
        update(CrmSync)
        .where(
            CrmSync.id == sync.id,
            CrmSync.status.in_(list(RETRYABLE_SYNC_STATUSES)),
        )
        .values(
            status=CrmSyncStatus.PROCESSING,
            attempt_count=CrmSync.attempt_count + 1,
            last_attempt_at=now,
        )
        # Do not let SQLAlchemy "evaluate" the increment back onto the ORM
        # object: the mirror below applies it exactly once.
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if claimed.rowcount != 1:
        record_side_effect("crm_sync", "duplicate")
        return SyncOutcome(
            sync_id=sync.id, status=sync.status,
            skipped_reason="claimed_by_another", duration_ms=_ms(started),
        )

    sync.status = CrmSyncStatus.PROCESSING
    sync.attempt_count += 1
    sync.last_attempt_at = now
    record_side_effect("crm_sync", "attempt")

    attempt = sync.attempt_count
    try:
        result = await _deliver(session, integration, event, sync)
    except CrmUnsupportedOperation as exc:
        # Not a failure to alarm on: the provider genuinely has no such
        # operation. Recorded as permanent so it stops, with a clear reason.
        return await _mark_permanent(
            session, sync, code=exc.code, message=exc.safe_message, started=started
        )
    except CrmError as exc:
        return await _handle_error(session, sync, exc, attempt, policy, started)
    except Exception as exc:
        # An adapter bug. Permanent, because retrying a `TypeError` five times
        # only delays the moment someone reads the log.
        log.error(
            "crm.sync_crashed", sync_id=str(sync.id), tenant_id=str(sync.tenant_id),
            provider=sync.provider.value, error=type(exc).__name__,
        )
        return await _mark_permanent(
            session, sync, code="internal",
            message=f"internal error ({type(exc).__name__})", started=started,
        )

    if result is None or not result.external_id:
        # Requirement: never claim a record was synced unless the provider
        # actually accepted it and told us what it created.
        return await _handle_error(
            session, sync,
            CrmError(f"{sync.provider.value} returned no external id"),
            attempt, policy, started,
        )

    return await _mark_synced(session, sync, result, started)


async def _deliver(
    session: AsyncSession, integration: CrmIntegration, event: CrmEvent, sync: CrmSync
) -> CrmResult | None:
    """Route one event to the right provider operation."""
    context = build_context(integration)
    provider = build_provider(integration.provider, context)
    payload = dict(event.payload or {})

    if event.event_type in (
        CrmEventType.APPOINTMENT_BOOKED, CrmEventType.APPOINTMENT_CANCELLED
    ):
        return await _deliver_appointment(
            session, provider, integration, event, payload
        )
    return await _deliver_contact_event(
        session, provider, integration, event, payload
    )


def _sources_for(event: CrmEvent, payload: dict, integration: CrmIntegration) -> dict:
    """
    The values a tenant's custom-field mapping may draw on.

    Assembled here rather than in `mapping.py` because this is where the
    integration's `share_transcripts` flag is in scope, and the transcript
    reference must not be offered to a provider the tenant did not opt in for.
    """
    sources = {
        "lead_score": payload.get("lead_score") or payload.get("score"),
        "call_summary": payload.get("summary"),
        "call_outcome": payload.get("status"),
        "call_id": payload.get("call_id"),
        "call_direction": payload.get("direction"),
        "call_duration_seconds": payload.get("duration_seconds"),
        "appointment_type": payload.get("reason"),
        "appointment_starts_at": payload.get("starts_at"),
        "source_campaign": payload.get("campaign_id"),
        "intent": payload.get("intent"),
        "booked": payload.get("booked"),
        "transferred": payload.get("transferred"),
        "recording_url": payload.get("recording_url"),
    }
    if integration.share_transcripts:
        sources["transcript_reference"] = payload.get("transcript_reference")
    return {k: v for k, v in sources.items() if v is not None}


async def _deliver_contact_event(
    session: AsyncSession, provider, integration: CrmIntegration,
    event: CrmEvent, payload: dict,
) -> CrmResult | None:
    tenant_id = integration.tenant_id
    custom_fields = mapping.resolve_custom_fields(
        integration.field_mappings or {}, _sources_for(event, payload, integration)
    )
    contact = _contact_for_event(event, payload, custom_fields)

    external_id, fingerprint = await _linked_contact_id(
        session, tenant_id=tenant_id, provider=integration.provider, contact=contact
    )

    # Reuse a known contact when we have one and the provider can update.
    if external_id and provider.supports(Capability.UPDATE_CONTACT):
        result = await provider.update_contact(external_id, contact)
    elif provider.supports(Capability.UPSERT_CONTACT):
        result = await provider.upsert_contact(contact)
    elif provider.supports(Capability.CREATE_CONTACT):
        result = await provider.create_contact(contact)
    else:
        raise CrmUnsupportedOperation(
            f"{provider.name} cannot write contacts", provider=provider.name
        )

    await _remember_contact(
        session, tenant_id=tenant_id, provider=integration.provider,
        fingerprint=fingerprint, external_id=result.external_id,
        lead_id=(
            event.entity_id if event.entity_type is CrmEntityType.LEAD else None
        ),
    )

    # Best-effort enrichment. A tag or a note failing must not undo a contact
    # that the provider already accepted -- the sync is SYNCED either way, and
    # a partial success recorded as a failure would be retried, creating the
    # duplicate note this whole layer exists to avoid.
    await _try_enrich(provider, result.external_id, contact, event, payload)

    await session.commit()
    return result


async def _try_enrich(
    provider, external_id: str, contact: NormalizedContact,
    event: CrmEvent, payload: dict,
) -> None:
    if contact.tags and provider.supports(Capability.ADD_TAG):
        try:
            await provider.add_tag(external_id, list(contact.tags))
        except CrmError as exc:
            log.warning(
                "crm.tag_failed", provider=provider.name, external_id=external_id,
                error_code=exc.code, error=exc.safe_message,
            )

    activity = _activity_for_event(event, payload)
    if activity and provider.supports(Capability.CREATE_NOTE):
        try:
            await provider.create_note(external_id, activity)
        except CrmError as exc:
            log.warning(
                "crm.note_failed", provider=provider.name, external_id=external_id,
                error_code=exc.code, error=exc.safe_message,
            )


async def _deliver_appointment(
    session: AsyncSession, provider, integration: CrmIntegration,
    event: CrmEvent, payload: dict,
) -> CrmResult | None:
    cancelled = event.event_type is CrmEventType.APPOINTMENT_CANCELLED
    needed = (
        Capability.CANCEL_APPOINTMENT if cancelled else Capability.CREATE_APPOINTMENT
    )
    if not provider.supports(needed):
        raise CrmUnsupportedOperation(
            f"{provider.name} has no calendar API for {needed.value}",
            provider=provider.name,
        )

    if cancelled:
        result = await provider.cancel_appointment(str(payload.get("appointment_id")))
        await session.commit()
        return result

    contact = NormalizedContact(
        *NormalizedContact.split_name(payload.get("customer_name") or ""),
        phone=payload.get("customer_phone"),
        source="VoxDesk Booking",
    )
    external_id, fingerprint = await _linked_contact_id(
        session, tenant_id=integration.tenant_id, provider=integration.provider,
        contact=contact,
    )
    if not external_id and provider.supports(Capability.UPSERT_CONTACT):
        upserted = await provider.upsert_contact(contact)
        external_id = upserted.external_id
        await _remember_contact(
            session, tenant_id=integration.tenant_id, provider=integration.provider,
            fingerprint=fingerprint, external_id=external_id,
        )

    appointment = NormalizedAppointment(
        title=payload.get("reason") or "Appointment",
        starts_at=_parse_dt(payload.get("starts_at")),
        ends_at=_parse_dt(payload.get("ends_at")),
        contact=contact,
        notes=payload.get("reason") or "",
    )
    result = await provider.create_appointment(
        appointment, external_contact_id=external_id
    )
    await session.commit()
    return result


def _contact_for_event(
    event: CrmEvent, payload: dict, custom_fields: dict
) -> NormalizedContact:
    if event.entity_type is CrmEntityType.LEAD:
        first, last = NormalizedContact.split_name(payload.get("name") or "")
        return NormalizedContact(
            first_name=first, last_name=last,
            phone=payload.get("phone"), email=payload.get("email"),
            company=payload.get("company"), source="VoxDesk Lead",
            tags=("voxdesk", "lead"),
            lead_score=payload.get("score"), notes=payload.get("notes") or "",
            custom_fields=custom_fields,
        )

    first, last = NormalizedContact.split_name(payload.get("customer_name") or "")
    phone = mapping.caller_number(
        payload.get("direction") or "inbound",
        payload.get("from_number") or "", payload.get("to_number") or "",
    )
    tags = ["voxdesk", "ai-call"]
    if payload.get("intent"):
        tags.append(str(payload["intent"]))
    if payload.get("booked"):
        tags.append("booked")
    if payload.get("transferred"):
        tags.append("transferred")
    if event.event_type is CrmEventType.CALL_MISSED:
        tags.append("missed-call")

    return NormalizedContact(
        first_name=first, last_name=last, phone=phone,
        source="VoxDesk AI Receptionist",
        tags=tuple(dict.fromkeys(tags)),
        intent=payload.get("intent"), lead_score=payload.get("lead_score"),
        custom_fields=custom_fields,
    )


def _activity_for_event(event: CrmEvent, payload: dict) -> NormalizedActivity | None:
    titles = {
        CrmEventType.CALL_COMPLETED: "VoxDesk AI call",
        CrmEventType.CALL_MISSED: "Missed call",
        CrmEventType.TRANSFER_COMPLETED: "Call transferred to a human",
    }
    title = titles.get(event.event_type)
    if title is None:
        return None

    lines = []
    if payload.get("summary"):
        lines.append(str(payload["summary"]))
    if payload.get("duration_seconds"):
        lines.append(f"Duration: {payload['duration_seconds']}s")
    if payload.get("intent"):
        lines.append(f"Intent: {payload['intent']}")
    if payload.get("booked"):
        lines.append("Outcome: appointment booked")

    return NormalizedActivity(
        title=title, body="\n".join(lines),
        occurred_at=event.created_at,
        duration_seconds=payload.get("duration_seconds"),
    )


# ------------------------------------------------------------ state writes ---

async def _mark_synced(
    session: AsyncSession, sync: CrmSync, result: CrmResult, started: float
) -> SyncOutcome:
    """
    The only path that writes SYNCED, and it requires an external id.

    Requirement 12 asks for the external id and the synced state to be
    persisted atomically as far as the architecture permits: they are set on
    the same object and flushed in one commit, so there is no window where a
    row is SYNCED with no proof of what it created.
    """
    sync.status = CrmSyncStatus.SYNCED
    sync.external_id = result.external_id
    sync.synced_at = datetime.utcnow()
    sync.next_attempt_at = None
    sync.last_error = None
    sync.last_error_code = None
    await session.commit()
    record_side_effect("crm_sync", "success")

    duration = _ms(started)
    log.info(
        "crm.sync_ok", sync_id=str(sync.id), tenant_id=str(sync.tenant_id),
        provider=sync.provider.value, event_type=sync.entity_type.value,
        entity_id=str(sync.entity_id), attempt=sync.attempt_count,
        duration_ms=duration, outcome="synced", external_id=result.external_id,
        already_existed=result.already_existed,
    )
    return SyncOutcome(
        sync_id=sync.id, status=CrmSyncStatus.SYNCED,
        external_id=result.external_id, duration_ms=duration,
    )


async def _handle_error(
    session: AsyncSession, sync: CrmSync, error: CrmError, attempt: int,
    policy, started: float,
) -> SyncOutcome:
    message = safe_message(str(error))

    if policy.should_retry(error, attempt):
        delay = policy.delay_for(attempt, error=error)
        sync.status = CrmSyncStatus.FAILED
        sync.next_attempt_at = datetime.utcnow() + timedelta(seconds=delay)
        sync.last_error = message
        sync.last_error_code = error.code
        await session.commit()
        record_side_effect("crm_sync", "retry")

        duration = _ms(started)
        log.warning(
            "crm.sync_retry", sync_id=str(sync.id), tenant_id=str(sync.tenant_id),
            provider=sync.provider.value, entity_id=str(sync.entity_id),
            attempt=attempt, duration_ms=duration, outcome="retry",
            error_code=error.code, retry_in_seconds=round(delay, 1),
        )
        return SyncOutcome(
            sync_id=sync.id, status=CrmSyncStatus.FAILED, error_code=error.code,
            safe_error=message, duration_ms=duration,
        )

    return await _mark_permanent(
        session, sync, code=error.code, message=message, started=started,
        attempt=attempt,
    )


async def _mark_permanent(
    session: AsyncSession, sync: CrmSync, *, code: str, message: str,
    started: float, attempt: int | None = None,
) -> SyncOutcome:
    sync.status = CrmSyncStatus.PERMANENT_FAILURE
    sync.next_attempt_at = None
    sync.last_error = safe_message(message)
    sync.last_error_code = code
    await session.commit()
    record_side_effect("crm_sync", "failure")

    duration = _ms(started)
    log.warning(
        "crm.sync_permanent_failure", sync_id=str(sync.id),
        tenant_id=str(sync.tenant_id), provider=sync.provider.value,
        entity_id=str(sync.entity_id), attempt=attempt or sync.attempt_count,
        duration_ms=duration, outcome="permanent_failure", error_code=code,
    )
    return SyncOutcome(
        sync_id=sync.id, status=CrmSyncStatus.PERMANENT_FAILURE, error_code=code,
        safe_error=sync.last_error, duration_ms=duration,
    )


async def _skip(
    session: AsyncSession, sync: CrmSync, reason: str, started: float
) -> SyncOutcome:
    sync.status = CrmSyncStatus.PERMANENT_FAILURE
    sync.last_error = reason
    sync.last_error_code = "skipped"
    sync.next_attempt_at = None
    await session.commit()
    return SyncOutcome(
        sync_id=sync.id, status=CrmSyncStatus.PERMANENT_FAILURE,
        skipped_reason=reason, duration_ms=_ms(started),
    )


# ------------------------------------------------------------------ health ---

async def check_health(
    session: AsyncSession, integration: CrmIntegration
) -> HealthResult:
    """
    Requirement 24. Always returns a normalized result; never raises.

    A tenant pressing "Test connection" with a bad token must see
    "credentials rejected", not a 500 and not the provider's raw body.
    """
    try:
        context = build_context(integration)
        provider = build_provider(integration.provider, context)
        result = await provider.health_check()
    except CrmError as exc:
        result = HealthResult(
            connected=False, provider=integration.provider.value,
            latency_ms=0.0, safe_message=exc.safe_message,
        )
    except Exception as exc:
        result = HealthResult(
            connected=False, provider=integration.provider.value, latency_ms=0.0,
            safe_message=f"unexpected error ({type(exc).__name__})",
        )

    integration.last_health_check_at = datetime.utcnow()
    integration.last_health_ok = result.connected
    integration.last_error = None if result.connected else result.safe_message[:500]
    await session.commit()

    log.info(
        "crm.health_check", tenant_id=str(integration.tenant_id),
        provider=integration.provider.value, outcome=(
            "connected" if result.connected else "failed"
        ),
        duration_ms=result.latency_ms,
    )
    return result


# ------------------------------------------------------------------ worker ---

async def claim_due_syncs(
    session: AsyncSession, *, limit: int = 20, now: datetime | None = None
) -> list[CrmSync]:
    """
    Syncs that are ready to be attempted.

    Ordered oldest-first so a backlog drains fairly rather than starving the
    first failures. `next_attempt_at IS NULL` covers freshly-created PENDING
    rows, which have no schedule yet.
    """
    moment = now or datetime.utcnow()
    rows = (
        (
            await session.execute(
                select(CrmSync)
                .where(
                    CrmSync.status.in_(list(RETRYABLE_SYNC_STATUSES)),
                    (CrmSync.next_attempt_at.is_(None))
                    | (CrmSync.next_attempt_at <= moment),
                )
                .order_by(CrmSync.created_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def reap_stuck_syncs(
    session: AsyncSession, *, older_than_minutes: int = 15
) -> int:
    """
    Recover syncs abandoned in PROCESSING by a worker that died.

    The same reaper pattern STEP 4 used for stuck documents, and for the same
    reason: without it, a restart at the wrong moment strands a row forever in
    a state no query will ever pick up. Returned to FAILED rather than PENDING
    so the attempt that was already spent still counts against the bound.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=older_than_minutes)
    result = await session.execute(
        update(CrmSync)
        .where(
            CrmSync.status == CrmSyncStatus.PROCESSING,
            CrmSync.last_attempt_at < cutoff,
        )
        .values(
            status=CrmSyncStatus.FAILED,
            last_error="worker did not finish; recovered by the reaper",
            last_error_code="stuck",
            next_attempt_at=datetime.utcnow(),
        )
    )
    await session.commit()
    count = result.rowcount or 0
    if count:
        log.warning("crm.reaped_stuck_syncs", count=count)
    return count


async def run_sync_tick(session: AsyncSession, *, limit: int = 20) -> dict:
    """One worker pass. Called by `scripts/scheduler.py`."""
    await reap_stuck_syncs(session)
    syncs = await claim_due_syncs(session, limit=limit)

    counts = {"attempted": 0, "synced": 0, "retry": 0, "failed": 0, "skipped": 0}
    for sync in syncs:
        outcome = await process_sync(session, sync)
        counts["attempted"] += 1
        if outcome.skipped_reason:
            counts["skipped"] += 1
        elif outcome.status is CrmSyncStatus.SYNCED:
            counts["synced"] += 1
        elif outcome.status is CrmSyncStatus.FAILED:
            counts["retry"] += 1
        else:
            counts["failed"] += 1
    return counts


# ----------------------------------------------------------------- helpers ---

def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.utcnow()