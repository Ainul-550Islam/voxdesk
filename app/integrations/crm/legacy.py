"""
CRM push. Mk Solution advertises "GoHighLevel / CRM / Zapier / APIs" -- this is
that feature. One outbound POST after every call so the client's existing tools
light up. Never let a CRM failure break a call: everything is best-effort.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

TIMEOUT = 8.0


def build_payload(
    *,
    tenant_name: str,
    call_id: str,
    direction: str,
    from_number: str,
    to_number: str,
    duration_seconds: float,
    intent: str | None,
    summary: str | None,
    booked: bool,
    escalated: bool,
    lead_score: int | None = None,
    customer_name: str = "",
    customer_email: str = "",
    transcript: list[dict[str, str]] | None = None,
    recording_url: str | None = None,
) -> dict[str, Any]:
    """Flat, boring JSON. Zapier/Make/GHL all map flat keys easily."""
    return {
        "source": "VoxDesk",
        "event": "call.completed",
        "occurred_at": datetime.utcnow().isoformat() + "Z",
        "business": tenant_name,
        "call_id": call_id,
        "direction": direction,
        "from": from_number,
        "to": to_number,
        "duration_seconds": round(duration_seconds, 1),
        "intent": intent or "unknown",
        "summary": summary or "",
        "booked": booked,
        "escalated": escalated,
        "lead_score": lead_score,
        "contact": {
            "name": customer_name,
            "phone": from_number if direction == "inbound" else to_number,
            "email": customer_email,
        },
        "recording_url": recording_url,
        "transcript": transcript or [],
    }


def _headers(crm_type: str, api_key: str | None) -> dict[str, str]:
    h = {"Content-Type": "application/json", "User-Agent": "VoxDesk/0.3"}
    if not api_key:
        return h
    if crm_type == "gohighlevel":
        h["Authorization"] = f"Bearer {api_key}"
        h["Version"] = "2021-07-28"
    elif crm_type == "hubspot":
        h["Authorization"] = f"Bearer {api_key}"
    else:
        h["X-API-Key"] = api_key
    return h


def to_gohighlevel(payload: dict[str, Any], location_id: str | None = None) -> dict[str, Any]:
    """GHL wants contact-shaped data, not our event shape."""
    contact = payload["contact"]
    first, _, last = (contact.get("name") or "").partition(" ")
    body: dict[str, Any] = {
        "firstName": first or "Unknown",
        "lastName": last or "Caller",
        "phone": contact.get("phone"),
        "source": "VoxDesk AI Receptionist",
        "tags": [t for t in ("ai-call", payload.get("intent"),
                             "booked" if payload.get("booked") else None) if t],
        "customFields": [
            {"key": "call_summary", "field_value": payload.get("summary", "")},
            {"key": "lead_score", "field_value": str(payload.get("lead_score") or "")},
        ],
    }
    if contact.get("email"):
        body["email"] = contact["email"]
    if location_id:
        body["locationId"] = location_id
    return body


async def push(
    *,
    webhook_url: str | None,
    payload: dict[str, Any],
    crm_type: str = "webhook",
    api_key: str | None = None,
    retries: int = 2,
) -> bool:
    """Best-effort POST. Returns True on 2xx. Never raises."""
    if not webhook_url:
        return False

    body = to_gohighlevel(payload) if crm_type == "gohighlevel" else payload
    headers = _headers(crm_type, api_key)

    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(webhook_url, json=body, headers=headers)
            if 200 <= resp.status_code < 300:
                log.info("crm.pushed", crm=crm_type, status=resp.status_code)
                return True
            # 4xx is our fault -- retrying will not help.
            if 400 <= resp.status_code < 500:
                log.warning("crm.rejected", status=resp.status_code, body=resp.text[:200])
                return False
            log.warning("crm.server_error", status=resp.status_code, attempt=attempt)
        except Exception as exc:
            log.warning("crm.push_failed", error=str(exc), attempt=attempt)

    return False

