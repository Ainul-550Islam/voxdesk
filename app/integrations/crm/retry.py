"""
Retry policy and per-tenant rate protection.

Two separate concerns that both answer the question "should this request go
out right now", kept in one module because they are always consulted together.

**Retry** (requirement 13). Bounded, exponential, jittered. The jitter is not
decoration: without it, a provider outage that fails a hundred syncs at once
produces a hundred retries at exactly t+2s, then t+6s, then t+14s. Every
retry wave lands simultaneously and re-creates the overload that caused the
failure. Full jitter spreads them.

**Rate protection** (requirement 23). A token bucket per (tenant, provider).
The provider's own 429 tells us we already went too fast; this stops us
getting there. Its real purpose is the one the brief names: one tenant whose
credentials are broken, or whose provider is throttling them, must not consume
the shared worker's every pass while other tenants' events queue behind.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from app.integrations.crm.errors import CrmError, CrmRateLimited


@dataclass(frozen=True)
class RetryPolicy:
    """
    Bounded exponential backoff with full jitter.

    Delay for attempt *n* is uniform in ``[0, min(base * 2^(n-1), cap)]``.

    Full jitter rather than "exponential plus a bit of noise" because the
    former is what actually decorrelates a thundering herd; the latter keeps
    the peak and only blurs its edges.
    """

    max_attempts: int = 5
    base_seconds: float = 2.0
    max_seconds: float = 900.0        # 15 minutes; past that a human should look
    jitter: bool = True

    def should_retry(self, error: CrmError, attempt: int) -> bool:
        """
        `attempt` is the number of attempts already made, including this one.

        Both halves matter. `error.retryable` is the taxonomy decision — a 401
        never becomes retryable no matter how few attempts have been used. The
        attempt bound is the safety net for a transient error that is not
        actually going to clear.
        """
        if not getattr(error, "retryable", False):
            return False
        return attempt < self.max_attempts

    def delay_for(self, attempt: int, *, error: CrmError | None = None) -> float:
        """
        Seconds to wait before attempt ``attempt + 1``.

        A provider's own `Retry-After` wins when it is longer than ours.
        Arguing with a rate limiter by retrying sooner than it asked is how a
        token gets suspended; waiting longer than it asked costs only latency.
        """
        exponential = min(self.base_seconds * (2 ** max(attempt - 1, 0)), self.max_seconds)
        delay = random.uniform(0, exponential) if self.jitter else exponential

        if isinstance(error, CrmRateLimited) and error.retry_after:
            delay = max(delay, float(error.retry_after))

        return min(delay, self.max_seconds)

    def is_exhausted(self, attempt: int) -> bool:
        return attempt >= self.max_attempts


def policy_from_settings() -> RetryPolicy:
    """Requirement 13: retry settings must be configurable."""
    from app.core.config import settings

    return RetryPolicy(
        max_attempts=settings.crm_retry_max_attempts,
        base_seconds=settings.crm_retry_base_seconds,
        max_seconds=settings.crm_retry_max_seconds,
    )


# ------------------------------------------------------------ rate limiting ---

@dataclass
class _Bucket:
    tokens: float
    updated_at: float


@dataclass
class TokenBucketLimiter:
    """
    Per-(tenant, provider) token bucket.

    Process-local, and that is a deliberate limit rather than an oversight.
    The product runs one scheduler process (see `scripts/scheduler.py`, and
    the same reasoning that kept Celery out of STEP 4), so a local bucket is
    the whole population. A second worker would need this in Redis, and the
    interface is small enough that swapping the backing store does not touch
    a caller.

    What it is genuinely for: stopping one tenant from monopolising a worker
    pass. It is a fairness device, not a security control.
    """

    rate_per_second: float = 5.0
    burst: float = 20.0
    _buckets: dict[tuple[str, str], _Bucket] = field(default_factory=dict)

    def _key(self, tenant_id: str, provider: str) -> tuple[str, str]:
        return (str(tenant_id), str(provider))

    def allow(self, tenant_id: str, provider: str, *, now: float | None = None) -> bool:
        """Take one token. False means "not yet"; the caller must not spin."""
        current = time.monotonic() if now is None else now
        key = self._key(tenant_id, provider)
        bucket = self._buckets.get(key)

        if bucket is None:
            # Start full, then fall through to the normal path rather than
            # granting unconditionally. Special-casing the first call meant a
            # bucket configured with burst=0 still handed out one token and
            # left itself at -1 -- so a limiter set to "allow nothing" allowed
            # exactly one request per (tenant, provider). A test caught it.
            bucket = _Bucket(tokens=float(self.burst), updated_at=current)
            self._buckets[key] = bucket

        elapsed = max(0.0, current - bucket.updated_at)
        bucket.tokens = min(self.burst, bucket.tokens + elapsed * self.rate_per_second)
        bucket.updated_at = current

        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return True
        return False

    def retry_after(self, tenant_id: str, provider: str) -> float:
        """How long until one token is available. Used to schedule, not sleep."""
        bucket = self._buckets.get(self._key(tenant_id, provider))
        if bucket is None or bucket.tokens >= 1:
            return 0.0
        return max(0.0, (1 - bucket.tokens) / max(self.rate_per_second, 0.001))

    def reset(self) -> None:
        self._buckets.clear()


#: The process-wide limiter. A module global for the same reason a connection
#: pool is: there must be exactly one, and threading it through every call
#: signature would obscure more than it reveals.
limiter = TokenBucketLimiter()


def configure_limiter_from_settings() -> None:
    from app.core.config import settings

    limiter.rate_per_second = settings.crm_rate_limit_per_second
    limiter.burst = settings.crm_rate_limit_burst
