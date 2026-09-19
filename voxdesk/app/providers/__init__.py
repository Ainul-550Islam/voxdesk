"""Provider hardening contracts (Phase 4 slice 5): capability gating,
fallback selection, bounded-label normalisation and circuit breaking.

Pure, deterministic, stdlib-only (plus the dependency-free
``app.agent.voice_settings`` for the speed range). These modules are the
database-free, import-light cores of the live voice layer's hardening logic,
so the rules can be asserted in tests without pipecat, Prometheus or the
config stack.
"""

from app.providers.breaker import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    BreakerError,
    CircuitBreaker,
)
from app.providers.contracts import (
    CAPABILITIES,
    LLM_PROVIDERS,
    VOICE_PROVIDERS,
    Contract,
    contract_for,
    supports,
    validate_contract,
)
from app.providers.normalize import (
    KNOWN_CATEGORIES,
    KNOWN_PROVIDERS,
    bounded_label,
    clamp,
    normalize_category_label,
    normalize_provider_label,
)
from app.providers.selection import (
    FALLBACK_MODELS,
    FALLBACK_ORDER,
    PRESETS,
    SUPPORTED_PROVIDERS,
    Resolution,
    resolve_preset,
    resolve_provider,
    resolve_tenant,
)

__all__ = [
    "CAPABILITIES",
    "CLOSED",
    "FALLBACK_MODELS",
    "FALLBACK_ORDER",
    "HALF_OPEN",
    "KNOWN_CATEGORIES",
    "KNOWN_PROVIDERS",
    "LLM_PROVIDERS",
    "OPEN",
    "PRESETS",
    "SUPPORTED_PROVIDERS",
    "VOICE_PROVIDERS",
    "BreakerError",
    "CircuitBreaker",
    "Contract",
    "Resolution",
    "bounded_label",
    "clamp",
    "contract_for",
    "normalize_category_label",
    "normalize_provider_label",
    "resolve_preset",
    "resolve_provider",
    "resolve_tenant",
    "supports",
    "validate_contract",
]
