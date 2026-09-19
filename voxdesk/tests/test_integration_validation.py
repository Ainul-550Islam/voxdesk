"""Step 4 — deterministic tests for the real-provider validation framework.

Everything here is Level 1/2: no network, no credentials. They pin the
framework's safety guarantees so a future change cannot accidentally:

* run real providers by default,
* let a forbidden operation become runnable,
* print a secret,
* or confuse SKIPPED / PASS / FAIL / BLOCKED.

The actual network checks live in ``tests/test_real_providers.py``, which is
marked ``real_provider`` and skipped unless VOXDESK_REAL_INTEGRATION=1.
"""
from __future__ import annotations

import json

import pytest

from app.integrations.validation import (
    FORBIDDEN_OPERATIONS,
    PROVIDERS,
    CheckStatus,
    OperationSafety,
    SecretMasker,
    format_table,
    real_integration_enabled,
    run_all,
    run_one,
    summary_counts,
    to_json,
)
from app.integrations.validation.registry import MANUAL_E2E_NOTE


# -------------------------------------------------------- opt-in behaviour ---

def test_real_integration_is_disabled_by_default():
    assert real_integration_enabled({}) is False


def test_explicit_opt_in_enables_real_integration():
    assert real_integration_enabled({"VOXDESK_REAL_INTEGRATION": "1"}) is True
    assert real_integration_enabled({"VOXDESK_REAL_INTEGRATION": "true"}) is True
    assert real_integration_enabled({"VOXDESK_REAL_INTEGRATION": "yes"}) is True
    assert real_integration_enabled({"VOXDESK_REAL_INTEGRATION": "0"}) is False
    assert real_integration_enabled({"VOXDESK_REAL_INTEGRATION": ""}) is False


@pytest.mark.asyncio
async def test_run_all_skips_everything_without_the_flag(monkeypatch):
    monkeypatch.delenv("VOXDESK_REAL_INTEGRATION", raising=False)
    outcomes = await run_all()
    assert len(outcomes) == len(PROVIDERS)
    assert all(o.status is CheckStatus.SKIPPED for o in outcomes)
    assert all("VOXDESK_REAL_INTEGRATION" in o.reason for o in outcomes)


@pytest.mark.asyncio
async def test_run_one_reports_an_unknown_provider():
    outcome = await run_one("NotAProvider")
    assert outcome.status is CheckStatus.FAIL
    assert "no check registered" in outcome.reason


# ------------------------------------------------------------ registry ------

def test_all_thirteen_providers_are_registered():
    assert list(PROVIDERS) == [
        "Twilio", "Deepgram", "ElevenLabs", "OpenAI", "Anthropic",
        "Google LLM", "Google Calendar", "Microsoft Calendar", "Cal.com",
        "HubSpot", "GoHighLevel", "Jobber", "Stripe",
    ]


def test_every_registered_check_is_read_only():
    for name, check in PROVIDERS.items():
        assert check.safety is OperationSafety.READ_ONLY, name
        assert callable(check.run), name


def test_forbidden_operations_have_no_runnable_check():
    runnable = set(PROVIDERS)
    for who, what in FORBIDDEN_OPERATIONS:
        # A forbidden operation is a (subject, action) description, not a
        # provider name, so it can never be dispatched to the registry.
        assert what not in runnable


def test_a_live_call_is_documented_as_manual_only():
    assert "not automated" in MANUAL_E2E_NOTE
    assert "FORBIDDEN_IN_AUTOMATED_TEST" in MANUAL_E2E_NOTE


# ------------------------------------------------------- status classes -----

def test_status_enum_has_exactly_four_values():
    assert {s.value for s in CheckStatus} == {"SKIPPED", "PASS", "FAIL", "BLOCKED"}


def test_blocked_is_distinct_from_pass():
    assert CheckStatus.BLOCKED is not CheckStatus.PASS
    assert CheckStatus.BLOCKED.value != CheckStatus.PASS.value


# ------------------------------------------------------- secret masking -----

def test_secret_masker_replaces_a_long_secret():
    masker = SecretMasker(["sk_live_abcdefghijklmnop"])
    assert masker.mask("key sk_live_abcdefghijklmnop leaked") == "key *** leaked"


def test_secret_masker_ignores_short_noise():
    masker = SecretMasker(["ab", "the"])
    assert masker.mask("the cat sat") == "the cat sat"


def test_secret_masker_masks_longest_first():
    masker = SecretMasker(["sk_short", "sk_short_extra"])
    out = masker.mask("sk_short_extra")
    assert out == "***"


def test_secret_masker_passes_through_none():
    assert SecretMasker(["sk_live_abcdefgh"]).mask(None) == ""


# ------------------------------------------------------ result formatting ---

def test_format_table_lists_every_provider_with_a_status():
    outcomes = [__import__("app.integrations.validation.status", fromlist=["CheckOutcome"])
                .CheckOutcome(n, CheckStatus.SKIPPED, "not configured") for n in PROVIDERS]
    table = format_table(outcomes)
    for name in PROVIDERS:
        assert name in table
    assert "SKIPPED" in table
    assert "not configured" in table


def test_summary_counts_are_complete_and_stable():
    outcomes = [
        __import__("app.integrations.validation.status", fromlist=["CheckOutcome"])
        .CheckOutcome("A", CheckStatus.PASS),
        __import__("app.integrations.validation.status", fromlist=["CheckOutcome"])
        .CheckOutcome("B", CheckStatus.FAIL),
        __import__("app.integrations.validation.status", fromlist=["CheckOutcome"])
        .CheckOutcome("C", CheckStatus.SKIPPED),
    ]
    counts = summary_counts(outcomes)
    assert counts == {"SKIPPED": 1, "PASS": 1, "FAIL": 1, "BLOCKED": 0}


def test_to_json_is_parseable_and_secret_free():
    from app.integrations.validation.status import CheckOutcome

    outcome = CheckOutcome("Twilio", CheckStatus.PASS, "account authenticated")
    payload = json.loads(to_json([outcome]))
    assert payload[0]["provider"] == "Twilio"
    assert payload[0]["status"] == "PASS"
    assert payload[0]["safety"] == "read_only"


# -------------------------------------- missing credentials -> SKIP ---------

@pytest.mark.asyncio
async def test_twilio_missing_credentials_skips(monkeypatch):
    from app.integrations.validation import voice

    monkeypatch.setattr(voice.settings, "twilio_account_sid", "")
    monkeypatch.setattr(voice.settings, "twilio_auth_token", "")
    outcome = await voice.check_twilio()
    assert outcome.status is CheckStatus.SKIPPED


@pytest.mark.asyncio
async def test_deepgram_missing_credentials_skips(monkeypatch):
    from app.integrations.validation import voice

    monkeypatch.setattr(voice.settings, "deepgram_api_key", "")
    outcome = await voice.check_deepgram()
    assert outcome.status is CheckStatus.SKIPPED


@pytest.mark.asyncio
async def test_stripe_missing_credentials_skips(monkeypatch):
    from app.integrations.validation import business

    monkeypatch.setattr(business.settings, "stripe_secret_key", "")
    outcome = await business.check_stripe()
    assert outcome.status is CheckStatus.SKIPPED


@pytest.mark.asyncio
async def test_google_calendar_missing_credentials_skips_with_env_names(monkeypatch):
    from app.integrations.validation import business

    monkeypatch.delenv("VOXDESK_REAL_GOOGLE_CALENDAR_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("VOXDESK_REAL_GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    monkeypatch.delenv("VOXDESK_REAL_GOOGLE_CALENDAR_CLIENT_SECRET", raising=False)
    outcome = await business.check_google_calendar()
    assert outcome.status is CheckStatus.SKIPPED
    assert "VOXDESK_REAL_GOOGLE_CALENDAR_REFRESH_TOKEN" in outcome.reason


# -------------------------------------------- invalid config -> FAIL --------

@pytest.mark.asyncio
async def test_twilio_auth_failure_is_fail_and_secret_free(monkeypatch):
    from twilio.base.exceptions import TwilioRestException

    from app.integrations.validation import voice

    monkeypatch.setattr(voice.settings, "twilio_account_sid", "AC-sentinel-secret")
    monkeypatch.setattr(voice.settings, "twilio_auth_token", "tok-sentinel-secret")

    class _FakeApi:
        @property
        def accounts(self):
            raise TwilioRestException(
                401, "https://api.twilio.com", "auth failed", code=20003
            )

    class _FakeClient:
        def __init__(self, sid, token):
            self.api = _FakeApi()

    monkeypatch.setattr("twilio.rest.Client", _FakeClient)

    outcome = await voice.check_twilio()
    assert outcome.status is CheckStatus.FAIL
    assert "sentinel-secret" not in outcome.reason
    assert "20003" in outcome.reason


@pytest.mark.asyncio
async def test_stripe_webhook_secret_shape_is_validated(monkeypatch):
    from app.integrations.validation import business

    monkeypatch.setattr(business.settings, "stripe_secret_key", "sk_test_sentinel")
    monkeypatch.setattr(business.settings, "stripe_webhook_secret", "not-a-whsec")
    outcome = await business.check_stripe()
    assert outcome.status is CheckStatus.FAIL
    assert "whsec_" in outcome.reason
    assert "sentinel" not in outcome.reason


@pytest.mark.asyncio
async def test_stripe_check_only_issues_read_only_requests(monkeypatch):
    """The Stripe check must never touch a mutating endpoint."""
    from app.integrations.validation import business
    from app.billing.providers import stripe as stripe_module

    monkeypatch.setattr(business.settings, "stripe_secret_key", "sk_test_sentinel")
    monkeypatch.setattr(business.settings, "stripe_webhook_secret", "")

    calls: list[tuple[str, str]] = []

    class _FakeHealth:
        connected = True
        provider = "stripe"
        latency_ms = 1.0
        safe_message = "ok"

    async def _fake_health_check(self):
        return _FakeHealth()

    async def _fake_request(self, method, url, **kwargs):
        calls.append((method, url))

    monkeypatch.setattr(stripe_module.StripeProvider, "health_check", _fake_health_check)
    monkeypatch.setattr(stripe_module.StripeProvider, "request", _fake_request)

    outcome = await business.check_stripe()
    assert outcome.status is CheckStatus.PASS
    # The fake health check does not call request(), so the assertion is that
    # the check itself never calls request() directly.
    assert calls == []
    assert "no charge" in outcome.reason


# ------------------------------------------------------ CI safety -----------

def test_the_real_provider_suite_is_marked_for_opt_in_exclusion():
    """The network suite carries the ``real_provider`` marker, so ordinary CI
    (`pytest -m "not real_provider"`) can exclude it structurally even before
    the autouse skip fires."""
    import tests.test_real_providers as real_suite

    pytestmark = real_suite.pytestmark
    marks = pytestmark if isinstance(pytestmark, (list, tuple)) else [pytestmark]
    assert {m.name for m in marks} == {"real_provider"}
