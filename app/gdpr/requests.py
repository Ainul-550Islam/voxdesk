"""Data-subject request (DSAR) modelling and action planning (Phase 4, GDPR).

The right of access, right to erasure and friends are obligations the API
layer must honour with a documented, bounded workflow. This module models the
*decision* part of that workflow — what a valid request is, whether it is
properly authenticated, and what actions a compliant response entails — as a
pure, deterministic function of the request and its context. The database
work stays in the service layer.

Encoded obligations:

* Every request must be **identity-verified** before any data moves (GDPR Art.
  12(6): a controller may refuse to act where it cannot verify identity).
* Erasure must be **refused where a legal hold applies** (e.g. a legal
  obligation to retain) — the hold is honoured, not ignored.
* The default deadline is **30 days**, matching GDPR Art. 12(3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

#: Request types recognised by the policy (GDPR Arts. 15–21).
REQUEST_TYPES = frozenset({
    "access",
    "rectification",
    "erasure",
    "portability",
    "objection",
    "restriction",
})

#: The statutory default response window, in days (GDPR Art. 12(3)).
DEFAULT_DEADLINE_DAYS = 30

#: Legal holds that block erasure: when one applies, the data must be kept,
#: so the action plan applies the hold instead of deleting.
KNOWN_HOLDS = frozenset({
    "legal_obligation",
    "public_interest",
    "defence_of_claims",
    "contract_pending",
})


class RequestError(ValueError):
    """Raised for malformed subject requests."""


@dataclass(frozen=True)
class SubjectRequest:
    """A data subject's request. ``subject_id`` is the caller's identifier
    within the tenant; ``verified`` records whether identity was confirmed."""

    id: str
    tenant_id: str
    subject_id: str
    type: str
    verified: bool = False
    requested_at: datetime | None = None

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.id or not self.id.strip():
            problems.append("request requires an id")
        if not self.tenant_id or not self.tenant_id.strip():
            problems.append("request requires a tenant_id")
        if not self.subject_id or not self.subject_id.strip():
            problems.append("request requires a subject_id")
        if self.type not in REQUEST_TYPES:
            problems.append(f"unknown request type {self.type!r}")
        return problems


#: The actions each request type maps to, in execution order.
_REQUEST_ACTIONS: dict[str, tuple[str, ...]] = {
    "access": ("verify_identity", "locate_records", "export_personal_data"),
    "rectification": ("verify_identity", "locate_records", "correct_personal_data"),
    "erasure": ("verify_identity", "locate_records", "delete_personal_data"),
    "portability": ("verify_identity", "locate_records", "export_machine_readable"),
    "objection": ("verify_identity", "assess_legitimate_interest", "suppress_processing"),
    "restriction": ("verify_identity", "restrict_processing"),
}


@dataclass(frozen=True)
class ActionPlan:
    """What a compliant response to a request entails."""

    request_id: str
    type: str
    actions: tuple[str, ...] = ()
    deadline_days: int = DEFAULT_DEADLINE_DAYS
    blocked: bool = False
    block_reason: str = ""
    identity_verified: bool = False

    @property
    def requires_identity_verification(self) -> bool:
        return "verify_identity" in self.actions


def action_plan(
    request: SubjectRequest,
    *,
    holds: set[str] = frozenset(),
) -> ActionPlan:
    """Compute the action plan for a request.

    * Unverifiable requests get a plan whose only action is identity
      verification (nothing else may proceed).
    * Erasure blocked by a hold yields a plan that records the hold and
      omits deletion, rather than silently deleting retained data.
    * Unknown holds are treated conservatively as blocking (fail closed).
    """
    problems = request.validate()
    if problems:
        raise RequestError("; ".join(problems))

    if not request.verified:
        return ActionPlan(
            request_id=request.id,
            type=request.type,
            actions=("verify_identity",),
            identity_verified=False,
        )

    if request.type == "erasure" and holds:
        # Any hold blocks erasure (fail closed: an unrecognised hold is
        # treated as blocking too); the plan applies the hold, never deletes.
        return ActionPlan(
            request_id=request.id,
            type=request.type,
            actions=("locate_records", "apply_hold"),
            blocked=True,
            block_reason=";".join(sorted(holds)),
            identity_verified=True,
        )

    actions = _REQUEST_ACTIONS.get(request.type, ("verify_identity",))
    return ActionPlan(
        request_id=request.id,
        type=request.type,
        actions=tuple(a for a in actions if a != "verify_identity"),
        identity_verified=True,
    )


def erasure_blocked(holds: set[str]) -> bool:
    """True when any hold (known or not) would prevent erasure."""
    return bool(holds)


def requires_identity_verification(request: SubjectRequest) -> bool:
    return action_plan(request).requires_identity_verification
