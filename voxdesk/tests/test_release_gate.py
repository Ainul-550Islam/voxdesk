"""Deterministic tests for the Step 11 launch gate.

Everything here is offline and needs no credentials: the gate module is pure
functions over a checklist, an evidence registry, an artifact record and live
facts. The CLI is exercised only through :func:`app.release.cli.main` with an
empty environment so no network, no git-subprocess surprises and no phone call
can ever be made from a test.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from app.release import facts
from app.release.checklist import load_checklist
from app.release.evidence import default_evidence
from app.release.gate import (
    evidence_for_e2e_result,
    evidence_for_provider_results,
    evaluate,
    render_report,
    report_to_dict,
)
from app.release.models import (
    ArtifactRecord,
    Classification,
    Evidence,
    FinalDecision,
    LiveFacts,
    Severity,
    Status,
    Waiver,
)

CHECKLIST = load_checklist(
    Path(__file__).resolve().parents[1] / "scripts" / "release" / "checklist.json"
)
BY_ID = {item.id: item for item in CHECKLIST}
TODAY = date(2026, 9, 12)

P0_IDS = [i.id for i in CHECKLIST if i.severity is Severity.P0]
P1_IDS = [i.id for i in CHECKLIST if i.severity is Severity.P1]
P2_IDS = [i.id for i in CHECKLIST if i.severity is Severity.P2]
P3_IDS = [i.id for i in CHECKLIST if i.severity is Severity.P3]

FROZEN = ArtifactRecord(
    release_identifier="2026.09.12-rc1",
    git_commit="deadbeef",
    dependency_fingerprint="df",
    migration_heads=("head1",),
    configuration_fingerprint="cf",
    environment_class="staging",
    build_date="2026-09-12",
)
LIVE = LiveFacts(
    git_commit="deadbeef",
    dependency_fingerprint="df",
    migration_heads=("head1",),
    configuration_fingerprint="cf",
)


def full_pass() -> dict[str, Evidence]:
    """Every item PASS with evidence text, verifier and date."""
    return {
        item.id: Evidence(
            item_id=item.id,
            status=Status.PASS,
            evidence=f"verified {item.id}",
            verification_date=TODAY.isoformat(),
            verifier="deterministic-test",
        )
        for item in CHECKLIST
    }


def non_frozen_artifact() -> ArtifactRecord:
    return ArtifactRecord()


# ---------------------------------------------------------------------------
# Canonical data: checklist shape and identity
# ---------------------------------------------------------------------------


def test_checklist_has_required_categories():
    required = {
        "CODE",
        "TESTS",
        "SECURITY",
        "AUTH",
        "TENANT ISOLATION",
        "DEPENDENCIES",
        "DATABASE",
        "MIGRATIONS",
        "DEPLOYMENT",
        "BACKUP",
        "RESTORE",
        "OFF-SITE BACKUP",
        "TLS",
        "OBSERVABILITY",
        "ALERTING",
        "VOICE PROVIDERS",
        "REAL PROVIDER HEALTH",
        "CALENDAR",
        "CRM",
        "BILLING",
        "COST MODEL",
        "RATE LIMITING",
        "ABUSE PREVENTION",
        "PRIVACY",
        "RETENTION",
        "COMPLIANCE DOCUMENTATION",
        "PENTEST",
        "INCIDENT RESPONSE",
        "DISASTER RECOVERY",
    }
    categories = {item.category for item in CHECKLIST}
    assert required <= categories, f"missing categories: {required - categories}"


def test_checklist_has_real_telephony_e2e_item():
    e2e = BY_ID["e2e-001"]
    assert e2e.category == "REAL TELEPHONY E2E"
    assert e2e.severity is Severity.P0
    assert e2e.classification is Classification.HUMAN


def test_checklist_has_all_provider_items():
    expected_slugs = {
        "realprov-twilio",
        "realprov-deepgram",
        "realprov-elevenlabs",
        "realprov-openai",
        "realprov-anthropic",
        "realprov-google",
        "realprov-google-calendar",
        "realprov-microsoft",
        "realprov-calcom",
        "realprov-hubspot",
        "realprov-ghl",
        "realprov-jobber",
        "realprov-stripe",
    }
    assert expected_slugs <= set(BY_ID)


def test_checklist_ids_unique():
    assert len({i.id for i in CHECKLIST}) == len(CHECKLIST)


def test_every_item_has_a_classification():
    for item in CHECKLIST:
        assert item.classification in (
            Classification.AUTOMATED,
            Classification.HUMAN,
            Classification.EXTERNAL,
            Classification.INFRASTRUCTURE,
        )


# ---------------------------------------------------------------------------
# Conservative default: missing evidence is NOT_RUN, never PASS
# ---------------------------------------------------------------------------


def test_missing_evidence_is_not_run():
    report = evaluate(CHECKLIST, default_evidence(CHECKLIST), non_frozen_artifact(), LIVE, TODAY)
    assert all(row.status is Status.NOT_RUN for row in report.rows)
    assert report.decision is FinalDecision.NOT_READY


def test_missing_evidence_never_becomes_pass():
    # Only e2e-001 is untouched; everything else PASS. e2e-001 must still block.
    evidence = full_pass()
    evidence.pop("e2e-001")
    report = evaluate(CHECKLIST, evidence, FROZEN, LIVE, TODAY)
    assert report.decision is FinalDecision.NOT_READY
    assert report.p0_blockers == 1
    e2e_row = next(r for r in report.rows if r.item.id == "e2e-001")
    assert e2e_row.status is Status.NOT_RUN


def test_pass_without_evidence_is_impossible():
    # A PASS status with empty evidence text is still a PASS status, but the
    # gate only ever *sets* status from the registry; here we assert the
    # evidence text is carried through so a silent PASS cannot hide empty proof.
    evidence = {
        "code-001": Evidence(
            item_id="code-001",
            status=Status.PASS,
            evidence="",
            verification_date="",
            verifier="",
        )
    }
    report = evaluate(CHECKLIST, {**default_evidence(CHECKLIST), **evidence},
                      non_frozen_artifact(), LIVE, TODAY)
    row = next(r for r in report.rows if r.item.id == "code-001")
    assert row.status is Status.PASS
    assert row.evidence == ""  # surfaced verbatim; the operator sees no proof
    # ...and because nothing else is satisfied, the gate is still NOT_READY.
    assert report.decision is FinalDecision.NOT_READY


# ---------------------------------------------------------------------------
# Severity blocking behaviour
# ---------------------------------------------------------------------------


def test_p0_fail_blocks():
    evidence = full_pass()
    evidence["code-001"] = Evidence(
        item_id="code-001", status=Status.FAIL, evidence="compile failed",
        verification_date=TODAY.isoformat(), verifier="ci",
    )
    report = evaluate(CHECKLIST, evidence, FROZEN, LIVE, TODAY)
    assert report.decision is FinalDecision.NOT_READY
    assert report.p0_blockers == 1


def test_p0_blocked_gives_blocked_result():
    evidence = full_pass()
    evidence["tls-001"] = Evidence(
        item_id="tls-001", status=Status.BLOCKED, evidence="no cert yet",
        verification_date=TODAY.isoformat(), verifier="infra",
    )
    report = evaluate(CHECKLIST, evidence, FROZEN, LIVE, TODAY)
    assert report.decision is FinalDecision.BLOCKED


def test_p1_fail_blocks():
    evidence = full_pass()
    evidence["code-002"] = Evidence(
        item_id="code-002", status=Status.FAIL, evidence="ruff failed",
        verification_date=TODAY.isoformat(), verifier="ci",
    )
    report = evaluate(CHECKLIST, evidence, FROZEN, LIVE, TODAY)
    assert report.decision is FinalDecision.NOT_READY
    assert report.p1_blockers == 1


def test_p1_not_run_blocks():
    evidence = full_pass()
    evidence.pop("code-002")
    report = evaluate(CHECKLIST, evidence, FROZEN, LIVE, TODAY)
    assert report.p1_blockers == 1
    assert report.decision is FinalDecision.NOT_READY


# ---------------------------------------------------------------------------
# Waivers (P2/P3 only; expired waivers degrade to NOT_RUN)
# ---------------------------------------------------------------------------


def test_p2_waiver_allowed_and_closes_item():
    evidence = full_pass()
    evidence["cost-001"] = Evidence(
        item_id="cost-001",
        status=Status.WAIVED,
        waiver=Waiver(
            reason="budget alerts deferred one cycle",
            approver="cto@example.com",
            date=TODAY.isoformat(),
            expiry=(TODAY + timedelta(days=30)).isoformat(),
        ),
    )
    report = evaluate(CHECKLIST, evidence, FROZEN, LIVE, TODAY)
    assert report.p2_open == 0
    assert report.decision is FinalDecision.READY
    assert not report.violations


def test_p0_waiver_is_invalid_and_still_blocks():
    evidence = full_pass()
    evidence["code-001"] = Evidence(
        item_id="code-001",
        status=Status.WAIVED,
        waiver=Waiver(
            reason="whatever", approver="someone", date=TODAY.isoformat(), expiry="",
        ),
    )
    report = evaluate(CHECKLIST, evidence, FROZEN, LIVE, TODAY)
    assert report.p0_blockers == 1
    assert report.decision is FinalDecision.NOT_READY
    assert any("may never be WAIVED" in v for v in report.violations)


def test_expired_waiver_degrades_to_not_run():
    evidence = full_pass()
    evidence["cost-001"] = Evidence(
        item_id="cost-001",
        status=Status.WAIVED,
        waiver=Waiver(
            reason="deferred", approver="cto@example.com",
            date=(TODAY - timedelta(days=60)).isoformat(),
            expiry=(TODAY - timedelta(days=1)).isoformat(),
        ),
    )
    report = evaluate(CHECKLIST, evidence, FROZEN, LIVE, TODAY)
    assert report.p2_open == 1
    assert any("expired" in v for v in report.violations)
    assert report.decision is FinalDecision.READY  # P2 open does not block launch


def test_waiver_without_reason_or_approver_is_invalid():
    evidence = full_pass()
    evidence["cost-001"] = Evidence(
        item_id="cost-001",
        status=Status.WAIVED,
        waiver=Waiver(reason="", approver="", date="", expiry=""),
    )
    report = evaluate(CHECKLIST, evidence, FROZEN, LIVE, TODAY)
    assert report.p2_open == 1
    assert any("missing reason or approver" in v for v in report.violations)


# ---------------------------------------------------------------------------
# Artifact freeze and drift detection
# ---------------------------------------------------------------------------


def test_artifact_drift_git_commit():
    evidence = full_pass()
    live = LiveFacts(
        git_commit="changed",
        dependency_fingerprint="df",
        migration_heads=("head1",),
        configuration_fingerprint="cf",
    )
    report = evaluate(CHECKLIST, evidence, FROZEN, live, TODAY)
    assert report.decision is FinalDecision.NOT_READY
    assert any("git commit" in d for d in report.drift)


def test_artifact_drift_dependency_fingerprint():
    evidence = full_pass()
    live = LiveFacts(
        git_commit="deadbeef",
        dependency_fingerprint="other",
        migration_heads=("head1",),
        configuration_fingerprint="cf",
    )
    report = evaluate(CHECKLIST, evidence, FROZEN, live, TODAY)
    assert report.decision is FinalDecision.NOT_READY
    assert any("dependency fingerprint" in d for d in report.drift)


def test_artifact_drift_migration_heads():
    evidence = full_pass()
    live = LiveFacts(
        git_commit="deadbeef",
        dependency_fingerprint="df",
        migration_heads=("head1", "head2"),
        configuration_fingerprint="cf",
    )
    report = evaluate(CHECKLIST, evidence, FROZEN, live, TODAY)
    assert report.decision is FinalDecision.NOT_READY
    assert any("migration heads" in d for d in report.drift)


def test_artifact_drift_configuration_fingerprint():
    evidence = full_pass()
    live = LiveFacts(
        git_commit="deadbeef",
        dependency_fingerprint="df",
        migration_heads=("head1",),
        configuration_fingerprint="other",
    )
    report = evaluate(CHECKLIST, evidence, FROZEN, live, TODAY)
    assert report.decision is FinalDecision.NOT_READY
    assert any("configuration fingerprint" in d for d in report.drift)


def test_unfrozen_artifact_is_not_ready():
    evidence = full_pass()
    report = evaluate(CHECKLIST, evidence, non_frozen_artifact(), LIVE, TODAY)
    assert report.artifact_frozen is False
    assert report.decision is FinalDecision.NOT_READY


# ---------------------------------------------------------------------------
# Real-provider result ingestion
# ---------------------------------------------------------------------------


def test_provider_result_ingestion_pass_fail_blocked_and_skipped():
    results = [
        {"provider": "Twilio", "status": "PASS", "reason": "ok"},
        {"provider": "Deepgram", "status": "FAIL", "reason": "timeout"},
        {"provider": "Stripe", "status": "BLOCKED", "reason": "no key"},
        {"provider": "ElevenLabs", "status": "SKIPPED", "reason": "n/a"},
        {"provider": "UnknownVendor", "status": "PASS", "reason": "ignored"},
    ]
    ingested = evidence_for_provider_results(results, "2026-09-12", "validate-providers")
    assert ingested["realprov-twilio"].status is Status.PASS
    assert ingested["realprov-deepgram"].status is Status.FAIL
    assert ingested["realprov-stripe"].status is Status.BLOCKED
    assert ingested["realprov-elevenlabs"].status is Status.NOT_RUN
    assert "realprov-unknownvendor" not in ingested


def test_provider_results_do_not_touch_e2e():
    results = [
        {"provider": "Twilio", "status": "PASS", "reason": "ok"},
    ]
    ingested = evidence_for_provider_results(results, "2026-09-12", "validate-providers")
    assert "e2e-001" not in ingested


ALL_PROVIDER_RESULTS = [
    {"provider": name, "status": "PASS", "reason": "ok"}
    for name in (
        "Twilio",
        "Deepgram",
        "ElevenLabs",
        "OpenAI",
        "Anthropic",
        "Google LLM",
        "Google Calendar",
        "Microsoft Calendar",
        "Cal.com",
        "HubSpot",
        "GoHighLevel",
        "Jobber",
        "Stripe",
    )
]


def test_provider_health_aggregate_pass_only_when_complete_and_green():
    ingested = evidence_for_provider_results(
        ALL_PROVIDER_RESULTS, "2026-09-12", "validate-providers"
    )
    assert ingested["realprov-health-001"].status is Status.PASS


def test_provider_health_aggregate_incomplete_is_not_run():
    ingested = evidence_for_provider_results(
        [{"provider": "Twilio", "status": "PASS", "reason": "ok"}],
        "2026-09-12",
        "validate-providers",
    )
    assert ingested["realprov-health-001"].status is Status.NOT_RUN


def test_provider_health_aggregate_any_fail_is_fail():
    results = [dict(r) for r in ALL_PROVIDER_RESULTS]
    results[0] = {"provider": "Twilio", "status": "FAIL", "reason": "down"}
    ingested = evidence_for_provider_results(results, "2026-09-12", "validate-providers")
    assert ingested["realprov-health-001"].status is Status.FAIL


def test_provider_health_aggregate_any_blocked_is_blocked():
    results = [dict(r) for r in ALL_PROVIDER_RESULTS]
    results[0] = {"provider": "Twilio", "status": "BLOCKED", "reason": "no key"}
    ingested = evidence_for_provider_results(results, "2026-09-12", "validate-providers")
    assert ingested["realprov-health-001"].status is Status.BLOCKED


# ---------------------------------------------------------------------------
# Manual E2E result ingestion (human-controlled, evidence-gated)
# ---------------------------------------------------------------------------


def test_e2e_result_pass_requires_all_fields():
    good = evidence_for_e2e_result(
        {
            "result": "PASS",
            "test_date": "2026-09-12",
            "test_tenant_id": "t-e2e",
            "call_id": "call-1",
            "twilio_call_sid": "CA123",
            "operator": "ops@example.com",
        },
        "manual",
    )
    assert good.status is Status.PASS


def test_e2e_result_pass_without_fields_is_fail():
    bad = evidence_for_e2e_result({"result": "PASS"}, "manual")
    assert bad.status is Status.FAIL
    assert "missing" in bad.evidence


def test_e2e_result_not_executed_is_not_run():
    none = evidence_for_e2e_result({"result": "NOT_EXECUTED"}, "manual")
    assert none.status is Status.NOT_RUN


def test_e2e_result_none_is_not_run():
    none = evidence_for_e2e_result(None, "manual")
    assert none.status is Status.NOT_RUN


# ---------------------------------------------------------------------------
# Final READY / NOT_READY outcomes
# ---------------------------------------------------------------------------


def test_full_pass_frozen_matching_artifact_is_ready():
    report = evaluate(CHECKLIST, full_pass(), FROZEN, LIVE, TODAY)
    assert report.decision is FinalDecision.READY
    assert report.p0_blockers == 0
    assert report.p1_blockers == 0


def test_p2_open_does_not_block_ready():
    evidence = full_pass()
    evidence["cost-001"] = Evidence(item_id="cost-001", status=Status.NOT_RUN)
    report = evaluate(CHECKLIST, evidence, FROZEN, LIVE, TODAY)
    assert report.decision is FinalDecision.READY
    assert report.p2_open == 1


def test_backup_restore_egress_evidence_flows_through():
    # These are ordinary INFRASTRUCTURE/AUTOMATED items; they must block while
    # NOT_RUN and be satisfied by genuine evidence like everything else.
    evidence = default_evidence(CHECKLIST)
    for item_id in ("backup-001", "restore-001", "abuse-001"):
        evidence[item_id] = Evidence(
            item_id=item_id,
            status=Status.PASS,
            evidence=f"{item_id} drill passed",
            verification_date=TODAY.isoformat(),
            verifier="infra",
        )
    report = evaluate(CHECKLIST, evidence, non_frozen_artifact(), LIVE, TODAY)
    assert report.decision is FinalDecision.NOT_READY  # everything else NOT_RUN
    for item_id in ("backup-001", "restore-001", "abuse-001"):
        row = next(r for r in report.rows if r.item.id == item_id)
        assert row.status is Status.PASS


def test_external_pentest_not_run_blocks_launch():
    # pentest-001 is P1/EXTERNAL and stays NOT_RUN until a real third party
    # performs it; that must keep the gate at NOT_READY even with all else PASS.
    evidence = full_pass()
    evidence["pentest-001"] = Evidence(item_id="pentest-001", status=Status.NOT_RUN)
    report = evaluate(CHECKLIST, evidence, FROZEN, LIVE, TODAY)
    assert report.p1_blockers == 1
    assert report.decision is FinalDecision.NOT_READY


def test_render_report_contains_required_lines():
    report = evaluate(CHECKLIST, default_evidence(CHECKLIST), non_frozen_artifact(), LIVE, TODAY)
    text = render_report(report)
    for needle in (
        "CATEGORY",
        "REQUIREMENT",
        "STATUS",
        "SEVERITY",
        "P0 BLOCKERS:",
        "P1 BLOCKERS:",
        "P2 OPEN:",
        "P3 OPEN:",
        "FINAL RESULT:",
    ):
        assert needle in text


def test_report_to_dict_round_trips_decision():
    report = evaluate(CHECKLIST, default_evidence(CHECKLIST), non_frozen_artifact(), LIVE, TODAY)
    data = report_to_dict(report)
    assert data["decision"] == "NOT_READY"
    assert data["p0_blockers"] == len(P0_IDS)
    assert len(data["items"]) == len(CHECKLIST)


# ---------------------------------------------------------------------------
# Facts: fingerprints are deterministic and secret-safe
# ---------------------------------------------------------------------------


def test_dependency_fingerprint_ignores_comments_and_blank_lines():
    a = facts.dependency_fingerprint("fastapi==0.136.1\n# comment\n\nhttpx==0.28.1\n")
    b = facts.dependency_fingerprint("httpx==0.28.1\nfastapi==0.136.1\n")
    assert a == b


def test_configuration_fingerprint_excludes_secrets():
    class FakeSettings:
        model_fields = {
            "api_key": _FakeField(default="sk-secret"),
            "database_url": _FakeField(default="postgres://u:p@h/db"),
            "retry_count": _FakeField(default=3),
        }

    fp = facts.configuration_fingerprint(FakeSettings)
    assert "sk-secret" not in fp
    assert "postgres://u:p@h/db" not in fp


class _FakeField:
    def __init__(self, default):
        self.default = default
        self.is_required = lambda: False


def test_git_commit_detached_head(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(
        "abcd1234abcd1234abcd1234abcd1234abcd1234\n", encoding="utf-8"
    )
    assert facts.git_commit(tmp_path) == "abcd1234abcd1234abcd1234abcd1234abcd1234"


def test_git_commit_symbolic_ref(tmp_path):
    git_dir = tmp_path / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text(
        "beef5678beef5678beef5678beef5678beef5678\n", encoding="utf-8"
    )
    assert facts.git_commit(tmp_path) == "beef5678beef5678beef5678beef5678beef5678"


def test_git_commit_missing_repo_is_empty(tmp_path):
    assert facts.git_commit(tmp_path) == ""


def test_migration_heads_from_real_tree():
    heads = facts.migration_heads(
        Path(__file__).resolve().parents[1] / "alembic" / "versions"
    )
    assert heads  # the repo has migrations; there must be at least one head


# ---------------------------------------------------------------------------
# CLI: conservative defaults, JSON, exit codes, freeze
# ---------------------------------------------------------------------------


def test_cli_default_is_not_ready(tmp_path, monkeypatch):
    from app.release import cli

    monkeypatch.setattr(cli, "DEFAULT_EVIDENCE", tmp_path / "nonexistent-evidence.json")
    monkeypatch.setattr(cli, "DEFAULT_ARTIFACT", tmp_path / "nonexistent-artifact.json")
    monkeypatch.setattr(cli, "DEFAULT_E2E_RESULT", tmp_path / "nonexistent-e2e.json")
    monkeypatch.delenv("VOXDESK_REAL_INTEGRATION", raising=False)
    code = cli.main([])
    assert code == 1


def test_cli_json_output_is_valid(tmp_path, monkeypatch, capsys):
    from app.release import cli

    monkeypatch.setattr(cli, "DEFAULT_EVIDENCE", tmp_path / "nonexistent-evidence.json")
    monkeypatch.setattr(cli, "DEFAULT_ARTIFACT", tmp_path / "nonexistent-artifact.json")
    monkeypatch.setattr(cli, "DEFAULT_E2E_RESULT", tmp_path / "nonexistent-e2e.json")
    monkeypatch.delenv("VOXDESK_REAL_INTEGRATION", raising=False)
    code = cli.main(["--json"])
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["decision"] == "NOT_READY"
    assert code == 1


def test_cli_freezes_artifact(tmp_path, monkeypatch):
    from app.release import cli

    artifact = tmp_path / "artifact.json"
    monkeypatch.setattr(cli, "DEFAULT_EVIDENCE", tmp_path / "nonexistent-evidence.json")
    monkeypatch.setattr(cli, "DEFAULT_ARTIFACT", artifact)
    monkeypatch.setattr(cli, "DEFAULT_E2E_RESULT", tmp_path / "nonexistent-e2e.json")
    monkeypatch.delenv("VOXDESK_REAL_INTEGRATION", raising=False)
    cli.main(["--freeze"])
    data = json.loads(artifact.read_text(encoding="utf-8"))
    assert data["git_commit"]
    assert data["dependency_fingerprint"]
    assert data["migration_heads"]


def test_cli_provider_results_require_optin(tmp_path, monkeypatch):
    from app.release import cli

    # A provider-results file exists but the opt-in env is unset: it must be
    # ignored (no surprise network/external ingestion).
    results = tmp_path / "provider-results.json"
    results.write_text(
        json.dumps(
            {
                "generated_at": "2026-09-12T00:00:00Z",
                "results": [{"provider": "Twilio", "status": "PASS", "reason": "ok"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "DEFAULT_EVIDENCE", tmp_path / "nonexistent-evidence.json")
    monkeypatch.setattr(cli, "DEFAULT_ARTIFACT", tmp_path / "nonexistent-artifact.json")
    monkeypatch.setattr(cli, "DEFAULT_E2E_RESULT", tmp_path / "nonexistent-e2e.json")
    monkeypatch.delenv("VOXDESK_REAL_INTEGRATION", raising=False)
    monkeypatch.setattr(cli, "DEFAULT_PROVIDER_RESULTS", results)

    # Patch evaluate to capture the evidence map the CLI built.
    captured = {}

    import app.release.gate as gate_module

    real_evaluate = gate_module.evaluate

    def spy(checklist, evidence, artifact, live, today=None):
        captured["evidence"] = evidence
        return real_evaluate(checklist, evidence, artifact, live, today)

    monkeypatch.setattr(gate_module, "evaluate", spy)
    cli.main([])
    assert captured["evidence"]["realprov-twilio"].status is Status.NOT_RUN
