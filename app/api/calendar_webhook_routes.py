"""
Inbound calendar provider notifications.

    POST /api/calendar/webhooks/{provider}/{routing_token}

Same architecture as the CRM inbound endpoint, for the same reasons — and the
same refusal to guess:

**The tenant comes from a path token, never the body.** A high-entropy value
derived per integration from the platform secret. Requirement 21 is explicit
that an unverified body's `tenant_id` must not select the tenant, so a body
field of that name is discarded and there is a test for it.

**The token is not the authentication.** It selects which integration to
verify against; the provider's own signature or channel token proves the
request is genuine. URLs leak — into logs, browser history, screenshots.

**Replay protection** is a `CalendarWebhookReceipt` per
`(tenant, provider, provider_event_id)` with a unique constraint. The second
delivery finds the row and stops.

**Every rejection is byte-identical**, so the endpoint cannot be used to
enumerate which tokens or providers are live.

What is verifiable today, honestly:

* **Google** signs push notifications with a `X-Goog-Channel-Token` that *we*
  supply when creating the watch channel. That is a shared secret we control,
  so it is verified.
* **Microsoft** sends a `clientState` we supply when creating the
  subscription — same story, verified. Graph also requires echoing a
  `validationToken` on subscription creation, which is handled.
* **Cal.com** signs with an HMAC over the raw body using a per-webhook secret
  the tenant configures. Verified when that secret is stored.

Providers with no stored secret **fail closed**. An unverified inbound
endpoint is worse than none: it is a public, tenant-addressable write path.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import log
from app.db.models import (
    Appointment,
    AppointmentStatus,
    CalendarIntegration,
    CalendarProviderType,
    CalendarWebhookReceipt,
)
from app.db.session import get_session
from app.integrations.calendar.timezones import now_utc

router = APIRouter(prefix="/api/calendar/webhooks", tags=["calendar"])

#: Deliberately uniform. Every rejection returns exactly this.
_REJECTED = HTTPException(status_code=401, detail="Invalid signature")

MAX_BODY_BYTES = 1_000_000


def routing_token(integration: CalendarIntegration) -> str:
    """
    The per-integration path token.

    HMAC of the integration id under the platform secret, rather than a stored
    column: no extra migration, no second secret to rotate. HMAC rather than a
    bare hash because the id is not secret — `sha256(id)` would be forgeable
    by anyone who can read an id.
    """
    digest = hmac.new(
        settings.secret_key.encode(),
        f"calendar-inbound:{integration.id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return digest[:40]


async def _resolve_integration(
    session: AsyncSession, provider: CalendarProviderType, token: str
) -> CalendarIntegration:
    rows = (
        (
            await session.execute(
                select(CalendarIntegration).where(
                    CalendarIntegration.provider == provider,
                    CalendarIntegration.is_enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for integration in rows:
        if hmac.compare_digest(routing_token(integration), token):
            return integration
    raise _REJECTED


@router.post("/{provider}/{token}")
async def receive_calendar_webhook(
    provider: str,
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """
    Verify, deduplicate, and apply the one state change that is safe.

    The only mutation performed is **marking a locally-known appointment
    cancelled** when the provider says its event is gone. That is the change
    where the provider is unambiguously authoritative — the event does not
    exist on their calendar any more, so continuing to hold the slot would
    block a real booking.

    Everything else an inbound notification might imply (a time changed
    outside VoxDesk, an attendee added) is bidirectional sync and out of
    scope. Acting on a half-understood notification is how a webhook endpoint
    silently rewrites a customer's diary.
    """
    try:
        provider_type = CalendarProviderType(provider.lower())
    except ValueError:
        raise _REJECTED

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")

    # Microsoft Graph validates a new subscription by POSTing a
    # `validationToken` query parameter that must be echoed back as plain text
    # within seconds. This happens before any clientState exists, so it is
    # answered before the signature check -- it carries no data and mutates
    # nothing.
    validation = request.query_params.get("validationToken")
    if validation and provider_type is CalendarProviderType.MICROSOFT:
        return Response(content=validation, media_type="text/plain")

    integration = await _resolve_integration(session, provider_type, token)
    tenant_id = str(integration.tenant_id)      # read before any rollback

    if not _verify(integration, provider_type, request, raw):
        log.warning(
            "calendar.inbound_rejected", tenant_id=tenant_id,
            provider=provider_type.value, reason="signature",
        )
        raise _REJECTED

    body = _parse_body(raw)
    # Explicitly discarded. Named here because "just read the tenant from the
    # payload" is exactly what requirement 21 forbids, and the code should say
    # so rather than merely not doing it.
    body.pop("tenant_id", None)

    event_id = _provider_event_id(provider_type, body, request)
    resource_id = _resource_id(provider_type, body, request)

    if event_id:
        session.add(CalendarWebhookReceipt(
            tenant_id=integration.tenant_id, provider=provider_type,
            provider_event_id=event_id, resource_id=resource_id,
        ))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            log.info(
                "calendar.inbound_replay", tenant_id=tenant_id,
                provider=provider_type.value, provider_event_id=event_id,
            )
            # 200, not 409: a provider that sees an error simply retries.
            return {"ok": True, "duplicate": True}

    applied = await _apply_cancellation(
        session, integration, provider_type, body, resource_id
    )

    log.info(
        "calendar.inbound_accepted", tenant_id=tenant_id,
        provider=provider_type.value, provider_event_id=event_id or "",
        outcome="cancelled" if applied else "recorded",
    )
    return {"ok": True, "duplicate": False, "applied": applied}


async def _apply_cancellation(
    session: AsyncSession,
    integration: CalendarIntegration,
    provider_type: CalendarProviderType,
    body: dict,
    resource_id: str | None,
) -> bool:
    """
    Mark an appointment cancelled when the provider says its event is gone.

    Scoped by `tenant_id` **and** `external_event_id`. The event id alone
    would be a cross-tenant write primitive: two tenants on the same provider
    could in principle see the same id, and an id is not authorization.
    """
    if not _signals_cancellation(provider_type, body):
        return False
    if not resource_id:
        return False

    appointment = (
        await session.execute(
            select(Appointment).where(
                Appointment.tenant_id == integration.tenant_id,
                Appointment.external_event_id == resource_id,
                Appointment.status.in_([
                    AppointmentStatus.PENDING,
                    AppointmentStatus.CONFIRMED,
                    AppointmentStatus.RESCHEDULED,
                ]),
            )
        )
    ).scalar_one_or_none()
    if appointment is None:
        return False

    appointment.status = AppointmentStatus.CANCELLED
    appointment.cancelled_at = now_utc()
    appointment.cancelled_by = f"provider:{provider_type.value}"
    appointment.cancellation_reason = "cancelled in the calendar provider"
    # Release the slot lock so the time can be rebooked.
    appointment.slot_key = None

    from app.integrations.crm import hooks as crm_hooks

    await session.flush()
    await crm_hooks.on_appointment_cancelled(session, appointment)
    await session.commit()
    return True


def _signals_cancellation(provider_type: CalendarProviderType, body: dict) -> bool:
    if provider_type is CalendarProviderType.CALCOM:
        return str(body.get("triggerEvent") or "").upper() == "BOOKING_CANCELLED"
    if provider_type is CalendarProviderType.MICROSOFT:
        return any(
            str(item.get("changeType") or "").lower() == "deleted"
            for item in body.get("value") or []
        )
    if provider_type in (
        CalendarProviderType.GOOGLE, CalendarProviderType.GOOGLE_SERVICE_ACCOUNT
    ):
        # Google push notifications carry no payload -- they only say "this
        # calendar changed, go and look". Acting on that without a sync query
        # would be guessing, so it is recorded and nothing is mutated.
        return False
    return False


def _verify(
    integration: CalendarIntegration, provider_type: CalendarProviderType,
    request: Request, raw: bytes,
) -> bool:
    secret = _webhook_secret(integration)
    if not secret:
        # Fail closed. No stored secret means nothing to verify against.
        log.info(
            "calendar.inbound_unverifiable", tenant_id=str(integration.tenant_id),
            provider=provider_type.value,
        )
        return False

    if provider_type in (
        CalendarProviderType.GOOGLE, CalendarProviderType.GOOGLE_SERVICE_ACCOUNT
    ):
        # Google echoes the channel token we supplied at watch creation.
        supplied = request.headers.get("X-Goog-Channel-Token", "")
        return bool(supplied) and hmac.compare_digest(supplied, secret)

    if provider_type is CalendarProviderType.MICROSOFT:
        # Graph echoes the clientState we supplied at subscription creation,
        # in the header and/or per-notification in the body.
        supplied = request.headers.get("ClientState") or request.headers.get(
            "clientState", ""
        )
        if supplied and hmac.compare_digest(supplied, secret):
            return True
        body = _parse_body(raw)
        states = [
            str(item.get("clientState") or "") for item in body.get("value") or []
        ]
        return bool(states) and all(
            hmac.compare_digest(state, secret) for state in states
        )

    if provider_type is CalendarProviderType.CALCOM:
        # HMAC-SHA256 over the raw body. Compared against the exact bytes
        # received -- re-serializing would break for any payload whose JSON
        # encoding differs by a space.
        supplied = request.headers.get("X-Cal-Signature-256", "")
        if not supplied:
            return False
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(supplied.lower(), expected)

    return False


def _webhook_secret(integration: CalendarIntegration) -> str | None:
    """
    The shared secret for this integration's inbound channel.

    Lives in the encrypted credential bundle alongside the OAuth tokens, so it
    inherits the same cipher, the same key ring and the same
    `(tenant, provider)` binding.
    """
    if not integration.credentials_encrypted:
        return None

    from app.integrations.crm import crypto

    key_ring = crypto.key_ring_from_settings()
    if key_ring is None:
        return None
    try:
        credentials = crypto.decrypt_credentials(
            integration.credentials_encrypted,
            tenant_id=str(integration.tenant_id),
            provider=integration.provider.value,
            key_ring=key_ring,
        )
    except crypto.CredentialDecryptionError:
        return None
    return credentials.get("webhook_secret") or None


def _parse_body(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _provider_event_id(
    provider_type: CalendarProviderType, body: dict, request: Request
) -> str:
    """
    The provider's identifier for this notification.

    Empty when the provider sends none — in which case replay protection
    degrades to nothing rather than being faked with a hash of the body. A
    synthetic id would make two genuinely distinct notifications with
    identical payloads look like a replay.
    """
    candidates = (
        request.headers.get("X-Goog-Message-Number") and (
            f"{request.headers.get('X-Goog-Channel-ID', '')}:"
            f"{request.headers.get('X-Goog-Message-Number')}"
        ),
        body.get("id"),                                    # Cal.com / generic
        (body.get("payload") or {}).get("uid")
        if isinstance(body.get("payload"), dict) else None,
        _first_graph_id(body),
    )
    for candidate in candidates:
        if candidate:
            return str(candidate)[:255]
    return ""


def _first_graph_id(body: dict) -> str | None:
    for item in body.get("value") or []:
        if isinstance(item, dict) and item.get("subscriptionId"):
            return f"{item['subscriptionId']}:{item.get('resource', '')}"
    return None


def _resource_id(
    provider_type: CalendarProviderType, body: dict, request: Request
) -> str | None:
    """The provider's event id, matched against `Appointment.external_event_id`."""
    if provider_type is CalendarProviderType.CALCOM:
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        return payload.get("uid") or body.get("uid")
    if provider_type is CalendarProviderType.MICROSOFT:
        for item in body.get("value") or []:
            data = item.get("resourceData") or {}
            if data.get("id"):
                return str(data["id"])
        return None
    return request.headers.get("X-Goog-Resource-ID")


async def prune_receipts(session: AsyncSession, *, older_than_days: int = 30) -> int:
    """
    Drop old replay records.

    Replay protection only has to cover the window a provider might plausibly
    redeliver in. Keeping receipts forever turns a defence into an unbounded
    table.
    """
    from datetime import timedelta

    from sqlalchemy import delete

    cutoff = now_utc() - timedelta(days=older_than_days)
    result = await session.execute(
        delete(CalendarWebhookReceipt).where(
            CalendarWebhookReceipt.received_at < cutoff
        )
    )
    await session.commit()
    return result.rowcount or 0

