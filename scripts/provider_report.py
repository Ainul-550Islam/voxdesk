#!/usr/bin/env python3
"""VoxDesk unified provider readiness report (Step 13 D/J, extended Step 16).

Reuses the Step 4 real-provider validation framework exactly — every check is
READ_ONLY and no provider API is reimplemented here. Without the opt-in switch
every provider reports SKIPPED (and SKIPPED is never converted to PASS). With
``VOXDESK_REAL_INTEGRATION=1`` and operator-supplied staging credentials, each
provider reports its true outcome:

    PASS     — the real read-only check succeeded.
    SKIPPED  — opt-in off or credential absent (never PASS).
    FAIL     — the check ran and failed (bad key, provider error, …).
    BLOCKED  — the execution environment prevented the check (e.g. an SDK that
               cannot run here). Distinct from SKIPPED.
    NOT_RUN  — no check was attempted (empty registry). Never PASS.

Stripe, calendar and CRM checks are read-only by construction: the framework
never creates a charge, subscription, invoice, customer, booking or CRM row.

Secret discipline: every reason/evidence string is re-scrubbed through a
``SecretMasker`` built from the same credential set as the Step 4 CLI, so a
token can never reach stdout or the evidence registry even if a check
interpolated one.

Usage:
    python scripts/provider_report.py [--json] [--write-evidence]

Exit codes: 0 = every check PASS or SKIPPED; 1 = any FAIL / BLOCKED / NOT_RUN.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations.validation import registry  # noqa: E402
from app.integrations.validation.cli import _collect_secrets  # noqa: E402
from app.integrations.validation.status import (  # noqa: E402
    SecretMasker,
    real_integration_enabled,
)
from app.release import gate, ops  # noqa: E402

EVIDENCE_PATH = Path(ops.REPO_ROOT) / "scripts" / "release" / "evidence.json"

#: Fixed, secret-free note recorded as each provider's evidence line. The
#: read-only guarantee comes from the Step 4 registry, not from this string.
READ_ONLY_NOTE = (
    "read-only check — no charge/subscription/invoice/customer/booking/CRM mutation"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provider_report.py", description="VoxDesk provider readiness report"
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write-evidence", action="store_true",
                        help="merge provider results into the evidence registry "
                             "(only meaningful with VOXDESK_REAL_INTEGRATION=1)")
    return parser


def _enriched_rows(masker: SecretMasker) -> tuple[list[dict], str]:
    """Run the Step 4 registry and annotate each outcome with the required
    provider/status/reason/evidence/timestamp/release_commit/environment."""
    outcomes = asyncio.run(registry.run_all())
    now = ops.now_iso()
    commit = ops.facts.git_commit(ops.REPO_ROOT)
    environment = (os.environ.get("APP_ENV") or "development").strip()
    rows = []
    for o in outcomes:
        d = o.as_dict()
        rows.append(
            {
                "provider": d.get("provider", ""),
                "status": d.get("status", ops.STATUS_SKIPPED),
                "reason": masker.mask(d.get("reason") or ""),
                "evidence": masker.mask(READ_ONLY_NOTE),
                "safety": d.get("safety", "read_only"),
                "timestamp": now,
                "release_commit": commit,
                "environment": environment,
            }
        )
    overall = ops.build_provider_report(
        [
            {
                "provider": r["provider"],
                "status": r["status"],
                "reason": r["reason"],
                "safety": r["safety"],
            }
            for r in rows
        ]
    )["status"]
    return rows, overall


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    masker = SecretMasker(_collect_secrets())
    rows, overall = _enriched_rows(masker)
    commit = ops.facts.git_commit(ops.REPO_ROOT)

    evidence_written = 0
    if args.write_evidence and real_integration_enabled():
        ingested = gate.evidence_for_provider_results(
            [
                {
                    "provider": r["provider"],
                    "status": r["status"],
                    "reason": r["reason"],
                    "safety": r["safety"],
                }
                for r in rows
            ],
            ops.today_iso(),
            "provider_report.py (real)",
        )
        records = [
            ops.EvidenceRecord(
                item_id=ev.item_id,
                status=ev.status.value,
                classification="EXTERNAL",
                severity="P1",
                command="scripts/provider_report.py --write-evidence",
                timestamp=(rows[0]["timestamp"] if rows else ops.now_iso()),
                release_commit=(rows[0]["release_commit"] if rows else commit),
                environment=(rows[0]["environment"] if rows else "development"),
                evidence=masker.mask(ev.evidence),
            )
            for ev in ingested.values()
        ]
        ops.merge_evidence_file(EVIDENCE_PATH, records)
        evidence_written = len(records)

    report = {
        "status": overall,
        "opt_in": real_integration_enabled(),
        "rows": rows,
        "evidence": (
            "read-only provider sweep; SKIPPED means no credentials/opt-in, never PASS"
        ),
        "evidence_written": evidence_written,
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        first = rows[0] if rows else {}
        print("Provider readiness report")
        print(
            f"opt-in={report['opt_in']}  "
            f"environment={first.get('environment', 'unknown')}  "
            f"release_commit={str(first.get('release_commit', ''))[:12] or '-'}  "
            f"timestamp={first.get('timestamp', '-')}"
        )
        print("-" * 88)
        print(f"{'Provider':<20} {'Status':<9} Reason")
        for r in rows:
            print(f"{r['provider']:<20} {r['status']:<9} {r['reason']}")
        print("-" * 88)
        print("Details (status / evidence / timestamp / release_commit / environment):")
        for r in rows:
            print(f"  - {r['provider']}: status={r['status']} safety={r['safety']}")
            print(f"      evidence={r['evidence']}")
            print(f"      timestamp={r['timestamp']} "
                  f"release_commit={r['release_commit']} environment={r['environment']}")
        print("-" * 88)
        print(f"OVERALL: {report['status']}")
        print(f"  {report['evidence']}")
        if evidence_written:
            print(f"  evidence merged for {evidence_written} provider item(s)")

    if overall in (ops.STATUS_FAIL, ops.STATUS_BLOCKED, ops.STATUS_NOT_RUN):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
