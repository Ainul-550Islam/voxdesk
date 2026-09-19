"""Usage forecasting and churn risk (Phase 4, analytics slice).

Pure, deterministic functions over an ordered series of ``UsagePoint``s. No
model training, no randomness: the forecast is a least-squares linear trend
(or a configurable moving average / exponential smoothing) plus a symmetric
band derived from the residual spread, and the churn score is a documented,
rule-based combination of observable signals. Every number is reproducible
from the input series alone, which is what makes it auditable.

``today`` is an explicit parameter wherever calendar context matters, so the
staleness signal is reproducible in tests and backfills instead of depending
on the wall clock of the machine that happens to run it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from app.analytics._stats import LinearTrend, linreg, mean, stdev_sample

FORECAST_METHODS = ("linear", "moving_average", "exponential_smoothing")

#: Churn-signal weights. Documented so the score can be reverse-engineered
#: from the signals alone (see churn_risk).
_WEIGHT_DECLINING_TREND = 30
_WEIGHT_RECENT_DROP = 25
_WEIGHT_TAIL_INACTIVITY = 20
_WEIGHT_VOLATILITY = 15
_WEIGHT_STALENESS = 10

#: A "recent drop" is the last value falling below this fraction of the prior
#: trailing mean. A "volatile" tail is one whose coefficient of variation
#: exceeds this.
_RECENT_DROP_FRACTION = 0.7
_VOLATILITY_CV_THRESHOLD = 0.8

#: Bands the score maps onto.
_BAND_HIGH = 70
_BAND_MEDIUM = 30


@dataclass(frozen=True)
class UsagePoint:
    """One period of usage. ``period`` is an ISO date (``YYYY-MM-DD``) for
    calendar-aligned series, or any other label the caller uses; ``value`` is
    in the caller's unit (voice seconds, millicents, SMS segments, …)."""

    period: str
    value: float


@dataclass(frozen=True)
class Projection:
    """One forecasted step with a symmetric band (±2×spread)."""

    step: int
    label: str
    value: float
    lower: float
    upper: float


@dataclass(frozen=True)
class ChurnRisk:
    """Churn score 0-100, its band, and the signals that produced it."""

    score: int
    band: str
    signals: tuple[str, ...]


def linear_trend(points: Sequence[UsagePoint]) -> LinearTrend:
    """Least-squares trend over the series with x = period index (0..n-1), so
    the slope is "value change per period"."""
    if len(points) < 2:
        raise ValueError("linear_trend needs at least two points")
    return linreg([float(i) for i in range(len(points))], [p.value for p in points])


def moving_average(points: Sequence[UsagePoint], window: int) -> list[float | None]:
    """Trailing simple moving average, aligned to the input. Positions before
    the window fills are ``None``."""
    if window <= 0:
        raise ValueError("window must be positive")
    values = [p.value for p in points]
    out: list[float | None] = []
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= window:
            running -= values[i - window]
        if i >= window - 1:
            out.append(running / window)
        else:
            out.append(None)
    return out


def exponential_smoothing(points: Sequence[UsagePoint], alpha: float) -> list[float]:
    """Simple exponential smoothing: ``level = alpha*v + (1-alpha)*level``.
    ``alpha`` must be in (0, 1]; the first point seeds the level."""
    if not 0.0 < alpha <= 1.0:
        raise ValueError("alpha must be in (0, 1]")
    values = [p.value for p in points]
    if not values:
        return []
    smoothed = [values[0]]
    for value in values[1:]:
        smoothed.append(alpha * value + (1.0 - alpha) * smoothed[-1])
    return smoothed


def next_period_labels(points: Sequence[UsagePoint], horizon: int) -> list[str]:
    """Future period labels for a forecast.

    When the input periods are ISO dates, the labels continue the series at
    its most common observed step (e.g. daily, weekly). Otherwise they are
    ``p+1`` … ``p+horizon``.
    """
    if not points or horizon == 0:
        return []
    periods = [p.period for p in points]
    try:
        dates = [date.fromisoformat(period) for period in periods]
    except ValueError:
        dates = []
    if dates and len(dates) >= 2:
        deltas = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        step_days = max(set(deltas), key=deltas.count)
        last = dates[-1]
        return [
            (last + timedelta(days=step_days * (i + 1))).isoformat()
            for i in range(horizon)
        ]
    if dates:
        last = dates[-1]
        return [(last + timedelta(days=i + 1)).isoformat() for i in range(horizon)]
    return [f"p+{i}" for i in range(1, horizon + 1)]


def project_usage(
    points: Sequence[UsagePoint],
    horizon: int,
    method: str = "linear",
    *,
    window: int = 3,
    alpha: float = 0.5,
) -> list[Projection]:
    """Forecast ``horizon`` steps.

    * ``linear`` — continue the least-squares trend.
    * ``moving_average`` — hold the last trailing average (``window``) flat.
    * ``exponential_smoothing`` — hold the last smoothed level (``alpha``) flat.

    The band is ±2× the spread (residual spread for the linear method, value
    spread otherwise), floored at 10% of the mean absolute value so a
    perfectly flat series still gets an honest, non-degenerate band.
    """
    if horizon < 0:
        raise ValueError("horizon must be >= 0")
    if method not in FORECAST_METHODS:
        raise ValueError(f"unknown method {method!r}")
    if not points:
        return []

    values = [p.value for p in points]
    labels = next_period_labels(points, horizon)
    n = len(values)

    if method == "linear" and n >= 2:
        trend = linear_trend(points)
        base = [
            trend.intercept + trend.slope * (n - 1 + step)
            for step in range(1, horizon + 1)
        ]
        residual = [
            value - (trend.slope * i + trend.intercept)
            for i, value in enumerate(values)
        ]
        spread = stdev_sample(residual)
    elif method == "moving_average":
        series = moving_average(points, window)
        last = series[-1] if series[-1] is not None else values[-1]
        base = [last] * horizon
        spread = stdev_sample(values)
    elif method == "exponential_smoothing":
        series = exponential_smoothing(points, alpha)
        base = [series[-1]] * horizon
        spread = stdev_sample(values)
    else:
        # Linear method with a single point: hold it flat.
        base = [values[-1]] * horizon
        spread = 0.0

    if spread == 0.0:
        spread = abs(mean(values)) * 0.1 or 1.0

    return [
        Projection(
            step=step,
            label=label,
            value=round(value, 2),
            lower=round(value - 2.0 * spread, 2),
            upper=round(value + 2.0 * spread, 2),
        )
        for step, (value, label) in enumerate(zip(base, labels), start=1)
    ]


def churn_risk(points: Sequence[UsagePoint], today: date | None = None) -> ChurnRisk:
    """Rule-based churn score 0-100 from observable, documented signals.

    Signals and weights:

    * declining trend (30)  — negative least-squares slope over the series;
    * recent drop (25)      — last value below 70% of the prior trailing mean;
    * tail inactivity (20)  — final value 0 while earlier periods were active;
    * volatility (15)       — coefficient of variation of the last 5 points
                              exceeds 0.8;
    * staleness (10)        — at least 6 ISO-date points and the latest point
                              is more than 1.5× the observed step behind
                              ``today``.

    Bands: <30 low, 30-69 medium, ≥70 high. ``today`` defaults to the wall
    clock but is injectable for reproducible tests and backfills.
    """
    if not points:
        return ChurnRisk(score=0, band="low", signals=())
    values = [p.value for p in points]
    signals: list[str] = []
    score = 0

    if len(points) >= 2 and linear_trend(points).slope < 0:
        signals.append("declining_trend")
        score += _WEIGHT_DECLINING_TREND

    if len(points) >= 3:
        prior_mean = mean(values[:-1])
        if prior_mean > 0 and values[-1] < prior_mean * _RECENT_DROP_FRACTION:
            signals.append("recent_drop")
            score += _WEIGHT_RECENT_DROP

    if values[-1] == 0 and any(value > 0 for value in values[:-1]):
        signals.append("tail_inactivity")
        score += _WEIGHT_TAIL_INACTIVITY

    recent = values[-5:]
    if len(recent) >= 2 and mean(recent) > 0:
        cv = stdev_sample(recent) / mean(recent)
        if cv > _VOLATILITY_CV_THRESHOLD:
            signals.append("volatility")
            score += _WEIGHT_VOLATILITY

    if len(points) >= 6:
        try:
            dates = [date.fromisoformat(p.period) for p in points]
        except ValueError:
            dates = []
        if dates:
            deltas = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
            if deltas:
                step_days = max(set(deltas), key=deltas.count)
                reference = today if today is not None else date.today()
                gap_days = (reference - dates[-1]).days
                if gap_days > step_days * 1.5:
                    signals.append("staleness")
                    score += _WEIGHT_STALENESS

    score = max(0, min(100, score))
    if score >= _BAND_HIGH:
        band = "high"
    elif score >= _BAND_MEDIUM:
        band = "medium"
    else:
        band = "low"
    return ChurnRisk(score=score, band=band, signals=tuple(signals))
