"""The closed registry of real-provider checks (Step 4).

One dict: display name -> check. Adding a provider is a check function plus a
line here. The registry enforces the safety rules structurally:

* every runnable entry is READ_ONLY;
* the FORBIDDEN operations exist only as documentation — there is no runnable
  check behind any of them, so an automated run can never perform one;
* ``run_one`` / ``run_all`` refuse to touch the network unless the single
  opt-in flag ``VOXDESK_REAL_INTEGRATION`` is set.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from app.integrations.validation import business, voice
from app.integrations.validation.status import (
    CheckOutcome,
    CheckStatus,
    OperationSafety,
    real_integration_enabled,
)


@dataclass(frozen=True)
class ProviderCheck:
    name: str
    safety: OperationSafety
    description: str
    run: Callable[[], Awaitable[CheckOutcome]]


PROVIDERS: dict[str, ProviderCheck] = {
    "Twilio": ProviderCheck(
        "Twilio", OperationSafety.READ_ONLY,
        "account access (no call, no SMS)", voice.check_twilio,
    ),
    "Deepgram": ProviderCheck(
        "Deepgram", OperationSafety.READ_ONLY,
        "key valid (projects list)", voice.check_deepgram,
    ),
    "ElevenLabs": ProviderCheck(
        "ElevenLabs", OperationSafety.READ_ONLY,
        "key, voice and model availability", voice.check_elevenlabs,
    ),
    "OpenAI": ProviderCheck(
        "OpenAI", OperationSafety.READ_ONLY,
        "key and configured model", voice.check_openai,
    ),
    "Anthropic": ProviderCheck(
        "Anthropic", OperationSafety.READ_ONLY,
        "key and configured model", voice.check_anthropic,
    ),
    "Google LLM": ProviderCheck(
        "Google LLM", OperationSafety.READ_ONLY,
        "key and configured model", voice.check_google_llm,
    ),
    "Google Calendar": ProviderCheck(
        "Google Calendar", OperationSafety.READ_ONLY,
        "read-only calendar access", business.check_google_calendar,
    ),
    "Microsoft Calendar": ProviderCheck(
        "Microsoft Calendar", OperationSafety.READ_ONLY,
        "read-only calendar access", business.check_microsoft_calendar,
    ),
    "Cal.com": ProviderCheck(
        "Cal.com", OperationSafety.READ_ONLY,
        "event-type listing (read-only)", business.check_calcom,
    ),
    "HubSpot": ProviderCheck(
        "HubSpot", OperationSafety.READ_ONLY,
        "contact listing (read-only)", business.check_hubspot,
    ),
    "GoHighLevel": ProviderCheck(
        "GoHighLevel", OperationSafety.READ_ONLY,
        "contact listing (read-only)", business.check_gohighlevel,
    ),
    "Jobber": ProviderCheck(
        "Jobber", OperationSafety.READ_ONLY,
        "account query (read-only)", business.check_jobber,
    ),
    "Stripe": ProviderCheck(
        "Stripe", OperationSafety.READ_ONLY,
        "key and webhook-secret shape (read-only)", business.check_stripe,
    ),
}

#: Operations that must never run automatically. Exposed only so the report
#: and the documentation can state them plainly; there is no check function
#: behind any entry, so an automated run is structurally unable to execute
#: them.
FORBIDDEN_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("Twilio", "place a live phone call"),
    ("Twilio", "send an SMS / WhatsApp message"),
    ("Stripe", "create a charge, invoice or subscription"),
    ("Google/Microsoft Calendar, Cal.com", "create a real customer booking"),
    ("HubSpot / GoHighLevel / Jobber", "create or modify a customer record"),
    ("Any provider", "send a customer-facing email or message"),
)

#: The manual E2E voice test that the framework deliberately does not run.
#: A real call is a FORBIDDEN_IN_AUTOMATED_TEST operation; when the project
#: later needs it, it must be a separate, human-triggered mechanism that
#: asserts on an explicit safe test target number.
MANUAL_E2E_NOTE = (
    "Live end-to-end voice validation (dial a number, hear the agent) is "
    "intentionally not automated: it requires a real phone call, which is a "
    "FORBIDDEN_IN_AUTOMATED_TEST operation. It must remain a manual step "
    "against an operator-controlled test number."
)


async def run_one(name: str) -> CheckOutcome:
    """Run one check by display name.

    Returns SKIPPED without touching the network when the opt-in flag is not
    set, or when the name is not registered.
    """
    check = PROVIDERS.get(name)
    if check is None:
        return CheckOutcome(name, CheckStatus.FAIL,
                            reason=f"no check registered for {name!r}")
    if not real_integration_enabled():
        return CheckOutcome(name, CheckStatus.SKIPPED,
                            reason="VOXDESK_REAL_INTEGRATION not set")
    return await check.run()


async def run_all() -> list[CheckOutcome]:
    """Run every registered check in registry order."""
    if not real_integration_enabled():
        return [
            CheckOutcome(check.name, CheckStatus.SKIPPED,
                         reason="VOXDESK_REAL_INTEGRATION not set")
            for check in PROVIDERS.values()
        ]
    return [await check.run() for check in PROVIDERS.values()]
