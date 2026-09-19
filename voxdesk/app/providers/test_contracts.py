"""Unit tests for app.providers.contracts.

Co-located with the package; run with: python -m pytest app/providers/ -q
"""

from __future__ import annotations

from app.providers.contracts import (
    CAPABILITIES,
    LLM_PROVIDERS,
    Contract,
    contract_for,
    supports,
    validate_contract,
)


def test_supports_is_true_only_when_declared():
    assert supports("openai", "supports_tool_calling")
    assert not supports("openai", "supports_pitch")          # absent
    assert not supports("deepgram", "supports_tool_calling")  # absent
    assert not supports("unknown_provider", "supports_streaming")  # unknown


def test_absent_means_cannot_for_unknown_provider():
    contract = contract_for("typo_provider", "supports_tool_calling")
    assert not contract.satisfied()
    assert "supports_tool_calling" in contract.missing_capabilities()


def test_contract_satisfied_when_all_present():
    contract = contract_for("openai", "supports_tool_calling", "supports_streaming")
    assert contract.satisfied()
    assert contract.missing_capabilities() == []


def test_contract_reports_missing_sorted():
    contract = contract_for("deepgram", "supports_tool_calling", "supports_pitch", "supports_speed")
    assert contract.missing_capabilities() == [
        "supports_pitch",
        "supports_speed",
        "supports_tool_calling",
    ]


def test_validate_contract_empty_when_ok():
    assert validate_contract("openai", ["supports_tool_calling"]) == []
    violations = validate_contract("elevenlabs", ["supports_tool_calling"])
    assert any("elevenlabs" in v and "supports_tool_calling" in v for v in violations)


def test_registry_mirrors_live_capabilities():
    # The live dicts in app/agent/tts.py and app/agent/stt.py must stay in
    # sync; assert the key facts so a drift is caught here.
    assert CAPABILITIES["elevenlabs"]["supports_speed"] is True
    assert CAPABILITIES["elevenlabs"]["supports_interruptions"] is False
    assert CAPABILITIES["deepgram"]["supports_model_selection"] is True
    assert CAPABILITIES["google"]["supports_tool_calling"] is True
    assert LLM_PROVIDERS == frozenset({"openai", "anthropic", "google"})
    assert Contract("openai").satisfied()  # no requirements is trivially fine
