"""Unit tests for app.orchestration.campaign.

Co-located with the package; run with: python -m pytest app/orchestration/ -q
"""

from __future__ import annotations

from datetime import datetime, time as _time

import pytest

from app.orchestration.campaign import (
    CHANNEL_SMS,
    CHANNEL_VOICE,
    CANCELLED,
    COMPLETED,
    DRAFT,
    PAUSED,
    RUNNING,
    SCHEDULED,
    Campaign,
    CampaignError,
    ComplianceGate,
    DayState,
    REASON_ATTEMPT_LIMIT,
    REASON_DAILY_LIMIT,
    REASON_DNC,
    REASON_WINDOW,
    Schedule,
    Throttle,
    can_transition,
    dispatch,
    intent_key,
    is_within_window,
)


def campaign(**overrides):
    kw = dict(id="c1", tenant_id="t1", name="Fall cleanup", state=RUNNING)
    kw.update(overrides)
    return Campaign(**kw)


def lead(lead_id, *, dnc=False):
    return {"id": lead_id, "dnc": dnc}


def in_window():
    return datetime(2026, 9, 14, 12, 0)  # any weekday; default schedule allows all


# -------------------------------------------------------------- states -------

def test_campaign_transitions():
    assert can_transition(DRAFT, SCHEDULED)
    assert can_transition(SCHEDULED, RUNNING)
    assert can_transition(RUNNING, PAUSED)
    assert can_transition(PAUSED, RUNNING)
    assert can_transition(RUNNING, COMPLETED)
    assert can_transition(DRAFT, CANCELLED)
    assert not can_transition(COMPLETED, RUNNING)
    assert not can_transition(CANCELLED, RUNNING)
    assert not can_transition(DRAFT, RUNNING)  # must pass through scheduled


def test_compliance_legal_controls_cannot_be_disabled():
    assert not ComplianceGate().validate()
    assert ComplianceGate(require_dnc_check=False).validate()
    assert ComplianceGate(require_call_window=False).validate()
    # Tightening is fine.
    assert not ComplianceGate(require_a2p_registration=True).validate()


def test_non_voice_requires_a2p():
    assert not campaign(channel=CHANNEL_VOICE).validate()
    assert campaign(channel=CHANNEL_SMS).validate()  # a2p missing → invalid
    assert not campaign(channel=CHANNEL_SMS,
                        compliance=ComplianceGate(require_a2p_registration=True)).validate()


def test_throttle_and_schedule_validation():
    assert Throttle(calls_per_minute=0).validate()
    assert Throttle(daily_limit=0).validate()
    assert Throttle(max_attempts_per_lead=11).validate()
    assert Schedule(days_of_week=()).validate()
    assert Schedule(days_of_week=(9,)).validate()


# ----------------------------------------------------------------- window ----

def test_is_within_window():
    schedule = Schedule(daily_start=_time(9, 0),
                        daily_end=_time(20, 0),
                        days_of_week=(0, 1, 2, 3, 4, 5, 6))
    assert is_within_window(datetime(2026, 9, 14, 12, 0), schedule)
    assert not is_within_window(datetime(2026, 9, 14, 8, 59), schedule)
    assert not is_within_window(datetime(2026, 9, 14, 20, 1), schedule)


def test_is_within_window_respects_allowed_days():
    # Only Mondays (0) allowed.
    schedule = Schedule(days_of_week=(0,))
    monday = datetime(2026, 1, 5, 12, 0)  # 2026-01-05 is a Monday
    assert monday.weekday() == 0
    assert is_within_window(monday, schedule)
    assert not is_within_window(datetime(2026, 1, 6, 12, 0), schedule)  # Tuesday


# --------------------------------------------------------------- dispatch ----

def test_dispatch_eligible_leads_consume_state():
    state = DayState()
    intents, metrics = dispatch(campaign(), [lead("l1"), lead("l2")], state, in_window())
    assert metrics.total_leads == 2
    assert metrics.attempted == 2
    assert all(not i.skipped for i in intents)
    assert state.daily_calls_today == 2
    assert state.attempts_for("l1") == 1


def test_dispatch_dnc_skipped_first():
    state = DayState()
    intents, metrics = dispatch(
        campaign(), [lead("l1", dnc=True), lead("l2")], state, in_window()
    )
    assert metrics.dnc_skipped == 1
    assert metrics.attempted == 1
    assert intents[0].reason_skipped == REASON_DNC
    assert intents[0].skipped
    assert state.daily_calls_today == 1


def test_dispatch_outside_window_skipped():
    state = DayState()
    now = datetime(2026, 9, 14, 23, 0)  # after 20:00 close
    intents, metrics = dispatch(campaign(), [lead("l1")], state, now)
    assert metrics.window_skipped == 1
    assert intents[0].reason_skipped == REASON_WINDOW
    assert state.daily_calls_today == 0


def test_dispatch_attempt_limit_skipped():
    state = DayState()
    state.attempts["l1"] = 3  # max_attempts_per_lead default is 3
    intents, metrics = dispatch(campaign(), [lead("l1")], state, in_window())
    assert metrics.attempt_limit_skipped == 1
    assert intents[0].reason_skipped == REASON_ATTEMPT_LIMIT


def test_dispatch_daily_limit_skipped():
    state = DayState(daily_calls_today=200)  # daily_limit default is 200
    intents, metrics = dispatch(campaign(), [lead("l1")], state, in_window())
    assert metrics.daily_limit_skipped == 1
    assert intents[0].reason_skipped == REASON_DAILY_LIMIT
    assert state.daily_calls_today == 200  # not consumed


def test_dispatch_reasons_sum_to_total():
    state = DayState(daily_calls_today=199)
    leads = [lead("dnc", dnc=True), lead("ok")]
    # daily_limit=1 → "ok" hits the daily limit, dnc hits dnc.
    intents, metrics = dispatch(
        campaign(throttle=Throttle(daily_limit=200)),
        leads, state, in_window(),
    )
    assert metrics.total_leads == 2
    assert metrics.dnc_skipped + metrics.attempted + metrics.daily_limit_skipped == 2
    assert state.daily_calls_today == 200  # "ok" consumed the last slot


def test_intent_keys_are_deterministic():
    a = intent_key("c1", "l1")
    b = intent_key("c1", "l1")
    assert a == b
    assert intent_key("c1", "l2") != a
    assert len(a) == 24


def test_invalid_campaign_raises_on_dispatch():
    with pytest.raises(CampaignError):
        dispatch(campaign(channel=CHANNEL_SMS), [lead("l1")], DayState(), in_window())
