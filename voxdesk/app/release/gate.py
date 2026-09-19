"""Launch-gate evaluation and rendering (Step 11).

Pure, deterministic functions. ``evaluate`` folds a checklist, an evidence
registry, the recorded release artifact and the observed live facts into a
``LaunchReport``; ``render_report`` turns that report into the operator-facing
text; ``report_to_dict`` is the machine-readable twin. Provider and manual-E2E
results are converted into evidence through the two ingestion helpers so the
CLI (and only the CLI) decides what external files exist.

The decision rules are the launch policy, stated once and only once:

* any P0/P1 item that is FAIL, BLOCKED or NOT_RUN blocks;
* if any of those blocking items is BLOCKED, the final result is BLOCKED;
* otherwise, with any P0/P1 blocker, NOT_READY;
* an artifact that was never frozen (no recorded git commit) is NOT_READY;
* a frozen artifact that drifted from the live tree is NOT_READY;
* only when none of the above hold is the result READY.

NOT_RUN never becomes PASS; a WAIVED P0/P1 item is an invalid waiver and is
treated as NOT_RUN (still blocking), never as satisfied.
"""
from __future__ import annotations

from datetime import date

from app.release.models import (
    ArtifactRecord,
    BLOCKING_STATUSES,
    ChecklistItem,
    Evidence,
    FinalDecision,
    LaunchReport,
    LiveFacts,
    Row,
    Severity,
    Status,
)

#: Provider display name (from the Step 4 registry) -> checklist item id.
PROVIDER_ITEM_SLUGS = {
    "Twilio": "realprov-twilio",
    "Deepgram": "realprov-deepgram",
    "ElevenLabs": "realprov-elevenlabs",
    "OpenAI": "realprov-openai",
    "Anthropic": "realprov-anthropic",
    "Google LLM": "realprov-google",
    "Google Calendar": "realprov-google-calendar",
    "Microsoft Calendar": "realprov-microsoft",
    "Cal.com": "realprov-calcom",
    "HubSpot": "realprov-hubspot",
    "GoHighLevel": "realprov-ghl",
    "Jobber": "realprov-jobber",
    "Stripe": "realprov-stripe",
}

#: The aggregate REAL PROVIDER HEALTH item, satisfied only by a complete,
#: green read-only sweep of every provider above.
PROVIDER_HEALTH_ITEM = "realprov-health-001"


def _waiver_valid(item: ChecklistItem, ev: Evidence, today: date) -> tuple[bool, str]:
    """Validate a WAIVED item's waiver. Returns (valid, violation_or_empty)."""
    if item.severity in (Severity.P0, Severity.P1):
        return False, f"{item.id}: P0/P1 requirement may never be WAIVED"
    if ev.waiver is None:
        return False, f"{item.id}: WAIVED without a waiver record"
    if not ev.waiver.reason or not ev.waiver.approver:
        return False, f"{item.id}: waiver is missing reason or approver"
    if ev.waiver.expiry and ev.waiver.expiry < today.isoformat():
        return False, f"{item.id}: waiver expired {ev.waiver.expiry}"
    return True, ""


def effective_status(item: ChecklistItem, ev: Evidence | None, today: date) -> Status:
    """The status the gate acts on. Missing evidence is NOT_RUN; an invalid
    waiver degrades to NOT_RUN so it still blocks if P0/P1."""
    if ev is None:
        return Status.NOT_RUN
    if ev.status is Status.WAIVED:
        ok, _ = _waiver_valid(item, ev, today)
        return Status.WAIVED if ok else Status.NOT_RUN
    return ev.status


def _drift(artifact: ArtifactRecord, live: LiveFacts) -> tuple[list[str], bool]:
    """Compare the frozen artifact to live facts. Returns (drift_lines, matches)."""
    if not artifact.frozen:
        return [], False
    lines: list[str] = []
    if artifact.git_commit != live.git_commit:
        lines.append(
            f"git commit: recorded {artifact.git_commit} != live {live.git_commit}"
        )
    if artifact.dependency_fingerprint != live.dependency_fingerprint:
        lines.append("dependency fingerprint mismatch")
    if tuple(artifact.migration_heads) != tuple(live.migration_heads):
        lines.append(
            "migration heads: recorded "
            f"{list(artifact.migration_heads)} != live {list(live.migration_heads)}"
        )
    if artifact.configuration_fingerprint != live.configuration_fingerprint:
        lines.append("configuration fingerprint mismatch")
    return lines, not lines


def evaluate(
    checklist: list[ChecklistItem],
    evidence: dict[str, Evidence],
    artifact: ArtifactRecord,
    live: LiveFacts,
    today: date | None = None,
) -> LaunchReport:
    """Evaluate the gate. Deterministic for a fixed (checklist, evidence,
    artifact, live, today)."""
    today = today or date.today()
    rows: list[Row] = []
    violations: list[str] = []
    p0b = p1b = p2o = p3o = 0
    any_blocked = False

    for item in checklist:
        ev = evidence.get(item.id)
        status = effective_status(item, ev, today)

        if ev is not None and ev.status is Status.WAIVED:
            ok, msg = _waiver_valid(item, ev, today)
            if not ok:
                violations.append(msg)

        blocker = ""
        if item.severity in (Severity.P0, Severity.P1) and status in BLOCKING_STATUSES:
            blocker = status.value

        rows.append(
            Row(
                item=item,
                status=status,
                evidence=ev.evidence if ev else "",
                verifier=ev.verifier if ev else "",
                verification_date=ev.verification_date if ev else "",
                notes=ev.notes if ev else "",
                waiver=ev.waiver if ev else None,
                blocker=blocker,
            )
        )

        if item.severity is Severity.P0:
            if status in BLOCKING_STATUSES:
                p0b += 1
                any_blocked = any_blocked or status is Status.BLOCKED
        elif item.severity is Severity.P1:
            if status in BLOCKING_STATUSES:
                p1b += 1
                any_blocked = any_blocked or status is Status.BLOCKED
        elif item.severity is Severity.P2:
            if status in BLOCKING_STATUSES:
                p2o += 1
        elif item.severity is Severity.P3:
            if status in BLOCKING_STATUSES:
                p3o += 1

    drift_lines, matches = _drift(artifact, live)

    report = LaunchReport(
        rows=rows,
        violations=violations,
        drift=drift_lines,
        artifact_frozen=artifact.frozen,
        p0_blockers=p0b,
        p1_blockers=p1b,
        p2_open=p2o,
        p3_open=p3o,
        decision=FinalDecision.NOT_READY,
    )

    if p0b or p1b:
        report.decision = FinalDecision.BLOCKED if any_blocked else FinalDecision.NOT_READY
    elif not artifact.frozen:
        report.decision = FinalDecision.NOT_READY
    elif not matches:
        report.decision = FinalDecision.NOT_READY
    else:
        report.decision = FinalDecision.READY
    return report


def render_report(report: LaunchReport) -> str:
    """The operator-facing text output (the columns required by Step 11 D)."""
    lines: list[str] = []
    lines.append("VoxDesk launch gate")
    lines.append("=" * 160)
    lines.append(
        f"{'CATEGORY':<22} {'REQUIREMENT':<40} {'STATUS':<9} {'SEVERITY':<9} "
        f"{'EVIDENCE':<48} BLOCKER"
    )
    lines.append("-" * 160)
    for row in report.rows:
        detail = row.evidence.replace("\n", " ").strip() or "-"
        lines.append(
            f"{row.item.category:<22} {row.item.requirement[:40]:<40} "
            f"{row.status.value:<9} {row.item.severity.value:<9} "
            f"{detail[:48]:<48} {row.blocker:<16}"
        )
    lines.append("")
    lines.append("Evidence detail (item -> evidence, verifier @ date)")
    lines.append("-" * 160)
    for row in report.rows:
        if row.status is Status.PASS or row.status is Status.FAIL:
            who = row.verifier or "-"
            when = row.verification_date or "-"
            detail = row.evidence.replace("\n", " ").strip() or "-"
            lines.append(f"  {row.item.id}: {detail}  [{who} @ {when}]")
    lines.append("")
    lines.append(f"P0 BLOCKERS: {report.p0_blockers}")
    lines.append(f"P1 BLOCKERS: {report.p1_blockers}")
    lines.append(f"P2 OPEN: {report.p2_open}")
    lines.append(f"P3 OPEN: {report.p3_open}")
    lines.append(f"ARTIFACT FROZEN: {'yes' if report.artifact_frozen else 'no'}")
    for line in report.drift:
        lines.append(f"DRIFT: {line}")
    for line in report.violations:
        lines.append(f"VIOLATION: {line}")
    lines.append("")
    lines.append(f"FINAL RESULT: {report.decision.value}")
    return "\n".join(lines)


def report_to_dict(report: LaunchReport) -> dict:
    """Machine-readable report for ``--json``."""
    return {
        "decision": report.decision.value,
        "artifact_frozen": report.artifact_frozen,
        "p0_blockers": report.p0_blockers,
        "p1_blockers": report.p1_blockers,
        "p2_open": report.p2_open,
        "p3_open": report.p3_open,
        "drift": list(report.drift),
        "violations": list(report.violations),
        "items": [
            {
                "id": r.item.id,
                "category": r.item.category,
                "requirement": r.item.requirement,
                "status": r.status.value,
                "severity": r.item.severity.value,
                "classification": r.item.classification.value,
                "evidence": r.evidence,
                "verifier": r.verifier,
                "verification_date": r.verification_date,
                "notes": r.notes,
                "blocker": r.blocker,
            }
            for r in report.rows
        ],
    }


def evidence_for_provider_results(
    results: list[dict], verification_date: str, source: str
) -> dict[str, Evidence]:
    """Convert a Step 4 provider-check JSON list into evidence entries.

    ``results`` is the shape produced by ``validate-providers --json``: a list
    of ``{provider, status, reason, latency_ms, safety}``. Statuses map:
    PASS->PASS, FAIL->FAIL, BLOCKED->BLOCKED, anything else (SKIPPED)->NOT_RUN.
    Unknown provider names are ignored.

    Also computes the aggregate REAL PROVIDER HEALTH item: it is PASS only
    when every one of the 13 registered providers reported PASS; FAIL when any
    reported FAIL; BLOCKED when any reported BLOCKED (and none FAIL); and
    NOT_RUN when the sweep is incomplete. A partial or skipped sweep can never
    be READY.
    """
    out: dict[str, Evidence] = {}
    per_provider: dict[str, Status] = {}
    for entry in results:
        slug = PROVIDER_ITEM_SLUGS.get(entry.get("provider", ""))
        if slug is None:
            continue
        raw = entry.get("status", "SKIPPED")
        status = {
            "PASS": Status.PASS,
            "FAIL": Status.FAIL,
            "BLOCKED": Status.BLOCKED,
        }.get(raw, Status.NOT_RUN)
        per_provider[slug] = status
        out[slug] = Evidence(
            item_id=slug,
            status=status,
            evidence=(entry.get("reason") or "").strip(),
            verification_date=verification_date,
            verifier=source,
            notes="read-only provider check (opt-in)",
        )

    expected = set(PROVIDER_ITEM_SLUGS.values())
    missing = expected - set(per_provider)
    statuses = list(per_provider.values())
    if missing:
        health = Status.NOT_RUN
        health_evidence = f"sweep incomplete — {len(missing)} provider(s) missing"
    elif Status.FAIL in statuses:
        health = Status.FAIL
        health_evidence = "one or more providers FAILED"
    elif Status.BLOCKED in statuses:
        health = Status.BLOCKED
        health_evidence = "one or more providers BLOCKED"
    elif Status.NOT_RUN in statuses:
        health = Status.NOT_RUN
        health_evidence = "sweep not complete (skipped providers)"
    else:
        health = Status.PASS
        health_evidence = f"all {len(expected)} providers PASS"
    out[PROVIDER_HEALTH_ITEM] = Evidence(
        item_id=PROVIDER_HEALTH_ITEM,
        status=health,
        evidence=health_evidence,
        verification_date=verification_date,
        verifier=source,
        notes="aggregate real-provider health (opt-in, read-only)",
    )
    return out


E2E_REQUIRED_FIELDS = (
    "test_date",
    "test_tenant_id",
    "call_id",
    "twilio_call_sid",
    "operator",
)


def evidence_for_e2e_result(data: dict | None, source: str) -> Evidence:
    """Convert a manual live-call E2E result file into evidence for e2e-001.

    A PASS requires ``result == "PASS"`` AND every required field present; a
    PASS with missing fields is downgraded to FAIL (a pass without evidence is
    not a pass). Any other value of ``result`` yields NOT_RUN (or FAIL when
    the operator recorded FAIL).
    """
    data = data or {}
    result = data.get("result", "")
    missing = [f for f in E2E_REQUIRED_FIELDS if not data.get(f)]
    if result == "PASS" and not missing:
        return Evidence(
            item_id="e2e-001",
            status=Status.PASS,
            evidence=(
                f"manual live-call E2E PASS — call {data.get('call_id')}, "
                f"SID {data.get('twilio_call_sid')}, tenant {data.get('test_tenant_id')}"
            ),
            verification_date=data.get("test_date", ""),
            verifier=data.get("operator", ""),
            notes="human-executed telephony E2E",
        )
    if result == "FAIL":
        return Evidence(
            item_id="e2e-001",
            status=Status.FAIL,
            evidence="; ".join(data.get("failures") or []) or "manual E2E reported FAIL",
            verification_date=data.get("test_date", ""),
            verifier=data.get("operator", ""),
            notes="human-executed telephony E2E",
        )
    if result == "PASS" and missing:
        return Evidence(
            item_id="e2e-001",
            status=Status.FAIL,
            evidence=f"PASS reported but required fields missing: {', '.join(missing)}",
            verification_date=data.get("test_date", ""),
            verifier=data.get("operator", ""),
            notes="incomplete E2E result",
        )
    return Evidence(
        item_id="e2e-001",
        status=Status.NOT_RUN,
        evidence="",
        verification_date="",
        verifier="",
        notes="no manual E2E result recorded",
    )
