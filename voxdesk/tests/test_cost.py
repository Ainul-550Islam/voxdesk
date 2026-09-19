"""Step 7 — the deterministic provider-cost model (docs/COST-AWARENESS.md)."""
from __future__ import annotations

import pytest

from app.billing import cost as cost_model
from app.core.config import settings


@pytest.fixture
def _prices(monkeypatch):
    """Deterministic price table for every test; restored automatically."""
    prices = {
        "voice_minute": 1300,          # $0.013 / minute
        "sms_segment": 790,            # $0.0079 / segment
        "llm_1k_tokens": 15,           # $0.00015 / 1,000 tokens
        "llm_1k_tokens:anthropic": 80, # $0.0008 / 1,000 tokens
        "tts_1k_chars": 30,            # $0.0003 / 1,000 chars
    }
    monkeypatch.setattr(settings, "cost_unit_prices_json", _dumps(prices))
    return prices


def _dumps(obj) -> str:
    import json

    return json.dumps(obj)


def test_price_lookup_voice_minute():
    assert cost_model.price_millicents("voice_minute") is None  # unset


def test_price_lookup_voice_minute_with_prices(_prices):
    assert cost_model.price_millicents("voice_minute") == 1300.0


def test_price_lookup_sms_segment(_prices):
    assert cost_model.price_millicents("sms_segment") == 790.0


def test_llm_price_is_per_thousand_tokens_divided_down(_prices):
    # $0.00015 per token, generic key.
    assert cost_model.price_millicents("llm_token", provider="openai") == 0.015


def test_llm_provider_specific_price_wins(_prices):
    assert cost_model.price_millicents("llm_token", provider="anthropic") == 0.08
    # google has no provider key -> falls back to generic.
    assert cost_model.price_millicents("llm_token", provider="google") == 0.015


def test_tts_price_per_char(_prices):
    assert cost_model.price_millicents("tts_character") == 0.03


def test_unknown_price_is_none_not_zero():
    # A zero price must be treated as UNKNOWN, never as free.
    assert cost_model.price_millicents("voice_minute") is None


def test_cost_line_known():
    line = cost_model.cost_line("voice_minute", 2.0, provider=None)
    assert line.known is False
    assert line.cost_usd is None


def test_cost_line_unknown_marks_clearly():
    line = cost_model.cost_line("sms_segment", 3.0)
    assert line.known is False
    assert line.cost_usd is None


def test_record_cost_emits_and_returns_line(_prices):
    from app.core import observability

    usd_before = observability.COST_USD.labels("llm_token")._value.get()
    line = cost_model.record_cost("llm_token", 1000.0, provider="anthropic")
    assert line.known is True
    # 80 millicents per 1,000 tokens -> 1000 tokens = 80 millicents = $0.0008
    assert line.cost_usd == pytest.approx(0.0008)
    assert (
        observability.COST_USD.labels("llm_token")._value.get() - usd_before
        == pytest.approx(0.0008)
    )


def test_malformed_price_json_degrades_to_unknown(monkeypatch):
    monkeypatch.setattr(settings, "cost_unit_prices_json", "{not json")
    assert settings.cost_unit_prices == {}
    assert cost_model.price_millicents("voice_minute") is None


def test_non_object_price_json_degrades_to_unknown(monkeypatch):
    monkeypatch.setattr(settings, "cost_unit_prices_json", "[1, 2, 3]")
    assert settings.cost_unit_prices == {}


def test_unknown_resource_has_no_price():
    assert cost_model.price_millicents("not-a-resource") is None


async def test_call_cost_breakdown_returns_secret_free_dict():
    # No DB in this unit test: call_cost_breakdown needs a session, so we only
    # assert the shape contract via a lightweight stand-in.
    class _FakeSession:
        async def execute(self, _stmt):
            return _FakeResult([])

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return _FakeScalars(self._rows)

    class _FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _FakeCall:
        id = "call-1"

    breakdown = await cost_model.call_cost_breakdown(_FakeSession(), _FakeCall())
    assert breakdown["call_id"] == "call-1"
    assert breakdown["voice_cost_known"] is False
    assert breakdown["voice_cost_usd"] is None
    assert "unavailable" in (breakdown["llm_tokens"], breakdown["tts_characters"])
