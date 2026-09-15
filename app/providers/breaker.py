"""Deterministic circuit breaker (Phase 4, multi-provider hardening).

A provider that is down should not be hammered: after ``failure_threshold``
failures inside ``window_seconds`` the breaker **opens**, short-circuiting
further attempts; after ``cooldown_seconds`` it moves to **half-open** and
admits a single probe; a probe success **closes** the breaker, a probe failure
re-opens it.

The breaker is fully deterministic: the clock is caller-supplied (a float
monotonic timestamp), and the only state is the failure history, so a
sequence of ``(now, event)`` inputs always yields the same transitions. This
mirrors the retry/backoff thinking in ``app.agent.errors`` (transient vs
permanent, bounded retries) but as a pure, testable state machine that never
sleeps and never dials.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

STATES = frozenset({CLOSED, OPEN, HALF_OPEN})


class BreakerError(ValueError):
    """Raised for invalid breaker configuration."""


@dataclass
class CircuitBreaker:
    """A single-provider circuit breaker keyed by a monotonic float clock."""

    failure_threshold: int = 5
    window_seconds: float = 60.0
    cooldown_seconds: float = 30.0
    half_open_max_probes: int = 1
    _state: str = CLOSED
    _failures: list[float] = field(default_factory=list)
    _opened_at: float | None = None
    _probes_outstanding: int = 0

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise BreakerError("failure_threshold must be >= 1")
        if self.window_seconds <= 0:
            raise BreakerError("window_seconds must be > 0")
        if self.cooldown_seconds < 0:
            raise BreakerError("cooldown_seconds must be >= 0")
        if self.half_open_max_probes < 1:
            raise BreakerError("half_open_max_probes must be >= 1")

    # -------------------------------------------------------------- state ----

    @property
    def state(self) -> str:
        return self._state

    def failure_count(self, now: float) -> int:
        """Failures still inside the rolling window (for dashboards/logs)."""
        cutoff = now - self.window_seconds
        self._failures = [t for t in self._failures if t >= cutoff]
        return len(self._failures)

    # -------------------------------------------------------------- calls ----

    def allow(self, now: float) -> bool:
        """May a call proceed right now?

        CLOSED: always. OPEN: only once the cooldown has elapsed (transition
        to HALF_OPEN and admit a probe); until then, blocked. HALF_OPEN: at
        most ``half_open_max_probes`` probes in flight.
        """
        if self._state == OPEN:
            if self._opened_at is not None and now - self._opened_at >= self.cooldown_seconds:
                self._state = HALF_OPEN
                self._probes_outstanding = 0
            else:
                return False
        if self._state == HALF_OPEN:
            if self._probes_outstanding >= self.half_open_max_probes:
                return False
            self._probes_outstanding += 1
            return True
        return True

    def record_failure(self, now: float) -> None:
        """Record a failed attempt; may open the breaker."""
        if self._state == HALF_OPEN:
            # A probe failed: re-open immediately, keep only this failure.
            self._state = OPEN
            self._opened_at = now
            self._failures = [now]
            self._probes_outstanding = 0
            return
        self._failures.append(now)
        self.failure_count(now)  # prune outside the window
        if self._state == CLOSED and len(self._failures) >= self.failure_threshold:
            self._state = OPEN
            self._opened_at = now

    def record_success(self, now: float) -> None:
        """Record a successful attempt; closes the breaker from HALF_OPEN."""
        if self._state == HALF_OPEN:
            self._state = CLOSED
            self._failures = []
            self._probes_outstanding = 0
            self._opened_at = None
            return
        # A success in CLOSED clears any stale failures.
        self.failure_count(now)

    def reset(self) -> None:
        self._state = CLOSED
        self._failures = []
        self._opened_at = None
        self._probes_outstanding = 0
