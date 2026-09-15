"""Step 7 — call/cost/job metric emission and the bounded-label discipline."""
from __future__ import annotations

from app.core import observability


def _value(counter, *labels) -> float:
    return counter.labels(*labels)._value.get()


def _total(counter) -> float:
    """Sum of a labelled counter across every label value."""
    return sum(c._value.get() for c in counter._metrics.values())


# ------------------------------------------------------------ call signals ---

def test_call_outcome_unknown_collapses_to_unknown():
    before = _value(observability.CALLS_TOTAL, "unknown")
    observability.record_call_outcome("a status that does not exist")
    assert _value(observability.CALLS_TOTAL, "unknown") - before == 1.0


def test_call_outcome_known_values_are_counted_verbatim():
    before = _value(observability.CALLS_TOTAL, "completed")
    observability.record_call_outcome("completed")
    assert _value(observability.CALLS_TOTAL, "completed") - before == 1.0


def test_call_answered_counter_increments():
    before = observability.CALLS_ANSWERED._value.get()
    observability.record_call_answered()
    assert observability.CALLS_ANSWERED._value.get() - before == 1.0


def test_observe_call_duration_ignores_none_and_negatives():
    before = observability.CALL_DURATION._sum.get()
    observability.observe_call_duration(None)
    observability.observe_call_duration(-3)
    assert observability.CALL_DURATION._sum.get() == before


def test_observe_call_duration_records_positive_seconds():
    sum_before = observability.CALL_DURATION._sum.get()
    observability.observe_call_duration(45.0)
    # Histograms expose the running total directly; the observation must add
    # exactly the observed seconds (the count is derived from the buckets).
    assert observability.CALL_DURATION._sum.get() - sum_before == 45.0


# -------------------------------------------------------------- AI usage ---

def test_llm_tokens_unknown_provider_collapses_to_other():
    before = _value(observability.LLM_TOKENS, "other")
    observability.record_llm_tokens("some-tenant-supplied-model", 5)
    assert _value(observability.LLM_TOKENS, "other") - before == 5.0


def test_llm_tokens_known_provider_is_counted():
    before = _value(observability.LLM_TOKENS, "openai")
    observability.record_llm_tokens("openai", 7)
    assert _value(observability.LLM_TOKENS, "openai") - before == 7.0


def test_llm_tokens_ignores_nonpositive():
    before = _total(observability.LLM_TOKENS)
    observability.record_llm_tokens("openai", 0)
    observability.record_llm_tokens("openai", -3)
    assert _total(observability.LLM_TOKENS) == before


def test_tts_and_stt_chars_count_only_positives():
    tts_before = observability.TTS_CHARS._value.get()
    stt_before = observability.STT_CHARS._value.get()
    observability.record_tts_chars(0)
    observability.record_stt_chars(0)
    observability.record_tts_chars(12)
    observability.record_stt_chars(9)
    assert observability.TTS_CHARS._value.get() - tts_before == 12.0
    assert observability.STT_CHARS._value.get() - stt_before == 9.0


# ------------------------------------------------------------------- cost ---

def test_cost_known_price_records_dollars_and_volume():
    usd_before = _value(observability.COST_USD, "voice_minute")
    units_before = _value(observability.COST_UNITS, "voice_minute")
    observability.record_cost("voice_minute", 2.0, unit_price_millicents=1300)
    # 2 minutes * 1300 millicents/minute = 2600 millicents = $0.026
    assert _value(observability.COST_USD, "voice_minute") - usd_before == 0.026
    assert _value(observability.COST_UNITS, "voice_minute") - units_before == 2.0


def test_cost_unknown_price_records_only_unknown_and_volume():
    usd_before = _value(observability.COST_USD, "sms_segment")
    unknown_before = _value(observability.COST_UNKNOWN_UNITS, "sms_segment")
    units_before = _value(observability.COST_UNITS, "sms_segment")
    observability.record_cost("sms_segment", 3.0, unit_price_millicents=None)
    assert _value(observability.COST_USD, "sms_segment") == usd_before
    assert _value(observability.COST_UNKNOWN_UNITS, "sms_segment") - unknown_before == 3.0
    assert _value(observability.COST_UNITS, "sms_segment") - units_before == 3.0


def test_cost_unknown_resource_is_ignored():
    before = _value(observability.COST_UNITS, "voice_minute")
    observability.record_cost("not-a-resource", 5.0, unit_price_millicents=100)
    assert _value(observability.COST_UNITS, "voice_minute") == before


# ------------------------------------------------------------------- jobs ---

def test_job_run_unknown_job_is_dropped():
    before = _total(observability.JOB_RUNS)
    observability.record_job_run("not-a-real-loop", ok=True)
    assert _total(observability.JOB_RUNS) == before


def test_job_run_known_job_records_outcome():
    before = _value(observability.JOB_RUNS, "crm_sync", "success")
    observability.record_job_run("crm_sync", ok=True)
    assert _value(observability.JOB_RUNS, "crm_sync", "success") - before == 1.0


def test_job_last_success_is_set_on_success_only():
    observability.record_job_run("reminders", ok=False)
    import time

    before = observability.JOB_LAST_SUCCESS.labels("reminders")._value.get()
    observability.record_job_run("reminders", ok=True)
    after = observability.JOB_LAST_SUCCESS.labels("reminders")._value.get()
    assert after >= time.time() - 5
    assert after >= before


# ------------------------------------------------------------ stuck effects ---

def test_stuck_side_effects_drop_unknown_kinds():
    observability.set_stuck_side_effects({"crm_sync": 2, "made_up": 99})
    assert observability.STUCK_SIDE_EFFECTS.labels("crm_sync")._value.get() == 2.0
    # "made_up" never touches a label value, so it cannot appear as a series.
    assert ("made_up",) not in observability.STUCK_SIDE_EFFECTS._metrics


# ----------------------------------------------------------- structural ---

def test_label_vocabularies_are_bounded_frozensets():
    """The whole cardinality argument rests on these being closed sets."""
    assert isinstance(observability.CALL_OUTCOMES, frozenset)
    assert isinstance(observability.LLM_PROVIDERS, frozenset)
    assert isinstance(observability.JOBS, frozenset)
    assert isinstance(observability.STUCK_KINDS, frozenset)
    assert isinstance(observability.COST_RESOURCES, frozenset)
