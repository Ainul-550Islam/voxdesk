"""Step 7 — deterministic validation of the observability configs and the
structured-logging pipeline.

These tests turn the "configs are real and consistent" part of the quality
gate into assertions that run in CI, not things a human has to re-check:
every alert has a severity and a runbook, every PromQL expression references a
metric the app actually exports (or a histogram bucket / recording rule /
built-in), the SLO recording rules exist, and the log pipeline emits
machine-readable JSON with secrets already redacted.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

import prometheus_client
from app.core import metrics  # noqa: F401  (registers the HTTP metrics)
from app.core import observability  # noqa: F401  (registers the metrics)
from app.core.logging import _choose_renderer, redact_secrets

REPO = Path(__file__).resolve().parent.parent

#: Every metric family the application actually registers (authoritative, from
#: the live Prometheus default registry). Counter/histogram *families* are
#: named without their export suffix (`voxdesk_calls` exports
#: `voxdesk_calls_total`), so the suffix is handled in `_metric_referenced_ok`.
_DEFINED = {m.name for m in prometheus_client.REGISTRY.collect()}

#: Prometheus built-ins / functions that legitimately appear in expressions
#: but are not application metrics.
_ALLOWED_BUILTINS = {"up", "time"}

_METRIC_SUFFIXES = ("_total", "_bucket", "_sum", "_count")


def _metric_referenced_ok(name: str) -> bool:
    base = name.split("{")[0]
    if ":" in base:  # recording rule like voxdesk:slo:availability:5m
        return True
    if base in _ALLOWED_BUILTINS:
        return True
    if base in _DEFINED:
        return True
    for suffix in _METRIC_SUFFIXES:
        if base.endswith(suffix) and base[: -len(suffix)] in _DEFINED:
            return True
    return False


def _expr_metric_tokens(expr: str) -> set[str]:
    return set(re.findall(r"voxdesk[a-zA-Z0-9_:]+", expr))


# ------------------------------------------------------------ alerts.yml ---

def test_alerts_every_rule_has_severity_and_runbook():
    alerts = yaml.safe_load((REPO / "observability" / "alerts.yml").read_text())
    rules = alerts["groups"][0]["rules"]
    assert rules, "alert rules must not be empty"
    for rule in rules:
        assert rule["labels"]["severity"] in {"critical", "warning", "info"}, rule["alert"]
        assert rule["annotations"]["summary"], rule["alert"]
        assert rule["annotations"]["runbook"].startswith("docs/"), rule["alert"]


def test_alerts_reference_only_real_metrics():
    alerts = yaml.safe_load((REPO / "observability" / "alerts.yml").read_text())
    bad: list[tuple[str, str]] = []
    for rule in alerts["groups"][0]["rules"]:
        for token in _expr_metric_tokens(rule["expr"]):
            if not _metric_referenced_ok(token):
                bad.append((rule["alert"], token))
    assert not bad, f"alerts reference unknown metrics: {bad}"


# ------------------------------------------------------------- slos.yml ---

def test_slo_recording_rules_exist_and_are_well_formed():
    slos = yaml.safe_load((REPO / "observability" / "slos.yml").read_text())
    rules = slos["groups"][0]["rules"]
    assert rules
    records = {r["record"] for r in rules}
    assert "voxdesk:slo:availability:5m" in records
    assert "voxdesk:slo:availability:30d" in records
    for rule in rules:
        assert rule.get("record", "").startswith("voxdesk:slo:")
        assert rule.get("expr")


def test_slo_expressions_reference_only_real_metrics():
    slos = yaml.safe_load((REPO / "observability" / "slos.yml").read_text())
    bad: list[str] = []
    for rule in slos["groups"][0]["rules"]:
        for token in _expr_metric_tokens(rule["expr"]):
            if not _metric_referenced_ok(token):
                bad.append(token)
    assert not bad, f"slo rules reference unknown metrics: {bad}"


# ---------------------------------------------------------- prometheus.yml ---

def test_prometheus_scrapes_both_processes_and_loads_rules():
    cfg = yaml.safe_load((REPO / "observability" / "prometheus.yml").read_text())
    rule_files = cfg["rule_files"]
    assert "/etc/prometheus/alerts.yml" in rule_files
    assert "/etc/prometheus/slos.yml" in rule_files
    jobs = [sc["job_name"] for sc in cfg["scrape_configs"]]
    assert "voxdesk-api" in jobs
    assert "voxdesk-scheduler" in jobs


# ------------------------------------------------------- grafana dashboard ---

def test_dashboard_is_valid_and_references_real_metrics():
    dash = json.loads(
        (REPO / "observability" / "grafana" / "dashboards" / "voxdesk.json").read_text()
    )
    assert dash["uid"] == "voxdesk-overview"
    bad: list[str] = []
    for panel in dash["panels"]:
        for target in panel.get("targets", []):
            for token in _expr_metric_tokens(target.get("expr", "")):
                if not _metric_referenced_ok(token):
                    bad.append(token)
    assert not bad, f"dashboard references unknown metrics: {bad}"


# ------------------------------------------------------- structured logging ---

def test_json_renderer_produces_machine_readable_record():
    renderer = _choose_renderer("json", True)
    out = renderer(None, "info", {"event": "login", "n": 3, "llm_tokens": 5})
    parsed = json.loads(out)
    assert parsed["event"] == "login"
    assert parsed["llm_tokens"] == 5
    assert parsed["n"] == 3


def test_log_pipeline_redacts_before_rendering():
    """The record the renderer receives has already been stripped of secrets —
    redaction runs earlier in the processor chain than the JSON renderer."""
    cleaned = redact_secrets(None, "info", {
        "event": "webhook", "token": "sekret", "authorization": "Bearer x",
        "llm_tokens": 5,
    })
    renderer = _choose_renderer("json", True)
    parsed = json.loads(renderer(None, "info", cleaned))
    assert parsed["token"] == "***"
    assert parsed["authorization"] == "***"
    assert parsed["llm_tokens"] == 5
