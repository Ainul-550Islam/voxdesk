"""Step 9 — HMAC-signed license tokens."""
from __future__ import annotations

import time

from app.billing import licensing


def test_round_trip():
    token = licensing.issue_license("tenant-1", "pro", 10, int(time.time()) + 3600)
    info = licensing.verify_license(token)
    assert info is not None
    assert info.tenant_id == "tenant-1"
    assert info.plan == "pro"
    assert info.seats == 10


def test_tampered_token_is_rejected():
    token = licensing.issue_license("t", "pro", 1, int(time.time()) + 3600)
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert licensing.verify_license(tampered) is None


def test_expired_token_is_rejected():
    token = licensing.issue_license("t", "pro", 1, int(time.time()) - 100)
    assert licensing.verify_license(token) is None


def test_garbage_inputs_are_rejected():
    for bad in ["", "garbage", "a.b", "!!!" * 20, None]:
        assert licensing.verify_license(bad) is None


def test_different_key_rejects(monkeypatch):
    from app.core.config import settings

    token = licensing.issue_license("t", "pro", 1, int(time.time()) + 3600)
    monkeypatch.setattr(settings, "license_secret", "a-completely-different-key")
    assert licensing.verify_license(token) is None
