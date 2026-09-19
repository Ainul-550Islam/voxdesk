"""Unit tests for app.gdpr.requests.

Co-located with the package; run with: python -m pytest app/gdpr/ -q
"""

from __future__ import annotations

import pytest

from app.gdpr.requests import (
    DEFAULT_DEADLINE_DAYS,
    REQUEST_TYPES,
    RequestError,
    SubjectRequest,
    action_plan,
    erasure_blocked,
    requires_identity_verification,
)


def request(type="access", verified=True):
    return SubjectRequest(id="r1", tenant_id="t1", subject_id="s1", type=type, verified=verified)


def test_validation():
    assert SubjectRequest("", "t1", "s1", "access").validate()
    assert SubjectRequest("r1", "", "s1", "access").validate()
    assert SubjectRequest("r1", "t1", "", "access").validate()
    assert SubjectRequest("r1", "t1", "s1", "bogus").validate()
    assert not request().validate()


def test_unverified_request_only_verifies_identity():
    plan = action_plan(request(verified=False))
    assert plan.actions == ("verify_identity",)
    assert plan.requires_identity_verification
    assert not plan.blocked


def test_access_plan_exports_data():
    plan = action_plan(request(type="access"))
    assert plan.actions == ("locate_records", "export_personal_data")
    assert not plan.requires_identity_verification
    assert plan.deadline_days == DEFAULT_DEADLINE_DAYS


def test_portability_plan_is_machine_readable():
    plan = action_plan(request(type="portability"))
    assert "export_machine_readable" in plan.actions


def test_erasure_without_hold_deletes():
    plan = action_plan(request(type="erasure"))
    assert plan.actions == ("locate_records", "delete_personal_data")
    assert not plan.blocked


def test_erasure_with_hold_is_blocked_not_deleted():
    plan = action_plan(request(type="erasure"), holds={"legal_obligation"})
    assert plan.blocked
    assert "apply_hold" in plan.actions
    assert "delete_personal_data" not in plan.actions
    assert "legal_obligation" in plan.block_reason


def test_erasure_with_unknown_hold_fails_closed():
    plan = action_plan(request(type="erasure"), holds={"some_future_obligation"})
    assert plan.blocked
    assert "some_future_obligation" in plan.block_reason


def test_erasure_blocked_helper():
    assert erasure_blocked({"legal_obligation"})
    assert not erasure_blocked(set())


def test_requires_identity_verification_helper():
    assert requires_identity_verification(request(verified=False))
    assert not requires_identity_verification(request(verified=True))


def test_invalid_request_raises():
    with pytest.raises(RequestError):
        action_plan(SubjectRequest("r1", "t1", "s1", "bogus"))


def test_request_types_are_the_gdpr_set():
    assert REQUEST_TYPES == frozenset({
        "access", "rectification", "erasure",
        "portability", "objection", "restriction",
    })
