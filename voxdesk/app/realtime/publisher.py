"""
The ingest HTTP client: API → gateway `POST /ingest/v1/publish`.

Deliberately thin, because every guarantee about this channel already has an
owner:

* shape validation — the gateway (it rejects unknown rooms/kinds/tenant
  shapes with a 422, and 422 is the one status this client treats as
  "permanent, stop retrying", exactly like the CRM retry taxonomy);
* authentication — a shared Bearer secret, compared in constant time on the
  gateway side;
* replay suppression — the gateway's (tenant, event_id) TTL cache, when an
  event_id is supplied here;
* observability — the gateway's ingest metrics plus our structured log.

What THIS module owns: a single shared httpx client, a hard per-publish
timeout, and the never-raises contract.
"""
from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.logging import log

#: Reuse one client: TCP+TLS setup per publish would cost more than the
#: event is worth, and the connection pool bounds how many sockets realtime
#: can ever hold open to the gateway. Limits mirror the billing provider
#: clients (small pools, explicit timeouts).
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.realtime_publish_timeout_seconds),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
        )
    return _client


def enabled() -> bool:
    """Realtime is on only when the operator configured BOTH endpoint and secret.

    An empty URL is the single off switch — development defaults stay silent,
    and a deployment without the gateway has no noisy failures.
    """
    return bool(settings.realtime_gateway_url and settings.realtime_gateway_ingest_secret)


async def publish_event(
    *,
    tenant_id: str,
    room: str,
    kind: str,
    payload: dict,
    event_id: str | None = None,
) -> bool:
    """Publish one event to the gateway. Returns True on acceptance.

    Acceptance covers exactly three gateway outcomes: fresh delivery,
    delivery-with-nobody-listening (delivered=0 is fine — dashboards
    reconnect), and replay suppression (duplicate=true is a SUCCESS: the
    event already happened once, which is the whole point of the id).

    Everything else — timeout, connection refused, 401, 422, 500 — returns
    False and is logged with the exception TYPE only (an httpx exception can
    quote the request, and the request headers carry the secret).
    """
    if not enabled():
        return False

    body: dict = {
        "tenant_id": tenant_id,
        "room": room,
        "kind": kind,
        "payload": payload,
    }
    if event_id:
        body["event_id"] = event_id

    url = settings.realtime_gateway_url.rstrip("/") + "/ingest/v1/publish"
    try:
        resp = await _get_client().post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {settings.realtime_gateway_ingest_secret}"},
        )
    except Exception as exc:  # noqa: BLE001 — the never-raises contract is the feature
        log.warning(
            "realtime.publish_failed",
            kind=kind,
            room=room,
            error=type(exc).__name__,
        )
        return False

    if resp.status_code == 200:
        return True

    # 401 = our secret is wrong (rotate it); 422 = our shape is wrong (fix
    # the caller). Both ARE operator-visible facts, logged once per publish
    # rather than retried into a scream.
    log.warning(
        "realtime.publish_rejected",
        kind=kind,
        room=room,
        status=resp.status_code,
    )
    return False
