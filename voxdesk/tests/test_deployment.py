"""Step 8 — deployment & disaster-recovery invariants.

Every test here is deterministic and requires no Docker, no live Postgres and
no network: they inspect the repository's configuration, scripts and source as
the evidence they are. The migration-chain test parses the Alembic revisions
themselves; the compose/Caddy/Dockerfile tests parse the files as YAML/text;
the Redis tests exercise the degradation path directly. Where a real runtime
check is possible (a live Postgres, a running stack) it is a separate,
opt-in validation — never assumed here.
"""
from __future__ import annotations

import base64
import os
import re
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings

REPO = Path(__file__).resolve().parent.parent


def _valid_prod(**overrides) -> Settings:
    """A production Settings that passes every non-test production rule, so a
    single override can be asserted against cleanly. All values are fake."""
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    values = dict(
        app_env="production",
        public_base_url="https://app.example.com",
        cors_origins="https://app.example.com",
        rate_limit_enabled=True,
        log_level="INFO",
        secret_key="a" * 48,
        jwt_secret="b" * 48,
        twilio_account_sid="AC" + "0" * 32,
        twilio_auth_token="c" * 32,
        twilio_phone_number="+15550001111",
        deepgram_api_key="d" * 32,
        openai_api_key="sk-" + "e" * 32,
        elevenlabs_api_key="f" * 32,
        crm_encryption_keys=f"k1:{key}",
        knowledge_embedding_provider="openai",
        knowledge_embedding_model="text-embedding-3-small",
        knowledge_embedding_dimensions=1536,
        billing_provider="manual",
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _staging(**overrides) -> Settings:
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    values = dict(
        app_env="staging",
        public_base_url="https://staging.example.com",
        secret_key="s" * 48,
        jwt_secret="t" * 48,
        crm_encryption_keys=f"k1:{key}",
        rate_limit_enabled=False,
        e2e_enabled=False,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ----------------------------------------------------------- (1)(2)(3) config ---

def test_production_rejects_e2e_mode():
    s = _valid_prod(e2e_enabled=True, e2e_test_number="+15550001111",
                    e2e_allowed_callers="+15550001111")
    problems = s.validate_security()
    assert any("E2E_ENABLED" in p for p in problems)


def test_production_rejects_rate_limit_off():
    s = _valid_prod(rate_limit_enabled=False)
    problems = s.validate_security()
    assert any("RATE_LIMIT_ENABLED" in p for p in problems)


def test_production_rejects_debug_log_level():
    s = _valid_prod(log_level="DEBUG")
    problems = s.validate_security()
    assert any("DEBUG" in p for p in problems)


def test_production_rejects_placeholder_secrets():
    s = _valid_prod(twilio_auth_token="xxxxxxxxxxxx")
    problems = s.validate_security()
    assert any("TWILIO_AUTH_TOKEN" in p for p in problems)


def test_production_rejects_development_cors_origins():
    s = _valid_prod(cors_origins="http://localhost:5173,https://app.example.com")
    problems = s.validate_security()
    assert any("https" in p and "localhost" in p for p in problems)


def test_production_rejects_localhost_base_url():
    s = _valid_prod(public_base_url="https://localhost:8000")
    problems = s.validate_security()
    assert any("localhost" in p for p in problems)


def test_fully_configured_production_is_clean():
    assert _valid_prod().validate_security() == []


def test_unknown_app_env_is_rejected():
    s = Settings(_env_file=None, app_env="qa")
    problems = s.validate_security()
    assert any("APP_ENV" in p for p in problems)


def test_staging_is_not_production():
    s = _staging()
    assert s.is_staging is True
    assert s.is_production is False
    assert s.uses_https is True


def test_staging_accepts_rate_limit_off_and_e2e_disarmed():
    s = _staging(rate_limit_enabled=False, e2e_enabled=False)
    problems = s.validate_security()
    assert not any("RATE_LIMIT_ENABLED" in p for p in problems)
    assert not any("E2E_ENABLED must not be set in production" in p for p in problems)


def test_secure_cookie_is_scheme_driven():
    assert Settings(_env_file=None, public_base_url="https://x.example.com").uses_https
    assert not Settings(_env_file=None, public_base_url="http://localhost:8000").uses_https


# ------------------------------------------------------- (4)(5) health endpoints ---

def test_main_defines_liveness_and_readiness():
    src = (REPO / "app" / "main.py").read_text()
    assert '@app.get("/health")' in src
    assert '@app.get("/health/ready")' in src
    assert "health_check.readiness" in src


def test_staging_does_not_run_create_all():
    src = (REPO / "app" / "main.py").read_text()
    # The dev/test-only schema bootstrap must exclude staging.
    assert '"development", "test"' in src


# ---------------------------------------------- (6) websocket / proxy assumptions ---

def test_caddy_preserves_websocket_and_adds_security():
    text = (REPO / "Caddyfile").read_text()
    assert "reverse_proxy api:8000" in text
    assert "admin off" in text
    assert "Strict-Transport-Security" in text
    assert "25MB" in text
    # Nothing may disable the WebSocket upgrade the voice stream depends on.
    assert "header_upgrade" not in text.lower()


def test_entrypoint_trusts_proxy_headers():
    text = (REPO / "scripts" / "entrypoint.sh").read_text()
    assert "--proxy-headers" in text
    assert "scripts/migrate.py" in text


# ------------------------------------------------------------ (7) migrations ---

def test_migration_chain_is_linear_with_no_gaps():
    versions = sorted(
        p for p in (REPO / "alembic" / "versions").iterdir() if p.suffix == ".py"
    )
    ids: dict[str, str | None] = {}
    for path in versions:
        text = path.read_text()
        rev = re.search(r'^revision = "(.+)"', text, re.M).group(1)
        down = re.search(r'^down_revision = (.+)$', text, re.M).group(1).strip()
        ids[rev] = None if down == "None" else down.strip('"')
    assert ids, "no migrations found"
    # Exactly one head: the revision nobody points down_revision at.
    heads = [r for r in ids if r not in {d for d in ids.values() if d}]
    assert heads == ["0011_side_effect_exactly_once"]
    # Exactly one base (down_revision None), and a single linear walk.
    bases = [r for r, d in ids.items() if d is None]
    assert bases == ["0001_baseline"]
    current: str | None = heads[0]
    visited = 0
    while current is not None:
        visited += 1
        current = ids[current]
    assert visited == len(ids)


# --------------------------------------------- (8)(9) backup/restore safety ---

def test_backup_scripts_use_environment_not_hardcoded_secrets():
    for name in ("backup.sh", "restore.sh", "backup_verify.sh"):
        text = (REPO / "scripts" / name).read_text()
        for marker in ("sk_live_", "AKIA", "-----BEGIN", "password=voxdesk"):
            assert marker not in text, f"{name} contains {marker!r}"
    backup = (REPO / "scripts" / "backup.sh").read_text()
    assert "POSTGRES_USER" in backup and "PGHOST" in backup
    assert "pg_restore --list" in backup  # integrity check before retention


def test_restore_refuses_overwrite_and_supports_drill():
    text = (REPO / "scripts" / "restore.sh").read_text()
    assert "RESTORE_TARGET_DB" in text      # drill restores into a scratch DB
    assert "RESTORE_ALLOW_OVERWRITE" in text  # never silently clobber a live DB
    assert "pg_restore --list" in text      # verify before writing


# ------------------------------------------------------ (10) secret exposure ---

def test_gitignore_covers_secrets_and_dumps():
    text = (REPO / ".gitignore").read_text()
    for entry in (".env", "secrets/", "backups/", "*.dump", "*.pem", "*.key", ".netrc"):
        assert entry in text


def test_dockerignore_excludes_secrets_and_dumps():
    text = (REPO / ".dockerignore").read_text()
    for entry in (".env", "secrets/", "backups/", "*.dump", "*.pem", "*.key"):
        assert entry in text


def test_env_examples_contain_only_placeholder_credentials():
    for name in (".env.example", ".env.staging.example"):
        text = (REPO / name).read_text()
        for marker in ("sk_live_", "AKIA", "BEGIN RSA", "BEGIN PRIVATE"):
            assert marker not in text, f"{name} contains {marker!r}"
    example = (REPO / ".env.example").read_text()
    assert "sk-xxxxxxxx" in example
    assert "change-me" in example


# ---------------------------------------------- (11)(12) docker/compose invariants ---

def test_dockerfile_runs_non_root_and_healthchecks():
    text = (REPO / "Dockerfile").read_text()
    assert "USER appuser" in text
    assert "HEALTHCHECK" in text
    assert "python:3.12-slim-bookworm" in text
    assert "COPY .env" not in text


def test_prod_compose_keeps_datastores_off_the_host():
    prod = yaml.safe_load((REPO / "docker-compose.prod.yml").read_text())
    assert "ports" not in prod["services"]["db"]
    assert "ports" not in prod["services"]["redis"]
    api_ports = [str(p) for p in prod["services"]["api"]["ports"]]
    assert api_ports and all(p.startswith("127.0.0.1:") for p in api_ports)
    assert prod["services"]["api"]["depends_on"]["db"]["condition"] == "service_healthy"
    assert prod["services"]["api"].get("init") is True
    assert prod["services"]["backup"]["depends_on"]["db"]["condition"] == "service_healthy"


def test_staging_compose_is_isolated_from_production():
    st = yaml.safe_load((REPO / "docker-compose.staging.yml").read_text())
    assert st["services"]["api"]["environment"]["APP_ENV"] == "staging"
    assert "pgdata_staging" in st.get("volumes", {})
    api_vols = " ".join(str(v) for v in st["services"]["api"]["volumes"])
    assert "secrets-staging" in api_vols
    api_ports = " ".join(str(p) for p in st["services"]["api"]["ports"])
    assert "8001" in api_ports
    # Staging scheduler inherits the staging env file, not production's.
    assert st["services"]["scheduler"]["env_file"] == ".env.staging"


# ----------------------------------------------------- (13) CI workflow invariants ---

def test_ci_workflow_gates_and_separates_real_providers():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "alembic upgrade head" in ci
    assert "npm ci" in ci
    assert "not real_provider" in ci
    assert "docker build ." in ci
    assert "npm audit --omit=dev" in ci
    real = (REPO / ".github" / "workflows" / "real-integrations.yml").read_text()
    assert "workflow_dispatch" in real
    sec = (REPO / ".github" / "workflows" / "security-scan.yml").read_text()
    assert "bandit" in sec and "pip-audit" in sec and "gitleaks" in sec


# ------------------------------------------- (14) monitoring endpoint protection ---

def test_metrics_endpoint_is_token_gated_when_configured():
    src = (REPO / "app" / "core" / "metrics.py").read_text()
    assert "_scrape_authorized" in src
    assert "METRICS_TOKEN" in src


def test_caddy_does_not_expose_metrics_as_a_separate_route():
    text = (REPO / "Caddyfile").read_text()
    assert "/metrics" not in text


# ---------------------------------------------- (15) redis restart behaviour ---

@pytest.mark.asyncio
async def test_redis_cache_degrades_gracefully_when_down(monkeypatch):
    from app.core.cache import _RedisCache

    class _Down:
        async def ping(self):
            raise ConnectionError("redis down")

        async def get(self, key):
            raise ConnectionError("redis down")

        async def set(self, key, value, ex):
            raise ConnectionError("redis down")

        async def delete(self, key):
            raise ConnectionError("redis down")

    rc = _RedisCache("redis://127.0.0.1:1/0")
    monkeypatch.setattr(rc, "_get", lambda: _Down())
    assert await rc.ping() is False
    assert await rc.get("k") is None
    await rc.set("k", "v", 60)   # must not raise
    await rc.delete("k")          # must not raise


@pytest.mark.asyncio
async def test_memory_cache_ping_reports_reachable():
    from app.core.cache import _MemoryCache

    assert await _MemoryCache().ping() is True


# ----------------------------------------------------- (16) rollback guard ---

def test_rollback_checks_out_and_redeploys_without_pull():
    text = (REPO / "scripts" / "rollback.sh").read_text()
    assert "git checkout" in text
    assert "deploy.sh --no-pull" in text


def test_rollback_never_executes_alembic_downgrade():
    """The downgrade command may appear only in comments/documentation; the
    script must never run it. Database downgrades are operator-only."""
    for line in (REPO / "scripts" / "rollback.sh").read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert "alembic downgrade" not in stripped
        assert "command.downgrade" not in stripped


def test_deploy_script_backs_up_before_migrating():
    text = (REPO / "scripts" / "deploy.sh").read_text()
    assert "pg_dump" in text
    assert "pg_restore --list" in text          # integrity check
    assert "scripts/migrate.py" in text          # advisory-locked migration
    assert "/health/ready" in text               # readiness wait
