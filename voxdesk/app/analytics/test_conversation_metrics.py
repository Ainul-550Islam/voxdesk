"""Unit tests for app.analytics.conversation_metrics.

Co-located with the package (not under tests/) for the same reason as
test_stats.py: the repo conftest needs the SQLAlchemy/async stack. Run with:
python -m pytest app/analytics/ -q
"""

from __future__ import annotations

import pytest

from app.analytics.conversation_metrics import (
    ASSISTANT,
    SYSTEM,
    USER,
    TurnRecord,
    aggregate,
    compute_conversation_metrics,
    sentiment_score,
)


def turn(speaker, text="", latency_ms=None, start_ms=None, end_ms=None):
    return TurnRecord(
        speaker=speaker,
        text=text,
        latency_ms=latency_ms,
        start_ms=start_ms,
        end_ms=end_ms,
    )


def test_sentiment_proxy_polarity():
    assert sentiment_score("this was great and helpful, thank you") > 0
    assert sentiment_score("this was terrible and slow, a real problem") < 0
    assert sentiment_score("") == 0.0
    assert sentiment_score("the cat sat on the mat") == 0.0
    # Mixed evens out toward zero.
    assert abs(sentiment_score("great but slow")) < 0.2


def test_empty_turns_are_all_zero():
    m = compute_conversation_metrics([])
    assert m.turns == 0
    assert m.words == 0
    assert m.turns_by_speaker[USER] == 0
    assert m.avg_response_latency_ms is None
    assert m.p95_response_latency_ms is None
    assert m.silence_ms is None
    assert m.sentiment(USER) == 0.0


def test_word_and_turn_counts_per_speaker():
    m = compute_conversation_metrics([
        turn(USER, "hello can you help me"),
        turn(ASSISTANT, "of course I can help"),
        turn(USER, "thanks"),
    ])
    assert m.turns == 3
    assert m.words == 11  # 5 + 5 + 1
    assert m.turns_by_speaker[USER] == 2
    assert m.turns_by_speaker[ASSISTANT] == 1
    assert m.turns_by_speaker[SYSTEM] == 0
    assert m.words_per_turn(USER) == 3.0  # (5+1)/2


def test_word_counts_exact():
    m = compute_conversation_metrics([
        turn(USER, "hello can you help me"),
        turn(ASSISTANT, "of course I can help"),
        turn(USER, "thanks"),
    ])
    assert m.words == 11
    assert m.words_by_speaker[USER] == 6
    assert m.words_by_speaker[ASSISTANT] == 5


def test_latency_percentiles_interpolate():
    m = compute_conversation_metrics([
        turn(USER, latency_ms=10),
        turn(ASSISTANT, latency_ms=20),
        turn(USER, latency_ms=30),
        turn(ASSISTANT, latency_ms=40),
    ])
    assert m.avg_response_latency_ms == 25.0
    assert m.p50_response_latency_ms == 25.0
    # rank = 0.95 * 3 = 2.85 → 30*(1-.85) + 40*.85 = 38.5
    assert m.p95_response_latency_ms == 38.5


def test_talk_and_silence_with_timing():
    m = compute_conversation_metrics(
        [
            turn(USER, "hi", start_ms=0, end_ms=2000),
            turn(ASSISTANT, "hello", start_ms=3000, end_ms=4000),
        ],
        total_duration_ms=10_000,
    )
    assert m.talk_ms_by_speaker[USER] == 2000
    assert m.talk_ms_by_speaker[ASSISTANT] == 1000
    assert m.silence_ms == 7000
    assert m.talk_ratio(USER) == 0.2
    assert m.silence_ratio() == 0.7


def test_duration_falls_back_to_turn_span():
    m = compute_conversation_metrics([
        turn(USER, "hi", start_ms=1000, end_ms=2000),
        turn(ASSISTANT, "hello", start_ms=3000, end_ms=4000),
    ])
    assert m.total_duration_ms == 3000  # 4000 - 1000
    assert m.silence_ms == 1000  # 3000 - (1000 + 1000)


def test_rates_need_duration():
    m = compute_conversation_metrics([
        turn(USER, "hello there"),
        turn(ASSISTANT, "hi"),
    ])
    assert m.turns_per_minute() is None
    assert m.words_per_minute() is None
    assert m.talk_ratio(USER) is None

    m2 = compute_conversation_metrics(
        [turn(USER, "a b c d"), turn(ASSISTANT, "e f")],
        total_duration_ms=60_000,
    )
    assert m2.turns_per_minute() == 2.0
    assert m2.words_per_minute() == 6.0


def test_sentiment_by_speaker():
    m = compute_conversation_metrics([
        turn(USER, "this is a terrible problem"),
        turn(ASSISTANT, "happy to help, all done and confirmed"),
    ])
    assert m.sentiment_by_speaker[USER] < 0
    assert m.sentiment_by_speaker[ASSISTANT] > 0
    assert m.sentiment(USER) < 0
    # overall is the mean of non-zero speaker sentiments
    assert -1.0 <= m.sentiment() <= 1.0


def test_validation_errors():
    with pytest.raises(ValueError):
        compute_conversation_metrics([turn("robot", "beep")])  # unknown speaker
    with pytest.raises(ValueError):
        compute_conversation_metrics([turn(USER, latency_ms=-1)])
    with pytest.raises(ValueError):
        compute_conversation_metrics([turn(USER, start_ms=5000, end_ms=1000)])
    with pytest.raises(ValueError):
        compute_conversation_metrics([turn(USER, start_ms=1000)])  # end missing
    with pytest.raises(ValueError):
        # mixed timing across turns
        compute_conversation_metrics([
            turn(USER, start_ms=0, end_ms=1000),
            turn(ASSISTANT),
        ])
    with pytest.raises(ValueError):
        compute_conversation_metrics([], total_duration_ms=-5)


def test_aggregate_pools_latency_and_averages_rates():
    a = compute_conversation_metrics(
        [turn(USER, latency_ms=10), turn(ASSISTANT, latency_ms=20)],
        total_duration_ms=60_000,
    )
    b = compute_conversation_metrics(
        [turn(USER, latency_ms=30), turn(ASSISTANT, latency_ms=40), turn(USER, latency_ms=50)],
        total_duration_ms=120_000,
    )
    agg = aggregate([a, b])
    assert agg.calls == 2
    assert agg.avg_turns == 2.5
    # pooled latencies [10,20,30,40,50] → p95 rank 0.95*4=3.8 → 40*.2+50*.8=48
    assert agg.p95_response_latency_ms == 48.0
    assert agg.avg_turns_per_minute == pytest.approx((2.0 + 1.5) / 2, abs=0.01)
    assert agg.avg_silence_ratio is None  # no timing on either call
    assert agg.avg_user_talk_ratio is None


def test_aggregate_empty():
    assert aggregate([]).calls == 0


def test_aggregate_timing_means_ignore_untimed_calls():
    timed = compute_conversation_metrics(
        [
            turn(USER, "hi", start_ms=0, end_ms=2000),
            turn(ASSISTANT, "yo", start_ms=3000, end_ms=4000),
        ],
        total_duration_ms=10_000,
    )
    untimed = compute_conversation_metrics([turn(USER, "hi"), turn(ASSISTANT, "yo")])
    agg = aggregate([timed, untimed])
    assert agg.avg_silence_ratio == 0.7  # only the timed call contributes
    assert agg.avg_user_talk_ratio == 0.2
