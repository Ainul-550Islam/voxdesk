"""GDPR expansion (Phase 4 slice 6): consent, redaction, DSAR and retention.

Pure, deterministic, stdlib-only, building on the existing compliance seams —
``app.core.data_policy`` (cutoff/is_expired/disclosure) and the decisions in
``app.core.retention.py`` (which owns the actual SQLAlchemy deletes). These
modules add the *decision* layer the API and scheduler need before they act:

* ``consent``  — legal-basis and consent-event policy.
* ``redact``   — PII scrubbing for transcripts and exports.
* ``requests`` — data-subject request modelling and action planning.
* ``schedule`` — per-category retention posture and deletion planning.
"""

from app.gdpr.consent import (
    LEGAL_BASES,
    ConsentRecord,
    consent_required,
    consent_status,
    has_consent,
    processing_allowed,
    valid_legal_basis,
)
from app.gdpr.redact import (
    TOKEN_CARD,
    TOKEN_EMAIL,
    TOKEN_PHONE,
    RedactionResult,
    luhn_valid,
    redact,
    redact_cards,
    redact_emails,
    redact_phones,
)
from app.gdpr.requests import (
    DEFAULT_DEADLINE_DAYS,
    KNOWN_HOLDS,
    REQUEST_TYPES,
    ActionPlan,
    SubjectRequest,
    action_plan,
    erasure_blocked,
    requires_identity_verification,
)
from app.gdpr.schedule import (
    AUDIT_CATEGORY,
    CATEGORY_NAMES,
    RETENTION_SCHEDULE,
    DeletionPlan,
    RecordRef,
    RetentionRule,
    deletion_plan,
    erasure_eligible,
    minimize,
    retention_days,
    rule_for,
    schedule_summary,
)

__all__ = [
    "AUDIT_CATEGORY",
    "CATEGORY_NAMES",
    "DEFAULT_DEADLINE_DAYS",
    "KNOWN_HOLDS",
    "LEGAL_BASES",
    "RETENTION_SCHEDULE",
    "REQUEST_TYPES",
    "TOKEN_CARD",
    "TOKEN_EMAIL",
    "TOKEN_PHONE",
    "ActionPlan",
    "ConsentRecord",
    "DeletionPlan",
    "RecordRef",
    "RedactionResult",
    "RetentionRule",
    "SubjectRequest",
    "action_plan",
    "consent_required",
    "consent_status",
    "deletion_plan",
    "erasure_blocked",
    "erasure_eligible",
    "has_consent",
    "luhn_valid",
    "minimize",
    "processing_allowed",
    "redact",
    "redact_cards",
    "redact_emails",
    "redact_phones",
    "requires_identity_verification",
    "retention_days",
    "rule_for",
    "schedule_summary",
    "valid_legal_basis",
]
