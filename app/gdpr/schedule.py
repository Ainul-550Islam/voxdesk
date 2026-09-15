"""Retention schedule + deletion planning + minimisation (Phase 4, GDPR).

A pure, database-free mirror of the deletion decisions in
``app/core/retention.py`` (which owns the SQLAlchemy deletes) and the policy
helpers in ``app/core/data_policy.py`` (which owns cutoff/is_expired). This
module adds what the service layer needs to *decide* before it deletes:

* a **per-category retention schedule** — how long each kind of record lives
  and whether it is eligible for erasure at all;
* a **deletion plan** — which record ids are past their window, oldest first,
  bounded by ``limit`` (mirroring the bounded-per-run semantics of
  ``purge_expired_calls``);
* a **minimisation** helper — drop fields outside an allow-list before a
  record is exported or logged.

The one rule this module treats as absolute, matching ``retention.py``'s
docstring: **audit logs are never auto-deleted** (SOC 2 requires the audit
trail to outlive the operational data it describes).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from app.core.data_policy import is_expired

# ------------------------------------------------------------- categories ----

#: Record categories and their retention posture.
#:
#: * ``retention_days`` — None means "no automatic expiry" (kept for a legal
#:   or audit obligation, never swept by the nightly purge).
#: * ``erasure_eligible`` — False means the record must NOT be deleted by the
#:   retention sweep even if a cutoff were computed (audit logs, invoices).
#: * ``purpose`` — the processing purpose, for records of processing.
CATEGORY_NAMES = frozenset({
    "call_transcript",
    "call_recording",
    "webhook_receipt",
    "consent_record",
    "audit_log",
    "billing_invoice",
})


@dataclass(frozen=True)
class RetentionRule:
    retention_days: int | None
    erasure_eligible: bool
    purpose: str


RETENTION_SCHEDULE: dict[str, RetentionRule] = {
    # Personal + health-adjacent data: expire, and delete.
    "call_transcript": RetentionRule(90, True, "customer_support"),
    "call_recording": RetentionRule(90, True, "customer_support"),
    # Receipts only cover the provider's plausible redelivery window.
    "webhook_receipt": RetentionRule(7, True, "idempotency"),
    # Consent evidence is kept long-term, but is still erasure-eligible on
    # request (deleted by DSAR, not by the nightly sweep at the same cadence).
    "consent_record": RetentionRule(3650, True, "consent_evidence"),
    # SOC 2: the audit trail outlives operational data. Never auto-deleted.
    "audit_log": RetentionRule(None, False, "security_audit"),
    # Legal obligation to retain invoices (multi-year); not swept.
    "billing_invoice": RetentionRule(2555, False, "legal_obligation"),
}

AUDIT_CATEGORY = "audit_log"


def rule_for(category: str) -> RetentionRule | None:
    return RETENTION_SCHEDULE.get(category)


def erasure_eligible(category: str) -> bool:
    """False for unknown categories too (fail closed: never delete something
    whose retention posture is unknown)."""
    rule = rule_for(category)
    return bool(rule and rule.erasure_eligible)


def retention_days(category: str) -> int | None:
    rule = rule_for(category)
    return rule.retention_days if rule else None


# ----------------------------------------------------------- deletion plan ---

@dataclass(frozen=True)
class RecordRef:
    """One record the service layer may be asked to delete."""

    category: str
    id: str
    created_at: datetime


@dataclass(frozen=True)
class DeletionPlan:
    delete_ids: tuple[str, ...] = ()
    kept_expired_audit: int = 0
    kept_ineligible: int = 0
    truncated: bool = False

    @property
    def delete_count(self) -> int:
        return len(self.delete_ids)


def deletion_plan(
    records: Iterable[RecordRef],
    *,
    now: datetime | None = None,
    limit: int = 1000,
) -> DeletionPlan:
    """Which records should be deleted right now, oldest first.

    * Unknown categories and non-erasure-eligible categories are never
      deleted (audit logs and invoices are kept regardless of age).
    * ``limit`` bounds the plan exactly like ``purge_expired_calls`` bounds a
      run; the next tick resumes where it left off.
    * Deterministic: candidates are ordered by (created_at, id) ascending.
    """
    if limit <= 0:
        return DeletionPlan(truncated=True)

    expired: list[tuple[datetime, str, str]] = []
    kept_expired_audit = 0
    kept_ineligible = 0

    for record in records:
        rule = rule_for(record.category)
        if rule is None:
            # Unknown category: keep (fail closed).
            kept_ineligible += 1
            continue
        if not rule.erasure_eligible:
            kept_ineligible += 1
            if record.category == AUDIT_CATEGORY:
                kept_expired_audit += 1
            continue
        days = rule.retention_days
        if days is None or not is_expired(record.created_at, days, now=now):
            continue
        expired.append((record.created_at, record.id, record.id))

    expired.sort(key=lambda entry: (entry[0], entry[1]))
    delete_ids = tuple(entry[1] for entry in expired[:limit])
    return DeletionPlan(
        delete_ids=delete_ids,
        kept_expired_audit=kept_expired_audit,
        kept_ineligible=kept_ineligible,
        truncated=len(expired) > limit,
    )


# ------------------------------------------------------------- minimisation --

def minimize(record: dict, allowed: set[str]) -> tuple[dict, int]:
    """Keep only the ``allowed`` fields; report how many were dropped.

    Data minimisation (GDPR Art. 5(1)(c)): an export or log should carry only
    the fields its purpose needs. This is the pure decision the exporters use
    before serialising a record.
    """
    kept = {key: value for key, value in record.items() if key in allowed}
    return kept, len(record) - len(kept)


def schedule_summary() -> dict[str, dict]:
    """A serialisable view of the retention schedule, for the compliance
    dashboard."""
    return {
        category: {
            "retention_days": rule.retention_days,
            "erasure_eligible": rule.erasure_eligible,
            "purpose": rule.purpose,
        }
        for category, rule in sorted(RETENTION_SCHEDULE.items())
    }
