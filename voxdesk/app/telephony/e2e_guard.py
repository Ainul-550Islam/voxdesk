"""Real-call E2E safety guard (Step 5, scale-compliance).

The application can already place outbound calls and answer inbound calls for
real tenants. This module is the *only* sanctioned way a genuine, end-to-end
Twilio media-stream call may be exercised on purpose, and it is deliberately
the narrowest possible gate:

* It never places a call, never mutates anything, and never talks to Twilio.
  It only classifies an *incoming* webhook against a strict allowlist.
* It is armed only when ``settings.e2e_enabled`` is true.
* When disarmed, it is a no-op: every inbound call is ordinary traffic and
  the product behaves exactly as before.
* When armed, production traffic is refused outright and the only calls that
  may proceed are explicit, allowlisted, test-tenant calls. Anything else is
  rejected before a `Call` row, session, media stream, or billing event can
  exist.

Error messages are intentionally generic: they never echo the caller's number,
the dialed number, or any configured value, so nothing secret or personally
identifiable leaks into responses or logs.

The operator-run flow (see docs/runbooks/REAL-E2E-RUNBOOK.md) is:

1. Provision a dedicated test tenant and mark it `is_test_tenant=True`.
2. Point that tenant's `twilio_number` at the dedicated test number.
3. Set `E2E_ENABLED=true`, `E2E_TEST_NUMBER=<test number>`,
   `E2E_ALLOWED_CALLERS=<operator phone, ...>` in a non-production
   environment.
4. A human dials the test number from the allowlisted phone and talks.
5. Record the outcome in the structured result record; never automate step 4.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.core.logging import log
from app.telephony import phone


class E2ERejected(Exception):
    """An inbound call may not proceed under E2E mode.

    The message is a stable, secret-free code for operators; it never contains
    phone numbers, tenant names, or configuration values.
    """


class E2EConfigurationError(Exception):
    """E2E mode is armed but misconfigured (fail-closed, secret-free message)."""


@dataclass(frozen=True)
class E2EContext:
    """A call that has passed every gate and may proceed as a live E2E test."""

    from_number: str
    to_number: str
    test_tenant_name: str


def is_armed(settings: Settings) -> bool:
    """True when E2E mode is switched on at all."""
    return bool(settings.e2e_enabled)


def assert_configuration(settings: Settings) -> None:
    """Raise :class:`E2EConfigurationError` when armed-but-misconfigured.

    Deliberately does not validate the *format* of the phone numbers here: a
    syntactically invalid allowlist entry simply never matches a caller, so the
    fail-closed behavior is unchanged. The startup ``validate_security`` check
    covers the "missing" cases before traffic is served.
    """
    if not settings.e2e_enabled:
        return
    if not settings.e2e_test_number:
        raise E2EConfigurationError("E2E test number is not configured")
    if not settings.e2e_caller_list:
        raise E2EConfigurationError("E2E caller allowlist is empty")


def check_inbound(
    settings: Settings,
    *,
    from_number: str,
    to_number: str,
    tenant: object | None,
) -> E2EContext | None:
    """Decide how an inbound Twilio call should be treated.

    Returns
    -------
    * ``None`` — ordinary traffic: E2E is disarmed and nothing changes.
    * :class:`E2EContext` — an explicit, allowlisted, test-tenant call.

    Raises
    ------
    * :class:`E2ERejected` — E2E is armed and this call is not an allowed test
      call (or the environment is production).
    * :class:`E2EConfigurationError` — E2E is armed but misconfigured.

    Production is rejected whenever the guard is armed; ``settings.is_production
    `` is passed in by the caller so this stays a pure function of its inputs.
    """
    if settings.is_production:
        if settings.e2e_enabled:
            log.error("call.e2e_rejected_production")
            raise E2ERejected("E2E calls are not permitted in production")
        return None

    if not settings.e2e_enabled:
        return None

    assert_configuration(settings)

    # The dialed number must be exactly the configured test number.
    if not phone.same_number(to_number, settings.e2e_test_number):
        log.warning(
            "call.e2e_rejected", reason="dialed_number_not_test_number"
        )
        raise E2ERejected("dialed number is not the configured E2E test number")

    # The caller must be on the allowlist. Normalize the raw From field the
    # same way Twilio's caller ID arrives so formatting cannot bypass the gate.
    allowed = {phone.try_normalize(n) for n in settings.e2e_caller_list}
    if phone.try_normalize(from_number) not in allowed:
        log.warning("call.e2e_rejected", reason="caller_not_allowlisted")
        raise E2ERejected("caller is not an allowlisted E2E operator number")

    # The resolved tenant must exist and be explicitly marked as a test tenant.
    if tenant is None or not getattr(tenant, "is_test_tenant", False):
        log.warning("call.e2e_rejected", reason="tenant_not_test_tenant")
        raise E2ERejected("tenant is not marked as a test tenant")

    # Belt-and-braces: the test tenant must actually answer the test number.
    tenant_number = getattr(tenant, "twilio_number", None)
    if not phone.same_number(tenant_number, settings.e2e_test_number):
        log.warning("call.e2e_rejected", reason="tenant_number_mismatch")
        raise E2ERejected("test tenant is not configured for the E2E test number")

    log.info(
        "call.e2e_allowed",
        test_tenant=getattr(tenant, "name", "?"),
        from_=phone.redact(from_number),
    )
    return E2EContext(
        from_number=from_number,
        to_number=to_number,
        test_tenant_name=getattr(tenant, "name", "?"),
    )


def mode_label(settings: Settings) -> str:
    """Human-readable test-mode marker for logs, never secret-bearing."""
    if not settings.e2e_enabled:
        return "production"
    return "e2e-test"
