#!/usr/bin/env python3
"""VoxDesk cost configuration validator (Step 13 section K).

Validates COST_UNIT_PRICES against the provider/model set that is actually
enabled (i.e. has credentials configured). A missing or zero price is UNKNOWN/
BLOCKED, a malformed price is FAIL, and a present non-negative number is PASS.
Prices are never invented and customer billing semantics are never changed.

Usage:
    VOXDESK_PRICE_SOURCE_DATE=2026-09-13 python scripts/verify_cost_config.py [--json]

Exit codes: 0 = PASS, 1 = FAIL, 2 = BLOCKED, 3 = invalid configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.release import ops  # noqa: E402


def enabled_providers() -> set[str]:
    """Providers whose credentials are configured (hence whose prices matter)."""
    enabled: set[str] = set()
    if settings.twilio_auth_token:
        enabled.add("twilio")
    if settings.elevenlabs_api_key:
        enabled.add("elevenlabs")
    if settings.openai_api_key:
        enabled.add("openai")
    if settings.anthropic_api_key:
        enabled.add("anthropic")
    if settings.google_api_key:
        enabled.add("google")
    return enabled


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_cost_config.py", description="VoxDesk cost configuration validator"
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    prices = dict(settings.cost_unit_prices)
    enabled = enabled_providers()
    source_doc = Path(ops.REPO_ROOT) / "docs" / "STEP10-COST-SOURCES.md"
    source_date = (os.environ.get("VOXDESK_PRICE_SOURCE_DATE") or "").strip()
    result = ops.verify_cost_config(
        prices, enabled, source_date=source_date, source_doc_exists=source_doc.exists()
    )
    result["enabled_providers"] = sorted(enabled)
    result["evidence"] = (
        f"cost configuration {result['status']}: enabled_providers={sorted(enabled)}, "
        f"prices_set={sorted(prices)}"
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Cost configuration: {result['status']}")
        print(f"  enabled providers: {sorted(enabled) or 'none (nothing priced)'}")
        for r in result["results"]:
            print(f"  [{r['status']:<8}] {r['key']:<20} {r['detail']}")
        print(f"  {result['evidence']}")
    return ops.exit_code_for_status(result["status"])


if __name__ == "__main__":
    sys.exit(main())
