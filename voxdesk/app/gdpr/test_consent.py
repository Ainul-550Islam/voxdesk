"""Unit tests for app.gdpr.consent.

Co-located with the package; run with: python -m pytest app/gdpr/ -q
"""

from __future__ import annotations

from datetime import datetime

from app.gdpr.consent import (
    LEGAL_BASES,
    ConsentRecord,
    consent_required,
    consent_status,
    has_consent,
    processing_allowed,
    valid_legal_basis,
)


def granted(subject_id="s1", ts="2026-01-01T00:00:00"):
    return ConsentRecord(subject_id, datetime.fromisoformat(ts))


def withdrawn(subject_id="s1", granted="2026-01-01T00:00:00", withdrawn="2026-02-01T00:00:00"):
    return ConsentRecord(subject_id, datetime.fromisoformat(granted),
                         datetime.fromisoformat(withdrawn))


def test_consent_status_granted_withdrawn_none():
    assert consent_status([]) == "none"
    assert consent_status([granted()]) == "granted"
    assert consent_status([withdrawn()]) == "withdrawn"


def test_latest_event_wins():
    records = [granted(), withdrawn()]
    assert consent_status(records) == "withdrawn"
    assert not has_consent(records)
    # A newer grant after the withdrawal re-establishes consent.
    records.append(granted(ts="2026-03-01T00:00:00"))
    assert consent_status(records) == "granted"
    assert has_consent(records)


def test_withdrawal_wins_on_timestamp_tie():
    records = [
        ConsentRecord("s1", datetime.fromisoformat("2026-01-01T00:00:00"),
                      datetime.fromisoformat("2026-02-01T00:00:00")),
        ConsentRecord("s1", datetime.fromisoformat("2026-02-01T00:00:00")),
    ]
    assert consent_status(records) == "withdrawn"


def test_consent_required_only_for_consent_basis():
    assert consent_required("consent")
    assert not consent_required("contract")
    assert not consent_required("legal_obligation")
    # Unknown basis: fail closed → treat as requiring consent.
    assert consent_required("not_a_basis")


def test_processing_allowed():
    assert processing_allowed("contract", [])
    assert processing_allowed("legal_obligation", [])
    assert processing_allowed("consent", [granted()])
    assert not processing_allowed("consent", [withdrawn()])
    assert not processing_allowed("consent", [])
    assert not processing_allowed("bogus", [granted()])


def test_validation_rejects_withdrawal_before_grant():
    bad = ConsentRecord("s1", datetime.fromisoformat("2026-02-01T00:00:00"),
                        datetime.fromisoformat("2026-01-01T00:00:00"))
    assert bad.validate()
    assert not granted().validate()


def test_legal_bases_set():
    assert valid_legal_basis("consent")
    assert not valid_legal_basis("whatever")
    assert "legitimate_interest" in LEGAL_BASES
