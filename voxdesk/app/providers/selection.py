"""Deterministic provider selection + fallback (Phase 4, multi-provider).

A dependency-free mirror of the resolution logic in ``app/agent/llm_factory.py``
(``build_llm`` + ``resolve``), with the same three rules:

* an **unknown** provider is a configuration error — never a silent fallback
  to whichever provider has a key;
* a **known** provider whose key is missing falls back to the next configured
  provider, in ``FALLBACK_ORDER``;
* only when **no** provider has a key is the result an error.

Because the real factory's decisions depend only on ``(provider, model,
which keys exist)``, this module reproduces them exactly with no pipecat or
config imports, so the choice can be asserted in tests.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Mirror of ``llm_factory.PRESETS`` — the preset name → provider/model map the
#: REST layer shows tenants.
PRESETS: dict[str, tuple[str, str]] = {
    "fast": ("openai", "gpt-4o-mini"),
    "natural": ("anthropic", "claude-haiku-4-5"),
    "cheap": ("google", "gemini-2.0-flash"),
    "smart": ("anthropic", "claude-sonnet-4-5"),
}

#: Mirror of ``llm_factory.SUPPORTED_PROVIDERS``.
SUPPORTED_PROVIDERS = ("openai", "anthropic", "google")

#: Mirror of ``llm_factory.FALLBACK_ORDER`` — the order providers are tried
#: when the requested one has no key.
FALLBACK_ORDER = ["openai", "anthropic", "google"]

#: The default model used when falling back to a provider (mirrors the
#: ``PRESETS`` lookup inside ``build_llm``).
FALLBACK_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
    "google": "gemini-2.0-flash",
}


@dataclass(frozen=True)
class Resolution:
    """The outcome of provider selection.

    ``provider``/``model`` are set only on success (with or without fallback).
    ``error`` is set only on failure, and ``fell_back`` records whether the
    resolved provider differs from the requested one.
    """

    requested: str
    provider: str | None = None
    model: str | None = None
    fell_back: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.provider is not None and self.error is None


def resolve_provider(requested: str, available: set[str], model: str = "") -> Resolution:
    """Resolve a requested provider against the set that have keys.

    ``available`` is the set of provider names that currently have a
    configured key. Deterministic: same inputs ⇒ same ``Resolution``.
    """
    if requested not in SUPPORTED_PROVIDERS:
        return Resolution(
            requested=requested,
            error=f"Unknown LLM provider {requested!r}; supported: "
            + ", ".join(SUPPORTED_PROVIDERS),
        )

    if requested in available:
        return Resolution(requested=requested, provider=requested, model=model)

    for alternative in FALLBACK_ORDER:
        if alternative in available:
            return Resolution(
                requested=requested,
                provider=alternative,
                model=model or FALLBACK_MODELS[alternative],
                fell_back=True,
            )

    return Resolution(requested=requested, error="No LLM provider API key is configured")


def resolve_preset(preset: str | None) -> tuple[str, str] | None:
    """The (provider, model) for a named preset, or ``None`` when unknown.

    Mirrors ``llm_factory.resolve``'s first branch: a valid preset wins;
    anything else does not silently map to a preset.
    """
    return PRESETS.get(preset or "") if (preset or "") in PRESETS else None


def resolve_tenant(
    preset: str | None,
    provider: str | None,
    model: str | None,
) -> tuple[str, str]:
    """Resolve a tenant's LLM choice, mirroring ``llm_factory.resolve``.

    Priority: valid preset → explicit provider+model → default ("natural").
    """
    preset_choice = resolve_preset(preset)
    if preset_choice is not None:
        return preset_choice
    if provider and model:
        return provider, model
    return PRESETS["natural"]
