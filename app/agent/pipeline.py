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
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transports.network.fastapi_websocket import (
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
from app.core.config import settings
from app.core.i18n import (
    deepgram_options,
    elevenlabs_options,
    llm_language_instruction,
)
from app.core.logging import log
from app.db.models import Call, Speaker, Tenant, Turn


async def run_voice_agent(
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
    # ভাষা অনুযায়ী STT সেটিং। nova-3 সব ভাষা কভার করে না -> nova-2 fallback।
    stt_opts = deepgram_options(tenant.language, settings.deepgram_model)
    stt = DeepgramSTTService(
        api_key=settings.deepgram_api_key,
        model=stt_opts["model"],
        live_options={
            "encoding": "mulaw",
            "sample_rate": 8000,
            "punctuate": stt_opts["punctuate"],
            "interim_results": True,     # দ্রুত turn detection-এর জন্য বাধ্যতামূলক
            "endpointing": 250,
            "smart_format": stt_opts["smart_format"],
            # filler_words শুধু ইংরেজি মডেলে আছে
            "filler_words": stt_opts["filler_words"],
            "language": stt_opts["language"],
        },
    )

    llm = build_llm(
        provider=choice.provider,
        model=choice.model,
        temperature=tenant.temperature,   # 0.6-0.8 = বেশি স্বাভাবিক, 0.2 = রোবটিক
        max_tokens=110,                   # শক্ত সীমা: লম্বা উত্তর = মরা কল
    )

    # অ-ইংরেজি হলে multilingual মডেল বাধ্যতামূলক, নইলে ইংরেজি টানে পড়বে
    tts_opts = elevenlabs_options(tenant.language, tenant.voice_id)
    tts = ElevenLabsTTSService(
        api_key=settings.elevenlabs_api_key,
        voice_id=tts_opts["voice_id"] or settings.elevenlabs_voice_id,
        model=tts_opts["model"],                  # flash = সর্বনিম্ন latency
        sample_rate=8000,
        params=ElevenLabsTTSService.InputParams(
            # stability কম = বেশি আবেগ/ওঠানামা = বেশি মানুষের মতো
            # কিন্তু খুব কম হলে উচ্চারণ অস্থির হয়ে যায়
            stability=0.45,
            similarity_boost=0.8,
            style=0.35,                # সামান্য অভিব্যক্তি
            use_speaker_boost=True,
            speed=tenant.speech_speed,  # 1.0 স্বাভাবিক, 1.05-1.1 একটু প্রাণবন্ত
        ),
    )

    # ------------------------------------------------------ tool wiring ---
    handlers = FunctionHandlers(session=session, tenant=tenant, call=call)

    # escalate_to_human is withheld when there is no usable destination or the
    # call cannot be transferred -- see FunctionHandlers.available_tools().
    active_tools = handlers.available_tools()
    if len(active_tools) != len(TOOL_SCHEMAS):
        log.info("tools.escalation_unavailable", call_sid=call_sid,
                 tenant=tenant.name)

    async def _tool_bridge(params):
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
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            *humanizers,                     # "mm-hmm" মাঝপথে
            context_aggregator.user(),
            llm,
            FillerInjector(),                # tool চলাকালীন "let me check"
            TextNormalizer(),                # সংখ্যা/markdown ঠিক করা
            tts,
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
        await session.commit()

    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    finally:
        await _persist_turns()
