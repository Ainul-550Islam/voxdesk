"""Unit tests for app.analytics.forecast.

Co-located with the package (not under tests/) for the same reason as
test_stats.py. Run with: python -m pytest app/analytics/ -q
"""

from __future__ import annotations

from datetime import date

import pytest

from app.analytics.forecast import (
    UsagePoint,
    churn_risk,
    exponential_smoothing,
    linear_trend,
    moving_average,
    next_period_labels,
    project_usage,
)


def points(values, prefix="p"):
    return [UsagePoint(f"{prefix}{i}", float(v)) for i, v in enumerate(values)]


def test_moving_average_trailing_window():
    assert moving_average(points([1, 2, 3, 4, 5]), 3) == [None, None, 2.0, 3.0, 4.0]


def test_moving_average_window_validation():
    with pytest.raises(ValueError):
        moving_average(points([1, 2, 3]), 0)


def test_exponential_smoothing_known_levels():
    assert exponential_smoothing(points([1, 2, 3, 4]), 0.5) == [1.0, 1.5, 2.25, 3.125]
    # alpha=1 reproduces the series exactly.
    assert exponential_smoothing(points([1, 2, 3, 4]), 1.0) == [1.0, 2.0, 3.0, 4.0]
    with pytest.raises(ValueError):
        exponential_smoothing(points([1, 2]), 0.0)
    with pytest.raises(ValueError):
        exponential_smoothing(points([1, 2]), 1.5)
    assert exponential_smoothing([], 0.5) == []


def test_linear_trend_recovers_exact_line():
    trend = linear_trend(points([2, 5, 8, 11]))
    assert trend.slope == 3.0
    assert trend.intercept == 2.0
    assert trend.r_squared == 1.0


def test_linear_trend_requires_two_points():
    with pytest.raises(ValueError):
        linear_trend(points([5]))


def test_next_period_labels_daily_weekly_and_plain():
    daily = [UsagePoint("2026-09-10", 1), UsagePoint("2026-09-11", 1), UsagePoint("2026-09-12", 1)]
    assert next_period_labels(daily, 2) == ["2026-09-13", "2026-09-14"]

    weekly = [UsagePoint("2026-09-01", 1), UsagePoint("2026-09-08", 1), UsagePoint("2026-09-15", 1)]
    assert next_period_labels(weekly, 2) == ["2026-09-22", "2026-09-29"]

    single = [UsagePoint("2026-09-10", 1)]
    assert next_period_labels(single, 2) == ["2026-09-11", "2026-09-12"]

    plain = [UsagePoint("a", 1), UsagePoint("b", 1)]
    assert next_period_labels(plain, 3) == ["p+1", "p+2", "p+3"]

    assert next_period_labels([], 2) == []


def test_project_linear_continues_trend_with_band():
    out = project_usage(points([2, 5, 8, 11]), 2, method="linear")
    assert [(p.step, p.value) for p in out] == [(1, 14.0), (2, 17.0)]
    for p in out:
        # symmetric band around the point estimate
        assert p.upper - p.value == pytest.approx(p.value - p.lower, abs=1e-9)
        assert p.lower < p.value < p.upper
    assert [p.label for p in out] == ["p+1", "p+2"]


def test_project_flat_methods_hold_level():
    ma = project_usage(points([1, 2, 3, 4, 5]), 3, method="moving_average", window=3)
    assert [p.value for p in ma] == [4.0, 4.0, 4.0]

    ses = project_usage(points([1, 2, 3, 4]), 2, method="exponential_smoothing", alpha=0.5)
    assert [p.value for p in ses] == [3.12, 3.12]  # 3.125 rounded to cents


def test_project_single_point_holds_flat_with_floor_band():
    out = project_usage([UsagePoint("a", 100.0)], 2, method="linear")
    assert [p.value for p in out] == [100.0, 100.0]
    # floor band = 10% of |value| → 100 ± 20
    assert out[0].lower == 80.0
    assert out[0].upper == 120.0


def test_project_usage_validation():
    with pytest.raises(ValueError):
        project_usage(points([1, 2]), -1)
    with pytest.raises(ValueError):
        project_usage(points([1, 2]), 2, method="bogus")
    assert project_usage([], 3) == []


def test_churn_risk_empty_is_low():
    risk = churn_risk([])
    assert (risk.score, risk.band, risk.signals) == (0, "low", ())


def test_churn_risk_flat_series_is_low():
    series = [
        UsagePoint(f"2026-09-0{i}", 10.0) for i in range(1, 7)
    ]  # 09-01..09-06
    risk = churn_risk(series, today=date(2026, 9, 7))
    assert risk.score == 0
    assert risk.band == "low"
    assert risk.signals == ()


def test_churn_risk_mild_decline_is_medium():
    series = points([100, 95, 90, 85, 80])
    risk = churn_risk(series)
    assert risk.score == 30
    assert risk.band == "medium"
    assert risk.signals == ("declining_trend",)


def test_churn_risk_decline_and_tail_is_high():
    series = points([10, 10, 10, 10, 0])
    risk = churn_risk(series)
    assert risk.score == 75
    assert risk.band == "high"
    assert risk.signals == ("declining_trend", "recent_drop", "tail_inactivity")


def test_churn_risk_recent_drop_and_volatility():
    series = points([1, 100, 1, 100, 1])
    risk = churn_risk(series)
    assert risk.score == 40
    assert risk.band == "medium"
    assert risk.signals == ("recent_drop", "volatility")


def test_churn_risk_staleness_needs_calendar_and_gap():
    series = [
        UsagePoint(f"2026-09-0{i}", 10.0) for i in range(1, 7)
    ]  # ends 09-06
    # today 4 days after the last point, step 1 day → stale
    risk = churn_risk(series, today=date(2026, 9, 10))
    assert risk.score == 10
    assert risk.band == "low"
    assert risk.signals == ("staleness",)


def test_churn_risk_all_signals_scores_100():
    series = [
        UsagePoint("2026-09-01", 100.0),
        UsagePoint("2026-09-02", 10.0),
        UsagePoint("2026-09-03", 100.0),
        UsagePoint("2026-09-04", 10.0),
        UsagePoint("2026-09-05", 100.0),
        UsagePoint("2026-09-06", 0.0),
    ]
    risk = churn_risk(series, today=date(2026, 9, 10))
    assert risk.score == 100
    assert risk.band == "high"
    assert risk.signals == (
        "declining_trend",
        "recent_drop",
        "tail_inactivity",
        "volatility",
        "staleness",
    )


def test_churn_risk_is_deterministic_with_injected_today():
    series = [UsagePoint(f"2026-09-0{i}", 10.0) for i in range(1, 7)]
    first = churn_risk(series, today=date(2026, 9, 10))
    second = churn_risk(series, today=date(2026, 9, 10))
    assert first == second
