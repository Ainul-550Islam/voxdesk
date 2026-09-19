"""Conversation-level metrics from transcript turns (Phase 4, analytics slice).

Computed from ``Turn``-shaped records: speaker role, text, and the optional
per-turn fields the database already carries (``latency_ms``) or a future
audio tap will add (``start_ms``/``end_ms``). Everything is a pure,
deterministic function of its inputs — no database, no network, no state — so
the same turns always yield the same metrics and the module can be unit-tested
without infrastructure.

Timing honesty: talk-time and silence metrics are computed only when turn
timing is present on *every* turn; otherwise those fields are ``None`` rather
than a fabricated number. The sentiment figure is a **lexical proxy** (a small,
documented word list), never presented as a learned model. Response-latency
percentiles here fill the ``p95_response_ms`` gap the DB roll-up service
explicitly defers.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.analytics._stats import mean, percentile

USER = "user"
ASSISTANT = "assistant"
SYSTEM = "system"
SPEAKERS = (USER, ASSISTANT, SYSTEM)

#: Marker lexicons for the sentiment proxy. Small and deterministic on
#: purpose: this is a heuristic, not a model, and it is documented as such
#: wherever its output is surfaced.
_POSITIVE_WORDS = frozenset({
    "great", "good", "thanks", "thank", "excellent", "perfect", "awesome",
    "love", "happy", "wonderful", "helpful", "appreciate", "yes", "sure",
    "please", "booked", "confirmed", "done", "resolved",
})
_NEGATIVE_WORDS = frozenset({
    "bad", "terrible", "awful", "hate", "angry", "unhappy", "no", "never",
    "cancel", "cancelled", "problem", "issue", "wrong", "slow", "refund",
    "useless", "unhelpful", "complaint",
})

_TOKEN_RE = re.compile(r"[^a-z0-9\s]")


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, split on whitespace."""
    return _TOKEN_RE.sub(" ", text.lower()).split()


def sentiment_score(text: str) -> float:
    """Lexical sentiment proxy in [-1, 1].

    Counts positive vs negative marker words (case-insensitive, whole tokens),
    then ``(pos - neg) / (pos + neg + 1)`` — the smoothing term of 1 keeps
    short, neutral text near 0 instead of dividing by zero. A proxy, not a
    model.
    """
    if not text.strip():
        return 0.0
    tokens = _tokenize(text)
    pos = sum(1 for token in tokens if token in _POSITIVE_WORDS)
    neg = sum(1 for token in tokens if token in _NEGATIVE_WORDS)
    return round((pos - neg) / (pos + neg + 1.0), 4)


@dataclass(frozen=True)
class TurnRecord:
    """One transcript turn, shaped like ``app.db.models.Turn``.

    ``speaker`` is one of ``user``/``assistant``/``system`` (the ``Speaker``
    enum values). ``latency_ms`` is seconds-to-first-response when recorded.
    ``start_ms``/``end_ms`` are optional turn timing (a future audio tap);
    they must be present together on every turn, or on none.
    """

    speaker: str
    text: str = ""
    latency_ms: float | None = None
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass(frozen=True)
class ConversationMetrics:
    """Aggregate-safe metrics for one call. Counts, rates and timings only —
    never a transcript, name, number or recording."""

    turns: int = 0
    words: int = 0
    turns_by_speaker: dict[str, int] = field(default_factory=dict)
    words_by_speaker: dict[str, int] = field(default_factory=dict)
    total_duration_ms: int | None = None
    talk_ms_by_speaker: dict[str, int] = field(default_factory=dict)
    silence_ms: int | None = None
    response_latencies_ms: tuple[float, ...] = ()
    avg_response_latency_ms: float | None = None
    p50_response_latency_ms: float | None = None
    p95_response_latency_ms: float | None = None
    sentiment_by_speaker: dict[str, float] = field(default_factory=dict)

    # -- derived ------------------------------------------------------------
    def words_per_turn(self, speaker: str) -> float:
        turns = self.turns_by_speaker.get(speaker, 0)
        if not turns:
            return 0.0
        return round(self.words_by_speaker.get(speaker, 0) / turns, 2)

    def turns_per_minute(self) -> float | None:
        if self.total_duration_ms is None or self.total_duration_ms <= 0:
            return None
        return round(self.turns / (self.total_duration_ms / 60000.0), 2)

    def words_per_minute(self) -> float | None:
        if self.total_duration_ms is None or self.total_duration_ms <= 0:
            return None
        return round(self.words / (self.total_duration_ms / 60000.0), 1)

    def talk_ratio(self, speaker: str) -> float | None:
        """Share of total duration this speaker talked, 0.0-1.0. ``None``
        when no turn timing is present (an empty ``talk_ms_by_speaker`` means
        timing was never supplied, so a ratio would be a fabrication)."""
        if not self.talk_ms_by_speaker:
            return None
        if self.total_duration_ms is None or self.total_duration_ms <= 0:
            return None
        return round(self.talk_ms_by_speaker.get(speaker, 0) / self.total_duration_ms, 4)

    def silence_ratio(self) -> float | None:
        if self.total_duration_ms is None or self.silence_ms is None or self.total_duration_ms <= 0:
            return None
        return round(self.silence_ms / self.total_duration_ms, 4)

    def sentiment(self, speaker: str | None = None) -> float:
        """Lexical sentiment proxy in [-1, 1]; all speakers if none given."""
        if speaker is not None:
            return self.sentiment_by_speaker.get(speaker, 0.0)
        values = [v for v in self.sentiment_by_speaker.values() if v != 0.0]
        if not values:
            return 0.0
        return round(mean(values), 4)


def _validate_turns(turns: Sequence[TurnRecord]) -> None:
    for turn in turns:
        if turn.speaker not in SPEAKERS:
            raise ValueError(f"unknown speaker {turn.speaker!r}")
        if turn.latency_ms is not None and turn.latency_ms < 0:
            raise ValueError("latency_ms must be >= 0")
        if (turn.start_ms is None) != (turn.end_ms is None):
            raise ValueError("start_ms and end_ms must be set together")
        if turn.start_ms is not None:
            if turn.start_ms < 0 or turn.end_ms < 0:
                raise ValueError("turn timing must be >= 0")
            if turn.end_ms < turn.start_ms:
                raise ValueError("turn end_ms must be >= start_ms")
    timed = [turn.start_ms is not None for turn in turns]
    if timed and any(timed) and not all(timed):
        raise ValueError("turn timing must be present on every turn or on none")


def compute_conversation_metrics(
    turns: Sequence[TurnRecord], total_duration_ms: int | None = None
) -> ConversationMetrics:
    """Compute one call's metrics.

    ``total_duration_ms`` is the call's recorded duration
    (``Call.duration_seconds * 1000``); when omitted and turn timing is
    present it falls back to the span between the first turn's start and the
    last turn's end.
    """
    if total_duration_ms is not None and total_duration_ms < 0:
        raise ValueError("total_duration_ms must be >= 0")
    _validate_turns(turns)

    turns_by_speaker: Counter[str] = Counter()
    words_by_speaker: Counter[str] = Counter()
    talk_by_speaker: Counter[str] = Counter()
    latencies: list[float] = []
    total_words = 0
    timed = bool(turns) and all(turn.start_ms is not None for turn in turns)

    for turn in turns:
        turns_by_speaker[turn.speaker] += 1
        words = len(_tokenize(turn.text))
        words_by_speaker[turn.speaker] += words
        total_words += words
        if turn.latency_ms is not None:
            latencies.append(float(turn.latency_ms))
        if turn.start_ms is not None:
            talk_by_speaker[turn.speaker] += turn.end_ms - turn.start_ms

    duration = total_duration_ms
    talk_ms_by_speaker: dict[str, int] = {}
    silence_ms: int | None = None
    if timed:
        if duration is None:
            first = min(turn.start_ms for turn in turns)
            last = max(turn.end_ms for turn in turns)
            duration = last - first
        talk_ms_by_speaker = {speaker: talk_by_speaker[speaker] for speaker in SPEAKERS}
        silence_ms = max(0, duration - sum(talk_by_speaker.values()))

    sentiments = {
        speaker: sentiment_score(" ".join(turn.text for turn in turns if turn.speaker == speaker))
        for speaker in SPEAKERS
    }

    return ConversationMetrics(
        turns=len(turns),
        words=total_words,
        turns_by_speaker={speaker: turns_by_speaker[speaker] for speaker in SPEAKERS},
        words_by_speaker={speaker: words_by_speaker[speaker] for speaker in SPEAKERS},
        total_duration_ms=duration,
        talk_ms_by_speaker=talk_ms_by_speaker,
        silence_ms=silence_ms,
        response_latencies_ms=tuple(latencies),
        avg_response_latency_ms=round(mean(latencies), 2) if latencies else None,
        p50_response_latency_ms=round(percentile(latencies, 50.0), 2) if latencies else None,
        p95_response_latency_ms=round(percentile(latencies, 95.0), 2) if latencies else None,
        sentiment_by_speaker=sentiments,
    )


@dataclass(frozen=True)
class AggregateMetrics:
    """Several calls rolled into one aggregate. Means of rates, p95 of the
    pooled response latencies."""

    calls: int = 0
    avg_turns: float = 0.0
    avg_words: float = 0.0
    avg_turns_per_minute: float | None = None
    avg_words_per_minute: float | None = None
    p95_response_latency_ms: float | None = None
    avg_silence_ratio: float | None = None
    avg_user_talk_ratio: float | None = None
    avg_sentiment: float = 0.0


def aggregate(metrics_list: Sequence[ConversationMetrics]) -> AggregateMetrics:
    """Roll several calls into one tenant-level aggregate."""
    if not metrics_list:
        return AggregateMetrics()

    latencies = [v for m in metrics_list for v in m.response_latencies_ms]
    tpms = [m.turns_per_minute() for m in metrics_list]
    wpms = [m.words_per_minute() for m in metrics_list]
    silence_ratios = [m.silence_ratio() for m in metrics_list]
    user_talk_ratios = [m.talk_ratio(USER) for m in metrics_list]
    sentiments = [m.sentiment() for m in metrics_list]

    return AggregateMetrics(
        calls=len(metrics_list),
        avg_turns=round(mean([m.turns for m in metrics_list]), 2),
        avg_words=round(mean([m.words for m in metrics_list]), 1),
        avg_turns_per_minute=(
            round(mean([v for v in tpms if v is not None]), 2)
            if any(v is not None for v in tpms)
            else None
        ),
        avg_words_per_minute=(
            round(mean([v for v in wpms if v is not None]), 1)
            if any(v is not None for v in wpms)
            else None
        ),
        p95_response_latency_ms=(
            round(percentile(latencies, 95.0), 2) if latencies else None
        ),
        avg_silence_ratio=(
            round(mean([v for v in silence_ratios if v is not None]), 4)
            if any(v is not None for v in silence_ratios)
            else None
        ),
        avg_user_talk_ratio=(
            round(mean([v for v in user_talk_ratios if v is not None]), 4)
            if any(v is not None for v in user_talk_ratios)
            else None
        ),
        avg_sentiment=round(mean(sentiments), 4),
    )
