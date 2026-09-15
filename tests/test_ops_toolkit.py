"""Deterministic tests for the Step 13 operational toolkit.

Every test here is offline: no Docker, no PostgreSQL, no Redis, no rclone, no
real credentials, no real providers, and no production system. They exercise
the pure decision logic of app.release.ops so the toolkit's fail-safe statuses
and exit codes are pinned down before anyone runs it on a staging host.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.release import evidence as evidence_mod
from app.release import gate, ops
from app.release.checklist import load_checklist
from app.release.models import ArtifactRecord

CHECKLIST = load_checklist(
    Path(__file__).resolve().parents[1] / "scripts" / "release" / "checklist.json"
)

NONE_TOOLS = {name: None for name in ops.TOOL_NAMES}


def _runtime_entry(report: dict, name: str) -> dict:
    for c in report["sections"]["runtime"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"no runtime check for {name!r}")


def make_git_repo(tmp_path: Path) -> Path:
    """A minimal clean git repo so preflight repo-checks are deterministic."""
    import subprocess

    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "requirements.txt").write_text("fastapi==0.136.1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    return tmp_path


def _preflight(tmp_path: Path) -> dict:
    return ops.run_preflight(
        make_git_repo(tmp_path),
        tools=NONE_TOOLS,
        env={"APP_ENV": "development"},
        tracked_files=["app/release/ops.py"],
    )


# ---------------------------------------------------------------------------
# 1-3. Preflight missing tools
# ---------------------------------------------------------------------------
def test_preflight_missing_docker_is_blocked(tmp_path):
    report = _preflight(tmp_path)
    assert _runtime_entry(report, "docker")["status"] == ops.STATUS_BLOCKED


def test_preflight_missing_postgresql_is_blocked(tmp_path):
    report = _preflight(tmp_path)
    assert _runtime_entry(report, "psql")["status"] == ops.STATUS_BLOCKED
    assert _runtime_entry(report, "pg_dump")["status"] == ops.STATUS_BLOCKED


def test_preflight_missing_rclone_is_blocked(tmp_path):
    report = _preflight(tmp_path)
    assert _runtime_entry(report, "rclone")["status"] == ops.STATUS_BLOCKED


def test_preflight_exit_code_is_blocked_when_tools_missing(tmp_path):
    report = _preflight(tmp_path)
    assert report["overall"] == ops.STATUS_BLOCKED
    assert report["exit_code"] == ops.EXIT_BLOCKED


# ---------------------------------------------------------------------------
# 4-5. Staging safety validation / production target rejection
# ---------------------------------------------------------------------------
def test_staging_target_validation_accepts_staging():
    allowed, status, _reason = ops.validate_staging_target("staging")
    assert allowed is True
    assert status == ops.STATUS_PASS


def test_staging_target_validation_rejects_production():
    allowed, status, reason = ops.validate_staging_target("production")
    assert allowed is False
    assert status == ops.STATUS_BLOCKED
    assert "production" in reason


def test_production_target_rejection_blocks_backup():
    result = ops.run_backup_orchestration(
        "production", tools={"pg_dump": "/usr/bin/pg_dump", "rclone": None}
    )
    assert result["status"] == ops.STATUS_BLOCKED
    assert result["record"].status == ops.STATUS_BLOCKED


def test_production_target_rejection_blocks_restore(tmp_path):
    dump = tmp_path / "voxdesk.dump"
    dump.write_bytes(b"not-empty")
    result = ops.run_restore_orchestration(
        "production",
        str(dump),
        "voxdesk_drill",
        tools={"pg_restore": "/usr/bin/pg_restore", "psql": "/usr/bin/psql"},
    )
    assert result["status"] == ops.STATUS_BLOCKED


# ---------------------------------------------------------------------------
# 6-8. Provider report (SKIPPED / PASS / FAIL)
# ---------------------------------------------------------------------------
def test_provider_skipped_is_skipped_not_pass():
    report = ops.build_provider_report(
        [{"provider": "Twilio", "status": "SKIPPED", "reason": "no flag", "safety": "read_only"}]
    )
    assert report["status"] == ops.STATUS_SKIPPED
    assert ops.to_registry_status(ops.STATUS_SKIPPED) == ops.STATUS_NOT_RUN


def test_provider_pass_is_pass():
    report = ops.build_provider_report(
        [{"provider": "Twilio", "status": "PASS", "reason": "ok", "safety": "read_only"}]
    )
    assert report["status"] == ops.STATUS_PASS


def test_provider_failure_is_fail():
    report = ops.build_provider_report(
        [
            {"provider": "Twilio", "status": "PASS", "reason": "ok", "safety": "read_only"},
            {"provider": "Stripe", "status": "FAIL", "reason": "bad key", "safety": "read_only"},
        ]
    )
    assert report["status"] == ops.STATUS_FAIL


# ---------------------------------------------------------------------------
# 9-10. TLS classification
# ---------------------------------------------------------------------------
def test_tls_invalid_certificate_fails():
    checks = {
        "localhost_only": False,
        "scheme": "https",
        "connected": True,
        "cert_valid_dates": False,
        "hostname_matches": True,
        "expiry_days": 30,
        "hsts": True,
        "x_frame": True,
        "x_content_type": True,
    }
    assert ops.classify_tls(checks) == ops.STATUS_FAIL


def test_tls_valid_response_passes():
    checks = {
        "localhost_only": False,
        "scheme": "https",
        "connected": True,
        "cert_valid_dates": True,
        "hostname_matches": True,
        "expiry_days": 30,
        "hsts": True,
        "x_frame": True,
        "x_content_type": True,
    }
    assert ops.classify_tls(checks) == ops.STATUS_PASS


def test_tls_localhost_plain_http_is_not_applicable():
    result = ops.verify_tls("http://localhost:8000")
    assert result["status"] == ops.STATUS_NA


def test_tls_no_url_is_blocked():
    result = ops.verify_tls("")
    assert result["status"] == ops.STATUS_BLOCKED


# ---------------------------------------------------------------------------
# 11. Egress blocked outside container
# ---------------------------------------------------------------------------
def test_egress_blocked_outside_container():
    result = ops.verify_egress(False)
    assert result["status"] == ops.STATUS_BLOCKED


def test_egress_fails_when_deny_target_reachable():
    def probe(host, port, timeout):
        return host == "127.0.0.1"

    result = ops.verify_egress(True, allow_hosts=["api.twilio.com"], probe_fn=probe)
    assert result["status"] == ops.STATUS_FAIL
    assert "127.0.0.1" in result["evidence"]


def test_egress_passes_when_deny_blocked_and_allow_reachable():
    def probe(host, port, timeout):
        return host == "api.twilio.com"

    result = ops.verify_egress(True, allow_hosts=["api.twilio.com"], probe_fn=probe)
    assert result["status"] == ops.STATUS_PASS


def test_egress_without_allowlist_is_blocked():
    result = ops.verify_egress(True, allow_hosts=[])
    assert result["status"] == ops.STATUS_BLOCKED


# ---------------------------------------------------------------------------
# 12-13. Cost configuration
# ---------------------------------------------------------------------------
def test_cost_missing_prices_are_unknown_blocked():
    result = ops.verify_cost_config({}, {"twilio"})
    assert result["status"] == ops.STATUS_BLOCKED
    assert any(r["key"] == "voice_minute" and r["status"] == ops.STATUS_BLOCKED
               for r in result["results"])


def test_cost_valid_configuration_passes():
    result = ops.verify_cost_config(
        {"voice_minute": 1300, "tts_1k_chars": 30, "llm_1k_tokens": 15},
        {"twilio", "elevenlabs", "openai", "anthropic", "google"},
        source_date="2026-09-13",
        source_doc_exists=True,
    )
    assert result["status"] == ops.STATUS_PASS


def test_cost_negative_price_fails():
    result = ops.verify_cost_config(
        {"voice_minute": -5}, {"twilio"}, source_date="2026-09-13", source_doc_exists=True
    )
    assert result["status"] == ops.STATUS_FAIL


# ---------------------------------------------------------------------------
# 14-15. Evidence merge + history preservation
# ---------------------------------------------------------------------------
def _record(item_id: str, status: str, evidence: str) -> ops.EvidenceRecord:
    return ops.EvidenceRecord(
        item_id=item_id, status=status, classification="INFRASTRUCTURE", severity="P1",
        command="test", timestamp="2026-09-13T00:00:00+00:00",
        release_commit="abc123", environment="staging", evidence=evidence,
    )


def test_evidence_merge_adds_new_record(tmp_path):
    path = tmp_path / "evidence.json"
    ops.merge_evidence_file(path, [_record("tls-001", ops.STATUS_PASS, "tls ok")])
    data = json.loads(path.read_text(encoding="utf-8"))
    items = {it["item_id"]: it for it in data["items"]}
    assert items["tls-001"]["status"] == ops.STATUS_PASS


def test_evidence_merge_preserves_history(tmp_path):
    path = tmp_path / "evidence.json"
    ops.merge_evidence_file(path, [_record("tls-001", ops.STATUS_BLOCKED, "first run")])
    ops.merge_evidence_file(path, [_record("tls-001", ops.STATUS_PASS, "second run")])
    data = json.loads(path.read_text(encoding="utf-8"))
    items = {it["item_id"]: it for it in data["items"]}
    assert items["tls-001"]["status"] == ops.STATUS_PASS
    assert len(data["history"]) == 1
    assert data["history"][0]["status"] == ops.STATUS_BLOCKED
    assert data["history"][0]["superseded"] == "tls-001"


def test_evidence_merge_is_idempotent(tmp_path):
    path = tmp_path / "evidence.json"
    rec = _record("tls-001", ops.STATUS_PASS, "same evidence")
    ops.merge_evidence_file(path, [rec])
    ops.merge_evidence_file(path, [rec])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("history", []) == []


def test_evidence_merge_preserves_unrelated_keys(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({"title": "kept", "items": [], "verified_on": "2026-09-12"}), encoding="utf-8")
    ops.merge_evidence_file(path, [_record("tls-001", ops.STATUS_PASS, "x")])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["title"] == "kept"
    assert data["verified_on"] == "2026-09-12"


# ---------------------------------------------------------------------------
# 16. Release-gate integration
# ---------------------------------------------------------------------------
def test_release_gate_reads_merged_evidence(tmp_path):
    path = tmp_path / "evidence.json"
    ops.merge_evidence_file(path, [_record("code-001", ops.STATUS_FAIL, "compile broke")])
    loaded = evidence_mod.load_evidence(path)
    evidence = evidence_mod.default_evidence(CHECKLIST)
    evidence.update(loaded)

    live = ops.compute_live_facts()
    artifact = ArtifactRecord(
        release_identifier="test",
        git_commit=live["git_commit"],
        dependency_fingerprint=live["dependency_fingerprint"],
        migration_heads=tuple(live["migration_heads"]),
        configuration_fingerprint=live["configuration_fingerprint"],
    )
    from app.release.models import LiveFacts

    report = gate.evaluate(
        CHECKLIST,
        evidence,
        artifact,
        LiveFacts(
            git_commit=live["git_commit"],
            dependency_fingerprint=live["dependency_fingerprint"],
            migration_heads=tuple(live["migration_heads"]),
            configuration_fingerprint=live["configuration_fingerprint"],
        ),
    )
    assert report.p0_blockers >= 1
    assert report.decision.value == "NOT_READY"


# ---------------------------------------------------------------------------
# 17. Production mutation guard
# ---------------------------------------------------------------------------
def test_production_mutation_guard_blocks_every_staging_op():
    allowed, status, _reason = ops.validate_staging_target("prod")
    assert allowed is False
    assert status == ops.STATUS_BLOCKED
    assert ops.validate_staging_target("production")[0] is False


# ---------------------------------------------------------------------------
# 18. Restore target safety
# ---------------------------------------------------------------------------
def test_restore_requires_explicit_target_db(tmp_path):
    dump = tmp_path / "voxdesk.dump"
    dump.write_bytes(b"not-empty")
    result = ops.run_restore_orchestration(
        "staging",
        str(dump),
        "",
        tools={"pg_restore": "/usr/bin/pg_restore", "psql": "/usr/bin/psql"},
    )
    assert result["status"] == ops.STATUS_BLOCKED
    assert "RESTORE_TARGET_DB" in result["evidence"]


# ---------------------------------------------------------------------------
# 19. Backup integrity failure
# ---------------------------------------------------------------------------
def test_backup_integrity_missing_file_fails():
    status, _detail = ops.verify_backup_integrity("/nonexistent/dump.dump")
    assert status == ops.STATUS_FAIL


def test_backup_integrity_empty_file_fails(tmp_path):
    dump = tmp_path / "empty.dump"
    dump.write_bytes(b"")
    status, _detail = ops.verify_backup_integrity(str(dump))
    assert status == ops.STATUS_FAIL


# ---------------------------------------------------------------------------
# 20. Deployment drift detection
# ---------------------------------------------------------------------------
def test_drift_detection_reports_commit_mismatch():
    recorded = {"git_commit": "aaaa", "dependency_fingerprint": "d",
                "migration_heads": ["h"], "configuration_fingerprint": "c"}
    live = {"git_commit": "bbbb", "dependency_fingerprint": "d",
            "migration_heads": ["h"], "configuration_fingerprint": "c"}
    lines = ops.drift_lines(recorded, live)
    assert any("git commit" in line for line in lines)


def test_drift_detection_clean_when_identical():
    recorded = {"git_commit": "aaaa", "dependency_fingerprint": "d",
                "migration_heads": ["h"], "configuration_fingerprint": "c"}
    live = dict(recorded)
    assert ops.drift_lines(recorded, live) == []


def test_exit_code_mapping():
    assert ops.exit_code_for_status(ops.STATUS_PASS) == 0
    assert ops.exit_code_for_status(ops.STATUS_FAIL) == 1
    assert ops.exit_code_for_status(ops.STATUS_BLOCKED) == 2
    assert ops.exit_code_for_status(ops.STATUS_SKIPPED) == 0
    assert ops.exit_code_for_status(ops.STATUS_NA) == 0
    assert ops.worst_exit_code([2, 0, 1]) == 1
    assert ops.worst_exit_code([2, 0, 2]) == 2
    assert ops.worst_exit_code([2, 0, 0]) == 2


# ---------------------------------------------------------------------------
# Secret-scan precision (templates and documented examples are not secrets)
# ---------------------------------------------------------------------------
def test_env_template_files_are_not_secret_files():
    assert ops._is_secret_filename(".env.example") is False
    assert ops._is_secret_filename(".env.staging.example") is False


def test_real_env_file_is_secret_file():
    assert ops._is_secret_filename(".env") is True


def test_key_and_netrc_files_are_secret_files():
    assert ops._is_secret_filename("secrets/private.pem") is True
    assert ops._is_secret_filename("certs/tls.key") is True
    assert ops._is_secret_filename(".netrc") is True


def test_aws_documented_example_key_is_not_a_secret(tmp_path):
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("api_key: 'AKIAIOSFODNN7EXAMPLE'\n", encoding="utf-8")
    hits = ops.scan_secrets_in_files([fixture.name], root=tmp_path)
    assert hits == []


def test_real_looking_key_is_detected(tmp_path):
    fixture = tmp_path / "fixture.txt"
    # Built by concatenation so no real-looking key literal lives in the
    # repository source (the preflight secret scan would otherwise flag it).
    fake_key = "AKIA" + "IMAGINARYKEY1234"
    fixture.write_text(f"key = '{fake_key}'\n", encoding="utf-8")
    hits = ops.scan_secrets_in_files([fixture.name], root=tmp_path)
    assert hits == [fixture.name]


def test_preflight_security_section_passes_on_clean_repo(tmp_path):
    repo = make_git_repo(tmp_path)
    (repo / ".env.example").write_text("SECRET_KEY=change-me\n", encoding="utf-8")
    report = ops.run_preflight(
        repo,
        tools={name: None for name in ops.TOOL_NAMES},
        env={"APP_ENV": "development"},
        tracked_files=[".env.example", "requirements.txt"],
    )
    security = report["sections"]["security"]
    assert all(c["status"] != ops.STATUS_FAIL for c in security)
