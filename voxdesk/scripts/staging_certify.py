#!/usr/bin/env python3
"""VoxDesk staging certification orchestrator (Step 13 section B/C).

Orchestrates only safe staging operations. The dangerous operations — a real
phone call, a production charge, a production booking, a production CRM
mutation — are never part of this tool.

Modes (choose one; ``--all-safe`` runs every safe mode):
    --preflight      machine/repo/config/secret preflight (read-only)
    --staging        full staging deployment certification sequence
    --backup         staging database backup (wraps scripts/backup.sh)
    --restore        staging restore drill (wraps scripts/restore.sh)
    --providers      real-provider health sweep (opt-in, read-only)
    --observability  Prometheus/Grafana validation (read-only)
    --egress         egress enforcement verification (inside staging only)
    --e2e-readiness  real-telephony E2E precondition check (never dials)
    --deploy-drill   deploy -> rollback drill (wraps deploy.sh/rollback.sh)
    --compose-up     guarded `docker compose up -d --build` for the staging project
    --compose-down   guarded `docker compose down` for the staging project

There are deliberately NO --force-production / --skip-safety / --ignore-gate
switches. The release gate in scripts/release_gate.py remains authoritative.
Every docker-compose operation (build/up/down/exec/ps/config) is gated on a
staging-identity check: APP_ENV must be non-production and COMPOSE_PROJECT_NAME
(if set) must identify staging. A failed guard is a FAIL, and the mutating
operation is never executed.

Usage:
    python scripts/staging_certify.py --preflight [--json] [--write-evidence]

Exit codes: 0 = PASS, 1 = FAIL, 2 = BLOCKED, 3 = invalid configuration.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.integrations.validation import registry  # noqa: E402
from app.integrations.validation.status import real_integration_enabled  # noqa: E402
from app.release import gate, ops  # noqa: E402

EVIDENCE_PATH = Path(ops.REPO_ROOT) / "scripts" / "release" / "evidence.json"
ARTIFACT_PATH = Path(ops.REPO_ROOT) / "var" / "release" / "artifact.json"

#: The one compose file this orchestrator may touch. Never the production one.
STAGING_COMPOSE_FILE = "docker-compose.staging.yml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="staging_certify.py", description="VoxDesk staging certification orchestrator"
    )
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--staging", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--providers", action="store_true")
    parser.add_argument("--observability", action="store_true")
    parser.add_argument("--egress", action="store_true")
    parser.add_argument("--e2e-readiness", action="store_true",
                        help="validate real-call E2E preconditions only (never places a call)")
    parser.add_argument("--deploy-drill", action="store_true")
    parser.add_argument("--compose-up", action="store_true",
                        help="guarded `docker compose -f docker-compose.staging.yml up -d --build`")
    parser.add_argument("--compose-down", action="store_true",
                        help="guarded `docker compose -f docker-compose.staging.yml down` (never -v)")
    parser.add_argument("--all-safe", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write-evidence", action="store_true",
                        help="merge produced evidence into scripts/release/evidence.json")
    parser.add_argument("--base-url", default=os.environ.get("SMOKE_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--url", default="", help="staging HTTPS URL for TLS verification")
    parser.add_argument("--prometheus-url", default="")
    parser.add_argument("--grafana-url", default="")
    parser.add_argument("--inside-staging", action="store_true")
    parser.add_argument("--dump", default="", help="backup dump path for --restore")
    parser.add_argument("--restore-target-db", default=os.environ.get("RESTORE_TARGET_DB", ""))
    parser.add_argument("--previous-sha", default="", help="previous-good sha for --deploy-drill")
    parser.add_argument("--expected-commit", default=None)
    return parser


def _http_get(url: str, timeout: float = 6.0) -> tuple[int, bool]:
    try:
        with httpx.Client(verify=True, timeout=timeout, follow_redirects=False) as client:
            resp = client.get(url)
            return resp.status_code, True
    except Exception:  # noqa: BLE001
        return 0, False


def _step(name: str, status: str, detail: str = "", record: ops.EvidenceRecord | None = None) -> dict:
    return {"step": name, "status": status, "detail": detail, "record": record}


def _tools() -> dict[str, str | None]:
    return {
        name: ops.which(name)
        for name in ("docker", "psql", "pg_dump", "pg_restore", "redis-cli", "rclone")
    }


def _compose_ready() -> tuple[bool, str]:
    """Whether the docker compose (v2) plugin is usable. Returns (ok, detail)."""
    if not ops.which("docker"):
        return False, "docker not found on PATH"
    try:
        proc = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"docker compose unavailable: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "docker compose version failed").strip()[:200]
    return True, "docker compose (v2) present"


def _staging_identity_ok(app_env: str) -> tuple[bool, str]:
    """Verify the compose target identifies itself as staging.

    Refuses production outright and refuses a COMPOSE_PROJECT_NAME that does
    not identify staging, so a mutating or exec operation can never touch a
    production (or ambiguous) compose project. Returns (ok, reason).
    """
    allowed, _status, reason = ops.validate_staging_target(app_env)
    if not allowed:
        return False, reason
    project = (os.environ.get("COMPOSE_PROJECT_NAME", "") or "").strip()
    if project and "staging" not in project.lower():
        return False, (
            f"COMPOSE_PROJECT_NAME={project!r} does not identify staging; "
            "refusing to touch a possibly-non-staging compose project"
        )
    return True, (
        f"staging identity confirmed (APP_ENV={app_env}, "
        f"project={project or 'voxdesk-staging'})"
    )


def _compose(args_list: list[str], timeout: int = 1800) -> tuple[int, str, str]:
    """Run ``docker compose -f <staging file> ...``. Returns (rc, stdout, stderr)."""
    proc = subprocess.run(
        ["docker", "compose", "-f", STAGING_COMPOSE_FILE, *args_list],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _compose_config_check() -> tuple[str, str]:
    """Validate the staging compose file resolves with `docker compose config`."""
    ok, detail = _compose_ready()
    if not ok:
        return ops.STATUS_BLOCKED, f"compose config validation unavailable: {detail}"
    rc, out, err = _compose(["config", "--quiet"], timeout=120)
    if rc != 0:
        return ops.STATUS_FAIL, f"docker compose config failed: {(err or out).strip()[-400:]}"
    return ops.STATUS_PASS, f"{STAGING_COMPOSE_FILE} resolves via `docker compose config`"


def _cleanup_staging_compose() -> None:
    """Bring down the staging project after a partial startup failure.

    Never passes ``-v`` (volumes/backups are preserved) and never targets any
    file other than STAGING_COMPOSE_FILE, so a failed certification cannot
    destroy data or touch another environment's services.
    """
    try:
        _compose(["down"], timeout=300)
    except Exception:  # noqa: BLE001 - cleanup is best-effort; the FAIL is already recorded
        pass


def _static_compose_checks() -> tuple[str, str]:
    """Static validation of the compose files without Docker or a YAML parser."""
    files = [
        "docker-compose.yml",
        "docker-compose.staging.yml",
        "docker-compose.prod.yml",
        "observability/prometheus.yml",
        "observability/alerts.yml",
        "observability/slos.yml",
    ]
    problems = []
    for name in files:
        p = Path(ops.REPO_ROOT) / name
        if not p.exists():
            problems.append(f"{name}: missing")
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            problems.append(f"{name}: empty")
    try:
        import yaml  # noqa: F401

        for name in files:
            p = Path(ops.REPO_ROOT) / name
            if p.exists():
                with p.open(encoding="utf-8") as fh:
                    yaml.safe_load(fh)
        parsed_with = "pyyaml"
    except Exception:  # noqa: BLE001
        parsed_with = "presence-only (pyyaml unavailable)"
    if problems:
        return ops.STATUS_FAIL, "; ".join(problems)
    return ops.STATUS_PASS, f"{len(files)} files present and valid ({parsed_with})"


def _security_smoke() -> tuple[str, str]:
    try:
        problems = settings.validate_security()
    except Exception as exc:  # noqa: BLE001
        return ops.STATUS_FAIL, f"validate_security raised {type(exc).__name__}"
    if problems:
        return ops.STATUS_FAIL, "; ".join(str(p) for p in problems[:5])
    return ops.STATUS_PASS, "settings.validate_security() clean"


def _e2e_readiness(app_env: str, provider_overall: str, api_ok: bool,
                   api_status_code: int) -> dict:
    """Validate real-telephony E2E PRECONDITIONS only (Step 16 section D).

    This never places a call, never mutates anything, and never marks e2e-001
    PASS. The live call stays human-executed and is recorded separately in
    var/release/e2e-result.json. Returns {"status", "checks", "detail"} using
    the standard vocabulary: FAIL (safety/config violation), BLOCKED (missing
    prerequisite), NOT_RUN (disarmed, or READY-but-call-not-yet-executed).
    """
    env = (app_env or "").strip().lower()
    production = env in ("production", "prod")
    armed = bool(settings.e2e_enabled)

    if production and armed:
        return {"status": ops.STATUS_FAIL,
                "checks": [{"name": "environment", "status": ops.STATUS_FAIL,
                            "detail": "E2E armed in production — refuse; never run a "
                                      "live E2E call against production"}],
                "detail": "E2E armed in production — safety guard failed"}
    if not armed:
        return {"status": ops.STATUS_NOT_RUN,
                "checks": [{"name": "E2E armed", "status": ops.STATUS_NOT_RUN,
                            "detail": "E2E_ENABLED=false — real-call E2E intentionally "
                                      "not attempted"}],
                "detail": "E2E disarmed; real-call E2E intentionally not attempted "
                          "(the live call is never automated)"}

    checks: list[dict] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    add("environment", ops.STATUS_PASS, f"APP_ENV={app_env} (non-production)")
    add("E2E armed", ops.STATUS_PASS, "E2E_ENABLED=true")
    add("E2E test number", ops.STATUS_PASS if settings.e2e_test_number else ops.STATUS_FAIL,
        "configured" if settings.e2e_test_number else "E2E_TEST_NUMBER missing")
    add("operator allowlist",
        ops.STATUS_PASS if settings.e2e_caller_list else ops.STATUS_FAIL,
        f"{len(settings.e2e_caller_list)} caller(s) allowlisted" if settings.e2e_caller_list
        else "E2E_ALLOWED_CALLERS empty")
    add("API/monitoring reachable",
        ops.STATUS_PASS if (api_ok and api_status_code == 200) else ops.STATUS_BLOCKED,
        f"/health/ready -> {api_status_code}" if api_ok else "API unreachable")
    add("provider health",
        ops.STATUS_PASS if provider_overall == ops.STATUS_PASS else
        (ops.STATUS_FAIL if provider_overall == ops.STATUS_FAIL else ops.STATUS_BLOCKED),
        f"provider sweep {provider_overall}"
        + ("; run --providers with VOXDESK_REAL_INTEGRATION=1"
           if provider_overall != ops.STATUS_PASS else ""))

    if settings.billing_provider == "stripe":
        key = settings.stripe_secret_key or ""
        if key.startswith("sk_live_"):
            add("billing test mode", ops.STATUS_FAIL,
                "Stripe LIVE key detected in staging — refuse; use sk_test_ keys only")
        elif key.startswith("sk_test_"):
            add("billing test mode", ops.STATUS_PASS, "Stripe test-mode key (sk_test_*)")
        else:
            add("billing test mode", ops.STATUS_BLOCKED, "STRIPE_SECRET_KEY not set")
    else:
        add("billing test mode", ops.STATUS_PASS,
            f"billing_provider={settings.billing_provider} (no live Stripe)")

    add("destination configured", ops.STATUS_PASS if settings.e2e_test_number else ops.STATUS_BLOCKED,
        "E2E_TEST_NUMBER is the dial destination")

    # Human-only checklist items the script cannot verify (never PASS):
    add("test tenant exists (is_test_tenant=true)", ops.STATUS_NOT_RUN,
        "operator checklist item — verify in the dashboard/DB before dialing")
    add("test tenant answers the test number", ops.STATUS_NOT_RUN,
        "operator checklist item — tenant twilio_number must equal E2E_TEST_NUMBER")

    statuses = [c["status"] for c in checks]
    if ops.STATUS_FAIL in statuses:
        status = ops.STATUS_FAIL
    elif ops.STATUS_BLOCKED in statuses:
        status = ops.STATUS_BLOCKED
    else:
        # Every verifiable precondition holds; the CALL has not happened, so
        # this is READY — surfaced as NOT_RUN, never PASS.
        status = ops.STATUS_NOT_RUN
    detail = (
        "READY: all verifiable E2E preconditions satisfied — execute the live call "
        "manually per docs/STEP16-LAUNCH-E2E-RUNBOOK.md and record it in "
        "var/release/e2e-result.json"
        if status == ops.STATUS_NOT_RUN
        else "E2E preconditions incomplete or unsafe — see checks"
    )
    return {"status": status, "checks": checks, "detail": detail}


def _run_staging_sequence(args: argparse.Namespace) -> dict:
    app_env = (os.environ.get("APP_ENV") or settings.app_env or "development").strip()
    tools = _tools()
    identity_ok, identity_reason = _staging_identity_ok(app_env)
    steps: list[dict] = []
    records: list[ops.EvidenceRecord] = []

    def add(step: dict) -> None:
        steps.append(step)
        if step.get("record") is not None:
            records.append(step["record"])

    # 1. preflight
    preflight = ops.run_preflight(ops.REPO_ROOT, expected_commit=args.expected_commit)
    add(_step("preflight", preflight["overall"], f"exit {preflight['exit_code']}"))

    # 2. verify artifact (drift vs frozen record, when present)
    live = ops.compute_live_facts()
    recorded: dict = {}
    if ARTIFACT_PATH.exists():
        try:
            recorded = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            recorded = {}
    drift = ops.drift_lines(recorded, live)
    if recorded.get("git_commit") and drift:
        add(_step("verify artifact", ops.STATUS_FAIL, "; ".join(drift)))
    else:
        add(_step("verify artifact", ops.STATUS_PASS,
                  f"commit {live['git_commit'][:12]} (no drift)" if not drift else "no frozen record"))

    # 3. verify staging environment
    allowed, status, reason = ops.validate_staging_target(app_env)
    add(_step("verify staging environment", status, reason))

    # 3b. staging identity guard — every compose operation below re-checks this
    add(_step("staging identity guard",
              ops.STATUS_PASS if identity_ok else ops.STATUS_FAIL, identity_reason))

    # 4. runtime dependencies
    missing = [name for name, path in tools.items() if not path]
    add(_step("runtime dependencies",
              ops.STATUS_PASS if not missing else ops.STATUS_BLOCKED,
              "all present" if not missing else f"missing: {', '.join(missing)}"))

    # 5. build candidate (guarded compose build — staging project only)
    if not tools.get("docker"):
        add(_step("build candidate", ops.STATUS_BLOCKED, "docker missing"))
    elif not identity_ok:
        add(_step("build candidate", ops.STATUS_FAIL, identity_reason))
    else:
        rc, out, err = _compose(["build"], timeout=1800)
        add(_step("build candidate", ops.STATUS_PASS if rc == 0 else ops.STATUS_FAIL,
                  "docker compose build ok" if rc == 0 else (err or out).strip()[-300:]))

    # 6. start staging (guarded compose up; cleaned up on partial failure)
    if not tools.get("docker"):
        add(_step("start staging", ops.STATUS_BLOCKED, "docker missing"))
    elif not identity_ok:
        add(_step("start staging", ops.STATUS_FAIL, identity_reason))
    else:
        rc, out, err = _compose(["up", "-d"], timeout=600)
        if rc == 0:
            add(_step("start staging", ops.STATUS_PASS, "docker compose up -d ok"))
        else:
            add(_step("start staging", ops.STATUS_FAIL, (err or out).strip()[-300:]))
            _cleanup_staging_compose()

    # 7. wait for readiness
    status_code, ok = _http_get(args.base_url.rstrip("/") + "/health/ready")
    add(_step("wait for readiness", ops.STATUS_PASS if ok and status_code == 200 else ops.STATUS_BLOCKED,
              f"{args.base_url}/health/ready -> {status_code}" if ok else "unreachable"))

    # 8. run migrations (guarded; advisory-locked via scripts/migrate.py)
    if not tools.get("docker"):
        add(_step("run migrations", ops.STATUS_BLOCKED, "docker missing"))
    elif not identity_ok:
        add(_step("run migrations", ops.STATUS_FAIL, identity_reason))
    else:
        rc, out, err = _compose(["exec", "-T", "api", "python", "scripts/migrate.py"], timeout=600)
        add(_step("run migrations", ops.STATUS_PASS if rc == 0 else ops.STATUS_FAIL,
                  "advisory-locked migrations applied" if rc == 0 else (err or out).strip()[-300:]))

    # 9. smoke test
    smoke_script = Path(ops.REPO_ROOT) / "scripts" / "smoke_test.py"
    if not smoke_script.exists():
        add(_step("smoke test", ops.STATUS_FAIL, "scripts/smoke_test.py missing"))
    elif status_code == 200:
        try:
            proc = subprocess.run(
                [sys.executable, str(smoke_script)],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "SMOKE_BASE_URL": args.base_url},
            )
            add(_step("smoke test", ops.STATUS_PASS if proc.returncode == 0 else ops.STATUS_FAIL,
                      (proc.stdout or proc.stderr).strip().splitlines()[-1] if (proc.stdout or proc.stderr) else ""))
        except (OSError, subprocess.SubprocessError) as exc:
            add(_step("smoke test", ops.STATUS_FAIL, f"failed: {exc}"))
    else:
        add(_step("smoke test", ops.STATUS_BLOCKED, "API not ready; cannot run smoke test"))

    # 10. health
    add(_step("verify health", ops.STATUS_PASS if ok and status_code == 200 else ops.STATUS_BLOCKED,
              f"/health/ready -> {status_code}" if ok else "unreachable"))

    # 11. metrics
    mcode, mok = _http_get(args.base_url.rstrip("/") + "/metrics")
    if mok and mcode in (200, 401):
        add(_step("verify metrics", ops.STATUS_PASS, f"/metrics -> {mcode} (200 or token-gated 401)"))
    else:
        add(_step("verify metrics", ops.STATUS_BLOCKED, f"/metrics -> {mcode}" if mok else "unreachable"))

    # 12. scheduler (guarded compose ps)
    if not tools.get("docker"):
        add(_step("verify scheduler", ops.STATUS_BLOCKED, "docker missing"))
    elif not identity_ok:
        add(_step("verify scheduler", ops.STATUS_FAIL, identity_reason))
    else:
        rc, out, err = _compose(["ps", "--status", "running", "-q", "scheduler"], timeout=120)
        running = rc == 0 and bool(out.strip())
        add(_step("verify scheduler", ops.STATUS_PASS if running else ops.STATUS_FAIL,
                  "scheduler container running" if running else (err or "scheduler container not running").strip()[-200:]))

    # 13. Redis (guarded compose exec; internal-only — no published port)
    if not tools.get("docker"):
        add(_step("verify Redis", ops.STATUS_BLOCKED,
                  "docker missing; staging Redis is internal-only (no published port)"))
    elif not identity_ok:
        add(_step("verify Redis", ops.STATUS_FAIL, identity_reason))
    else:
        rc, out, err = _compose(["exec", "-T", "redis", "redis-cli", "ping"], timeout=120)
        pong = rc == 0 and "PONG" in (out + err)
        add(_step("verify Redis", ops.STATUS_PASS if pong else ops.STATUS_FAIL,
                  "redis PONG" if pong else (err or out or "no PONG").strip()[-200:]))

    # 14. PostgreSQL (guarded compose exec; internal-only — no published port)
    if not tools.get("docker"):
        add(_step("verify PostgreSQL", ops.STATUS_BLOCKED,
                  "docker missing; staging database is internal-only (no published port)"))
    elif not identity_ok:
        add(_step("verify PostgreSQL", ops.STATUS_FAIL, identity_reason))
    else:
        pg_user = os.environ.get("POSTGRES_USER", "voxdesk")
        rc, out, err = _compose(["exec", "-T", "db", "pg_isready", "-U", pg_user], timeout=120)
        add(_step("verify PostgreSQL", ops.STATUS_PASS if rc == 0 else ops.STATUS_FAIL,
                  "pg_isready ok" if rc == 0 else (err or out or "pg_isready failed").strip()[-200:]))

    # 15. proxy
    pcode, pok = _http_get(args.base_url.rstrip("/") + "/")
    add(_step("verify proxy", ops.STATUS_PASS if pok and pcode < 500 else ops.STATUS_BLOCKED,
              f"GET / -> {pcode}" if pok else "unreachable"))

    # 16. deployment checks (static)
    cstatus, cdetail = _static_compose_checks()
    add(_step("deployment checks (static)", cstatus, cdetail))

    # 16b. compose config validation (docker, read-only)
    cc_status, cc_detail = _compose_config_check()
    add(_step("compose config (docker)", cc_status, cc_detail))

    # 17. security smoke
    sstatus, sdetail = _security_smoke()
    add(_step("security smoke checks", sstatus, sdetail))

    # 18. provider health (opt-in)
    outcomes = asyncio.run(registry.run_all())
    provider_report = ops.build_provider_report([o.as_dict() for o in outcomes])
    add(_step("provider health checks", provider_report["status"],
              f"{len(provider_report['rows'])} providers, opt-in={real_integration_enabled()}"))

    # 18b. E2E readiness (preconditions only — the live call is never automated)
    e2e_ready = _e2e_readiness(app_env, provider_report["status"], ok, status_code)
    add(_step("E2E readiness (preconditions)", e2e_ready["status"], e2e_ready["detail"]))

    # 19. backup verification
    backup = ops.run_backup_orchestration(app_env, tools=tools)
    add(_step("backup verification", backup["status"], backup["evidence"], backup.get("record")))

    # 20. TLS (BLOCKED when no URL is supplied; certificate validation never weakened)
    if args.url:
        tls = ops.verify_tls(args.url)
        add(_step("TLS certification", tls["status"], tls["evidence"],
                  ops.EvidenceRecord(
                      item_id="tls-001", status=tls["status"],
                      classification="INFRASTRUCTURE", severity="P0",
                      command="scripts/staging_certify.py --staging --url",
                      timestamp=ops.now_iso(), release_commit=live["git_commit"],
                      environment=app_env, evidence=tls["evidence"])))
    else:
        add(_step("TLS certification", ops.STATUS_BLOCKED,
                  "no staging URL supplied (pass --url https://staging.example.com)"))

    # Observability (BLOCKED when no Prometheus URL is supplied)
    if args.prometheus_url:
        obs = ops.verify_observability(args.prometheus_url, args.grafana_url)
        add(_step("observability", obs["status"], obs["evidence"],
                  ops.EvidenceRecord(
                      item_id="obs-001", status=obs["status"],
                      classification="INFRASTRUCTURE", severity="P1",
                      command="scripts/staging_certify.py --staging --prometheus-url",
                      timestamp=ops.now_iso(), release_commit=live["git_commit"],
                      environment=app_env, evidence=obs["evidence"])))
    else:
        add(_step("observability", ops.STATUS_BLOCKED,
                  "Prometheus URL not configured (pass --prometheus-url)"))

    # Aggregate deploy-001 from the deployment steps
    dep_statuses = [s["status"] for s in steps
                    if s["step"] in ("build candidate", "start staging", "run migrations",
                                     "wait for readiness", "smoke test", "verify health")]
    if ops.STATUS_FAIL in dep_statuses:
        deploy_status = ops.STATUS_FAIL
    elif ops.STATUS_BLOCKED in dep_statuses:
        deploy_status = ops.STATUS_BLOCKED
    elif all(s == ops.STATUS_PASS for s in dep_statuses):
        deploy_status = ops.STATUS_PASS
    else:
        deploy_status = ops.STATUS_NOT_RUN
    add(_step("deployment certification (aggregate)", deploy_status,
              "aggregate of build/start/migrate/readiness/smoke/health"))
    records.append(ops.EvidenceRecord(
        item_id="deploy-001", status=deploy_status,
        classification="INFRASTRUCTURE", severity="P1",
        command="scripts/staging_certify.py --staging",
        timestamp=ops.now_iso(), release_commit=live["git_commit"],
        environment=app_env,
        evidence="aggregate of build/start/migrate/readiness/smoke/health "
                 f"({deploy_status})"))

    codes = [ops.exit_code_for_status(s["status"]) for s in steps
             if s["status"] not in (ops.STATUS_NOT_RUN,)]
    statuses = [s["status"] for s in steps]
    if ops.STATUS_FAIL in statuses:
        overall = "FAIL"
    elif ops.STATUS_BLOCKED in statuses:
        overall = "BLOCKED"
    elif codes and max(codes) == 0:
        overall = "PASS"
    else:
        # NOT_RUN steps (e.g. E2E readiness while disarmed) are informational:
        # they never flip an otherwise-green certification to BLOCKED.
        overall = "NOT_RUN"
    return {
        "mode": "staging",
        "timestamp": ops.now_iso(),
        "release_commit": live["git_commit"],
        "environment": app_env,
        "steps": [{"step": s["step"], "status": s["status"], "detail": s["detail"]}
                  for s in steps],
        "overall": overall,
        "exit_code": ops.worst_exit_code(codes) if codes else ops.EXIT_BLOCKED,
        "records": records,
    }


def _run_single_mode(args: argparse.Namespace, mode: str) -> dict:
    app_env = (os.environ.get("APP_ENV") or settings.app_env or "development").strip()
    live = ops.compute_live_facts()
    records: list[ops.EvidenceRecord] = []
    steps: list[dict] = []

    if mode == "preflight":
        report = ops.run_preflight(ops.REPO_ROOT, expected_commit=args.expected_commit)
        return {"mode": mode, "preflight": report, "overall": report["overall"],
                "exit_code": report["exit_code"], "records": records}

    if mode == "backup":
        result = ops.run_backup_orchestration(app_env)
        steps.append(_step("backup", result["status"], result["evidence"], result.get("record")))
        if result.get("record"):
            records.append(result["record"])

    elif mode == "restore":
        result = ops.run_restore_orchestration(app_env, args.dump, args.restore_target_db)
        steps.append(_step("restore", result["status"], result["evidence"], result.get("record")))
        if result.get("record"):
            records.append(result["record"])

    elif mode == "providers":
        outcomes = asyncio.run(registry.run_all())
        report = ops.build_provider_report([o.as_dict() for o in outcomes])
        steps.append(_step("providers", report["status"],
                           f"{len(report['rows'])} providers, opt-in={real_integration_enabled()}"))
        if real_integration_enabled():
            ingested = gate.evidence_for_provider_results(
                [o.as_dict() for o in outcomes], ops.today_iso(), "staging_certify.py --providers"
            )
            for ev in ingested.values():
                records.append(ops.EvidenceRecord(
                    item_id=ev.item_id, status=ev.status.value,
                    classification="EXTERNAL", severity="P1",
                    command="scripts/staging_certify.py --providers",
                    timestamp=ops.now_iso(), release_commit=live["git_commit"],
                    environment=app_env, evidence=ev.evidence))

    elif mode == "observability":
        result = ops.verify_observability(args.prometheus_url, args.grafana_url)
        steps.append(_step("observability", result["status"], result["evidence"],
                           ops.EvidenceRecord(
                               item_id="obs-001", status=result["status"],
                               classification="INFRASTRUCTURE", severity="P1",
                               command="scripts/staging_certify.py --observability",
                               timestamp=ops.now_iso(), release_commit=live["git_commit"],
                               environment=app_env, evidence=result["evidence"])))
        if steps[-1].get("record"):
            records.append(steps[-1]["record"])

    elif mode == "egress":
        inside = args.inside_staging or (
            os.environ.get("VOXDESK_STAGING_NETWORK", "").strip().lower() in {"1", "true", "yes", "on"}
        )
        allow_hosts = [
            h.strip() for h in (os.environ.get("VOXDESK_EGRESS_ALLOW_HOSTS", "") or "").split(",")
            if h.strip()
        ]
        result = ops.verify_egress(inside, allow_hosts=allow_hosts)
        steps.append(_step("egress", result["status"], result["evidence"],
                           ops.EvidenceRecord(
                               item_id="egress-001", status=result["status"],
                               classification="INFRASTRUCTURE", severity="P1",
                               command="scripts/staging_certify.py --egress",
                               timestamp=ops.now_iso(), release_commit=live["git_commit"],
                               environment=app_env, evidence=result["evidence"])))
        if steps[-1].get("record"):
            records.append(steps[-1]["record"])

    elif mode == "deploy-drill":
        result = ops.run_deploy_drill_orchestration(app_env, args.previous_sha)
        steps.append(_step("deploy-drill", result["status"], result["evidence"],
                           result.get("record")))
        if result.get("record"):
            records.append(result["record"])

    elif mode == "e2e-readiness":
        # PRECONDITIONS ONLY. Never places a call, never writes e2e-001
        # evidence: that item is satisfied solely by a human-authored
        # var/release/e2e-result.json produced after a real operator call.
        status_code, ok = _http_get(args.base_url.rstrip("/") + "/health/ready")
        outcomes = asyncio.run(registry.run_all())
        provider_report = ops.build_provider_report([o.as_dict() for o in outcomes])
        e2e_ready = _e2e_readiness(app_env, provider_report["status"], ok, status_code)
        detail = e2e_ready["detail"] + " | " + "; ".join(
            f"{c['name']}={c['status']}" for c in e2e_ready["checks"]
        )
        steps.append(_step("E2E readiness (preconditions)", e2e_ready["status"], detail))

    codes = [ops.exit_code_for_status(s["status"]) for s in steps]
    statuses = [s["status"] for s in steps]
    if ops.STATUS_FAIL in statuses:
        overall = "FAIL"
    elif ops.STATUS_BLOCKED in statuses:
        overall = "BLOCKED"
    elif ops.STATUS_NOT_RUN in statuses:
        overall = "NOT_RUN"
    elif statuses and all(s == ops.STATUS_PASS for s in statuses):
        overall = "PASS"
    else:
        # Includes the all-SKIPPED case: credentials absent is never PASS.
        overall = "SKIPPED"
    return {
        "mode": mode,
        "timestamp": ops.now_iso(),
        "release_commit": live["git_commit"],
        "environment": app_env,
        "steps": [{"step": s["step"], "status": s["status"], "detail": s["detail"]}
                  for s in steps],
        "overall": overall,
        "exit_code": ops.worst_exit_code(codes),
        "records": records,
    }


def _render(report: dict) -> str:
    lines = [f"staging_certify — mode={report.get('mode')}", "=" * 78]
    if report.get("preflight"):
        lines.append(ops.render_preflight(report["preflight"]))
        return "\n".join(lines)
    for s in report.get("steps", []):
        lines.append(f"  [{s['status']:<11}] {s['step']:<34} {s['detail']}")
    lines.append("-" * 78)
    lines.append(f"OVERALL: {report['overall']}   (exit {report['exit_code']})")
    return "\n".join(lines)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    modes = [m for m, on in (
        ("preflight", args.preflight), ("staging", args.staging),
        ("backup", args.backup), ("restore", args.restore),
        ("providers", args.providers), ("observability", args.observability),
        ("egress", args.egress), ("e2e-readiness", args.e2e_readiness),
        ("deploy-drill", args.deploy_drill),
    ) if on]
    if args.all_safe or not modes:
        modes = ["preflight", "staging", "backup", "providers", "observability", "egress", "deploy-drill"]

    reports = []
    for mode in modes:
        if mode == "staging":
            reports.append(_run_staging_sequence(args))
        else:
            reports.append(_run_single_mode(args, mode))

    # Guarded compose lifecycle (opt-in, staging project only). These run as
    # their own reports and are never part of the default mode list.
    if args.compose_up or args.compose_down:
        app_env = (os.environ.get("APP_ENV") or settings.app_env or "development").strip()
        live = ops.compute_live_facts()
        identity_ok, identity_reason = _staging_identity_ok(app_env)
        for action in ([m for m, on in (("compose-up", args.compose_up),
                                        ("compose-down", args.compose_down)) if on]):
            if not identity_ok:
                reports.append({
                    "mode": action,
                    "timestamp": ops.now_iso(),
                    "release_commit": live["git_commit"],
                    "environment": app_env,
                    "steps": [{"step": "staging identity guard", "status": ops.STATUS_FAIL,
                               "detail": identity_reason}],
                    "overall": "FAIL",
                    "exit_code": ops.EXIT_FAIL,
                    "records": [],
                })
                continue
            ok, detail = _compose_ready()
            if not ok:
                reports.append({
                    "mode": action,
                    "timestamp": ops.now_iso(),
                    "release_commit": live["git_commit"],
                    "environment": app_env,
                    "steps": [{"step": "docker compose", "status": ops.STATUS_BLOCKED,
                               "detail": detail}],
                    "overall": "BLOCKED",
                    "exit_code": ops.EXIT_BLOCKED,
                    "records": [],
                })
                continue
            if action == "compose-up":
                rc, out, err = _compose(["up", "-d", "--build"], timeout=1800)
                if rc == 0:
                    step_status, step_detail = ops.STATUS_PASS, "docker compose up -d --build ok"
                else:
                    step_status, step_detail = ops.STATUS_FAIL, (err or out).strip()[-300:]
                    _cleanup_staging_compose()
            else:
                rc, out, err = _compose(["down"], timeout=600)
                step_status, step_detail = (
                    (ops.STATUS_PASS, "docker compose down ok") if rc == 0
                    else (ops.STATUS_FAIL, (err or out).strip()[-300:])
                )
            reports.append({
                "mode": action,
                "timestamp": ops.now_iso(),
                "release_commit": live["git_commit"],
                "environment": app_env,
                "steps": [{"step": "docker compose", "status": step_status, "detail": step_detail}],
                "overall": ("PASS" if step_status == ops.STATUS_PASS else "FAIL"),
                "exit_code": ops.exit_code_for_status(step_status),
                "records": [],
            })

    if args.json:
        print(json.dumps(reports, indent=2, default=str))
    else:
        for report in reports:
            print(_render(report))
            print()

    if args.write_evidence:
        total = 0
        for report in reports:
            total += len(report.get("records", []))
            if report.get("records"):
                ops.merge_evidence_file(EVIDENCE_PATH, report["records"])
        print(f"evidence: merged {total} record(s) into {EVIDENCE_PATH}")

    codes = [r["exit_code"] for r in reports]
    return ops.worst_exit_code(codes)


if __name__ == "__main__":
    sys.exit(main())
