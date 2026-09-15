"""Readiness probe internals (Step 7 observability).

Liveness vs readiness
---------------------
* **Liveness** (``/health``) answers "is this process up?". It performs no
  dependency checks at all, so a lost database or Redis never makes a node
  look dead — the orchestrator must not restart a healthy process because a
  dependency blipped.
* **Readiness** (``/health/ready``) answers "can this process serve traffic
  now?". It fails (503) when a *required* dependency is down, so a load
  balancer stops routing to a node that cannot complete requests.

The rules that keep readiness honest and cheap:

* **No provider API call per request.** The provider check verifies only that
  the required *credentials are configured* — a local, free read of settings.
  Live provider reachability is a periodic/event-driven concern (the CRM
  health check, call-time fail-fast), never a per-probe network round trip.
* **Redis is required only when configured.** ``REDIS_URL`` unset means the
  in-process cache is in use and there is nothing to probe.
* **Missing voice providers fail readiness only in production.** A dev/test
  box intentionally runs without live keys; failing its readiness would make
  local tooling and CI pointlessly red. Production fails, loudly, because a
  deployment that cannot answer voice traffic must not accept it.

The probe mutates the ``voxdesk_db_up`` / ``voxdesk_redis_up`` gauges so the
Prometheus side of the same signal stays current between scrapes.
"""
from __future__ import annotations

from sqlalchemy import text

from app.core import observability
from app.core.cache import get_cache
from app.core.config import settings
from app.core.metrics import set_db_up
from app.db.session import get_engine

#: Providers the live voice path cannot work without. Presence of their key
#: is the readiness signal (configuration, not reachability).
_REQUIRED_VOICE_PROVIDERS = ("deepgram", "elevenlabs")

#: At least one LLM provider key must be present to serve voice/text agents.
_LLM_PROVIDERS = ("openai", "anthropic", "google")


def provider_config_ok() -> dict:
    """Which voice-provider credentials are configured. Never talks to a
    provider — this is a settings read, not a health check."""
    missing: list[str] = []
    if not (settings.deepgram_api_key or "").strip():
        missing.append("deepgram")
    if not (settings.elevenlabs_api_key or "").strip():
        missing.append("elevenlabs")
    llm = any(
        (getattr(settings, f"{p}_api_key", "") or "").strip()
        for p in _LLM_PROVIDERS
    )
    if not llm:
        missing.append("llm")
    return {"ok": not missing, "missing": missing}


async def check_database() -> bool:
    """A real ``SELECT 1`` against the configured engine. Returns a bool."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def check_redis() -> bool | None:
    """Redis reachability, or None when Redis is not configured."""
    if not settings.redis_url:
        return None
    return await get_cache().ping()


async def readiness() -> dict:
    """Build the full readiness payload. Pure aggregation; never raises.

    Returns ``{"ready": bool, "body": dict}`` where ``body`` is the JSON the
    endpoint returns.
    """
    db_ok = await check_database()
    set_db_up(db_ok)

    redis_ok = await check_redis()
    if redis_ok is not None:
        observability.set_redis_up(redis_ok)

    providers = provider_config_ok()
    providers_required = settings.is_production and not providers["ok"]

    ready = db_ok and (redis_ok is None or redis_ok) and not providers_required

    return {
        "ready": ready,
        "body": {
            "status": "ready" if ready else "unavailable",
            "checks": {
                "database": {"ok": db_ok},
                "redis": (
                    {"ok": redis_ok, "configured": True}
                    if redis_ok is not None
                    else {"ok": True, "configured": False}
                ),
                "providers": providers,
            },
        },
    }
