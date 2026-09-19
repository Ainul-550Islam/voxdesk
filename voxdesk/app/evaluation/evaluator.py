"""Pass/fail-per-named-case evaluation harness (Phase 4, evaluation slice).

The model is a *function* the caller injects (``Callable[[str], str]``), so a
suite runs against a stub, a cached transcript, or a live provider — the
harness itself never touches the network. That keeps evaluation deterministic
and re-runnable, which is the only way a prompt change can be compared
before/after honestly.

Reporting follows the same stance as the repo's RAG eval: **pass/fail per
named case**, never an accuracy percentage off a small corpus. ``report()``
prints counts and the failing checks; a regression names itself.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.evaluation import scoring

#: Kinds a Check understands. Unknown kinds are rejected loudly.
KINDS = ("exact", "contains", "forbids", "token_f1", "similarity", "numbers")


@dataclass(frozen=True)
class Check:
    """One assertion against a model output.

    * ``exact``      — ``reference`` matches (case/whitespace-insensitive).
    * ``contains``   — every ``needles`` substring is present.
    * ``forbids``    — no ``needles`` substring is present.
    * ``token_f1``   — token F1 vs ``reference`` is >= ``minimum`` (default 0.5).
    * ``similarity`` — char similarity vs ``reference`` is >= ``minimum``.
    * ``numbers``    — extracted numbers equal ``reference``'s, in order.
    """

    name: str
    kind: str
    reference: str = ""
    needles: tuple[str, ...] = ()
    minimum: float = 0.5


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Case:
    """One named scenario: a prompt, the checks a correct answer must satisfy,
    and (optionally) the gold output the checks compare against."""

    name: str
    prompt: str
    reference: str = ""
    checks: tuple[Check, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class CaseResult:
    case: Case
    output: str
    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


@dataclass(frozen=True)
class SuiteResult:
    results: tuple[CaseResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    def counts(self) -> dict[str, int]:
        passed = sum(1 for result in self.results if result.passed)
        return {"total": len(self.results), "passed": passed, "failed": len(self.results) - passed}

    def failed_names(self) -> tuple[str, ...]:
        return tuple(result.case.name for result in self.results if not result.passed)

    def report(self) -> str:
        """Human-readable summary: one line per case, then failing checks."""
        lines: list[str] = []
        for result in self.results:
            n_passed = sum(1 for check in result.checks if check.passed)
            verdict = "PASS" if result.passed else "FAIL"
            lines.append(f"{verdict}  {result.case.name:<36} {n_passed}/{len(result.checks)} checks")
            for check in result.checks:
                if not check.passed:
                    lines.append(f"      [x] {check.name}: {check.detail}")
        counts = self.counts()
        lines.append("-" * 56)
        lines.append(f"passed {counts['passed']}/{counts['total']} cases")
        return "\n".join(lines)


def run_check(check: Check, output: str) -> CheckResult:
    """Evaluate one check against one output. Raises on an unknown kind or on
    a ``contains``/``forbids`` check with no needles (a check that asserts
    nothing must not exist)."""
    if check.kind not in KINDS:
        raise ValueError(f"unknown check kind {check.kind!r}")

    if check.kind == "exact":
        if scoring.exact_match(output, check.reference):
            return CheckResult(check.name, True, "exact match")
        return CheckResult(
            check.name, False,
            f"expected {check.reference!r}, got {output!r}",
        )

    if check.kind == "contains":
        if not check.needles:
            raise ValueError(f"check {check.name!r}: 'contains' needs needles")
        missing = [n for n in check.needles if not scoring.contains_all(output, (n,))]
        if not missing:
            return CheckResult(check.name, True, "all required text present")
        return CheckResult(check.name, False, f"missing: {missing!r}")

    if check.kind == "forbids":
        if not check.needles:
            raise ValueError(f"check {check.name!r}: 'forbids' needs needles")
        found = scoring.found_forbidden(output, check.needles)
        if not found:
            return CheckResult(check.name, True, "no forbidden text present")
        return CheckResult(check.name, False, f"found forbidden: {found!r}")

    if check.kind == "token_f1":
        f1 = scoring.token_f1(output, check.reference)
        passed = f1 >= check.minimum
        detail = f"token_f1={f1:.3f} {'>=' if passed else '<'} {check.minimum}"
        return CheckResult(check.name, passed, detail)

    if check.kind == "similarity":
        similarity = scoring.char_similarity(output, check.reference)
        passed = similarity >= check.minimum
        detail = f"similarity={similarity:.3f} {'>=' if passed else '<'} {check.minimum}"
        return CheckResult(check.name, passed, detail)

    # kind == "numbers"
    if scoring.numbers_match(output, check.reference):
        return CheckResult(check.name, True, f"numbers match {scoring.extract_numbers(output)!r}")
    return CheckResult(
        check.name, False,
        f"numbers differ: got {scoring.extract_numbers(output)!r}, "
        f"want {scoring.extract_numbers(check.reference)!r}",
    )


def evaluate_case(case: Case, output: str) -> CaseResult:
    """Run every check of a case against one output. A case with no checks is
    rejected — a case that asserts nothing would pass vacuously and prove
    nothing."""
    if not case.checks:
        raise ValueError(f"case {case.name!r} has no checks")
    results = tuple(run_check(check, output) for check in case.checks)
    return CaseResult(case=case, output=output, checks=results)


def run_suite(cases: Sequence[Case], model_fn: Callable[[str], str]) -> SuiteResult:
    """Run every case against the injected model function and fold the results.

    ``model_fn`` receives the rendered prompt and returns the candidate text.
    It is called once per case, in order; the harness itself is deterministic.
    """
    results = tuple(
        evaluate_case(case, model_fn(case.prompt)) for case in cases
    )
    return SuiteResult(results=results)
