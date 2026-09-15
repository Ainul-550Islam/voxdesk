"""Unit tests for app.analytics._stats — pure functions, no fixtures needed.

These tests are co-located with the package (not under tests/) on purpose:
the repo-level tests/conftest.py imports the SQLAlchemy/async stack, which is
unavailable in a bare checkout. Run with: python -m pytest app/analytics/ -q
"""

from __future__ import annotations

import math

import pytest

from app.analytics._stats import linreg, mean, median, percentile, stdev_sample


def test_mean_basic_and_empty():
    assert mean([1, 2, 3, 4]) == 2.5
    with pytest.raises(ValueError):
        mean([])


def test_median_odd_and_even():
    assert median([3, 1, 2]) == 2
    assert median([1, 2, 3, 4]) == 2.5


def test_stdev_sample_known_values():
    # Population 2, 4, 4, 4, 5, 5, 7, 9 has sample stdev 2.138 (wikipedia example).
    values = [2, 4, 4, 4, 5, 5, 7, 9]
    assert math.isclose(stdev_sample(values), 2.138, rel_tol=1e-3)
    # Fewer than two points → 0.0 (safe divisor).
    assert stdev_sample([7]) == 0.0
    assert stdev_sample([]) == 0.0


def test_percentile_interpolation_matches_numpy_default():
    # numpy.percentile([1,2,3,4], 50) == 2.5 ; 25 == 1.75
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4], 25) == 1.75
    assert percentile([1, 2, 3, 4], 0) == 1
    assert percentile([1, 2, 3, 4], 100) == 4


def test_percentile_single_value():
    assert percentile([42], 95) == 42


def test_percentile_errors():
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1, 2], -1)
    with pytest.raises(ValueError):
        percentile([1, 2], 101)
    with pytest.raises(ValueError):
        percentile([1.0, float("inf")], 50)


def test_linreg_recovers_exact_line():
    # y = 2x + 1
    trend = linreg([0.0, 1.0, 2.0, 3.0], [1.0, 3.0, 5.0, 7.0])
    assert trend.slope == 2.0
    assert trend.intercept == 1.0
    assert trend.r_squared == 1.0


def test_linreg_scattered_r2_in_range():
    trend = linreg([0.0, 1.0, 2.0, 3.0, 4.0], [1.0, 2.5, 3.0, 4.5, 5.0])
    assert -1.0 <= trend.r_squared <= 1.0
    assert trend.slope > 0


def test_linreg_errors():
    with pytest.raises(ValueError):
        linreg([1.0], [1.0])  # needs two points
    with pytest.raises(ValueError):
        linreg([1.0, 2.0], [1.0])  # mismatched lengths
    with pytest.raises(ValueError):
        linreg([2.0, 2.0], [1.0, 3.0])  # all x equal
