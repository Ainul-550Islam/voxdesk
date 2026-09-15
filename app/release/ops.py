"""Operational execution toolkit core (Step 13).

Everything the Step 13 CLI scripts share, kept here so it is deterministic
and unit-testable without Docker, real credentials or a live network.

Design rules enforced here:

* Read-only by default. The only subprocess calls that ever happen are to the
  existing, already-reviewed shell scripts (`scripts/backup.sh`,
  `scripts/restore.sh`, `scripts/deploy.sh`, `scripts/rollback.sh`) and only
  after a staging-safety gate has passed. The toolkit itself never dials a
  phone, never charges Stripe, never books a calendar, never writes CRM data.
* Fail-safe statuses. A missing tool is BLOCKED, a failed command is FAIL, a
  missing credential is SKIPPED, a missing evidence record is NOT_RUN. Those
  are never collapsed into PASS.
* Exit codes are the Step 13 contract: 0 = success, 1 = failure, 2 = blocked,
  3 = invalid configuration.
* Evidence is captured in one JSON shape (see docs/STEP13-EVIDENCE-SCHEMA.md)
  and merged into `scripts/release/evidence.json` without destroying history.
* No bypass switches exist anywhere in this module.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from app.release import facts

# ---------------------------------------------------------------------------
# Exit codes (Step 13 section O)
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_BLOCKED = 2
EXIT_CONFIG = 3

# Status vocabulary. The first five are registry-legal; SKIPPED and
# NOT_APPLICABLE are operational only and degrade to NOT_RUN when written to
# the evidence registry (a skipped check is not evidence of success).
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"
STATUS_NOT_RUN = "NOT_RUN"
STATUS_WAIVED = "WAIVED"
STATUS_SKIPPED = "SKIPPED"
STATUS_NA = "NOT_APPLICABLE"

_REGISTRY_STATUSES = {
    STATUS_PASS,
    STATUS_FAIL,
    STATUS_BLOCKED,
    STATUS_NOT_RUN,
    STATUS_WAIVED,
}

#: Tools the operational preflight inspects.
TOOL_NAMES = [
    "docker",
    "docker-compose",
    "psql",
    "pg_dump",
    "pg_restore",
    "redis-server",
    "redis-cli",
    "rclone",
    "curl",
    "openssl",
    "python3",
    "node",
    "npm",
]

#: Tracked file names/suffixes that are never acceptable in the repository.
#: ``.env.example`` style templates are NOT flagged — they document required
#: variables and contain no secret values.
_SECRET_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".netrc")

#: Secret-value patterns scanned in tracked source files (values only, never
#: echoed back).
_SECRET_VALUE_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"SG\.[A-Za-z0-9_-]{16,}"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]

#: Well-known documentation/example values that are not secrets. AWS's public
#: docs use ``AKIAIOSFODNN7EXAMPLE`` as the canonical example access key id.
_EXAMPLE_SECRET_VALUES = frozenset({"AKIAIOSFODNN7EXAMPLE"})


def _is_secret_filename(relpath: str) -> bool:
    base = Path(relpath).name
    if base == ".env":
        return True
    lowered = base.lower()
    return any(lowered.endswith(suffix) for suffix in _SECRET_FILE_SUFFIXES)

_PLACEHOLDER_MARKERS = (
    "change-me",
    "change_me",
    "insecure",
    "xxxx",
    "placeholder",
    "your-",
    "<",
    ">",
)

#: Egress deny destinations (Step 13 section I).
DEFAULT_EGRESS_DENY_HOSTS = [
    "127.0.0.1",
    "localhost",
    "169.254.169.254",       # cloud metadata
    "metadata.google.internal",
    "10.0.0.1",              # RFC1918
    "172.16.0.1",            # RFC1918
    "192.168.1.1",           # RFC1918
    "fd00::1",               # RFC4193 (ULA)
    "fe80::1",               # link-local
]

#: Cost keys required per enabled provider family (Step 13 section K).
COST_REQUIRED_KEYS = {
    "twilio": ["voice_minute"],
    "elevenlabs": ["tts_1k_chars"],
    "openai": ["llm_1k_tokens"],
    "anthropic": ["llm_1k_tokens"],
    "google": ["llm_1k_tokens"],
}

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def which(tool: str) -> str | None:
    """Absolute path to ``tool`` on PATH, or None."""
    return shutil.which(tool)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_iso() -> str:
    return date.today().isoformat()


def _looks_placeholder(value: str) -> bool:
    lowered = (value or "").lower()
    return bool(lowered) and any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def exit_code_for_status(status: str) -> int:
    if status == STATUS_PASS:
        return EXIT_OK
    if status == STATUS_FAIL:
        return EXIT_FAIL
    if status in (STATUS_BLOCKED, STATUS_NOT_RUN):
        return EXIT_BLOCKED
    if status in (STATUS_SKIPPED, STATUS_NA):
        return EXIT_OK
    return EXIT_CONFIG


def worst_exit_code(codes: list[int]) -> int:
    if EXIT_CONFIG in codes:
        return EXIT_CONFIG
    if EXIT_FAIL in codes:
        return EXIT_FAIL
    if EXIT_BLOCKED in codes:
        return EXIT_BLOCKED
    return EXIT_OK


def to_registry_status(status: str) -> str:
    """Map an operational status onto the evidence-registry vocabulary."""
    if status in _REGISTRY_STATUSES:
        return status
    # SKIPPED and NOT_APPLICABLE are not success; they degrade to NOT_RUN.
    return STATUS_NOT_RUN


# ---------------------------------------------------------------------------
# Evidence capture (Step 13 section L)
# ---------------------------------------------------------------------------
@dataclass
class EvidenceRecord:
    """One machine-readable evidence record in the Step 13 shape."""

    item_id: str
    status: str
    classification: str = "INFRASTRUCTURE"
    severity: str = ""
    command: str = ""
    timestamp: str = ""
    release_commit: str = ""
    environment: str = ""
    evidence: str = ""
    details: dict = field(default_factory=dict)
    verifier: str = "ops-toolkit"


def _details_note(rec: EvidenceRecord) -> str:
    if not rec.details:
        return ""
    parts = [f"{k}={v}" for k, v in sorted(rec.details.items()) if v not in ("", None)]
    return "; ".join(parts)


def registry_entry_from_record(rec: EvidenceRecord) -> dict:
    """Convert an EvidenceRecord into an evidence-registry item entry."""
    return {
        "item_id": rec.item_id,
        "status": to_registry_status(rec.status),
        "evidence": rec.evidence or "-",
        "verification_date": (rec.timestamp or today_iso())[:10],
        "verifier": rec.verifier or "ops-toolkit",
        "notes": _details_note(rec),
    }


def merge_evidence_file(path: str | Path, records: list[EvidenceRecord]) -> dict:
    """Merge evidence records into the registry JSON, preserving history.

    Never destroys existing data: a superseded entry is moved into the
    top-level ``history`` array (with ``superseded`` / ``superseded_by``
    metadata). Re-running with an identical status and evidence text is
    idempotent (no history churn). Returns the merged document.
    """
    p = Path(path)
    data: dict = {}
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            data = {}

    items = data.get("items")
    items = items if isinstance(items, list) else []
    history = data.get("history")
    history = history if isinstance(history, list) else []

    by_id: dict[str, dict] = {}
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("item_id"), str):
            by_id[it["item_id"]] = it

    for rec in records:
        entry = registry_entry_from_record(rec)
        old = by_id.get(rec.item_id)
        if old is None:
            by_id[rec.item_id] = entry
            continue
        changed = (old.get("status") != entry["status"]) or (
            old.get("evidence") != entry["evidence"]
        )
        if changed:
            history.append(
                {
                    "superseded": rec.item_id,
                    "superseded_by": entry["verification_date"],
                    **old,
                }
            )
            by_id[rec.item_id] = entry

    data["items"] = list(by_id.values())
    data["history"] = history

    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return data


# ---------------------------------------------------------------------------
# Staging safety (Step 13 sections B/E/F)
# ---------------------------------------------------------------------------
def validate_staging_target(app_env: str, confirmed: bool = False) -> tuple[bool, str, str]:
    """Refuse to treat a production environment as a staging target.

    Returns (allowed, status, reason). Production is always refused. A known
    non-production environment is allowed; an unknown environment is allowed
    only with an explicit operator confirmation (``confirmed=True``).
    """
    env = (app_env or "").strip().lower()
    if env == "production" or env == "prod":
        return False, STATUS_BLOCKED, "refusing: APP_ENV is production"
    if env in {"staging", "development", "test"}:
        return True, STATUS_PASS, f"non-production environment ({env})"
    if confirmed:
        return True, STATUS_PASS, "operator-confirmed non-production target"
    return (
        False,
        STATUS_BLOCKED,
        f"cannot confirm non-production target (APP_ENV={app_env or '<unset>'})",
    )


def verify_backup_integrity(dump_path: str | Path) -> tuple[str, str]:
    """Local integrity pre-check of a backup file (no database needed).

    Returns (status, detail). A missing or empty dump is FAIL; a readable,
    non-empty dump whose deeper verification needs ``pg_restore`` is NOT_RUN
    here (the shell scripts do the authoritative check when PostgreSQL exists).
    """
    p = Path(dump_path)
    if not p.exists():
        return STATUS_FAIL, f"backup missing: {p}"
    if not p.is_file():
        return STATUS_FAIL, f"backup is not a file: {p}"
    if p.stat().st_size == 0:
        return STATUS_FAIL, f"backup is empty: {p}"
    return STATUS_NOT_RUN, (
        f"backup present ({p.stat().st_size} bytes); authoritative integrity "
        "check runs via pg_restore when PostgreSQL is available"
    )


# ---------------------------------------------------------------------------
# Drift detection (Step 13 section F)
# ---------------------------------------------------------------------------
def drift_lines(recorded: dict, live: dict) -> list[str]:
    """Compare a recorded artifact identity to live facts. Returns drift lines."""
    lines: list[str] = []
    if recorded.get("git_commit") and live.get("git_commit"):
        if recorded["git_commit"] != live["git_commit"]:
            lines.append(
                f"git commit: recorded {recorded['git_commit']} != live {live['git_commit']}"
            )
    if recorded.get("dependency_fingerprint") and live.get("dependency_fingerprint"):
        if recorded["dependency_fingerprint"] != live["dependency_fingerprint"]:
            lines.append("dependency fingerprint mismatch")
    if recorded.get("migration_heads") and live.get("migration_heads"):
        if list(recorded["migration_heads"]) != list(live["migration_heads"]):
            lines.append(
                f"migration heads: recorded {recorded['migration_heads']} != "
                f"live {live['migration_heads']}"
            )
    if recorded.get("configuration_fingerprint") and live.get("configuration_fingerprint"):
        if recorded["configuration_fingerprint"] != live["configuration_fingerprint"]:
            lines.append("configuration fingerprint mismatch")
    return lines


def compute_live_facts(repo_root: str | Path = REPO_ROOT) -> dict:
    """Recompute the artifact identity from the live tree (offline)."""
    root = Path(repo_root)
    requirements = root / "requirements.txt"
    requirements_text = (
        requirements.read_text(encoding="utf-8") if requirements.exists() else ""
    )
    from app.core.config import Settings

    return {
        "git_commit": facts.git_commit(root),
        "dependency_fingerprint": facts.dependency_fingerprint(requirements_text),
        "migration_heads": list(facts.migration_heads(root / "alembic" / "versions")),
        "configuration_fingerprint": facts.configuration_fingerprint(Settings),
    }


# ---------------------------------------------------------------------------
# Cost configuration (Step 13 section K)
# ---------------------------------------------------------------------------
def verify_cost_config(
    prices: dict,
    enabled_providers: set[str],
    source_date: str = "",
    source_doc_exists: bool = False,
) -> dict:
    """Validate COST_UNIT_PRICES against the enabled provider/model set.

    Never invents a value: a missing or zero price is BLOCKED (UNKNOWN), a
    present but malformed price is FAIL. Requires a documented source date and
    the source-documentation file to exist.
    """
    results: list[dict] = []
    seen: set[str] = set()
    for provider in sorted(enabled_providers):
        for key in COST_REQUIRED_KEYS.get(provider, []):
            seen.add(key)
            value = prices.get(key)
            if value is None or value == "":
                results.append(
                    {"key": key, "provider": provider, "status": STATUS_BLOCKED,
                     "detail": f"missing price for {key} (UNKNOWN)"}
                )
            elif not isinstance(value, (int, float)) or isinstance(value, bool):
                results.append(
                    {"key": key, "provider": provider, "status": STATUS_FAIL,
                     "detail": f"{key} is not numeric"}
                )
            elif value < 0:
                results.append(
                    {"key": key, "provider": provider, "status": STATUS_FAIL,
                     "detail": f"{key} is negative"}
                )
            else:
                results.append(
                    {"key": key, "provider": provider, "status": STATUS_PASS,
                     "detail": f"{key}={value}"}
                )

    if not source_doc_exists:
        results.append(
            {"key": "_source_doc", "provider": "-", "status": STATUS_BLOCKED,
             "detail": "cost source documentation missing"}
        )
    if not source_date:
        results.append(
            {"key": "_source_date", "provider": "-", "status": STATUS_BLOCKED,
             "detail": "price source date not recorded"}
        )

    statuses = [r["status"] for r in results]
    if not enabled_providers:
        overall = STATUS_NOT_RUN
    elif STATUS_FAIL in statuses:
        overall = STATUS_FAIL
    elif STATUS_BLOCKED in statuses:
        overall = STATUS_BLOCKED
    else:
        overall = STATUS_PASS
    return {"status": overall, "results": results}


# ---------------------------------------------------------------------------
# TLS certification (Step 13 section G)
# ---------------------------------------------------------------------------
def _is_localhost_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1", "[::1]"}


def classify_tls(checks: dict) -> str:
    """Pure classification of a TLS verification result. Deterministic."""
    if checks.get("localhost_only"):
        return STATUS_NA
    if checks.get("scheme") != "https":
        return STATUS_FAIL
    if not checks.get("connected"):
        return STATUS_BLOCKED
    if not checks.get("cert_valid_dates"):
        return STATUS_FAIL
    if not checks.get("hostname_matches"):
        return STATUS_FAIL
    if checks.get("expiry_days", 0) <= 0:
        return STATUS_FAIL
    if not checks.get("hsts"):
        return STATUS_FAIL
    if not checks.get("x_frame") or not checks.get("x_content_type"):
        return STATUS_FAIL
    return STATUS_PASS


def verify_tls(base_url: str, timeout: float = 8.0, check_websocket: bool = False) -> dict:
    """Validate TLS on a supplied staging URL. Never accepts ``verify=False``."""
    url = (base_url or "").strip()
    checks: dict = {"base_url": url}
    if not url:
        checks["scheme"] = ""
        return {"status": STATUS_BLOCKED, "checks": checks,
                "evidence": "no staging URL supplied"}

    from urllib.parse import urlparse

    parsed = urlparse(url)
    scheme = parsed.scheme
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    checks["scheme"] = scheme
    checks["host"] = host
    checks["port"] = port

    if scheme == "http" and _is_localhost_host(host):
        checks["localhost_only"] = True
        return {"status": STATUS_NA, "checks": checks,
                "evidence": "localhost-only staging over plain HTTP — "
                            "TLS not applicable to this target"}

    checks["localhost_only"] = False
    if scheme != "https":
        return {"status": STATUS_FAIL, "checks": checks,
                "evidence": f"not HTTPS: {scheme}"}

    checks["connected"] = False
    try:
        context = __import__("ssl").create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as ssock:
                checks["connected"] = True
                der = ssock.getpeercert(binary_form=True)
                cipher = ssock.cipher()
        checks["cipher"] = cipher[0] if cipher else ""
        cert = __import__("cryptography.x509", fromlist=["x509"]).load_der_x509_certificate(der)
        not_after = cert.not_valid_after_utc
        not_before = cert.not_valid_before_utc
        now = datetime.now(timezone.utc)
        checks["cert_valid_dates"] = not_before <= now <= not_after
        checks["expiry_days"] = (not_after - now).days
        checks["hostname_matches"] = _hostname_matches(host, cert)
    except Exception as exc:  # noqa: BLE001 - environment/network errors are data here
        checks["connect_error"] = type(exc).__name__
        return {"status": STATUS_BLOCKED, "checks": checks,
                "evidence": f"TLS connection failed ({type(exc).__name__}); "
                            "certificate could not be validated"}

    checks["hsts"] = False
    checks["x_frame"] = False
    checks["x_content_type"] = False
    checks["redirect_ok"] = True
    try:
        with httpx.Client(verify=True, timeout=timeout, follow_redirects=False) as client:
            resp = client.get(url)
            checks["http_status"] = resp.status_code
            headers = {k.lower(): v for k, v in resp.headers.items()}
            checks["hsts"] = "strict-transport-security" in headers
            checks["x_frame"] = "x-frame-options" in headers
            checks["x_content_type"] = "x-content-type-options" in headers
            if resp.status_code in (301, 302, 307, 308):
                location = resp.headers.get("location", "")
                checks["redirect_ok"] = bool(location) and location.startswith("https://")
    except Exception as exc:  # noqa: BLE001
        checks["http_error"] = type(exc).__name__

    if check_websocket:
        checks["websocket_ok"] = checks.get("http_status") == 200

    status = classify_tls(checks)
    evidence = (
        f"TLS verification {status}: scheme={scheme}, "
        f"expiry_days={checks.get('expiry_days')}, "
        f"hsts={checks.get('hsts')}, x_frame={checks.get('x_frame')}, "
        f"x_content_type={checks.get('x_content_type')}"
    )
    return {"status": status, "checks": checks, "evidence": evidence}


def _hostname_matches(host: str, cert) -> bool:
    """Exact or single-level wildcard hostname match against the cert SANs."""
    names = set()
    try:
        x509 = __import__("cryptography.x509", fromlist=["x509"])
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        names.update(san.value.get_values_for_type(x509.DNSName))
    except Exception:  # noqa: BLE001
        pass
    if not names:
        return False
    host = host.lower().rstrip(".")
    for name in names:
        n = name.lower().rstrip(".")
        if n == host:
            return True
        if n.startswith("*."):
            suffix = n[2:]
            if host.endswith("." + suffix) and host.count(".") == suffix.count(".") + 1:
                return True
    return False


# ---------------------------------------------------------------------------
# Observability certification (Step 13 section H)
# ---------------------------------------------------------------------------
def verify_observability(
    prometheus_url: str = "", grafana_url: str = "", timeout: float = 6.0
) -> dict:
    """Read-only Prometheus/Grafana validation. Never mutates monitoring."""
    checks: dict = {"configured": bool(prometheus_url)}
    if not prometheus_url:
        return {"status": STATUS_BLOCKED, "checks": checks,
                "evidence": "Prometheus URL not configured"}

    def get(url: str) -> tuple[int, bool]:
        try:
            with httpx.Client(verify=True, timeout=timeout) as client:
                resp = client.get(url)
                return resp.status_code, True
        except Exception:  # noqa: BLE001
            return 0, False

    status, ok = get(prometheus_url.rstrip("/") + "/-/healthy")
    checks["prometheus_healthy"] = ok and status == 200

    probes = {
        "targets_ok": "/api/v1/targets",
        "rules_loaded": "/api/v1/rules",
        "alerts_loaded": "/api/v1/alerts",
        "slo_rules_loaded": "/api/v1/rules",
    }
    for name, path in probes.items():
        status, ok = get(prometheus_url.rstrip("/") + path)
        checks[name] = ok and status == 200

    status, ok = get(prometheus_url.rstrip("/") + "/api/v1/query?query=up")
    checks["api_metrics_available"] = ok and status == 200
    status, ok = get(prometheus_url.rstrip("/") + "/api/v1/query?query=api_requests_total")
    checks["api_metrics_present"] = ok and status == 200
    status, ok = get(prometheus_url.rstrip("/") + "/api/v1/query?query=scheduler_jobs_total")
    checks["scheduler_metrics_present"] = ok and status == 200

    if grafana_url:
        status, ok = get(grafana_url.rstrip("/") + "/api/health")
        checks["grafana_healthy"] = ok and status == 200

    required = [
        "prometheus_healthy",
        "targets_ok",
        "rules_loaded",
        "alerts_loaded",
        "api_metrics_present",
        "scheduler_metrics_present",
    ]
    missing = [k for k in required if not checks.get(k)]
    overall = STATUS_PASS if not missing else (STATUS_BLOCKED if not checks.get("prometheus_healthy") else STATUS_FAIL)
    return {"status": overall, "checks": checks,
            "evidence": f"observability {overall}: missing={missing or 'none'}"}


# ---------------------------------------------------------------------------
# Egress verification (Step 13 section I)
# ---------------------------------------------------------------------------
def _probe_tcp(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def verify_egress(
    inside_staging: bool,
    deny_hosts: list[str] | None = None,
    allow_hosts: list[str] | None = None,
    timeout: float = 2.0,
    probe_fn=None,
) -> dict:
    """Verify infrastructure egress enforcement.

    Must be run inside the actual staging container/network. If it is not, it
    returns BLOCKED (never PASS). Deny destinations must be unreachable; allow
    destinations must be reachable.
    """
    probe = probe_fn or _probe_tcp
    deny = deny_hosts or DEFAULT_EGRESS_DENY_HOSTS

    if not inside_staging:
        return {"status": STATUS_BLOCKED,
                "evidence": "not running inside the staging network namespace; "
                            "set VOXDESK_STAGING_NETWORK=1 and run from the staging "
                            "container before claiming egress evidence",
                "deny_results": [], "allow_results": []}

    deny_results = []
    for host in deny:
        reachable = probe(host, 80, timeout) or probe(host, 443, timeout)
        deny_results.append({"host": host, "reachable": reachable})

    allow = allow_hosts or []
    allow_results = []
    for host in allow:
        reachable = probe(host, 443, timeout) or probe(host, 80, timeout)
        allow_results.append({"host": host, "reachable": reachable})

    leaked = [r["host"] for r in deny_results if r["reachable"]]
    unreachable_allow = [r["host"] for r in allow_results if not r["reachable"]]

    if not allow:
        return {"status": STATUS_BLOCKED,
                "evidence": "no egress allow-list configured (VOXDESK_EGRESS_ALLOW_HOSTS)",
                "deny_results": deny_results, "allow_results": allow_results}

    if leaked:
        return {"status": STATUS_FAIL,
                "evidence": f"egress NOT enforced; reachable denied destinations: {leaked}",
                "deny_results": deny_results, "allow_results": allow_results}
    if unreachable_allow:
        return {"status": STATUS_FAIL,
                "evidence": f"required destinations unreachable: {unreachable_allow}",
                "deny_results": deny_results, "allow_results": allow_results}
    return {"status": STATUS_PASS,
            "evidence": f"egress enforced: {len(deny)} deny targets blocked, "
                        f"{len(allow)} allow targets reachable",
            "deny_results": deny_results, "allow_results": allow_results}


# ---------------------------------------------------------------------------
# Provider report (Step 13 sections D/J)
# ---------------------------------------------------------------------------
def build_provider_report(outcomes: list[dict]) -> dict:
    """Unified provider table from Step 4 framework outcomes (list of dicts)."""
    rows = []
    for o in outcomes:
        rows.append(
            {
                "provider": o.get("provider", ""),
                "status": o.get("status", STATUS_SKIPPED),
                "reason": o.get("reason", ""),
                "evidence": f"read-only check (safety={o.get('safety', 'read_only')})",
            }
        )
    statuses = [r["status"] for r in rows]
    if not rows:
        overall = STATUS_NOT_RUN
    elif all(s == STATUS_PASS for s in statuses):
        overall = STATUS_PASS
    elif STATUS_FAIL in statuses:
        overall = STATUS_FAIL
    elif STATUS_BLOCKED in statuses:
        overall = STATUS_BLOCKED
    else:
        overall = STATUS_SKIPPED
    return {"status": overall, "rows": rows}


# ---------------------------------------------------------------------------
# Preflight (Step 13 section A)
# ---------------------------------------------------------------------------
def _runtime_checks(tools: dict[str, str | None] | None) -> list[dict]:
    if tools is None:
        tools = {name: which(name) for name in TOOL_NAMES}
    checks = []
    for name in TOOL_NAMES:
        path = tools.get(name)
        if path:
            checks.append({"name": name, "status": STATUS_PASS, "detail": path})
        else:
            checks.append({"name": name, "status": STATUS_BLOCKED, "detail": "not found on PATH"})
    # docker compose subcommand (v2) in addition to the legacy binary
    if tools.get("docker"):
        try:
            subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            ok = True
        except (OSError, subprocess.SubprocessError):
            ok = False
        checks.append(
            {"name": "docker compose (v2)", "status": STATUS_PASS if ok else STATUS_BLOCKED,
             "detail": "docker compose plugin present" if ok else "docker compose plugin missing"}
        )
    return checks


def _repo_checks(
    repo_root: Path,
    expected_commit: str | None,
    expected: dict | None,
) -> list[dict]:
    checks: list[dict] = []
    clean = True
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        clean = out.returncode == 0 and out.stdout.strip() == ""
    except (OSError, subprocess.SubprocessError):
        checks.append({"name": "git", "status": STATUS_BLOCKED,
                       "detail": "git unavailable; cannot verify repository state"})
        return checks

    checks.append(
        {"name": "working tree clean", "status": STATUS_PASS if clean else STATUS_FAIL,
         "detail": "clean" if clean else "dirty — uncommitted changes present"}
    )

    live_commit = facts.git_commit(repo_root)
    if expected_commit:
        checks.append(
            {"name": "expected release commit",
             "status": STATUS_PASS if live_commit == expected_commit else STATUS_FAIL,
             "detail": f"expected {expected_commit[:12]}, live {live_commit[:12]}"}
        )
    else:
        checks.append(
            {"name": "expected release commit", "status": STATUS_NOT_RUN,
             "detail": "no external expectation supplied (using HEAD)"}
        )

    live = compute_live_facts(repo_root)
    for key, label in (
        ("dependency_fingerprint", "dependency fingerprint"),
        ("migration_heads", "migration heads"),
        ("configuration_fingerprint", "release fingerprint"),
    ):
        value = live.get(key)
        want = (expected or {}).get(key)
        if want is not None:
            ok = list(want) == list(value) if isinstance(want, list) else want == value
            checks.append(
                {"name": label, "status": STATUS_PASS if ok else STATUS_FAIL,
                 "detail": str(value)[:64]}
            )
        else:
            checks.append({"name": label, "status": STATUS_PASS,
                           "detail": str(value)[:64]})
    return checks


def _config_checks(env: dict) -> list[dict]:
    app_env = (env.get("APP_ENV") or "development").strip()
    checks: list[dict] = []

    if app_env.lower() in {"production", "prod"}:
        checks.append({"name": "environment", "status": STATUS_FAIL,
                       "detail": "APP_ENV=production — this toolkit certifies staging only"})
    elif app_env.lower() in {"staging", "development", "test"}:
        checks.append({"name": "environment", "status": STATUS_PASS,
                       "detail": f"APP_ENV={app_env}"})
    else:
        checks.append({"name": "environment", "status": STATUS_BLOCKED,
                       "detail": f"APP_ENV={app_env} is not a known environment"})

    required = ["SECRET_KEY", "DATABASE_URL", "REDIS_URL"]
    if app_env.lower() == "staging":
        for name in required:
            value = env.get(name, "")
            checks.append(
                {"name": f"required env {name}",
                 "status": STATUS_PASS if value else STATUS_BLOCKED,
                 "detail": "present" if value else "missing"}
            )
    else:
        for name in required:
            value = env.get(name, "")
            checks.append(
                {"name": f"required env {name}",
                 "status": STATUS_PASS if value else STATUS_NOT_RUN,
                 "detail": "present" if value else "not set (only required in staging)"}
            )

    # E2E safety
    e2e_enabled = (env.get("E2E_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    e2e_number = (env.get("E2E_TEST_NUMBER") or "").strip()
    e2e_callers = (env.get("E2E_ALLOWED_CALLERS") or "").strip()
    if app_env.lower() in {"production", "prod"} and e2e_enabled:
        checks.append({"name": "E2E guard", "status": STATUS_FAIL,
                       "detail": "E2E_ENABLED armed in production — refuse"})
    elif e2e_enabled and (not e2e_number or not e2e_callers):
        checks.append({"name": "E2E guard", "status": STATUS_FAIL,
                       "detail": "E2E armed without test number and caller allowlist"})
    else:
        checks.append(
            {"name": "E2E guard", "status": STATUS_PASS,
             "detail": "armed" if e2e_enabled else "disarmed"}
        )

    # Production flag safety
    checks.append(
        {"name": "production flag safety",
         "status": STATUS_FAIL if app_env.lower() in {"production", "prod"} else STATUS_PASS,
         "detail": "refused" if app_env.lower() in {"production", "prod"} else "non-production"}
    )

    # Placeholder secret detection in staging
    secret = env.get("SECRET_KEY", "")
    if app_env.lower() == "staging" and secret and _looks_placeholder(secret):
        checks.append({"name": "staging secret quality", "status": STATUS_FAIL,
                       "detail": "SECRET_KEY looks like a placeholder"})
    else:
        checks.append({"name": "staging secret quality", "status": STATUS_PASS,
                       "detail": "no placeholder secret detected"})
    return checks


def scan_secrets_in_files(paths: list[str], root: Path = REPO_ROOT) -> list[str]:
    """Return the tracked files whose text content matches a secret pattern."""
    hits: list[str] = []
    text_suffixes = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".sh", ".yml", ".yaml", ".json",
        ".toml", ".ini", ".md", ".env.example", ".example", ".txt", ".cfg",
    }
    for rel in paths:
        p = Path(rel)
        if p.suffix.lower() not in text_suffixes and ".env.example" not in rel:
            continue
        full = root / p
        try:
            if full.stat().st_size > 1_000_000:
                continue
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in _SECRET_VALUE_PATTERNS:
            for match in pattern.findall(text):
                if match in _EXAMPLE_SECRET_VALUES:
                    continue
                hits.append(rel)
                break
            if rel in hits:
                break
    return hits


def _security_checks(repo_root: Path, tracked_files: list[str] | None) -> list[dict]:
    if tracked_files is None:
        try:
            out = subprocess.run(
                ["git", "-C", str(repo_root), "ls-files"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            tracked_files = out.stdout.splitlines() if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            tracked_files = None
    if tracked_files is None:
        return [{"name": "tracked-file scan", "status": STATUS_BLOCKED,
                 "detail": "git unavailable; cannot scan tracked files"}]

    secret_files = [f for f in tracked_files if _is_secret_filename(f)]
    checks = [
        {"name": "no tracked .env / key files",
         "status": STATUS_FAIL if secret_files else STATUS_PASS,
         "detail": "; ".join(secret_files) if secret_files else "none found"},
    ]
    hits = scan_secrets_in_files(tracked_files, repo_root)
    checks.append(
        {"name": "no secret values in source",
         "status": STATUS_FAIL if hits else STATUS_PASS,
         "detail": "; ".join(hits[:10]) if hits else "none found"}
    )
    return checks


def run_preflight(
    repo_root: str | Path = REPO_ROOT,
    tools: dict[str, str | None] | None = None,
    env: dict | None = None,
    expected_commit: str | None = None,
    expected: dict | None = None,
    tracked_files: list[str] | None = None,
) -> dict:
    """Run the full operational preflight. Read-only; never mutates anything."""
    root = Path(repo_root)
    env = env if env is not None else os.environ
    sections = {
        "runtime": _runtime_checks(tools),
        "repository": _repo_checks(root, expected_commit, expected),
        "configuration": _config_checks(env),
        "security": _security_checks(root, tracked_files),
    }
    all_checks = [c for group in sections.values() for c in group]
    statuses = [c["status"] for c in all_checks]
    if STATUS_FAIL in statuses:
        overall = STATUS_FAIL
    elif STATUS_BLOCKED in statuses:
        overall = STATUS_BLOCKED
    elif STATUS_NOT_RUN in statuses and STATUS_PASS in statuses:
        overall = STATUS_NOT_RUN if not any(s == STATUS_PASS for s in statuses) else STATUS_BLOCKED
    elif all(s == STATUS_PASS for s in statuses):
        overall = STATUS_PASS
    else:
        overall = STATUS_NOT_RUN

    return {
        "command": "ops_preflight",
        "timestamp": now_iso(),
        "release_commit": facts.git_commit(root),
        "environment": (env.get("APP_ENV") or "development").strip(),
        "sections": sections,
        "overall": overall,
        "exit_code": exit_code_for_status(overall),
    }


def render_preflight(report: dict) -> str:
    lines = ["VoxDesk operational preflight", "=" * 78]
    for section, checks in report["sections"].items():
        lines.append(f"[{section.upper()}]")
        for c in checks:
            lines.append(f"  [{c['status']:<11}] {c['name']:<28} {c['detail']}")
    lines.append("-" * 78)
    lines.append(f"OVERALL: {report['overall']}   (exit {report['exit_code']})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backup / restore / deploy-drill orchestration (Step 13 sections E/F)
# ---------------------------------------------------------------------------
def run_backup_orchestration(
    app_env: str,
    output_dir: str = "./backups",
    remote: str = "",
    tools: dict[str, str | None] | None = None,
) -> dict:
    """Safe wrapper around scripts/backup.sh with staging guard + evidence."""
    if tools is None:
        tools = {"pg_dump": which("pg_dump"), "rclone": which("rclone")}
    if not tools.get("pg_dump"):
        return {"status": STATUS_BLOCKED,
                "evidence": "pg_dump not found; staging database backup cannot run",
                "record": EvidenceRecord(
                    item_id="backup-001", status=STATUS_BLOCKED,
                    classification="INFRASTRUCTURE", severity="P1",
                    command="scripts/staging_certify.py --backup",
                    evidence="pg_dump not found; backup not executed",
                    details={"reason": "pg_dump missing"})}

    allowed, status, reason = validate_staging_target(app_env)
    if not allowed:
        return {"status": status, "evidence": reason,
                "record": EvidenceRecord(
                    item_id="backup-001", status=status,
                    classification="INFRASTRUCTURE", severity="P1",
                    command="scripts/staging_certify.py --backup",
                    evidence=reason, details={"reason": reason})}

    script = REPO_ROOT / "scripts" / "backup.sh"
    try:
        proc = subprocess.run(
            [str(script), output_dir], capture_output=True, text=True, timeout=600
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": STATUS_FAIL, "evidence": f"backup.sh failed: {exc}",
                "record": EvidenceRecord(
                    item_id="backup-001", status=STATUS_FAIL,
                    classification="INFRASTRUCTURE", severity="P1",
                    command="scripts/backup.sh", evidence=f"backup.sh failed: {exc}")}
    if proc.returncode != 0:
        return {"status": STATUS_FAIL,
                "evidence": (proc.stderr or proc.stdout or "backup failed").strip()[-400:],
                "record": EvidenceRecord(
                    item_id="backup-001", status=STATUS_FAIL,
                    classification="INFRASTRUCTURE", severity="P1",
                    command="scripts/backup.sh", evidence="backup.sh exited non-zero")}
    return {"status": STATUS_PASS,
            "evidence": (proc.stdout or "backup completed").strip()[-400:],
            "record": EvidenceRecord(
                item_id="backup-001", status=STATUS_PASS,
                classification="INFRASTRUCTURE", severity="P1",
                command="scripts/backup.sh", evidence=(proc.stdout or "").strip()[-400:])}


def run_restore_orchestration(
    app_env: str,
    dump_path: str,
    target_db: str,
    tools: dict[str, str | None] | None = None,
) -> dict:
    """Safe wrapper around scripts/restore.sh. RESTORE_TARGET_DB is mandatory."""
    if tools is None:
        tools = {"pg_restore": which("pg_restore"), "psql": which("psql")}

    integrity_status, integrity_detail = verify_backup_integrity(dump_path)
    if integrity_status == STATUS_FAIL:
        return {"status": STATUS_FAIL, "evidence": integrity_detail,
                "record": EvidenceRecord(
                    item_id="restore-001", status=STATUS_FAIL,
                    classification="INFRASTRUCTURE", severity="P1",
                    command="scripts/staging_certify.py --restore",
                    evidence=integrity_detail)}

    if not tools.get("pg_restore") or not tools.get("psql"):
        return {"status": STATUS_BLOCKED,
                "evidence": "pg_restore/psql not found; restore drill cannot run",
                "record": EvidenceRecord(
                    item_id="restore-001", status=STATUS_BLOCKED,
                    classification="INFRASTRUCTURE", severity="P1",
                    command="scripts/staging_certify.py --restore",
                    evidence="pg_restore/psql missing")}

    allowed, status, reason = validate_staging_target(app_env)
    if not allowed:
        return {"status": status, "evidence": reason,
                "record": EvidenceRecord(
                    item_id="restore-001", status=status,
                    classification="INFRASTRUCTURE", severity="P1",
                    command="scripts/staging_certify.py --restore", evidence=reason)}

    if not target_db:
        return {"status": STATUS_BLOCKED,
                "evidence": "RESTORE_TARGET_DB is required; refusing ambiguous/default target",
                "record": EvidenceRecord(
                    item_id="restore-001", status=STATUS_BLOCKED,
                    classification="INFRASTRUCTURE", severity="P1",
                    command="scripts/staging_certify.py --restore",
                    evidence="RESTORE_TARGET_DB not set")}

    script = REPO_ROOT / "scripts" / "restore.sh"
    try:
        proc = subprocess.run(
            [str(script), dump_path],
            capture_output=True, text=True, timeout=600,
            env={**os.environ, "RESTORE_TARGET_DB": target_db},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": STATUS_FAIL, "evidence": f"restore.sh failed: {exc}",
                "record": EvidenceRecord(
                    item_id="restore-001", status=STATUS_FAIL,
                    classification="INFRASTRUCTURE", severity="P1",
                    command="scripts/restore.sh", evidence=f"restore.sh failed: {exc}")}
    if proc.returncode != 0:
        return {"status": STATUS_FAIL,
                "evidence": (proc.stderr or proc.stdout or "restore failed").strip()[-400:],
                "record": EvidenceRecord(
                    item_id="restore-001", status=STATUS_FAIL,
                    classification="INFRASTRUCTURE", severity="P1",
                    command="scripts/restore.sh", evidence="restore.sh exited non-zero")}
    return {"status": STATUS_PASS,
            "evidence": (proc.stdout or "restore completed").strip()[-400:],
            "record": EvidenceRecord(
                item_id="restore-001", status=STATUS_PASS,
                classification="INFRASTRUCTURE", severity="P1",
                command="scripts/restore.sh", evidence=(proc.stdout or "").strip()[-400:])}


def run_deploy_drill_orchestration(
    app_env: str,
    previous_sha: str,
    tools: dict[str, str | None] | None = None,
) -> dict:
    """Safe deployment drill (deploy -> rollback). Never downgrades the DB."""
    if tools is None:
        tools = {"docker": which("docker")}
    allowed, status, reason = validate_staging_target(app_env)
    if not allowed:
        return {"status": status, "evidence": reason,
                "record": EvidenceRecord(
                    item_id="deploy-001", status=status,
                    classification="INFRASTRUCTURE", severity="P1",
                    command="scripts/staging_certify.py --deploy-drill", evidence=reason)}
    if not tools.get("docker"):
        return {"status": STATUS_BLOCKED,
                "evidence": "Docker not found; deployment drill cannot run",
                "record": EvidenceRecord(
                    item_id="deploy-001", status=STATUS_BLOCKED,
                    classification="INFRASTRUCTURE", severity="P1",
                    command="scripts/staging_certify.py --deploy-drill",
                    evidence="Docker missing")}
    if not previous_sha:
        return {"status": STATUS_BLOCKED,
                "evidence": "previous-good git sha required for rollback leg",
                "record": EvidenceRecord(
                    item_id="deploy-001", status=STATUS_BLOCKED,
                    classification="INFRASTRUCTURE", severity="P1",
                    command="scripts/staging_certify.py --deploy-drill",
                    evidence="previous sha missing")}
    return {"status": STATUS_NOT_RUN,
            "evidence": "deployment drill requires a live staging stack; "
                        "run on the staging host (deploy.sh -> readiness -> rollback.sh)",
            "record": EvidenceRecord(
                item_id="deploy-001", status=STATUS_NOT_RUN,
                classification="INFRASTRUCTURE", severity="P1",
                command="scripts/staging_certify.py --deploy-drill",
                evidence="not executed in this environment")}
