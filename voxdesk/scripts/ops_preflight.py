#!/usr/bin/env python3
"""VoxDesk operational preflight (Step 13 section A).

Inspects the current machine and repository and reports PASS/FAIL/BLOCKED/
NOT_RUN for runtime tooling, repository state, configuration safety and
secret hygiene. Read-only: it never writes anything and never dials, charges,
books or mutates.

Usage:
    python scripts/ops_preflight.py [--json] [--expected-commit <sha>]

Exit codes: 0 = PASS, 1 = FAIL, 2 = BLOCKED, 3 = invalid configuration.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.release import ops  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ops_preflight.py",
        description="VoxDesk operational preflight (read-only).",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--expected-commit", default=None,
        help="expected release commit (optional; FAIL if HEAD differs)",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    report = ops.run_preflight(ops.REPO_ROOT, expected_commit=args.expected_commit)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(ops.render_preflight(report))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
