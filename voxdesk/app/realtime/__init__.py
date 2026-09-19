"""
Realtime event fan-out to the public WebSocket edge (services/realtime/gateway-go).

The API is the PRODUCER side of the realtime loop: when a durable fact about
a call changes (created, status transition, transfer outcome), this package
POSTs a small *notice* event to the gateway's authenticated ingest endpoint,
and the gateway fans it out to that tenant's dashboards.

Three rules govern everything in here, for the same reasons the billing
hooks obey them:

1. **It never raises.** A realtime outage must not turn a Twilio webhook
   into a 500 (which would retry, and could double-bill or double-transfer).
2. **It never blocks the live call path.** Publishing happens on status
   callbacks and webhook flows, under a sub-two-second timeout — never on
   the media-stream loop.
3. **A notice is not data.** Payloads carry identifiers and states, never
   phone numbers, transcripts, or customer content. The dashboard re-fetches
   detail through the authenticated REST API. A leaked event frame should
   tell an onlooker almost nothing.

Wire vocabulary the package emits is defined in
services/realtime/gateway-go/README.md; the rooms it publishes to are the
closed set the gateway enforces ("calls" and "call:<uuid>").
"""

from app.realtime import events, publisher  # noqa: F401
