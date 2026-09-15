"""Data retention and AI-disclosure policy helpers (Step 9 compliance).

Two regulatory obligations every voice product must codify, not just document:

1. Retention: call recordings and transcripts contain personally identifiable,
   health-adjacent data. GDPR, the EU AI Act and healthcare-buyer contracts all
   require a documented, configurable retention period and deletion of records
   past it.
2. Disclosure: the EU AI Act (transparency, Art. 50) and several US state laws
   require a caller to know they are speaking to an automated agent.

This module is deliberately pure: it computes policy decisions without touching
the database, so it is fast, testable and safe to call from any context (API,
scheduler, tests). Wiring deletion into the scheduler is documented in
docs/COMPLIANCE.md; the policy itself lives here so it is reviewable in one
place.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Terms a compliant greeting should contain so a caller knows they are speaking
# to a machine. Matched case-insensitively against the tenant's greeting.
DISCLOSURE_TERMS = (
    "ai",
    "a.i.",
    "artificial",
    "virtual",
    "automated",
    "assistant",
    "digital assistant",
    "bot",
)


def retention_cutoff(days: int, now: datetime | None = None) -> datetime:
    """The moment before which a record is past the retention policy."""
    now = now or datetime.now(timezone.utc)
    return now - timedelta(days=max(days, 1))


def is_expired(
    created_at: datetime,
    retention_days: int,
    now: datetime | None = None,
) -> bool:
    """True when ``created_at`` falls before the retention cutoff."""
    return _as_utc(created_at) < retention_cutoff(retention_days, now=now)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        # Naive datetimes in this codebase are UTC (datetime.utcnow).
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ai_disclosure_present(greeting: str) -> bool:
    """True when the greeting identifies the agent as automated.

    The agent pipeline introduces itself with the tenant's greeting, so this is
    the moment a caller learns whether they are talking to a machine. A greeting
    such as "Thanks for calling Bright Smile Dental, this is Alex" names a human
    voice and gives no automated-agent signal -- which is why the check exists
    and the dashboard flags it (see docs/COMPLIANCE.md).
    """
    text = (greeting or "").lower()
    return any(term in text for term in DISCLOSURE_TERMS)


def ai_disclosure_compliant(greeting: str, *, required: bool) -> bool:
    """Policy decision: is the greeting compliant given whether disclosure is
    required (app.core.config.Settings.ai_disclosure_required)."""
    return (not required) or ai_disclosure_present(greeting)


def compliance_summary(*, greeting: str, disclosure_required: bool) -> dict:
    """A small, pure summary used by tests and available to the API layer."""
    return {
        "ai_disclosure_required": disclosure_required,
        "ai_disclosure_present": ai_disclosure_present(greeting),
        "compliant": ai_disclosure_compliant(
            greeting, required=disclosure_required
        ),
    }
