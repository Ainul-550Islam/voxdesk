"""Consent records and legal-basis policy (Phase 4, GDPR expansion).

GDPR processing needs a documented legal basis, and "consent" is the one
basis that can be granted *and* withdrawn by the data subject. This module
models that: a tenant holds zero or more ``ConsentRecord`` events (grant and,
optionally, withdrawal), and the policy questions are answered purely from the
record sequence — no database, no clock injection beyond the caller's ``now``.

Rules encoded here:

* Consent is valid only while the latest event is a **grant** (a withdrawal
  that comes later invalidates it).
* When a grant and a withdrawal share a timestamp, **withdrawal wins** — the
  conservative reading, and the one that avoids over-retention.
* Only the "consent" basis requires an affirmative record; "contract",
  "legal_obligation" and the rest never depend on consent.

Mirrors and extends the pure ``app.core.data_policy`` style: deterministic,
dependency-free, safe to call from the API layer or the scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

#: Legal bases recognised by the policy. ``consent`` is the only one that
#: needs a consent record; the rest are objective facts about the processing.
LEGAL_BASES = frozenset({
    "consent",
    "contract",
    "legal_obligation",
    "vital_interests",
    "public_interest",
    "legitimate_interest",
})

#: Bases that require an affirmative, revocable consent record.
CONSENT_REQUIRING_BASES = frozenset({"consent"})


class ConsentError(ValueError):
    """Raised for malformed consent input."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ConsentRecord:
    """A single grant (``withdrawn_at=None``) or a withdrawal event.

    A record with ``withdrawn_at`` set records *both* the original grant and
    its withdrawal; a record with ``withdrawn_at=None`` is a grant that is
    still open-ended.
    """

    subject_id: str
    granted_at: datetime
    withdrawn_at: datetime | None = None

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.subject_id or not self.subject_id.strip():
            problems.append("consent record requires a subject_id")
        if self.withdrawn_at is not None and _as_utc(self.withdrawn_at) < _as_utc(self.granted_at):
            problems.append("withdrawn_at must not precede granted_at")
        return problems

    @property
    def is_withdrawn(self) -> bool:
        return self.withdrawn_at is not None

    def event_time(self) -> datetime:
        """The latest timestamp in this record (the decisive event)."""
        if self.withdrawn_at is None:
            return self.granted_at
        return max(_as_utc(self.granted_at), _as_utc(self.withdrawn_at))


def consent_status(records: list[ConsentRecord]) -> str:
    """``"granted"``, ``"withdrawn"`` or ``"none"`` from a record sequence.

    The latest event decides; on a timestamp tie, withdrawal wins.
    """
    if not records:
        return "none"
    latest = max(records, key=lambda record: (_as_utc(record.event_time()).timestamp()))
    return "withdrawn" if latest.is_withdrawn else "granted"


def has_consent(records: list[ConsentRecord]) -> bool:
    return consent_status(records) == "granted"


def valid_legal_basis(basis: str) -> bool:
    return basis in LEGAL_BASES


def consent_required(basis: str) -> bool:
    """True only for the consent basis (and for unknown bases, which must be
    treated as needing consent rather than silently assuming a right)."""
    if basis not in LEGAL_BASES:
        return True
    return basis in CONSENT_REQUIRING_BASES


def processing_allowed(basis: str, records: list[ConsentRecord]) -> bool:
    """The one-line policy decision: may processing proceed on this basis?

    Consent basis ⇒ consent must currently be granted. Every other valid basis
    ⇒ allowed without consent. An unknown basis ⇒ refused (fail closed).
    """
    if basis not in LEGAL_BASES:
        return False
    if basis in CONSENT_REQUIRING_BASES:
        return has_consent(records)
    return True
