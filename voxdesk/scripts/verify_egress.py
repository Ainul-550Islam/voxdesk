#!/usr/bin/env python3
"""VoxDesk egress verification (Step 13 section I).

This command must run INSIDE the actual staging container/network namespace.
It tests that denied destinations (loopback, RFC1918, RFC4193, link-local,
cloud metadata, internal hostnames) are unreachable and that the configured
allow-list destinations are reachable.

If it is not running inside the staging network, it reports BLOCKED — never
PASS — because application-level SSRF validation does not prove network
enforcement.

Usage (from inside the staging container):
    VOXDESK_STAGING_NETWORK=1 \
    VOXDESK_EGRESS_ALLOW_HOSTS=api.twilio.com,api.deepgram.com \
    python scripts/verify_egress.py [--json]

Exit codes: 0 = PASS, 1 = FAIL, 2 = BLOCKED, 3 = invalid configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.release import ops  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verify_egress.py", description="VoxDesk egress verification")
    parser.add_argument("--inside-staging", action="store_true",
                        help="assert we are inside the staging network namespace")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    inside = args.inside_staging or (
        os.environ.get("VOXDESK_STAGING_NETWORK", "").strip().lower() in {"1", "true", "yes", "on"}
    )
    allow_hosts = [
        h.strip()
        for h in (os.environ.get("VOXDESK_EGRESS_ALLOW_HOSTS", "") or "").split(",")
        if h.strip()
    ]
    result = ops.verify_egress(inside, allow_hosts=allow_hosts)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Egress verification: {result['status']}")
        print(f"  {result['evidence']}")
        for r in result.get("deny_results", []):
            print(f"  deny  {r['host']:<28} reachable={r['reachable']}")
        for r in result.get("allow_results", []):
            print(f"  allow {r['host']:<28} reachable={r['reachable']}")
    return ops.exit_code_for_status(result["status"])


if __name__ == "__main__":
    sys.exit(main())
