"""Condition evaluation for workflow branches and campaign filters (Phase 4).

This is the *one* evaluator shared by the orchestration engine, mirroring
``app/domain/workflow_models.evaluate_condition`` — the domain layer reuses a
single operator vocabulary for workflow branches and campaign filter rules,
and so does this engine. Pure and defensive: an unknown operator evaluates to
``False`` (validation rejects it upstream), and ordering operators raise
``ValueError`` on incomparable types instead of guessing.
"""

from __future__ import annotations

from typing import Any

#: The operator vocabulary (mirrors ``workflow_models.CONDITION_OPERATORS``).
CONDITION_OPERATORS = frozenset({
    "eq", "ne", "gt", "gte", "lt", "lte",
    "in", "not_in", "contains", "starts_with", "ends_with",
    "exists", "not_exists",
})


def compare(a: Any, b: Any) -> int:
    """Total, type-aware comparison used by the ordering operators.

    Numerics compare numerically, strings lexicographically; anything else
    raises ``ValueError`` because an ordering on mixed types would be a guess.
    """
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return (a > b) - (a < b)
    if isinstance(a, str) and isinstance(b, str):
        return (a > b) - (a < b)
    raise ValueError(f"cannot order-compare {type(a).__name__} with {type(b).__name__}")


def evaluate(field_value: Any, operator: str, expected: Any) -> bool:
    """Evaluate one predicate. ``field_value`` is ``payload.get(field)``.

    ``exists``/``not_exists`` ignore ``expected`` and test for ``None``.
    ``in``/``not_in`` require ``expected`` to be a list/tuple/set. Ordering
    operators delegate to :func:`compare`.
    """
    if operator == "exists":
        return field_value is not None
    if operator == "not_exists":
        return field_value is None
    if field_value is None:
        return False
    if operator == "eq":
        return field_value == expected
    if operator == "ne":
        return field_value != expected
    if operator == "gt":
        return compare(field_value, expected) > 0
    if operator == "gte":
        return compare(field_value, expected) >= 0
    if operator == "lt":
        return compare(field_value, expected) < 0
    if operator == "lte":
        return compare(field_value, expected) <= 0
    if operator == "in":
        return isinstance(expected, (list, tuple, set)) and field_value in expected
    if operator == "not_in":
        return isinstance(expected, (list, tuple, set)) and field_value not in expected
    if operator == "contains":
        return isinstance(field_value, (str, list, tuple)) and expected in field_value
    if operator == "starts_with":
        return isinstance(field_value, str) and field_value.startswith(str(expected))
    if operator == "ends_with":
        return isinstance(field_value, str) and field_value.endswith(str(expected))
    return False
