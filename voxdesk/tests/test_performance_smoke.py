"""
Step 9 scale/performance — hot-path guard rails.

These are guard rails, not a benchmark suite, and they follow the same
philosophy as tests/test_knowledge_performance.py: the ceilings are several
times the observed median so a busy CI box does not cause flaky failures. What
they catch is a change of *complexity* -- an accidental O(n^2), a hash cost
bumped to production cost in a hot loop, a serializer that suddenly copies the
whole call history.

Run with `-s` to see the measured timings.
"""
from __future__ import annotations

import statistics
import time

from app.auth import password as pw
from app.core.compliance import count_segments
from app.core.data_policy import retention_cutoff
from app.telephony.stream_auth import create_stream_token, verify_stream_token


def _median_ms(fn, runs: int = 5) -> float:
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def _report(capsys, label: str, ms: float, ceiling: float) -> None:
    with capsys.disabled():
        print(f"\n    {label:<40} {ms:8.2f} ms   (ceiling {ceiling:.0f} ms)")


def test_password_hash_cost_stays_bounded(capsys):
    """Hashing must never blow past a sane ceiling; at default cost this is
    ~250ms, and a hot-loop mistake would make it several times that."""
    ms = _median_ms(lambda: pw.hash_password("Correct-Horse-Battery-9!"), runs=3)
    _report(capsys, "bcrypt hash (1x)", ms, 2000)
    assert ms < 2000


def test_stream_token_round_trip_is_fast(capsys):
    """The signed WebSocket token is minted per call -- it must stay cheap."""

    def round_trip():
        token = create_stream_token("CA-perf")
        assert verify_stream_token("CA-perf", token) is True

    ms = _median_ms(round_trip, runs=50)
    _report(capsys, "stream token mint+verify", ms, 5)
    assert ms < 5


def test_message_segment_counting_is_fast(capsys):
    body = "Your appointment is confirmed. " * 40

    ms = _median_ms(lambda: count_segments(body), runs=20)
    _report(capsys, "SMS segment count (~1k chars)", ms, 10)
    assert ms < 10


def test_retention_cutoff_math_is_constant_time(capsys):
    ms = _median_ms(lambda: retention_cutoff(365), runs=100)
    _report(capsys, "retention cutoff", ms, 1)
    assert ms < 1
