"""Shared vocabulary for the real-provider validation framework (Step 4).

Two enums and one outcome dataclass. Everything else — checks, registry,
reporting, the CLI and the pytest layer — speaks only in these terms, so the
boundaries between "not configured", "configured but broken" and "confirmed
working" can never blur, and no raw exception ever crosses into a report.

Secret discipline lives here too. Provider checks are written to emit fixed,
safe text, but the reporter also runs every string through ``SecretMasker``
as defence-in-depth: even a buggy check that interpolated a token into its
reason cannot print it.
"""
from __future__ import annotations

import enum
import os
from dataclasses import dataclass


class CheckStatus(str, enum.Enum):
    """The four answers a provider check may give.

    ``BLOCKED`` must never be reported as ``PASS``; it means the environment
    prevented execution (for example, an SDK that cannot run here), not that
    the provider was confirmed working.
    """

    SKIPPED = "SKIPPED"   # required credentials/flag absent
    PASS = "PASS"         # provider successfully validated
    FAIL = "FAIL"         # configuration or provider error
    BLOCKED = "BLOCKED"   # environment prevents execution


class OperationSafety(str, enum.Enum):
    """How dangerous a real-provider operation is.

    The registry runs READ_ONLY checks automatically (under the opt-in flag).
    SAFE_SANDBOX_WRITE and FORBIDDEN_IN_AUTOMATED_TEST operations exist only
    as documentation: there is deliberately no code path that executes them.
    """

    READ_ONLY = "read_only"
    SAFE_SANDBOX_WRITE = "safe_sandbox_write"
    FORBIDDEN_IN_AUTOMATED_TEST = "forbidden_in_automated_test"


@dataclass
class CheckOutcome:
    """One provider's validation result. ``reason`` is always secret-safe."""

    provider: str
    status: CheckStatus
    reason: str = ""
    latency_ms: float | None = None
    safety: OperationSafety = OperationSafety.READ_ONLY

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "status": self.status.value,
            "reason": self.reason,
            "latency_ms": self.latency_ms,
            "safety": self.safety.value,
        }


#: The single opt-in switch. Everything that could talk to a real provider —
#: the pytest layer AND the CLI — checks it first. There is deliberately no
#: second flag to disagree with it.
OPT_IN_ENV = "VOXDESK_REAL_INTEGRATION"


def real_integration_enabled(environ=None) -> bool:
    """True only when the operator explicitly opted in."""
    env = os.environ if environ is None else environ
    return (env.get(OPT_IN_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def env_or(name: str, default: str = "") -> str:
    """Read one environment variable, trimmed. Empty means absent."""
    return (os.environ.get(name) or default).strip()


class SecretMasker:
    """Replaces known secret values in any text with ``***``.

    Values shorter than 8 characters are ignored: masking a 2-character
    substring would shred ordinary words in every message. Real keys, tokens
    and SIDs are all far longer than that. Longer values are masked first so a
    key that contains another key as a prefix cannot leak through the shorter
    replacement.
    """

    def __init__(self, secrets=()):
        self._secrets = sorted(
            {str(s) for s in secrets if s and len(str(s)) >= 8},
            key=len,
            reverse=True,
        )

    def mask(self, text) -> str:
        out = "" if text is None else str(text)
        for secret in self._secrets:
            out = out.replace(secret, "***")
        return out

    @property
    def count(self) -> int:
        return len(self._secrets)
