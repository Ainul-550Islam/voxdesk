"""Multi-provider LLM factory.

একই কোডে তিনটা AI চলে — ক্লায়েন্ট প্রতি আলাদা করে বেছে নেওয়া যায়:

    openai     -> gpt-4o-mini        সবচেয়ে দ্রুত, সস্তা, tool calling নিখুঁত
    anthropic  -> claude-haiku-4-5   সবচেয়ে natural কথা বলে, কম hallucinate করে
    google     -> gemini-2.0-flash   সবচেয়ে সস্তা, বহু ভাষা, লম্বা context

কেন তিনটাই দরকার?
  1. একটা provider ডাউন হলে fallback লাগে (ফোন কল অপেক্ষা করে না)
  2. ক্লায়েন্টভেদে খরচ/মান আলাদা — ডেন্টাল ক্লিনিকে haiku, রেস্টুরেন্টে flash
  3. Upwork-এ "multi-LLM, provider-agnostic" লিখলে রেট বাড়ে
"""
from __future__ import annotations

from dataclasses import dataclass

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


def build_llm(provider: str, model: str, temperature: float = 0.6, max_tokens: int = 120):
    """Pipecat LLM service বানায়। key না থাকলে নিজে থেকেই fallback নেয়।"""

    if not _has_key(provider):
        for alt in FALLBACK_ORDER:
            if _has_key(alt):
                log.warning("llm.fallback", requested=provider, using=alt)
                provider, model = alt, PRESETS[
                    {"openai": "fast", "anthropic": "natural", "google": "cheap"}[alt]
                ].model
                break
        else:
            raise RuntimeError("কোনো LLM provider-এর API key পাওয়া যায়নি")

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

    raise ValueError(f"অজানা provider: {provider}")


def resolve(tenant) -> LLMChoice:
    """Tenant-এর সেটিং থেকে provider/model ঠিক করে।"""
    if tenant.llm_preset and tenant.llm_preset in PRESETS:
        return PRESETS[tenant.llm_preset]
    if tenant.llm_provider and tenant.llm_model:
        return LLMChoice(tenant.llm_provider, tenant.llm_model, 300, "custom")
    return PRESETS["natural"]      # ডিফল্ট: সবচেয়ে মানুষের মতো
