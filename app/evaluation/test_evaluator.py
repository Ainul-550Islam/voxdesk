"""Unit tests for app.evaluation.evaluator.

Co-located with the package for the same reason as the other slices. Run with:
python -m pytest app/evaluation/ -q
"""

from __future__ import annotations

import pytest

from app.evaluation.evaluator import (
    Case,
    Check,
    CaseResult,
    SuiteResult,
    evaluate_case,
    run_check,
    run_suite,
)


def test_exact_check_pass_and_fail():
    check = Check(name="exact", kind="exact", reference="Hello world")
    assert run_check(check, "  hello   world ").passed
    result = run_check(check, "goodbye")
    assert not result.passed
    assert "goodbye" in result.detail


def test_contains_check():
    check = Check(name="mentions price", kind="contains", needles=("120 dollars", "cleaning"))
    assert run_check(check, "a standard cleaning costs 120 dollars").passed
    failed = run_check(check, "a standard cleaning costs 260 dollars")
    assert not failed.passed
    assert "120 dollars" in failed.detail


def test_forbids_check():
    check = Check(name="no cross-tenant leak", kind="forbids", needles=("300 euros", "Beta"))
    assert run_check(check, "a cleaning costs 120 dollars").passed
    failed = run_check(check, "Beta charges 300 euros")
    assert not failed.passed
    assert "300 euros" in failed.detail


def test_token_f1_threshold():
    check = Check(name="overlap", kind="token_f1", reference="a b c", minimum=0.6)
    assert run_check(check, "a b").passed
    assert not run_check(check, "x y z").passed


def test_similarity_threshold():
    check = Check(name="near-match", kind="similarity", reference="kitten", minimum=0.8)
    assert run_check(check, "kitten").passed
    assert not run_check(check, "kitchen").passed


def test_numbers_check():
    check = Check(name="price", kind="numbers", reference="120 dollars")
    assert run_check(check, "the price is 120").passed
    failed = run_check(check, "the price is 260")
    assert not failed.passed


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        run_check(Check(name="x", kind="bogus"), "anything")


def test_needle_checks_require_needles():
    with pytest.raises(ValueError):
        run_check(Check(name="x", kind="contains"), "anything")
    with pytest.raises(ValueError):
        run_check(Check(name="x", kind="forbids"), "anything")


def test_case_with_no_checks_is_rejected():
    with pytest.raises(ValueError):
        evaluate_case(Case(name="empty", prompt="q"), "answer")


def test_evaluate_case_folds_all_checks():
    case = Case(
        name="booking",
        prompt="book a cleaning",
        reference="A standard cleaning costs 120 dollars.",
        checks=(
            Check(name="contains price", kind="contains", needles=("120 dollars",)),
            Check(name="no wrong price", kind="forbids", needles=("260 dollars",)),
            Check(name="price number", kind="numbers", reference="120 dollars"),
        ),
    )
    result = evaluate_case(case, "A standard cleaning costs 120 dollars.")
    assert isinstance(result, CaseResult)
    assert result.passed
    assert len(result.checks) == 3


def test_run_suite_with_stub_model_is_deterministic():
    outputs = {
        "what does a cleaning cost?": "A standard cleaning costs 120 dollars.",
        "what does a deep cleaning cost?": "A deep cleaning costs 260 dollars.",
    }

    def model(prompt: str) -> str:
        return outputs[prompt]

    cases = (
        Case(
            name="standard_price",
            prompt="what does a cleaning cost?",
            checks=(
                Check(name="contains price", kind="contains", needles=("120 dollars",)),
                Check(name="not deep price", kind="forbids", needles=("260 dollars",)),
            ),
        ),
        Case(
            name="deep_price",
            prompt="what does a deep cleaning cost?",
            checks=(
                Check(name="contains price", kind="contains", needles=("260 dollars",)),
                # Deliberately wrong needle: must fail and name itself.
                Check(name="wrong expectation", kind="contains", needles=("120 dollars",)),
            ),
        ),
    )

    suite = run_suite(cases, model)
    assert isinstance(suite, SuiteResult)
    assert not suite.passed
    assert suite.counts() == {"total": 2, "passed": 1, "failed": 1}
    assert suite.failed_names() == ("deep_price",)

    report = suite.report()
    assert "PASS  standard_price" in report
    assert "FAIL  deep_price" in report
    assert "wrong expectation" in report
    assert "passed 1/2 cases" in report
