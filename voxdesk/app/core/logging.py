"""Structured logging. In voice apps you MUST log timings or you can never debug latency."""
import logging
import time
from contextlib import contextmanager

import structlog

from app.core.config import settings

#: Exact log-field names whose values are always redacted. Deliberately a
#: *closed* set — note ``tokens`` (a count) is NOT here while ``token`` is.
_REDACT_KEYS = frozenset({
    "password", "passwd", "pwd", "secret", "secret_key", "api_key", "apikey",
    "token", "access_token", "refresh_token", "auth_token", "bearer",
    "authorization", "cookie", "set_cookie", "credential", "credentials",
    "private_key", "client_secret", "webhook_secret", "dsn",
})

#: Keys whose *prefix* triggers redaction, so ``authorization_*`` variants are
#: covered without enumerating them.
_REDACT_PREFIXES = ("authorization", "cookie")

_REDACTED = "***"

#: Configured secret *values* collected once, so a secret that leaks into an
#: ``error=``/``detail=`` field under a non-obvious name is still masked.
_KNOWN_SECRETS = tuple(
    v for v in (
        getattr(settings, name, "") or ""
        for name in (
            "secret_key", "jwt_secret", "twilio_auth_token", "twilio_account_sid",
            "openai_api_key", "anthropic_api_key", "google_api_key",
            "deepgram_api_key", "elevenlabs_api_key", "stripe_secret_key",
            "stripe_webhook_secret", "metrics_token", "license_secret",
        )
    )
    if v
)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _REDACT_KEYS:
        return True
    return lowered.startswith(_REDACT_PREFIXES)


def _redact(value, key: str = ""):
    """Mask a log value in place, recursively, without touching numbers/bools."""
    if isinstance(value, str):
        if _is_secret_key(key):
            return _REDACTED
        for secret in _KNOWN_SECRETS:
            if secret and secret in value:
                value = value.replace(secret, _REDACTED)
        return value
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, key) for v in value]
    return value


def redact_secrets(_logger, _method_name: str, event_dict: dict) -> dict:
    """structlog processor: strip secret keys and secret values from a record.

    Runs just before the renderer, so neither the JSON nor the console output
    can contain a token, key, cookie, or authorization header. Keys are a
    closed set (no heuristic that could eat legitimate fields); values are
    additionally matched against the configured secrets so a key that slipped
    into ``error=`` under a non-obvious name is still masked.
    """
    return {k: _redact(v, k) for k, v in event_dict.items()}


def _choose_renderer(log_format: str, is_production: bool):
    """JSON in production (machine readable); coloured console in development.

    LOG_FORMAT can force either renderer; by default production gets JSON and
    development gets the console renderer.
    """
    use_json = (log_format or "").lower() == "json" or is_production
    if use_json:
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer()


def _configure_logging() -> None:
    level = getattr(logging, (settings.log_level or "INFO").upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_secrets,
            _choose_renderer(settings.log_format, settings.is_production),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


_configure_logging()

log = structlog.get_logger()


@contextmanager
def timed(label: str, **ctx):
    """Usage:  with timed("stt"): ...   -> logs elapsed ms"""
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log.info("timing", stage=label, ms=round(elapsed_ms, 1), **ctx)
