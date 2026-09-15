"""Deterministic statistics helpers for analytics (Phase 4, pure stdlib).

Conversation metrics and forecasting share one audited definition of
``percentile`` and ``linreg`` instead of each re-deriving them. Every function
here is pure: same inputs, same outputs, no state, no I/O, no numpy — which
is what keeps the analytics core runnable and testable without infrastructure.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean. Raises ``ValueError`` on empty input."""
    if not values:
        raise ValueError("mean of empty sequence")
    return sum(values) / len(values)


def median(values: Sequence[float]) -> float:
    """Median; the average of the two middle values for even-length input."""
    if not values:
        raise ValueError("median of empty sequence")
    ordered = sorted(values)
    n = len(ordered)
    if n % 2:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0


def stdev_sample(values: Sequence[float]) -> float:
    """Sample standard deviation (n-1 denominator). 0.0 for fewer than two
    points, so callers can divide by it without a special case."""
    if len(values) < 2:
        return 0.0
    m = mean(values)
    variance = sum((x - m) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(variance)


def percentile(values: Sequence[float], p: float) -> float:
    """The p-th percentile (0-100) with linear interpolation between the two
    closest ranks — the same rule numpy's default uses.

    Raises ``ValueError`` on empty input, on ``p`` outside ``[0, 100]``, or on
    any non-finite value. A single value returns that value.
    """
    if not 0.0 <= p <= 100.0:
        raise ValueError("percentile p must be in [0, 100]")
    ordered = sorted(float(v) for v in values)
    if not ordered:
        raise ValueError("percentile of empty sequence")
    for v in ordered:
        if not math.isfinite(v):
            raise ValueError("percentile of non-finite values")
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


@dataclass(frozen=True)
class LinearTrend:
    """Least-squares fit ``y = slope * x + intercept`` with R-squared."""

    slope: float
    intercept: float
    r_squared: float


def linreg(xs: Sequence[float], ys: Sequence[float]) -> LinearTrend:
    """Ordinary least squares fit.

    Requires at least two points and matching lengths. A degenerate all-x-equal
    input raises ``ValueError`` because the slope is undefined.
    """
    if len(xs) != len(ys):
        raise ValueError("xs and ys must have equal length")
    n = len(xs)
    if n < 2:
        raise ValueError("linreg needs at least two points")
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    denom = sum((x - x_mean) ** 2 for x in xs)
    if denom == 0.0:
        raise ValueError("linreg is undefined when all x are equal")
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denom
    intercept = y_mean - slope * x_mean

    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot else 1.0
    return LinearTrend(slope=slope, intercept=intercept, r_squared=r_squared)
