"""তিনটা LLM provider বেছে নেওয়ার লজিক।"""
from types import SimpleNamespace

import pytest

from app.agent import llm_factory
from app.agent.llm_factory import PRESETS, resolve


def _tenant(**kw):
    base = dict(llm_preset=None, llm_provider=None, llm_model=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_all_three_providers_have_presets():
    providers = {c.provider for c in PRESETS.values()}
    assert providers == {"openai", "anthropic", "google"}


def test_preset_fast_is_openai():
    assert PRESETS["fast"].provider == "openai"      # ChatGPT


def test_preset_natural_is_claude():
    assert PRESETS["natural"].provider == "anthropic"  # Claude


def test_preset_cheap_is_gemini():
    assert PRESETS["cheap"].provider == "google"     # Gemini


def test_resolve_uses_preset():
    assert resolve(_tenant(llm_preset="cheap")).provider == "google"


def test_resolve_uses_custom_pair():
    c = resolve(_tenant(llm_provider="openai", llm_model="gpt-4o"))
    assert (c.provider, c.model) == ("openai", "gpt-4o")


def test_resolve_defaults_to_natural():
    assert resolve(_tenant()).provider == "anthropic"


def test_unknown_preset_falls_back():
    assert resolve(_tenant(llm_preset="banana")).provider == "anthropic"


def test_build_llm_raises_without_any_key(monkeypatch):
    monkeypatch.setattr(llm_factory, "_has_key", lambda p: False)
    with pytest.raises(RuntimeError, match="API key"):
        llm_factory.build_llm("openai", "gpt-4o-mini")


def test_all_presets_are_low_latency():
    """ফোন কলে 600ms এর বেশি first-token = কল মরে যায়।"""
    for name, choice in PRESETS.items():
        if name == "smart":
            continue
        assert choice.est_latency_ms <= 350, f"{name} খুব ধীর"