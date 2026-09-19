"""Human and machine rendering for validation results (Step 4).

The table formatter is the "environment validation command" output: one row
per provider with a SKIPPED / PASS / FAIL / BLOCKED status and a secret-safe
explanation. ``to_json`` is the machine-readable twin, used by CI.
"""
from __future__ import annotations

import json
from collections import OrderedDict

from app.integrations.validation.status import CheckStatus


def format_table(outcomes, masker=None) -> str:
    """One provider per line. ``masker`` re-scrubs reasons defence-in-depth."""
    mask = masker.mask if masker is not None else (lambda s: s)
    name_w = max((len(o.provider) for o in outcomes), default=8)
    status_w = max((len(o.status.value) for o in outcomes), default=7)
    lines = [f"{'Provider':<{name_w}}  {'Status':<{status_w}}  Detail"]
    lines.append("-" * (name_w + status_w + 12))
    for outcome in outcomes:
        detail = mask(outcome.reason) if outcome.reason else ""
        lines.append(
            f"{outcome.provider:<{name_w}}  "
            f"{outcome.status.value:<{status_w}}  {detail}"
        )
    return "\n".join(lines)


def summary_counts(outcomes) -> "OrderedDict[str, int]":
    """Counts in a stable order: PASS, FAIL, BLOCKED, SKIPPED."""
    counts: OrderedDict[str, int] = OrderedDict(
        (s.value, 0) for s in CheckStatus
    )
    for outcome in outcomes:
        counts[outcome.status.value] += 1
    return counts


def to_json(outcomes) -> str:
    """Machine-readable output; every string already secret-safe."""
    return json.dumps([o.as_dict() for o in outcomes], indent=2)
