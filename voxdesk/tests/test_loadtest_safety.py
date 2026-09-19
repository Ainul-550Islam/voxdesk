"""Step 7 — load-test safety guards (loadtest/safety.py).

The guard is the whole point: a load test is only "safe by default" if a
mis-aimed --host cannot silently become a production load test. These tests
lock that behaviour down without running Locust.
"""
from __future__ import annotations

from loadtest import safety


def test_loopback_targets_always_allowed(monkeypatch):
    monkeypatch.delenv("LOADTEST_ALLOW_REMOTE", raising=False)
    assert safety.validate_target("http://localhost:8000") is None
    assert safety.validate_target("http://127.0.0.1:8000") is None
    assert safety.validate_target("http://[::1]:8000") is None
    assert safety.validate_target("https://localhost") is None


def test_remote_target_refused_without_opt_in(monkeypatch):
    monkeypatch.delenv("LOADTEST_ALLOW_REMOTE", raising=False)
    reason = safety.validate_target("https://staging.example.com")
    assert reason is not None
    assert "LOADTEST_ALLOW_REMOTE" in reason


def test_remote_target_allowed_with_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("LOADTEST_ALLOW_REMOTE", "1")
    assert safety.validate_target("https://staging.example.com") is None


def test_missing_host_is_refused():
    assert safety.validate_target(None) is not None
    assert safety.validate_target("") is not None


def test_allow_remote_parses_truthy_values(monkeypatch):
    monkeypatch.setenv("LOADTEST_ALLOW_REMOTE", "true")
    assert safety.allow_remote() is True
    monkeypatch.setenv("LOADTEST_ALLOW_REMOTE", "YES")
    assert safety.allow_remote() is True
    monkeypatch.setenv("LOADTEST_ALLOW_REMOTE", "no")
    assert safety.allow_remote() is False
    monkeypatch.delenv("LOADTEST_ALLOW_REMOTE", raising=False)
    assert safety.allow_remote() is False
