"""Unit tests for app.gdpr.schedule.

Co-located with the package; run with: python -m pytest app/gdpr/ -q
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.gdpr.schedule import (
    CATEGORY_NAMES,
    DeletionPlan,
    RecordRef,
    deletion_plan,
    erasure_eligible,
    minimize,
    retention_days,
    rule_for,
    schedule_summary,
)


NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def record(category, id, days_ago):
    return RecordRef(category, id, NOW - timedelta(days=days_ago))


def test_schedule_categories_known():
    assert CATEGORY_NAMES == frozenset({
        "call_transcript", "call_recording", "webhook_receipt",
        "consent_record", "audit_log", "billing_invoice",
    })


def test_erasure_eligibility_fails_closed():
    assert erasure_eligible("call_transcript")
    assert erasure_eligible("consent_record")
    assert not erasure_eligible("audit_log")
    assert not erasure_eligible("billing_invoice")
    assert not erasure_eligible("unknown_category")


def test_retention_days_per_category():
    assert retention_days("webhook_receipt") == 7
    assert retention_days("call_transcript") == 90
    assert retention_days("audit_log") is None
    assert retention_days("unknown") is None


def test_deletion_plan_purges_only_expired_eligible():
    plan = deletion_plan(
        [
            record("call_transcript", "c1", days_ago=200),   # expired → delete
            record("call_transcript", "c2", days_ago=10),    # fresh → keep
            record("webhook_receipt", "w1", days_ago=30),    # expired → delete
        ],
        now=NOW,
    )
    assert plan.delete_ids == ("c1", "w1")
    assert plan.delete_count == 2
    assert not plan.truncated


def test_audit_logs_never_deleted_even_when_ancient():
    plan = deletion_plan([record("audit_log", "a1", days_ago=5000)], now=NOW)
    assert plan.delete_ids == ()
    assert plan.kept_expired_audit == 1
    assert plan.kept_ineligible == 1


def test_invoices_kept_but_not_counted_as_audit():
    plan = deletion_plan([record("billing_invoice", "b1", days_ago=3000)], now=NOW)
    assert plan.delete_ids == ()
    assert plan.kept_ineligible == 1
    assert plan.kept_expired_audit == 0


def test_unknown_category_kept():
    plan = deletion_plan([RecordRef("mystery", "m1", NOW - timedelta(days=9999))], now=NOW)
    assert plan.delete_ids == ()
    assert plan.kept_ineligible == 1


def test_deletion_plan_oldest_first_and_bounded():
    records = [
        record("call_transcript", "newer", days_ago=120),
        record("call_transcript", "older", days_ago=400),
        record("call_transcript", "oldest", days_ago=500),
    ]
    plan = deletion_plan(records, now=NOW, limit=2)
    assert plan.delete_ids == ("oldest", "older")
    assert plan.truncated  # one more candidate remained


def test_deletion_plan_zero_limit():
    plan = deletion_plan([record("call_transcript", "c1", days_ago=200)], now=NOW, limit=0)
    assert plan.delete_count == 0
    assert plan.truncated


def test_minimize_drops_non_allowed_fields():
    record_dict = {"id": "c1", "transcript": "hello", "phone": "555-1234", "tenant_id": "t1"}
    kept, dropped = minimize(record_dict, {"id", "tenant_id"})
    assert kept == {"id": "c1", "tenant_id": "t1"}
    assert dropped == 2


def test_schedule_summary_serialisable():
    summary = schedule_summary()
    assert summary["audit_log"]["erasure_eligible"] is False
    assert summary["call_transcript"]["retention_days"] == 90
    assert set(summary) == CATEGORY_NAMES


def test_rule_for_returns_none_for_unknown():
    assert rule_for("call_transcript") is not None
    assert rule_for("nope") is None
    assert isinstance(DeletionPlan(), DeletionPlan)
