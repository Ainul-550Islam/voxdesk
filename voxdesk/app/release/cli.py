"""Launch gate command-line interface (Step 11).

``python scripts/release_gate.py`` prints, for every checklist item,
CATEGORY / REQUIREMENT / STATUS / SEVERITY / EVIDENCE / BLOCKER, then the
summary lines P0 BLOCKERS / P1 BLOCKERS / P2 OPEN / P3 OPEN / FINAL RESULT.

Conservatism is the default and is enforced structurally:

* the checklist has no evidence: the registry starts every item at NOT_RUN;
* registry entries are read-only JSON on disk — no flag can flip NOT_RUN to
  PASS; the only ways to satisfy an item are a genuine evidence record or,
  for real-provider and manual-E2E items, a real result file on disk;
* real-provider results are ingested only from a file the operator explicitly
  names (or the default Step 4 output path), and only if the opt-in switch
  ``VOXDESK_REAL_INTEGRATION`` is set — the gate itself performs no network
  calls and no phone calls, ever;
* the manual E2E item is NEVER set from provider results; it is satisfied only
  by a human-authored result file that records a PASS with all required
  fields.

Optionally the CLI freezes the artifact (``--freeze``) or checks drift against
an existing freeze (``--artifact``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

try:
    from app.core.config import Settings as _Settings

    _SETTINGS_MODEL = _Settings
except Exception:  # pragma: no cover - configuration import failure surfaces later
    _SETTINGS_MODEL = None

from app.release import evidence as evidence_mod
from app.release import facts, gate
from app.release.checklist import load_checklist
from app.release.models import ArtifactRecord, Evidence, FinalDecision, LiveFacts

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CHECKLIST = REPO_ROOT / "scripts" / "release" / "checklist.json"
DEFAULT_EVIDENCE = REPO_ROOT / "scripts" / "release" / "evidence.json"
DEFAULT_ARTIFACT = REPO_ROOT / "var" / "release" / "artifact.json"
DEFAULT_PROVIDER_RESULTS = REPO_ROOT / "var" / "release" / "provider-results.json"
DEFAULT_E2E_RESULT = REPO_ROOT / "var" / "release" / "e2e-result.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_gate.py",
        description="VoxDesk production launch gate (Step 11).",
    )
    parser.add_argument("--checklist", type=Path, default=DEFAULT_CHECKLIST)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--provider-results", type=Path, default=None)
    parser.add_argument("--e2e-result", type=Path, default=DEFAULT_E2E_RESULT)
    parser.add_argument(
        "--freeze",
        action="store_true",
        help="write the certified artifact record (commit, fingerprints) to --artifact",
    )
    parser.add_argument(
        "--environment-class", default="production", help="env class recorded in the freeze"
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument(
        "--release-identifier", default="", help="release tag recorded in the freeze"
    )
    return parser


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if isinstance(data, list):
        data = {"items": data}
    return data


def _real_integration_enabled() -> bool:
    return os.environ.get("VOXDESK_REAL_INTEGRATION", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _read_artifact(path: Path) -> ArtifactRecord:
    data = _load_json(path) or {}
    return ArtifactRecord(
        release_identifier=data.get("release_identifier", ""),
        git_commit=data.get("git_commit", ""),
        dependency_fingerprint=data.get("dependency_fingerprint", ""),
        migration_heads=tuple(data.get("migration_heads", []) or []),
        configuration_fingerprint=data.get("configuration_fingerprint", ""),
        environment_class=data.get("environment_class", ""),
        build_date=data.get("build_date", ""),
        image_digest=data.get("image_digest", ""),
    )


def _compute_live_facts() -> LiveFacts:
    requirements = REPO_ROOT / "requirements.txt"
    requirements_text = (
        requirements.read_text(encoding="utf-8") if requirements.exists() else ""
    )
    return LiveFacts(
        git_commit=facts.git_commit(REPO_ROOT),
        dependency_fingerprint=facts.dependency_fingerprint(requirements_text),
        migration_heads=facts.migration_heads(REPO_ROOT / "alembic" / "versions"),
        configuration_fingerprint=(
            facts.configuration_fingerprint(_SETTINGS_MODEL)
            if _SETTINGS_MODEL is not None
            else ""
        ),
    )


def _freeze(live: LiveFacts, args: argparse.Namespace) -> ArtifactRecord:
    record = ArtifactRecord(
        release_identifier=args.release_identifier,
        git_commit=live.git_commit,
        dependency_fingerprint=live.dependency_fingerprint,
        migration_heads=live.migration_heads,
        configuration_fingerprint=live.configuration_fingerprint,
        environment_class=args.environment_class,
        build_date=date.today().isoformat(),
    )
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_text(
        json.dumps(
            {
                "release_identifier": record.release_identifier,
                "git_commit": record.git_commit,
                "dependency_fingerprint": record.dependency_fingerprint,
                "migration_heads": list(record.migration_heads),
                "configuration_fingerprint": record.configuration_fingerprint,
                "environment_class": record.environment_class,
                "build_date": record.build_date,
                "image_digest": record.image_digest,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return record


def _ingest_providers(args: argparse.Namespace, base: dict[str, Evidence]) -> dict[str, Evidence]:
    """Merge real-provider results (opt-in, read-only) into the evidence map."""
    path = args.provider_results or DEFAULT_PROVIDER_RESULTS
    if not path.exists() or not _real_integration_enabled():
        return base
    data = _load_json(path)
    if not data:
        return base
    raw = data.get("items") if isinstance(data.get("items"), list) else data.get("results")
    if not isinstance(raw, list):
        return base
    try:
        provider_date = data.get("generated_at", "")[:10] or date.today().isoformat()
    except AttributeError:
        provider_date = date.today().isoformat()
    merged = dict(base)
    merged.update(
        gate.evidence_for_provider_results(raw, provider_date, "validate-providers (real)")
    )
    return merged


def _ingest_e2e(args: argparse.Namespace, base: dict[str, Evidence]) -> dict[str, Evidence]:
    """Merge the human-authored manual E2E result into the evidence map.

    The file must actually exist; otherwise e2e-001 stays NOT_RUN. This is the
    only way e2e-001 can become PASS, and it requires a human-triggered call
    with all required evidence fields present.
    """
    if not args.e2e_result.exists():
        return base
    data = _load_json(args.e2e_result)
    merged = dict(base)
    merged["e2e-001"] = gate.evidence_for_e2e_result(data, "manual E2E record")
    return merged


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    checklist = load_checklist(args.checklist)

    live = _compute_live_facts()

    artifact = _freeze(live, args) if args.freeze else _read_artifact(args.artifact)

    base = evidence_mod.default_evidence(checklist)
    if args.evidence.exists():
        base.update(evidence_mod.load_evidence(args.evidence))
    evidence = _ingest_providers(args, base)
    evidence = _ingest_e2e(args, evidence)

    report = gate.evaluate(checklist, evidence, artifact, live)

    if args.json:
        print(json.dumps(gate.report_to_dict(report), indent=2))
    else:
        print(gate.render_report(report))

    if report.decision is FinalDecision.READY:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
