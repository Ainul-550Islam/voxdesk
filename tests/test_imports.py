"""Step 1 — dependency/import startup contract.

The voice pipeline imports pipecat both eagerly (pipeline.py) and lazily
(llm_factory.py). A version bump that flattens or moves a pipecat submodule
used to surface only on the first live call; these tests pin the exact import
paths the code relies on so the failure shows up in CI instead.
"""
from __future__ import annotations


def test_agent_pipeline_imports_cleanly():
    # Eager pipecat imports live at module top: VAD, serializer, STT, TTS,
    # transport, aggregators. Importing the module proves they all resolve.
    import app.agent.pipeline  # noqa: F401


def test_llm_factory_imports_cleanly():
    import app.agent.llm_factory  # noqa: F401


def test_agent_support_modules_import_cleanly():
    import app.agent.functions  # noqa: F401
    import app.agent.humanize  # noqa: F401
    import app.agent.prompts  # noqa: F401


def test_pipecat_service_import_paths_used_by_the_code():
    """The exact paths app/agent/llm_factory.py, pipeline.py, stt.py and tts.py
    import from.

    pipecat 0.0.55 shipped several of these as flat modules
    (``pipecat.services.openai``); 0.0.94 split each provider into a package
    whose ``__init__`` is a ``DeprecatedModuleProxy`` and whose canonical
    module lives one level deeper (``pipecat.services.openai.llm``). The code
    was updated to the canonical paths so a further pipecat upgrade that
    removes the deprecation proxies fails here first instead of on a live
    call.
    """
    from pipecat.services.anthropic.llm import AnthropicLLMService  # noqa: F401
    from pipecat.services.deepgram.stt import DeepgramSTTService  # noqa: F401
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSService  # noqa: F401
    from pipecat.services.google.llm import GoogleLLMService  # noqa: F401
    from pipecat.services.openai.llm import OpenAILLMService  # noqa: F401


def test_pipecat_runtime_api_surface_is_present():
    """Symbols the pipeline calls on the services/task/transport classes."""
    from pipecat.pipeline.task import PipelineTask
    from pipecat.services.openai.llm import OpenAILLMService
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketTransport,
    )

    assert hasattr(OpenAILLMService, "register_function")
    assert hasattr(OpenAILLMService, "create_context_aggregator")
    assert hasattr(PipelineTask, "stop_when_done")
    assert hasattr(PipelineTask, "cancel")
    assert hasattr(PipelineTask, "queue_frames")
    assert hasattr(FastAPIWebsocketTransport, "event_handler")
