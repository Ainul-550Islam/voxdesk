"""Tests for the booking brain -- no phone, no API keys needed.

Run:  pytest -q
"""
import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.agent.functions import FunctionHandlers
from app.agent.prompts import build_system_prompt
from app.db.models import Call, Tenant

TZ = ZoneInfo("America/New_York")


class FakeCalendar:
    def __init__(self, busy=None):
        self.busy = busy or []
        self.created = []

    async def list_busy(self, start, end):
        return [(s, e) for s, e in self.busy if s < end and start < e]

    async def create_event(self, summary, description, start, end):
        self.created.append((summary, start, end))
        return "evt_123"


class FakeSession:
    """
    Stand-in for AsyncSession.

    Grew a `flush` and an `execute` in STEP 5: booking now emits a CRM event
    in the same transaction, and a double that is missing methods the real
    interface has stops testing the code and starts testing the double.
    `execute` returns an empty result, so the emit takes its "this tenant has
    no integrations connected" path rather than being swallowed by the
    hook's error handler.
    """

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def flush(self):
        pass

    async def rollback(self):
        pass

    async def execute(self, *args, **kwargs):
        return _EmptyResult()


class _EmptyResult:
    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []



@pytest.fixture
def tenant():
    return Tenant(
        id=uuid.uuid4(),
        name="Bright Smile Dental",
        industry="dental",
        twilio_number="+15550001111",
        agent_name="Alex",
        greeting="Thanks for calling Bright Smile Dental.",
        timezone="America/New_York",
        business_open=time(9, 0),
        business_close=time(17, 0),
        appointment_minutes=30,
        knowledge_base={"services": ["cleaning", "whitening"], "parking": "free lot"},
        notify_sms_number=None,
        google_calendar_id="cal@example.com",
    )


@pytest.fixture
def handlers(tenant):
    call = Call(id=uuid.uuid4(), tenant_id=tenant.id, call_sid="CA1",
                from_number="+15559998888", to_number=tenant.twilio_number)
    h = FunctionHandlers(session=FakeSession(), tenant=tenant, call=call)
    h.calendar = FakeCalendar()
    return h


def _future_date(days=3):
    return (datetime.now(TZ) + timedelta(days=days)).strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_offers_at_most_three_slots(handlers):
    res = await handlers.check_availability(date=_future_date())
    assert res["ok"] is True
    # Reading 16 slots aloud would destroy the call.
    assert len(res["available"]) <= 3


@pytest.mark.asyncio
async def test_busy_slots_are_excluded(handlers):
    day = datetime.now(TZ) + timedelta(days=3)
    blocked_start = day.replace(hour=9, minute=0, second=0, microsecond=0)
    handlers.calendar.busy = [(blocked_start, blocked_start + timedelta(hours=2))]
    res = await handlers.check_availability(date=day.strftime("%Y-%m-%d"))
    assert "9:00 am" not in res["available"]


@pytest.mark.asyncio
async def test_morning_filter(handlers):
    res = await handlers.check_availability(date=_future_date(), part_of_day="morning")
    for slot_iso in res["iso_slots"]:
        assert datetime.fromisoformat(slot_iso).hour < 12


@pytest.mark.asyncio
async def test_double_booking_is_rejected(handlers):
    day = datetime.now(TZ) + timedelta(days=3)
    slot = day.replace(hour=11, minute=0, second=0, microsecond=0)
    handlers.calendar.busy = [(slot, slot + timedelta(minutes=30))]
    res = await handlers.book_appointment(
        customer_name="Rahim", customer_phone="+15551234567", starts_at=slot.isoformat()
    )
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_booking_creates_calendar_event(handlers):
    day = datetime.now(TZ) + timedelta(days=3)
    slot = day.replace(hour=14, minute=0, second=0, microsecond=0)
    res = await handlers.book_appointment(
        customer_name="Rahim", customer_phone="+15551234567",
        starts_at=slot.isoformat(), reason="cleaning",
    )
    assert res["ok"] is True
    assert len(handlers.calendar.created) == 1


@pytest.mark.asyncio
async def test_bad_date_does_not_crash(handlers):
    """
    Garbage input must not crash the call.

    The original version of this test asserted that "next tuesday" *failed*,
    because the pre-STEP-6 handler required a literal YYYY-MM-DD and rejected
    everything else. STEP 6 routes `dispatch` to the scheduling layer, which
    resolves natural language deterministically, so that phrase now succeeds.
    The test's actual intent -- no crash, and a structured answer either way --
    is preserved and extended to input that is genuinely unparseable.
    """
    resolved = await handlers.dispatch("check_availability", {"date": "next tuesday"})
    assert "outcome" in resolved
    assert resolved["outcome"] in ("AVAILABLE_SLOTS", "NO_SLOTS", "FAILED")

    nonsense = await handlers.dispatch("check_availability", {"date": "hjkl qwerty"})
    assert nonsense["ok"] is False
    assert nonsense["outcome"] == "NEEDS_CLARIFICATION"


@pytest.mark.asyncio
async def test_unknown_function_is_safe(handlers):
    res = await handlers.dispatch("delete_everything", {})
    assert res["ok"] is False


def test_prompt_enforces_short_answers(tenant):
    prompt = build_system_prompt(tenant)
    assert "Maximum 25 words" in prompt
    assert "Bright Smile Dental" in prompt
    assert "cleaning" in prompt          # knowledge base injected


def test_prompt_forbids_admitting_it_is_ai(tenant):
    assert "NOT an AI assistant" in build_system_prompt(tenant)


def test_prompt_demands_contractions(tenant):
    assert "contractions" in build_system_prompt(tenant)


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
def test_prompt_tuned_per_provider(tenant, provider):
    """একই প্রম্পটে তিন AI একরকম আচরণ করে না -- তাই আলাদা STYLE NOTE।"""
    assert "STYLE NOTE" in build_system_prompt(tenant, provider)

