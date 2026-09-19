"""
Typed realtime events: which facts about a call are announced, and how.

An emission happens only after the fact is DURABLE (the callers emit after
`session.commit()`, never before) — a dashboard must never be told about a
state the database then rolled back.

Idempotency mirrors the database's exactly-once discipline: the event id is
``uuid5(namespace, "<call_id>:<kind>:<room_scope>:<status>:<transfer_state>")``
— deterministic, so a crash between the DB commit and the gateway POST,
replayed by the operator's own retry, is suppressed by the gateway's replay
cache instead of double-animating a wallboard.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.core.logging import log
from app.db.models import Call, CallStatus
from app.realtime.publisher import publish_event

#: Closed room vocabulary (the gateway refuses anything else).
ROOM_CALLS = "calls"


def room_call(call_id: uuid.UUID | str) -> str:
    """The per-call room a CallDetail page subscribes to."""
    return f"call:{call_id}"


#: Fixed namespace for derived event ids. Not a secret: it only scopes the
#: uuid5 space so a v5 derived for another purpose can never collide.
_EVENT_NAMESPACE = uuid.UUID("7e9f6d3a-2b1c-4f5e-9a8d-0c1b2a3f4e5d")


def _event_id(call: Call, *parts: str) -> str:
    """Deterministic, gateway-valid (UUID) dedupe id for one logical event."""
    material = ":".join([str(call.id), *parts])
    return str(uuid.uuid5(_EVENT_NAMESPACE, material))


def call_payload(call: Call) -> dict:
    """The notice payload. ROUTING + DISPLAY facts only.

    Deliberately absent: from_number/to_number (PII), summary, transcript,
    recording_url. The dashboard learns THAT a call changed and re-reads the
    detail through the authenticated REST API; a realtime frame on its own
    stays near-contentless.
    """
    def _iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    return {
        "call_id": str(call.id),
        "call_sid": call.call_sid,
        "status": call.status.value if isinstance(call.status, CallStatus) else str(call.status),
        "direction": call.direction.value if call.direction else None,
        "duration_seconds": call.duration_seconds,
        "booked": bool(call.booked),
        "escalated": bool(call.escalated),
        "lead_score": call.lead_score,
        "transfer_state": call.transfer_state.value if call.transfer_state else None,
        "started_at": _iso(call.started_at),
        "ended_at": _iso(call.ended_at),
    }


async def emit_call_event(
    call: Call,
    *,
    kind: str,
    extra: dict | None = None,
) -> bool:
    """Publish one call event to both rooms a dashboard may be watching.

    Two publishes, one logical event, deliberately: the list page subscribes
    "calls", the detail page subscribes "call:<id>", and the gateway has no
    room aliasing (by design — aliasing is how an impersonator-named room
    sneaks into a closed namespace). The replay id differs per room so a
    partial outage that delivered ONE of the two stays retryable for the other.

    Never raises (the publisher owns that contract); returns False if either
    publish was refused, so callers may log one line.
    """
    payload = call_payload(call)
    if extra:
        # Callers may attach transfer outcome etc. — same PII discipline
        # applies at the call site (states and reasons, never content).
        payload.update(extra)

    tenant_id = str(call.tenant_id)
    results = []
    for room, scope in ((ROOM_CALLS, "all"), (room_call(call.id), "one")):
        results.append(
            await publish_event(
                tenant_id=tenant_id,
                room=room,
                kind=kind,
                payload=payload,
                event_id=_event_id(call, kind, scope, *_event_parts(call)),
            )
        )
    ok = all(results)
    if not ok:
        # One line per logical event, not per publish — the publisher already
        # logged the specifics, this just makes "the dashboard may be stale"
        # greppable.
        log.info(
            "realtime.emit_degraded",
            kind=kind,
            call_id=str(call.id),
            tenant_id=tenant_id,
        )
    return ok


def _event_parts(call: Call) -> list[str]:
    """The state material that makes one logical event distinct from another.

    Same call + same status + same transfer state ⇒ same id ⇒ replays are
    suppressed by the gateway. Anything that legitimately changes the story
    produces a fresh id.
    """
    status = call.status.value if isinstance(call.status, CallStatus) else str(call.status)
    transfer = call.transfer_state.value if call.transfer_state else "none"
    return [status, transfer]
