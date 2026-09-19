#!/usr/bin/env python3
"""VoxDesk observability certification (Step 13 section H).

Read-only validation of the Prometheus/Grafana stack: health, scrape targets,
rule files, alert rules, SLO rules, API metrics, scheduler metrics and Grafana
health where configured. Never mutates monitoring configuration.

Usage:
    python scripts/verify_observability.py --prometheus-url http://localhost:9090 \
        [--grafana-url http://localhost:3000] [--json]

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
        prog="verify_observability.py", description="VoxDesk observability certification"
    )
    parser.add_argument("--prometheus-url", default="", help="Prometheus base URL")
    parser.add_argument("--grafana-url", default="", help="Grafana base URL (optional)")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    result = ops.verify_observability(args.prometheus_url, args.grafana_url)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Observability verification: {result['status']}")
        print(f"  {result['evidence']}")
        for key, value in sorted(result.get("checks", {}).items()):
            print(f"  {key}: {value}")
    return ops.exit_code_for_status(result["status"])


if __name__ == "__main__":
    sys.exit(main())
