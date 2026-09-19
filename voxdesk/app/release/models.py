"""Launch-gate data model (Step 11 — launch control).

The vocabulary every other module in this package speaks. Five statuses, four
severities, four validation classes, three final decisions, and the dataclasses
that carry a checklist item, its evidence, its (optional) waiver, the recorded
release artifact, the observed live facts, one evaluated row, and the assembled
report.

Design constraints this module enforces by construction:

* A requirement is either PASS, FAIL, BLOCKED, NOT_RUN or WAIVED — nothing
  else. "Tests pass" is never folded into PASS implicitly; PASS is what an
  evidence record says after a real run.
* Severity is P0..P3. P0/P1 block launch; P2/P3 do not (they are open items).
* Classification (AUTOMATED / HUMAN / EXTERNAL / INFRASTRUCTURE) is recorded
  per item so the gate can never silently mix a deterministic test with a
  human sign-off or an infrastructure drill.
* Waivers carry a reason, approver, date and expiry and are only ever legal
  for P2/P3 (enforced in ``app.release.gate``, not here, because validity also
  depends on today's date).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    PASS = "PASS"  # nosec B105 -- launch status label, not a credential
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"
    WAIVED = "WAIVED"


class Severity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class Classification(str, Enum):
    AUTOMATED = "AUTOMATED"
    HUMAN = "HUMAN"
    EXTERNAL = "EXTERNAL"
    INFRASTRUCTURE = "INFRASTRUCTURE"


class FinalDecision(str, Enum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    BLOCKED = "BLOCKED"


#: Statuses that keep a requirement from being satisfied. WAIVED is excluded:
#: a validly-waived P2/P3 item is closed, not open.
BLOCKING_STATUSES = (Status.FAIL, Status.BLOCKED, Status.NOT_RUN)


@dataclass(frozen=True)
class ChecklistItem:
    """One launch requirement from the canonical checklist."""

    id: str
    category: str
    requirement: str
    severity: Severity
    classification: Classification
    evidence_hint: str = ""
    notes: str = ""


@dataclass(frozen=True)
class Waiver:
    """A recorded, reviewable waiver. Only P2/P3 may be waived."""

    reason: str
    approver: str
    date: str      # ISO date the waiver was granted
    expiry: str    # ISO date the waiver stops applying ("" = none, discouraged)


@dataclass(frozen=True)
class Evidence:
    """One item's recorded evidence. ``status`` is the only source of truth
    for whether a requirement is satisfied; the free-text ``evidence`` field
    is what makes a PASS auditable (command, CI run, checksum, sign-off)."""

    item_id: str
    status: Status
    evidence: str = ""
    verification_date: str = ""
    verifier: str = ""
    notes: str = ""
    waiver: Waiver | None = None


@dataclass(frozen=True)
class ArtifactRecord:
    """The certified release identity, frozen at certification time."""

    release_identifier: str = ""
    git_commit: str = ""
    dependency_fingerprint: str = ""
    migration_heads: tuple[str, ...] = ()
    configuration_fingerprint: str = ""
    environment_class: str = ""
    build_date: str = ""
    image_digest: str = ""  # populated when a container image is built; "" = n/a

    @property
    def frozen(self) -> bool:
        """An artifact is frozen only once its git commit is recorded."""
        return bool(self.git_commit)


@dataclass(frozen=True)
class LiveFacts:
    """The identity observed right now, computed by the CLI from the tree."""

    git_commit: str = ""
    dependency_fingerprint: str = ""
    migration_heads: tuple[str, ...] = ()
    configuration_fingerprint: str = ""


@dataclass(frozen=True)
class Row:
    """The evaluated state of one checklist item."""

    item: ChecklistItem
    status: Status
    evidence: str
    verifier: str
    verification_date: str
    notes: str
    waiver: Waiver | None
    blocker: str


@dataclass
class LaunchReport:
    """The assembled gate output: per-item rows, counts, drift, decision."""

    rows: list[Row] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)
    artifact_frozen: bool = False
    p0_blockers: int = 0
    p1_blockers: int = 0
    p2_open: int = 0
    p3_open: int = 0
    decision: FinalDecision = FinalDecision.NOT_READY
