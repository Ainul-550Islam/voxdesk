"""
Step 9 compliance — data retention and AI-disclosure policy.

Pure-policy tests: no database, no network, deterministic. These pin the two
regulatory obligations codified in app/core/data_policy.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.data_policy import (
    ai_disclosure_compliant,
    ai_disclosure_present,
    compliance_summary,
    is_expired,
    retention_cutoff,
)


# -------------------------------------------------------------- retention ---

def test_cutoff_is_now_minus_days():
    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    assert retention_cutoff(30, now=now) == now - timedelta(days=30)


def test_days_are_never_below_one():
    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    assert retention_cutoff(0, now=now) == now - timedelta(days=1)
    assert retention_cutoff(-5, now=now) == now - timedelta(days=1)


def test_recent_record_is_not_expired():
    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    recent = now - timedelta(days=10)
    assert is_expired(recent, retention_days=30, now=now) is False


def test_old_record_is_expired():
    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(days=31)
    assert is_expired(old, retention_days=30, now=now) is True


def test_naive_utc_timestamp_is_treated_as_utc():
    # The codebase stores naive UTC via datetime.utcnow(); a record exactly on
    # the cutoff boundary must not be deleted.
    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    boundary = now - timedelta(days=30)
    assert is_expired(boundary, retention_days=30, now=now) is False


# ------------------------------------------------------------ disclosure ---

def test_disclosure_recognises_automated_greeting():
    assert ai_disclosure_present(
        "Thanks for calling Bright Smile Dental, this is Alex, our virtual AI assistant. How can I help?"
    ) is True


def test_disclosure_missing_in_human_sounding_greeting():
    assert ai_disclosure_present(
        "Thanks for calling Bright Smile Dental, this is Alex. How can I help?"
    ) is False


def test_disclosure_is_case_insensitive():
    assert ai_disclosure_present("You are speaking to an AI agent.") is True


def test_disclosure_not_required_means_compliant_regardless():
    assert ai_disclosure_compliant("Hello, this is Alex.", required=False) is True


def test_disclosure_required_flags_missing_disclosure():
    assert ai_disclosure_compliant("Hello, this is Alex.", required=True) is False
    assert ai_disclosure_compliant("Hello, this is our AI assistant.", required=True) is True


def test_compliance_summary_shape():
    summary = compliance_summary(
        greeting="Thanks for calling, this is our virtual assistant.",
        disclosure_required=True,
    )
    assert summary == {
        "ai_disclosure_required": True,
        "ai_disclosure_present": True,
        "compliant": True,
    }
