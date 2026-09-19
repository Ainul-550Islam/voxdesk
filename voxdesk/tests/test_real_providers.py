"""Step 4 — REAL external provider tests (Level 3).

These tests make real network calls to the real providers. They are disabled
by default and skipped unless the operator explicitly opts in:

    VOXDESK_REAL_INTEGRATION=1 python -m pytest tests/test_real_providers.py

Every test also skips itself when the provider's credentials are not
configured, so a partial configuration validates only what is present.

Safety: every check exercised here is READ_ONLY. No call is placed, no SMS
is sent, no charge is created, no booking is made, no customer record is
written. See docs/INTEGRATION-VALIDATION.md.
"""
from __future__ import annotations

import pytest

from app.integrations.validation import (
    CheckStatus,
    PROVIDERS,
    run_one,
)

pytestmark = pytest.mark.real_provider


@pytest.fixture(autouse=True)
def _require_opt_in():
    from app.integrations.validation import real_integration_enabled

    if not real_integration_enabled():
        pytest.skip("real provider tests disabled; set VOXDESK_REAL_INTEGRATION=1")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PROVIDERS))
async def test_provider_health_check(name):
    outcome = await run_one(name)
    if outcome.status is CheckStatus.SKIPPED:
        pytest.skip(outcome.reason)
    assert outcome.status is CheckStatus.PASS, (
        f"{name}: {outcome.status.value} — {outcome.reason}"
    )
