"""Measured AI-usage tracking for the live voice pipeline (Step 7).

The operator questions "how many AI tokens / how many TTS characters / how
many transcription characters did a call use?" are answered here, from the
**provider-reported** usage pipecat 0.0.55 already emits when
``enable_usage_metrics=True`` (the pipeline sets this), plus the transcription
frames Deepgram sends us.

Honesty rules this module exists to uphold:

* Everything counted is *measured* — the LLM's ``total_tokens``, the TTS
  service's reported character count, the transcript text we actually
  received. Nothing is estimated or synthesised.
* The counters go to Prometheus (``app.core.observability``) with **bounded
  labels**; the model string and per-call totals go to the structured log,
  never into a label.
* Cost is recorded through ``app.billing.cost``, which marks UNKNOWN whenever
  no price is configured — a missing price never becomes a made-up number.
* The tracker is pass-through and cannot fail the call: every observation is
  wrapped so a metrics bug can never drop a frame or crash the pipeline.
"""
from __future__ import annotations

from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    MetricsFrame,
    TranscriptionFrame,
)
from pipecat.metrics.metrics import LLMUsageMetricsData, TTSUsageMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.billing import cost as cost_tracking
from app.core import observability


class UsageTracker(FrameProcessor):
    """Pass-through frame processor that tallies measured AI usage.

    Two instances are used in the pipeline:

    * ``UsageTracker(track_stt=True)`` after the STT service — counts the
      final transcription text.
    * ``UsageTracker(track_voice=True, provider=...)`` between the TTS service
      and the transport output — catches the LLM and TTS usage-metrics frames
      both services emit downstream.

    ``process_frame`` never raises: an unexpected frame shape is skipped and
    the frame still flows on.
    """

    def __init__(
        self,
        *,
        track_stt: bool = False,
        track_voice: bool = False,
        provider: str | None = None,
    ):
        super().__init__()
        self._track_stt = track_stt
        self._track_voice = track_voice
        self._provider = provider
        self._stt_chars = 0
        self._tts_chars = 0
        self._llm_tokens = 0

    # ------------------------------------------------------------- counting ---

    def _count_metrics_frame(self, frame: MetricsFrame) -> None:
        if not self._track_voice:
            return
        for item in frame.data or []:
            if isinstance(item, LLMUsageMetricsData):
                tokens = int((item.value and item.value.total_tokens) or 0)
                if tokens > 0:
                    self._llm_tokens += tokens
                    observability.record_llm_tokens(self._provider, tokens)
                    cost_tracking.record_cost(
                        "llm_token", float(tokens), provider=self._provider
                    )
            elif isinstance(item, TTSUsageMetricsData):
                chars = int(item.value or 0)
                if chars > 0:
                    self._tts_chars += chars
                    observability.record_tts_chars(chars)
                    cost_tracking.record_cost("tts_character", float(chars))

    def _count_transcription(self, frame: TranscriptionFrame) -> None:
        if not self._track_stt or not frame.text:
            return
        chars = len(frame.text)
        self._stt_chars += chars
        observability.record_stt_chars(chars)

    # --------------------------------------------------------------- frames ---

    async def process_frame(self, frame, direction: FrameDirection):
        # Pass through first: observation must never gate the audio graph.
        await self.push_frame(frame, direction)
        try:
            if isinstance(frame, MetricsFrame):
                self._count_metrics_frame(frame)
            elif isinstance(frame, TranscriptionFrame) and not isinstance(
                frame, InterimTranscriptionFrame
            ):
                self._count_transcription(frame)
        except Exception:  # noqa: BLE001 - metrics must never break a live call
            pass

    # ------------------------------------------------------------- snapshot ---

    def snapshot(self) -> dict:
        """The call's measured AI usage, safe to log (no secrets, no PII)."""
        return {
            "stt_chars": self._stt_chars,
            "tts_chars": self._tts_chars,
            "llm_tokens": self._llm_tokens,
            "llm_provider": self._provider,
        }
