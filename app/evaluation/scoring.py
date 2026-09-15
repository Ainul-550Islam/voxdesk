"""Deterministic text-scoring metrics (Phase 4, evaluation slice).

Every metric is a pure function of its inputs, so a candidate output can be
scored against a reference with no model, no database and no randomness. These
are the building blocks the ``evaluator`` harness turns into pass/fail checks.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, in order. Numbers count as tokens."""
    return _TOKEN_RE.findall(text.lower())


def exact_match(candidate: str, reference: str) -> bool:
    """Case- and whitespace-insensitive equality."""
    return " ".join(candidate.split()).lower() == " ".join(reference.split()).lower()


def contains_all(candidate: str, needles: Sequence[str]) -> bool:
    """True when every needle appears (case-insensitive) in the candidate."""
    low = candidate.lower()
    return all(needle.lower() in low for needle in needles)


def found_forbidden(candidate: str, needles: Sequence[str]) -> list[str]:
    """The needles that DO appear in the candidate (empty = clean)."""
    low = candidate.lower()
    return [needle for needle in needles if needle.lower() in low]


def token_precision(candidate: str, reference: str) -> float:
    """|candidate ∩ reference| / |candidate| over token multisets."""
    cand = Counter(tokenize(candidate))
    ref = Counter(tokenize(reference))
    overlap = sum((cand & ref).values())
    cand_total = sum(cand.values())
    return overlap / cand_total if cand_total else 0.0


def token_recall(candidate: str, reference: str) -> float:
    """|candidate ∩ reference| / |reference| over token multisets."""
    cand = Counter(tokenize(candidate))
    ref = Counter(tokenize(reference))
    overlap = sum((cand & ref).values())
    ref_total = sum(ref.values())
    return overlap / ref_total if ref_total else 0.0


def token_f1(candidate: str, reference: str) -> float:
    """Harmonic mean of token precision and recall. 1.0 when both sides are
    empty, 0.0 when exactly one is empty."""
    precision = token_precision(candidate, reference)
    recall = token_recall(candidate, reference)
    if precision + recall == 0.0:
        return 1.0 if not tokenize(candidate) and not tokenize(reference) else 0.0
    return 2.0 * precision * recall / (precision + recall)


def levenshtein(a: str, b: str) -> int:
    """Edit distance between two strings (insertions, deletions, substitutions),
    computed iteratively with two rows."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            curr.append(
                min(
                    prev[j] + 1,
                    curr[j - 1] + 1,
                    prev[j - 1] + (0 if ca == cb else 1),
                )
            )
        prev = curr
    return prev[-1]


def char_similarity(a: str, b: str) -> float:
    """1 - (edit distance / max length), in [0, 1]. 1.0 when both empty."""
    longest = max(len(a), len(b))
    if longest == 0:
        return 1.0
    return 1.0 - levenshtein(a, b) / longest


def extract_numbers(text: str) -> list[float]:
    """Every integer/decimal number in the text, in order of appearance."""
    return [float(match) for match in _NUMBER_RE.findall(text)]


def numbers_match(candidate: str, reference: str) -> bool:
    """True when the candidate's numbers equal the reference's, in order. This
    is the "did it get the price/time right" check."""
    return extract_numbers(candidate) == extract_numbers(reference)
