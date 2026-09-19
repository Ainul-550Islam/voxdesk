"""Unit tests for app.providers.breaker.

Co-located with the package; run with: python -m pytest app/providers/ -q
"""

from __future__ import annotations

import pytest

from app.providers.breaker import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    BreakerError,
    CircuitBreaker,
)


def test_invalid_configuration_rejected():
    with pytest.raises(BreakerError):
        CircuitBreaker(failure_threshold=0)
    with pytest.raises(BreakerError):
        CircuitBreaker(window_seconds=0)
    with pytest.raises(BreakerError):
        CircuitBreaker(cooldown_seconds=-1)
    with pytest.raises(BreakerError):
        CircuitBreaker(half_open_max_probes=0)


def test_starts_closed_and_admits():
    breaker = CircuitBreaker()
    assert breaker.state == CLOSED
    assert breaker.allow(0.0)


def test_opens_after_threshold_failures_within_window():
    breaker = CircuitBreaker(failure_threshold=3, window_seconds=60.0)
    for second in range(3):
        assert breaker.allow(float(second))
        breaker.record_failure(float(second))
    assert breaker.state == OPEN
    assert not breaker.allow(10.0)


def test_open_blocks_until_cooldown_then_half_opens():
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=30.0)
    breaker.allow(0.0)
    breaker.record_failure(0.0)  # opens at t=0
    assert breaker.state == OPEN
    assert not breaker.allow(10.0)   # still cooling down
    assert breaker.state == OPEN
    assert breaker.allow(30.0)       # cooldown elapsed → half-open probe
    assert breaker.state == HALF_OPEN


def test_probe_success_closes_breaker():
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10.0)
    breaker.allow(0.0)
    breaker.record_failure(0.0)
    assert breaker.allow(10.0)  # half-open probe
    breaker.record_success(10.0)
    assert breaker.state == CLOSED
    assert breaker.failure_count(11.0) == 0
    assert breaker.allow(12.0)


def test_probe_failure_reopens():
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10.0)
    breaker.allow(0.0)
    breaker.record_failure(0.0)
    assert breaker.allow(10.0)
    breaker.record_failure(10.0)
    assert breaker.state == OPEN
    assert not breaker.allow(11.0)


def test_failures_outside_window_do_not_count():
    breaker = CircuitBreaker(failure_threshold=2, window_seconds=5.0)
    breaker.allow(0.0)
    breaker.record_failure(0.0)
    # 10s later the first failure has aged out of the 5s window.
    assert breaker.failure_count(10.0) == 0
    assert breaker.allow(10.0)
    breaker.record_failure(10.0)  # only this one counts → still closed
    assert breaker.state == CLOSED


def test_half_open_limits_probes_in_flight():
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.0,
                             half_open_max_probes=1)
    breaker.allow(0.0)
    breaker.record_failure(0.0)
    assert breaker.allow(0.0)  # first probe admitted
    assert breaker.state == HALF_OPEN
    assert not breaker.allow(0.0)  # second probe refused


def test_reset_returns_to_closed():
    breaker = CircuitBreaker(failure_threshold=1)
    breaker.allow(0.0)
    breaker.record_failure(0.0)
    assert breaker.state == OPEN
    breaker.reset()
    assert breaker.state == CLOSED
    assert breaker.allow(0.0)
