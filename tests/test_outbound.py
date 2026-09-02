"""Outbound dialer guardrails: TCPA window, backoff, DNC, lead scoring."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.agent.functions import FunctionHandlers
from app.integrations import crm
from app.integrations.reminders import reminder_call_script, reminder_text
from app.telephony.outbound import (
    backoff_for,
    build_outbound_twiml_url,
    is_call_window_open,
    next_window_start,
)


class FakeTenant:
    timezone = "America/New_York"
    outbound_window_open = time(9, 0)
    outbound_window_close = time(20, 0)


NY = ZoneInfo("America/New_York")


# ------------------------------------------------------------- call window ---

@pytest.mark.parametrize("hour,expected", [
    (3, False), (8, False), (9, True), (13, True), (19, True), (20, False), (21, False),
])
def test_call_window(hour, expected):
    now = datetime(2026, 6, 15, hour, 30, tzinfo=NY)   # :30 past -- 20:30 is past close
    assert is_call_window_open(FakeTenant(), now) is expected


def test_window_respects_tenant_timezone():
    """3pm UTC is 10am in New York -- must be open, not judged in UTC."""
    now = datetime(2026, 6, 15, 15, 0, tzinfo=ZoneInfo("UTC"))
    assert is_call_window_open(FakeTenant(), now) is True


def test_next_window_start_before_open():
    now = datetime(2026, 6, 15, 6, 0, tzinfo=NY)
    assert next_window_start(FakeTenant(), now).hour == 9


def test_next_window_start_after_close_is_tomorrow():
    now = datetime(2026, 6, 15, 22, 0, tzinfo=NY)
    nxt = next_window_start(FakeTenant(), now)
    assert nxt.day == 16 and nxt.hour == 9


def test_next_window_start_inside_window_is_now():
    now = datetime(2026, 6, 15, 11, 0, tzinfo=NY)
    assert next_window_start(FakeTenant(), now).hour == 11


# ----------------------------------------------------------------- backoff ---

def test_backoff_grows_then_caps():
    assert backoff_for(1) == timedelta(hours=1)
    assert backoff_for(2) == timedelta(hours=4)
    assert backoff_for(3) == timedelta(hours=24)
    assert backoff_for(99) == timedelta(hours=24)


def test_backoff_handles_zero():
    assert backoff_for(0) == timedelta(hours=1)


def test_twiml_url_carries_ids():
    url = build_outbound_twiml_url("camp-1", "lead-9")
    assert "campaign_id=camp-1" in url and "lead_id=lead-9" in url


# ------------------------------------------------------------ lead scoring ---

def test_hot_lead_scores_high():
    score = FunctionHandlers.score_lead(
        timeline="immediately", budget_known=True, decision_maker=True,
        has_contact=True, booked=True,
    )
    assert score == 100


def test_cold_lead_scores_low():
    assert FunctionHandlers.score_lead(timeline="just_looking") == 5


def test_unknown_timeline_scores_zero_base():
    assert FunctionHandlers.score_lead(timeline="") == 0


def test_score_never_exceeds_100():
    assert FunctionHandlers.score_lead(
        timeline="immediately", budget_known=True, decision_maker=True,
        has_contact=True, booked=True,
    ) <= 100


def test_score_ordering_is_sane():
    hot = FunctionHandlers.score_lead(timeline="immediately", has_contact=True)
    warm = FunctionHandlers.score_lead(timeline="this_month", has_contact=True)
    cold = FunctionHandlers.score_lead(timeline="just_looking")
    assert hot > warm > cold


# ---------------------------------------------------------------------- CRM ---

def _payload(**kw):
    base = dict(
        tenant_name="Bright Smile Dental", call_id="c1", direction="inbound",
        from_number="+15551234567", to_number="+15559876543",
        duration_seconds=91.4, intent="booking", summary="Booked cleaning",
        booked=True, escalated=False, lead_score=85, customer_name="Jane Doe",
    )
    base.update(kw)
    return crm.build_payload(**base)


def test_crm_payload_is_flat_and_complete():
    p = _payload()
    for key in ("source", "event", "business", "call_id", "intent", "booked", "contact"):
        assert key in p
    assert p["source"] == "VoxDesk"
    assert p["contact"]["phone"] == "+15551234567"


def test_crm_payload_outbound_uses_to_number_as_contact():
    p = _payload(direction="outbound")
    assert p["contact"]["phone"] == "+15559876543"


def test_gohighlevel_mapping_splits_name_and_tags():
    body = crm.to_gohighlevel(_payload())
    assert body["firstName"] == "Jane" and body["lastName"] == "Doe"
    assert "ai-call" in body["tags"] and "booked" in body["tags"]


def test_gohighlevel_handles_missing_name():
    body = crm.to_gohighlevel(_payload(customer_name=""))
    assert body["firstName"] == "Unknown"


def test_crm_headers_per_provider():
    assert "Authorization" in crm._headers("gohighlevel", "k")
    assert crm._headers("gohighlevel", "k")["Version"] == "2021-07-28"
    assert "X-API-Key" in crm._headers("webhook", "k")
    assert "Authorization" not in crm._headers("webhook", None)


@pytest.mark.asyncio
async def test_crm_push_without_url_is_noop():
    assert await crm.push(webhook_url=None, payload={}) is False


# ----------------------------------------------------------------- reminders ---

def test_reminder_text_is_human_and_actionable():
    when = datetime(2026, 6, 15, 14, 30)
    text = reminder_text("Bright Smile Dental", when, "Jane")
    assert "Jane" in text and "Bright Smile Dental" in text
    assert "2:30 PM" in text
    assert "Reply C" in text


def test_reminder_text_without_name_still_reads_ok():
    text = reminder_text("Acme Clinic", datetime(2026, 6, 15, 9, 0))
    assert text.startswith("Hi, ") and "Acme Clinic" in text


def test_reminder_call_script_asks_a_question():
    script = reminder_call_script("Acme", datetime(2026, 6, 15, 9, 0), "Bob")
    assert script.endswith("?") and "Bob" in script
