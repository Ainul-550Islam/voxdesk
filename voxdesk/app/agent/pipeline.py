"""THE CORE FILE — এখানেই সব হয়।

অডিও চেইন:
  Twilio (mu-law 8kHz)
    -> Silero VAD          কে কখন থামল          ~ 30ms
    -> Deepgram STT        কথা -> টেক্সট         ~150ms
    -> Backchannel         "mm-hmm" (মানুষের মতো)
    -> LLM  (ChatGPT / Claude / Gemini — tenant বেছে নেয়)   ~250-300ms
    -> FillerInjector      tool চলার সময় "let me check"
    -> TextNormalizer      "$150" -> "one hundred fifty dollars"
    -> ElevenLabs TTS      টেক্সট -> কথা          ~ 90ms
    -> Twilio out
                           মোট প্রথম শব্দ: ~550-750ms
"""
from __future__ import annotations

from fastapi import WebSocket
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.functions import TOOL_SCHEMAS, FunctionHandlers
from app.agent.humanize import (
    Backchannel,
    FillerInjector,
    TextNormalizer,
    vary_greeting,
)
from app.agent.llm_factory import build_llm, resolve
from app.agent.prompts import build_system_prompt
from app.agent.stt import build_stt
from app.agent.tts import build_tts
from app.agent.usage_tracker import UsageTracker
from app.core.config import settings
from app.core.i18n import llm_language_instruction
from app.core.logging import log
from app.db.models import Call, Speaker, Tenant, Turn


#: STEP 9 (item N): ceiling on tool invocations per live call. The text path
#: caps tool *rounds* at 4; the voice path caps individual tool calls at 12 so
#: a prompt-injected or looped model cannot run unlimited paid operations.
MAX_TOOL_CALLS_PER_CALL = 12


async def run_voice_agent(
    websocket: WebSocket,
    stream_sid: str,
    call_sid: str,
    session: AsyncSession,
    tenant: Tenant,
    call: Call,
) -> None:
    """Run the voice pipeline inside a call-scoped correlation context.

    Every log line the call produces — provider selection, fallback, TTS
    normalisation, tool calls, usage, crashes — then carries the same
    ``call_sid``/``call_id``/``tenant_id``, which is what turns a single
    call's scattered logs into one trace (Step 7 correlation).
    """
    from app.core.correlation import correlation_scope

    with correlation_scope(
        call_sid=call_sid, call_id=str(call.id), tenant_id=str(tenant.id)
    ):
        await _run_voice_agent(
            websocket, stream_sid, call_sid, session, tenant, call
        )


async def _run_voice_agent(
    websocket: WebSocket,
    stream_sid: str,
    call_sid: str,
    session: AsyncSession,
    tenant: Tenant,
    call: Call,
) -> None:
    """একটা ফোন কল শুরু থেকে শেষ পর্যন্ত চালায়।"""

    # ---------------------------------------------------- LLM নির্বাচন ----
    choice = resolve(tenant)
    log.info("llm.selected", provider=choice.provider, model=choice.model,
             tenant=tenant.name, note=choice.notes)

    # ---------------------------------------------------------- transport --
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=TwilioFrameSerializer(
                stream_sid=stream_sid,
                call_sid=call_sid,
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token,
            ),
            # ==== barge-in এখানে ====
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    # stop_secs = সবচেয়ে গুরুত্বপূর্ণ নব
                    #   0.30 -> খুব দ্রুত, কিন্তু কাস্টমারের কথা কেটে দেবে
                    #   0.45 -> ভারসাম্য (ডিফল্ট)
                    #   0.70 -> নিরাপদ, কিন্তু ধীর/মৃত মনে হবে
                    stop_secs=tenant.vad_stop_secs,
                    start_secs=0.15,
                    confidence=0.7,
                    min_volume=0.6,
                )
            ),
        ),
    )

    # ---------------------------------------------------------- services ---
    # Each builder validates its own configuration and raises a typed
    # ProviderError (configuration_error / unsupported_feature) *before* any
    # network activity, so a misconfigured deployment fails fast instead of
    # starting a call that can never work.
    stt = build_stt(tenant)

    llm = build_llm(
        provider=choice.provider,
        model=choice.model,
        temperature=tenant.temperature,   # 0.6-0.8 = বেশি স্বাভাবিক, 0.2 = রোবটিক
        max_tokens=110,                   # শক্ত সীমা: লম্বা উত্তর = মরা কল
    )

    # অ-ইংরেজি হলে multilingual মডেল বাধ্যতামূলক, নইলে ইংরেজি টানে পড়বে।
    # The whole ElevenLabs mapping (model/voice resolution, voice settings,
    # speech speed) lives in app/agent/tts.py so the provider contract is
    # isolated from the pipeline.
    tts = build_tts(tenant)

    # ------------------------------------------------------ tool wiring ---
    handlers = FunctionHandlers(session=session, tenant=tenant, call=call)

    # escalate_to_human is withheld when there is no usable destination or the
    # call cannot be transferred -- see FunctionHandlers.available_tools().
    active_tools = handlers.available_tools()
    if len(active_tools) != len(TOOL_SCHEMAS):
        log.info("tools.escalation_unavailable", call_sid=call_sid,
                 tenant=tenant.name)

    tool_calls_this_call = 0

    async def _tool_bridge(params):
        # STEP 9 (item N): a per-call ceiling on tool invocations, mirroring the
        # text path's MAX_TOOL_ROUNDS. The LLM decides what tools to call and is
        # never trusted to stop on its own -- a prompt-injected or looped model
        # cannot run unlimited tools (each of which costs money and may touch
        # the calendar/CRM).
        nonlocal tool_calls_this_call
        tool_calls_this_call += 1
        if tool_calls_this_call > MAX_TOOL_CALLS_PER_CALL:
            log.warning("tools.per_call_limit", call_sid=call_sid,
                        calls=tool_calls_this_call)
            await params.result_callback(
                {"ok": False, "message": "I can't do that right now. Offer to take a message."}
            )
            return
        result = await handlers.dispatch(params.function_name, params.arguments or {})
        log.info("tool.called", name=params.function_name, ok=result.get("ok"),
                 outcome=result.get("outcome"))
        await params.result_callback(result)

        # A successful escalation has already told Twilio to redirect this
        # call, so our media stream is about to be torn down. Ending the task
        # ourselves makes that orderly instead of surfacing as a stream crash.
        if (
            params.function_name == "escalate_to_human"
            and result.get("outcome") == "TRANSFER_STARTED"
        ):
            log.info("pipeline.stopping_for_transfer", call_sid=call_sid)
            await task.stop_when_done()

    for schema in active_tools:
        llm.register_function(schema["function"]["name"], _tool_bridge)

    # --------------------------------------------------------- context ----
    greeting = vary_greeting(tenant.greeting, tenant.name, tenant.agent_name)

    context = OpenAILLMContext(
        messages=[
            {
                "role": "system",
                "content": (
                    build_system_prompt(tenant, choice.provider)
                    + llm_language_instruction(tenant.language)
                ),
            },
            {"role": "assistant", "content": greeting},
        ],
        tools=active_tools,
    )
    context_aggregator = llm.create_context_aggregator(context)

    # ------------------------------------------------- মানুষের মতো লেয়ার --
    humanizers = []
    if tenant.humanize:
        humanizers = [
            Backchannel(after_seconds=3.0, cooldown=15.0),   # STT-এর পরে
        ]

    # -------------------------------------------------------- pipeline ----
    # Step 7 usage trackers. Pass-through processors that tally the measured
    # AI usage (STT characters, LLM tokens, TTS characters) and feed the
    # Prometheus counters + cost model. They never raise and never gate a
    # frame — see app/agent/usage_tracker.py.
    stt_usage = UsageTracker(track_stt=True)
    voice_usage = UsageTracker(track_voice=True, provider=choice.provider)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            stt_usage,                       # measured transcription characters
            *humanizers,                     # "mm-hmm" মাঝপথে
            context_aggregator.user(),
            llm,
            FillerInjector(),                # tool চলাকালীন "let me check"
            TextNormalizer(),                # সংখ্যা/markdown ঠিক করা
            tts,
            voice_usage,                     # measured LLM tokens + TTS characters
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,        # <-- barge-in ON
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
        ),
    )

    # ------------------------------------------------------- lifecycle ----
    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _client):
        log.info("call.connected", call_sid=call_sid, tenant=tenant.name,
                 llm=f"{choice.provider}/{choice.model}")
        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _client):
        log.info("call.disconnected", call_sid=call_sid)
        await task.cancel()

    async def _persist_turns() -> None:
        """
        Flush the conversation to `turns`.

        SYSTEM turns written by the transfer service are never touched here --
        this only appends user/assistant utterances.
        """
        for msg in context.get_messages():
            role, content = msg.get("role"), msg.get("content")
            if not content or role == "system":
                continue
            session.add(
                Turn(
                    call_id=call.id,
                    speaker=Speaker.USER if role == "user" else Speaker.ASSISTANT,
                    text=content if isinstance(content, str) else str(content),
                )
            )
        call.llm_used = f"{choice.provider}/{choice.model}"
        # Step 7: the single-call AI-usage trace. Model string and counts are
        # log fields (correlation), never Prometheus labels — that is what
        # makes a per-call trace possible without unbounded cardinality.
        log.info(
            "call.usage",
            call_sid=call_sid,
            tenant=tenant.name,
            llm=f"{choice.provider}/{choice.model}",
            stt_chars=stt_usage.snapshot()["stt_chars"],
            tts_chars=voice_usage.snapshot()["tts_chars"],
            llm_tokens=voice_usage.snapshot()["llm_tokens"],
        )
        await session.commit()

    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    except Exception:
        # A pipeline-level failure must propagate to the caller (the media
        # stream handler) so the call can be finalised accurately; it must not
        # be masked by the persistence step below.
        log.exception("pipeline.run_failed", call_sid=call_sid,
                      tenant=tenant.name)
        raise
    finally:
        # Persisting turns is best-effort cleanup. It must never mask the
        # primary outcome of the call (a provider crash, a hangup) and never
        # turn a finished call into a crash for the caller.
        try:
            await _persist_turns()
        except Exception:
            log.exception("pipeline.persist_turns_failed", call_sid=call_sid,
                          tenant=tenant.name)
