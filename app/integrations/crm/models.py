"""
The provider-neutral domain.

Everything above the adapters speaks these types; everything below speaks
JSON shaped the way one particular vendor likes. That boundary is the whole
architecture — adding Salesforce means writing an adapter that consumes
`NormalizedContact` and `NormalizedActivity`, and touching nothing else.

These are plain frozen dataclasses rather than SQLAlchemy rows or Pydantic
models on purpose. They are values that cross a boundary, they need to be
constructible in a test without a database, and they must not drag a session
into an adapter.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ------------------------------------------------------------- identity ---

_NON_DIGITS = re.compile(r"[^0-9]")


def normalize_phone(raw: str | None) -> str | None:
    """
    Reduce a phone number to comparable digits.

    Deliberately crude: strip everything that is not a digit, then drop a
    single leading US country code when the result is 11 digits starting
    with 1. Full E.164 parsing needs a region hint this layer does not have,
    and the goal here is only to make `(555) 010-2000` and `+15550102000`
    hash to the same value so they do not become two CRM contacts.
    """
    if not raw:
        return None
    digits = _NON_DIGITS.sub("", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits or None


def normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip().lower()
    return cleaned or None


def identity_hash(*, tenant_id: str, phone: str | None, email: str | None) -> str | None:
    """
    A stable, tenant-salted fingerprint of who this person is.

    Phone wins over email because a voice product always has a phone number
    and frequently has no email, so preferring email would fragment the same
    caller across two links.

    Salted with the tenant id for two reasons. It guarantees requirement 19's
    "never match contacts across tenants" at the level of the value itself
    rather than the query — two tenants with the same customer produce
    different hashes, so even a query that forgot its `WHERE tenant_id` could
    not join them. And it means the table is not a rainbow-table-able index of
    every phone number the platform has ever seen.
    """
    phone = normalize_phone(phone)
    email = normalize_email(email)
    basis = f"tel:{phone}" if phone else (f"mailto:{email}" if email else None)
    if basis is None:
        return None
    return hashlib.sha256(f"{tenant_id}|{basis}".encode()).hexdigest()


# ------------------------------------------------------------- contact ---

@dataclass(frozen=True)
class NormalizedContact:
    """
    A person, in VoxDesk's vocabulary. Requirement 16's field list.

    Adapters map *from* this; nothing maps into it from a provider shape
    except `get_contact`, which is a read.
    """

    first_name: str = ""
    last_name: str = ""
    phone: str | None = None
    email: str | None = None
    company: str | None = None
    source: str = "VoxDesk"
    tags: tuple[str, ...] = ()
    intent: str | None = None
    lead_score: int | None = None
    notes: str = ""
    #: Tenant-configured extras, already validated by `mapping.py`.
    custom_fields: dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()

    def identity_hash(self, tenant_id: str) -> str | None:
        return identity_hash(tenant_id=tenant_id, phone=self.phone, email=self.email)

    @staticmethod
    def split_name(full: str) -> tuple[str, str]:
        """
        Best-effort first/last split.

        Every provider wants two fields and callers give one. The old code
        defaulted to `("Unknown", "Caller")`, which is genuinely better than
        an empty required field — a CRM full of blank contacts is worse than
        one full of obviously-placeholder ones — so that behaviour is kept.
        """
        cleaned = (full or "").strip()
        if not cleaned:
            return "Unknown", "Caller"
        first, _, last = cleaned.partition(" ")
        return first, (last.strip() or "Caller")


@dataclass(frozen=True)
class NormalizedActivity:
    """
    Something that happened, attached to a contact: a call, a note, an
    outcome. Providers model this as notes, engagements, timeline events or
    tasks; the adapter picks whichever its API actually has.
    """

    title: str
    body: str = ""
    occurred_at: datetime | None = None
    duration_seconds: float | None = None
    #: Structured extras an adapter may fold into a body or into properties.
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedAppointment:
    """A scheduled slot. Not every provider has a calendar; see capabilities."""

    title: str
    starts_at: datetime
    ends_at: datetime
    contact: NormalizedContact
    notes: str = ""
    external_calendar_id: str | None = None
    cancelled: bool = False


# ------------------------------------------------------------- results ---

@dataclass(frozen=True)
class CrmResult:
    """
    What an adapter returns when the provider accepted the operation.

    `external_id` is the point of the whole type. Requirement 12 forbids
    claiming a sync succeeded without it, so the service layer refuses to
    write SYNCED unless a result carries one — a provider that returns 200
    with no id is treated as a failure to record, not a success.
    """

    external_id: str
    #: True when the provider matched an existing record instead of creating.
    already_existed: bool = False
    #: Safe, non-secret extras worth keeping: which endpoint, which object type.
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HealthResult:
    """Requirement 24's normalized shape."""

    connected: bool
    provider: str
    latency_ms: float
    safe_message: str = ""