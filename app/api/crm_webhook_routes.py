"""
Inbound provider webhooks.

    POST /api/integrations/crm/inbound/{provider}/{routing_token}

Requirement 21 is mostly a list of ways to get this wrong, so each one is
addressed explicitly here.

**"Do NOT accept `tenant_id` from an untrusted webhook body as the sole tenant
selector."** The tenant is identified by a `routing_token` in the *path* — a
high-entropy value we generate per integration and hand to the provider when
the webhook is registered. The body is never consulted for routing. A body
field named `tenant_id` is ignored entirely, and there is a test for that.

Why a path token rather than a header or a body field: the provider is
configuring a URL, and a URL is the one thing every provider's webhook UI can
store. A token in the path is also the only option that works for providers
that send no custom headers.

**The token is not the authentication.** It selects which integration to
verify against; the signature is what proves the request is genuine. Anyone
who learns the token still cannot forge an event. This distinction matters,
because URLs leak — into logs, into browser history, into screenshots in
support tickets.

**Replay.** A `CrmWebhookReceipt` row per (tenant, provider, provider event
id) with a unique constraint. The second delivery of the same event finds the
row and stops. Where a provider sends no event id, the timestamp window is the
only defence available and that is stated rather than pretended around.

**A signature failure returns 401 and nothing else.** No hint about whether
the token matched, whether the integration exists, or which part was wrong.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import log
from app.db.models import CrmIntegration, CrmProviderType, CrmWebhookReceipt
from app.db.session import get_session
from app.integrations.crm import crypto
from app.integrations.crm.providers.webhook import verify as verify_voxdesk_signature

router = APIRouter(prefix="/api/integrations/crm/inbound", tags=["integrations"])

#: Deliberately uniform. Every rejection returns exactly this, so the endpoint
#: cannot be used to enumerate which routing tokens or providers are live.
_REJECTED = HTTPException(status_code=401, detail="Invalid signature")

MAX_BODY_BYTES = 1_000_000


def routing_token(integration: CrmIntegration) -> str:
    """
    The per-integration path token.

    Derived from the integration id and the platform secret rather than
    stored, so there is no extra column to migrate and no second secret to
    rotate. HMAC rather than a plain hash: the id is not secret, so a bare
    `sha256(id)` would be forgeable by anyone who can read an id.
    """
    digest = hmac.new(
        settings.secret_key.encode(),
        f"crm-inbound:{integration.id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return digest[:40]


async def _resolve_integration(
    session: AsyncSession, provider: CrmProviderType, token: str
) -> CrmIntegration:
    """
    Find the integration this token addresses.

    A linear scan over one provider's integrations, comparing in constant
    time. That is fine at this scale and it avoids storing a second indexed
    secret; if the table ever grows enough for it to matter, the token becomes
    a column with an index and this function is the only thing that changes.
    """
    rows = (
        (
            await session.execute(
                select(CrmIntegration).where(
                    CrmIntegration.provider == provider,
                    CrmIntegration.is_enabled.is_(True),
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
async def receive_webhook(
    provider: str,
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """
    Verify, deduplicate, record.

    This endpoint deliberately does **not** mutate VoxDesk data yet. It
    establishes the trusted plumbing — identity, signature, replay — and
    records that an event arrived. Acting on inbound CRM changes (a contact
    renamed in HubSpot updating a lead here) is a bidirectional-sync feature
    and would be scope beyond this step; building the unverified half first
    and bolting verification on later is how webhook endpoints become
    incidents.
    """
    return await _handle(session, provider, token, request)


async def _handle(
    session: AsyncSession, provider: str, token: str, request: Request
) -> dict:
    try:
        provider_type = CrmProviderType(provider.lower())
    except ValueError:
        raise _REJECTED

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")

    integration = await _resolve_integration(session, provider_type, token)

    if not _verify(integration, provider_type, request, raw):
        log.warning(
            "crm.inbound_rejected", tenant_id=str(integration.tenant_id),
            provider=provider_type.value, reason="signature",
        )
        raise _REJECTED

    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Body is not JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be an object")

    # Explicitly ignored. Present as a comment and as a test because the
    # temptation to "just read the tenant from the payload" is exactly what
    # requirement 21 forbids, and the code needs to say so.
    _ = body.pop("tenant_id", None)

    event_id = _provider_event_id(provider_type, body, request)
    event_type = str(body.get("type") or body.get("topic") or body.get("event") or "")

    # Read the tenant id out of the ORM object *before* attempting a commit
    # that may fail. `rollback()` expires every loaded object, so touching
    # `integration.tenant_id` afterwards triggers a lazy refresh from inside
    # an exception handler -- which on an AsyncSession raises MissingGreenlet,
    # and with a pending failed INSERT re-runs it through autoflush and raises
    # the IntegrityError a second time, uncaught. The replay path then returns
    # a 500 instead of the 200 it is supposed to.
    tenant_id = str(integration.tenant_id)

    if event_id:
        receipt = CrmWebhookReceipt(
            tenant_id=integration.tenant_id, provider=provider_type,
            provider_event_id=event_id, event_type=event_type[:120],
        )
        session.add(receipt)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            log.info(
                "crm.inbound_replay", tenant_id=tenant_id,
                provider=provider_type.value, provider_event_id=event_id,
            )
            # 200, not 409: a provider that sees an error retries, and a
            # retry of an event we have already processed is not a problem
            # worth making them keep trying to report.
            return {"ok": True, "duplicate": True}

    log.info(
        "crm.inbound_accepted", tenant_id=tenant_id,
        provider=provider_type.value, event_type=event_type,
        provider_event_id=event_id or "",
        deduplicated=bool(event_id),
    )
    return {"ok": True, "duplicate": False}


def _verify(
    integration: CrmIntegration, provider_type: CrmProviderType,
    request: Request, raw: bytes,
) -> bool:
    """
    Signature verification, per provider.

    Only the webhook provider is fully verifiable here, because it is the one
    whose secret we generated. The others are handled honestly rather than
    approximately — see the comments on each branch.
    """
    if provider_type is CrmProviderType.WEBHOOK:
        return _verify_voxdesk(integration, request, raw)

    # GoHighLevel signs with Ed25519 against a HighLevel *public* key, and
    # Jobber and HubSpot each have their own scheme. Verifying those requires
    # provider-specific key material this step does not provision, and an
    # unverified inbound endpoint is worse than no inbound endpoint: it is a
    # public, tenant-addressable write path.
    #
    # So these providers are refused rather than accepted-without-checking.
    # That is a real limitation, it is documented as one, and it fails closed.
    log.info(
        "crm.inbound_unsupported", tenant_id=str(integration.tenant_id),
        provider=provider_type.value,
    )
    return False


def _verify_voxdesk(integration: CrmIntegration, request: Request, raw: bytes) -> bool:
    """
    Verify a signature produced with this integration's own signing secret.

    Used for the loopback case: a tenant's middleware receiving our webhook
    and calling back. Same secret, same scheme, so the verification code is
    the one exported by the provider module rather than a second copy.
    """
    signature = request.headers.get("X-VoxDesk-Signature", "")
    timestamp_raw = request.headers.get("X-VoxDesk-Timestamp", "")
    if not signature or not timestamp_raw:
        return False
    try:
        timestamp = int(timestamp_raw)
    except ValueError:
        return False

    key_ring = crypto.key_ring_from_settings()
    if key_ring is None or not integration.credentials_encrypted:
        return False
    try:
        credentials = crypto.decrypt_credentials(
            integration.credentials_encrypted,
            tenant_id=str(integration.tenant_id),
            provider=integration.provider.value,
            key_ring=key_ring,
        )
    except crypto.CredentialDecryptionError:
        return False

    secret = credentials.get("signing_secret") or ""
    if not secret:
        return False

    return verify_voxdesk_signature(
        secret, timestamp=timestamp, body=raw.decode("utf-8", "replace"),
        signature=signature,
        tolerance_seconds=settings.crm_webhook_tolerance_seconds,
    )


def _provider_event_id(
    provider_type: CrmProviderType, body: dict, request: Request
) -> str:
    """
    The provider's own identifier for this event, used for replay protection.

    Each provider names it differently, and some do not send one at all. When
    there is none, replay protection degrades to the timestamp window — which
    is weaker, and is why this returns an empty string rather than inventing
    an id from a hash of the body. A synthetic id would make two genuinely
    distinct events with identical payloads look like a replay.
    """
    candidates = (
        body.get("webhookId"),      # GoHighLevel
        body.get("itemId"),         # Jobber
        body.get("eventId"),        # HubSpot
        body.get("id"),             # our own envelope
        request.headers.get("X-VoxDesk-Idempotency-Key"),
    )
    for candidate in candidates:
        if candidate:
            return str(candidate)[:255]
    return ""


async def prune_receipts(session: AsyncSession, *, older_than_days: int = 30) -> int:
    """
    Drop old replay records.

    Replay protection only needs to cover the window in which a provider might
    plausibly redeliver. Keeping receipts forever turns a defence into an
    unbounded table.
    """
    from datetime import datetime, timedelta

    from sqlalchemy import delete

    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    result = await session.execute(
        delete(CrmWebhookReceipt).where(CrmWebhookReceipt.received_at < cutoff)
    )
    await session.commit()
    return result.rowcount or 0

