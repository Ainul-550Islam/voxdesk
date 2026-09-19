"""Unit tests for app.orchestration.conditions.

Co-located with the package; run with: python -m pytest app/orchestration/ -q
"""

from __future__ import annotations

import pytest

from app.orchestration.conditions import compare, evaluate


def test_equality_operators():
    assert evaluate(5, "eq", 5)
    assert not evaluate(5, "eq", "5")  # no type coercion
    assert evaluate("x", "ne", "y")


def test_ordering_operators_numeric_and_string():
    assert evaluate(5, "gt", 4)
    assert evaluate(5, "gte", 5)
    assert evaluate(4, "lt", 5)
    assert evaluate(4, "lte", 4)
    assert evaluate("b", "gt", "a")


def test_in_operators():
    assert evaluate("a", "in", ["a", "b"])
    assert not evaluate("c", "in", ["a", "b"])
    assert evaluate("c", "not_in", ["a", "b"])


def test_contains_operator():
    assert evaluate("hello world", "contains", "world")
    assert evaluate([1, 2, 3], "contains", 2)
    assert not evaluate("hello", "contains", "xyz")


def test_prefix_suffix_operators():
    assert evaluate("hello world", "starts_with", "hello")
    assert evaluate("hello world", "ends_with", "world")
    assert not evaluate("hello world", "starts_with", "world")


def test_exists_operators_ignore_expected():
    assert evaluate("anything", "exists", None)
    assert not evaluate(None, "exists", None)
    assert evaluate(None, "not_exists", None)
    assert not evaluate("x", "not_exists", None)


def test_none_field_value_is_false_except_exists():
    assert not evaluate(None, "eq", None)
    assert not evaluate(None, "contains", "x")


def test_ordering_incomparable_types_raise():
    with pytest.raises(ValueError):
        evaluate(5, "gt", "a")
    with pytest.raises(ValueError):
        compare([1], [2])


def test_unknown_operator_is_false():
    assert not evaluate(5, "bogus_operator", 5)
