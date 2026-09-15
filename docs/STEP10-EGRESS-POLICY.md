# VoxDesk — Egress network policy (Step 10, item J)

The API/worker container must only reach the destinations the product needs.
This file is the **allowed-domain inventory** and the deny rules. It is an
operational requirement (applied at the container/network layer), not a code
claim: the static SSRF guard (`app/core/ssrf.py`) is the in-process control;
this policy is the complementary network control that stops DNS rebinding and
metadata/private-range targets that a no-resolve string check cannot see.

## Allowed egress domains (allowlist)

| Provider | Domain(s) | Port | Purpose |
|---|---|---|---|
| Twilio | `api.twilio.com` | 443 | REST SDK (calls/SMS/media status) |
| Deepgram | `api.deepgram.com` (REST + `wss://`) | 443 | STT live transcription |
| ElevenLabs | `api.elevenlabs.io` (REST + `wss://`) | 443 | TTS streaming |
| OpenAI | `api.openai.com` | 443 | LLM + embeddings |
| Anthropic | `api.anthropic.com` | 443 | LLM |
| Google Gemini | `generativelanguage.googleapis.com` | 443 | LLM (Google provider) |
| Google Calendar | `www.googleapis.com`, `oauth2.googleapis.com` | 443 | calendar + OAuth |
| Microsoft | `login.microsoftonline.com`, `graph.microsoft.com` | 443 | calendar + OAuth |
| Cal.com | `api.cal.com` | 443 | calendar |
| HubSpot | `api.hubapi.com` | 443 | CRM |
| GoHighLevel | `services.leadconnectorhq.com` | 443 | CRM |
| Jobber | `api.getjobber.com` | 443 | CRM |
| Stripe | `api.stripe.com` | 443 | billing |
| Sentry | `*.ingest.sentry.io` (only when `SENTRY_DSN` set) | 443 | error reporting |
| Postgres | configured `POSTGRES_*` host | 5432 | database |
| Redis | configured `REDIS_URL` host | 6379 | cache/rate-limit |

## Deny rules (must be enforced)

1. **No** link-local `169.254.0.0/16` (cloud metadata) — the highest priority.
2. **No** loopback `127.0.0.0/8`, `::1` (the container's own loopback except
   the reverse proxy's declared upstream, if any).
3. **No** RFC 1918 `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` and RFC
   4193 ULA `fc00::/7`, except the explicit Postgres/Redis/proxy peers.
4. **No** multicast/reserved/unspecified ranges.
5. DNS rebinding is mitigated by resolving at the egress proxy and re-checking
   the resolved address against the deny ranges before connect (or by a
   filtering resolver).

## Enforcement preference

Prefer **infrastructure controls** (Docker network policy, cloud security
groups, Kubernetes NetworkPolicy, an egress proxy) over in-app checks. A
Docker-compose implementation: attach the API service to an internal network,
put an egress allowlist on the gateway, and drop all private/link-local
prefixes. `scripts/` and `docker-compose*.yml` are the place to wire this in;
this sandbox has no Docker, so the change is documented here and remains an
open operational item (see `docs/SECURITY.md` §8 item 1).

## In-process control (already implemented, Step 9)

`app/core/ssrf.py` classifies tenant-configurable outbound URLs statically
(scheme, userinfo, IP-literal loopback/private/link-local/reserved/metadata,
and `localhost`/`*.localhost`/`.local`/`.internal`/metadata hostnames) and is
enforced at the API boundary and in every provider's base/token-url accessor.
Tested by `tests/test_ssrf.py` (21 cases).
