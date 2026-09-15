"""Multi-provider LLM factory.

একই কোডে তিনটা AI চলে — ক্লায়েন্ট প্রতি আলাদা করে বেছে নেওয়া যায়:

    openai     -> gpt-4o-mini        সবচেয়ে দ্রুত, সস্তা, tool calling নিখুঁত
    anthropic  -> claude-haiku-4-5   সবচেয়ে natural কথা বলে, কম hallucinate করে
    google     -> gemini-2.0-flash   সবচেয়ে সস্তা, বহু ভাষা, লম্বা context

কেন তিনটাই দরকার?
  1. একটা provider ডাউন হলে fallback লাগে (ফোন কল অপেক্ষা করে না)
  2. ক্লায়েন্টভেদে খরচ/মান আলাদা — ডেন্টাল ক্লিনিকে haiku, রেস্টুরেন্টে flash
  3. Upwork-এ "multi-LLM, provider-agnostic" লিখলে রেট বাড়ে

Failure behaviour (Step 3)
--------------------------
* An unknown provider name is an ``UnsupportedProviderFeatureError`` — it
  fails clearly instead of silently falling back to whichever provider has a
  key, so a tenant with a typo'd provider does not get a different AI without
  anyone noticing.
* A known provider whose key is missing falls back to the next configured
  provider (existing behaviour). Only when **no** provider has a key do we
  raise ``ProviderConfigurationError``.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.agent.errors import (
    ProviderConfigurationError,
    UnsupportedProviderFeatureError,
)
from app.core.config import settings
from app.core.logging import log


@dataclass(frozen=True)
class LLMChoice:
    provider: str          # openai | anthropic | google
    model: str
    est_latency_ms: int    # first-token, রেফারেন্স হিসেবে
    notes: str


# ক্লায়েন্টকে দেখানোর জন্য প্রিসেট
PRESETS: dict[str, LLMChoice] = {
    "fast": LLMChoice("openai", "gpt-4o-mini", 250, "সবচেয়ে দ্রুত, tool calling নিখুঁত"),
    "natural": LLMChoice("anthropic", "claude-haiku-4-5", 300, "সবচেয়ে মানুষের মতো কথা"),
    "cheap": LLMChoice("google", "gemini-2.0-flash", 280, "সবচেয়ে সস্তা, বহুভাষী"),
    "smart": LLMChoice("anthropic", "claude-sonnet-4-5", 600, "জটিল কথোপকথন, ধীর"),
}

#: The providers this factory can actually construct a service for.
SUPPORTED_PROVIDERS = ("openai", "anthropic", "google")

#: What each LLM provider integration can and cannot express. The pipeline
#: consults this before registering tools: the agent's booking/escalation
#: tools are business-critical, so a provider that cannot do tool-calling
#: must fail clearly rather than silently ignore the tool schemas. Streaming
#: and interruptions are declared here for the same reason — an "unsupported"
#: entry means the pipeline must not silently promise barge-in it cannot give.
LLM_CAPABILITIES: dict[str, dict[str, bool]] = {
    "openai": {
        "supports_tool_calling": True,
        "supports_streaming": True,
        "supports_interruptions": True,
    },
    "anthropic": {
        "supports_tool_calling": True,
        "supports_streaming": True,
        "supports_interruptions": True,
    },
    "google": {
        "supports_tool_calling": True,
        "supports_streaming": True,
        "supports_interruptions": True,
    },
}

# provider ডাউন হলে এই ক্রমে চেষ্টা হবে
FALLBACK_ORDER = ["openai", "anthropic", "google"]


def _has_key(provider: str) -> bool:
    return bool(
        {
            "openai": settings.openai_api_key,
            "anthropic": settings.anthropic_api_key,
            "google": settings.google_api_key,
        }.get(provider)
    )


def llm_capabilities(provider: str) -> dict[str, bool]:
    """The capability contract for a provider; empty dict when unknown.

    An unknown provider has no capability entry, and callers treat "absent"
    the same as "cannot do it" — never the same as "can do it".
    """
    return LLM_CAPABILITIES.get(provider, {})


def build_llm(provider: str, model: str, temperature: float = 0.6, max_tokens: int = 120):
    """Pipecat LLM service বানায়। key না থাকলে নিজে থেকেই fallback নেয়।"""

    if provider not in SUPPORTED_PROVIDERS:
        # A provider we cannot construct is a configuration error in the
        # tenant's settings — never a silent fallback to an unrelated AI.
        raise UnsupportedProviderFeatureError(
            f"Unknown LLM provider {provider!r}; supported: "
            + ", ".join(SUPPORTED_PROVIDERS),
            provider=provider,
        )

    # Capability validation: the agent always registers its booking/
    # escalation tools, so a provider without tool-calling must fail here,
    # loudly, instead of silently dropping the tools and producing an agent
    # that cannot do its job.
    if not llm_capabilities(provider).get("supports_tool_calling", False):
        raise UnsupportedProviderFeatureError(
            f"LLM provider {provider!r} does not support tool calling",
            provider=provider,
        )

    if not _has_key(provider):
        for alt in FALLBACK_ORDER:
            if _has_key(alt):
                log.warning("llm.fallback", requested=provider, using=alt)
                provider, model = alt, PRESETS[
                    {"openai": "fast", "anthropic": "natural", "google": "cheap"}[alt]
                ].model
                break
        else:
            raise ProviderConfigurationError(
                "No LLM provider API key is configured",
                provider="llm",
            )

    # ---- OpenAI (ChatGPT) ------------------------------------------------
    if provider == "openai":
        from pipecat.services.openai.llm import OpenAILLMService

        return OpenAILLMService(
            api_key=settings.openai_api_key,
            model=model,
            params=OpenAILLMService.InputParams(
                temperature=temperature,
                max_tokens=max_tokens,
                # ফোনে লম্বা উত্তর = মৃত্যু। শক্ত করে বেঁধে দিলাম।
                frequency_penalty=0.3,
                presence_penalty=0.3,
            ),
        )

    # ---- Anthropic (Claude) ---------------------------------------------
    if provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        return AnthropicLLMService(
            api_key=settings.anthropic_api_key,
            model=model,
            params=AnthropicLLMService.InputParams(
                temperature=temperature,
                max_tokens=max_tokens,
            ),
        )

    # ---- Google (Gemini) -------------------------------------------------
    if provider == "google":
        from pipecat.services.google.llm import GoogleLLMService

        return GoogleLLMService(
            api_key=settings.google_api_key,
            model=model,
            params=GoogleLLMService.InputParams(
                temperature=temperature,
                max_tokens=max_tokens,
            ),
        )

    # Unreachable: the SUPPORTED_PROVIDERS guard above already rejected
    # anything else. Kept so the function is total.
    raise UnsupportedProviderFeatureError(
        f"Unknown LLM provider {provider!r}",
        provider=provider,
    )


def resolve(tenant) -> LLMChoice:
    """Tenant-এর সেটিং থেকে provider/model ঠিক করে।"""
    if tenant.llm_preset and tenant.llm_preset in PRESETS:
        return PRESETS[tenant.llm_preset]
    if tenant.llm_provider and tenant.llm_model:
        return LLMChoice(tenant.llm_provider, tenant.llm_model, 300, "custom")
    return PRESETS["natural"]      # ডিফল্ট: সবচেয়ে মানুষের মতো
