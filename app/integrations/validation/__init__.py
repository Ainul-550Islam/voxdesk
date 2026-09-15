"""Real-provider integration validation framework (Step 4).

Three levels of testing live in this repository, and this package is the
machinery behind the third:

* **Level 1 — unit:** no network, no providers. The existing ``tests/`` suite.
* **Level 2 — mocked integration:** realistic fake provider responses, no real
  network. Also the existing ``tests/`` suite (adapter contract tests).
* **Level 3 — real provider:** the checks in this package, which talk to the
  real Twilio / Deepgram / ElevenLabs / LLM / calendar / CRM / Stripe APIs.

Level 3 is **opt-in and off by default**. Everything here checks the single
flag ``VOXDESK_REAL_INTEGRATION=1`` before it will make a network call, every
registered check is read-only, and the one CLI
(``python -m app.integrations.validation.cli``) reports a
SKIPPED / PASS / FAIL / BLOCKED table without ever printing a credential.

See ``docs/INTEGRATION-VALIDATION.md`` for the full contract.
"""
from __future__ import annotations

from app.integrations.validation.report import format_table, summary_counts, to_json
from app.integrations.validation.status import (
    CheckOutcome,
    CheckStatus,
    OperationSafety,
    SecretMasker,
    env_or,
    real_integration_enabled,
)
from app.integrations.validation.registry import (
    FORBIDDEN_OPERATIONS,
    PROVIDERS,
    run_all,
    run_one,
)

__all__ = [
    "CheckOutcome",
    "CheckStatus",
    "OperationSafety",
    "SecretMasker",
    "env_or",
    "real_integration_enabled",
    "FORBIDDEN_OPERATIONS",
    "PROVIDERS",
    "run_all",
    "run_one",
    "format_table",
    "summary_counts",
    "to_json",
]
