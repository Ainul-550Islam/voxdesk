"""
Field mapping and custom-field configuration.

Two jobs, both of which requirement 16 and 17 ask for and the old code did
inside `crm.py` where every provider could see every other provider's shape.

**Building the normalized contact.** `contact_from_call`, `contact_from_lead`
and friends turn VoxDesk rows into `NormalizedContact`. This is the only place
that knows a `Call` has `from_number` on inbound and `to_number` on outbound.
Adapters never see a `Call`.

**Validating tenant custom-field configuration.** A tenant can map VoxDesk
values into their own provider fields. Requirement 17 is explicit that this
must not become "arbitrary unbounded JSON becomes a provider payload", and
that is not paranoia: the mapping is tenant-supplied, it is stored as JSON,
and it ends up serialized into an outbound HTTPS request against credentials
the tenant owns. Unbounded means a tenant can post a ten-megabyte body to
HubSpot on our worker's time.

So the config is validated on write, against an allowlist of source values,
with bounds on key length, value length and count.
"""
from __future__ import annotations

from typing import Any

from app.integrations.crm.models import NormalizedContact

#: VoxDesk values a tenant may route into a provider custom field. An
#: allowlist rather than "any attribute of the payload", so adding a source
#: is a deliberate act and no internal field can be exfiltrated by guessing
#: its name in a mapping.
ALLOWED_SOURCES: frozenset[str] = frozenset({
    "lead_score",
    "call_summary",
    "call_outcome",
    "call_id",
    "call_direction",
    "call_duration_seconds",
    "appointment_type",
    "appointment_starts_at",
    "source_campaign",
    "intent",
    "booked",
    "transferred",
    "recording_url",
    "transcript_reference",
})

#: Bounds. Chosen to be generous for real use and hostile to abuse.
MAX_CUSTOM_FIELDS = 25
MAX_KEY_LENGTH = 120
MAX_VALUE_LENGTH = 2000


class MappingError(ValueError):
    """Invalid custom-field configuration. Raised on write, never on send."""


def validate_field_mappings(mappings: Any) -> dict[str, str]:
    """
    Validate a tenant's `{provider_field: voxdesk_source}` mapping.

    Raises `MappingError` with a message meant for the tenant. Called from the
    integration API on `PUT`, so a bad mapping is a 422 at configuration time
    rather than a mystery at three in the morning when a call ends.
    """
    if mappings in (None, {}):
        return {}
    if not isinstance(mappings, dict):
        raise MappingError("field_mappings must be an object")
    if len(mappings) > MAX_CUSTOM_FIELDS:
        raise MappingError(
            f"at most {MAX_CUSTOM_FIELDS} custom field mappings are allowed "
            f"(got {len(mappings)})"
        )

    validated: dict[str, str] = {}
    for provider_field, source in mappings.items():
        if not isinstance(provider_field, str) or not provider_field.strip():
            raise MappingError("custom field keys must be non-empty strings")
        if len(provider_field) > MAX_KEY_LENGTH:
            raise MappingError(
                f"custom field key {provider_field[:40]!r} exceeds "
                f"{MAX_KEY_LENGTH} characters"
            )
        if not isinstance(source, str):
            raise MappingError(
                f"mapping for {provider_field!r} must name a VoxDesk field as a string"
            )
        if source not in ALLOWED_SOURCES:
            raise MappingError(
                f"{source!r} is not a mappable VoxDesk field; allowed: "
                f"{', '.join(sorted(ALLOWED_SOURCES))}"
            )
        validated[provider_field.strip()] = source
    return validated


def resolve_custom_fields(
    mappings: dict[str, str], sources: dict[str, Any]
) -> dict[str, Any]:
    """
    Apply a validated mapping to the values available for one event.

    Missing sources are skipped rather than sent as null: a CRM field that
    silently becomes empty on every call without a summary is worse than one
    that is simply not written.

    Values are stringified and truncated here, at the last moment, because
    this is the point where the size of the outbound payload is decided. The
    write-time validation bounds the *configuration*; this bounds the *data*,
    and a summary long enough to matter is generated at runtime, not
    configured.
    """
    resolved: dict[str, Any] = {}
    for provider_field, source in (mappings or {}).items():
        if source not in sources:
            continue
        value = sources[source]
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            resolved[provider_field] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            resolved[provider_field] = value
        else:
            text = str(value)
            resolved[provider_field] = (
                text if len(text) <= MAX_VALUE_LENGTH
                else text[: MAX_VALUE_LENGTH - 1] + "…"
            )
    return resolved


# --------------------------------------------------- VoxDesk row -> contact ---

def caller_number(direction: str, from_number: str, to_number: str) -> str:
    """
    Which end of the call is the customer.

    On an inbound call it is whoever rang us; on an outbound campaign call it
    is whoever we rang. Getting this backwards files the business's own
    Twilio number as a lead, which the old `build_payload` got right and is
    preserved here.
    """
    return from_number if (direction or "").lower() == "inbound" else to_number


def contact_from_call(
    call: Any, *, tags: tuple[str, ...] = (), custom_fields: dict[str, Any] | None = None,
    name: str = "", email: str | None = None,
) -> NormalizedContact:
    """Build a contact from a `Call` row."""
    first, last = NormalizedContact.split_name(name)
    phone = caller_number(
        getattr(call.direction, "value", str(call.direction)),
        call.from_number, call.to_number,
    )
    return NormalizedContact(
        first_name=first,
        last_name=last,
        phone=phone,
        email=email,
        source="VoxDesk AI Receptionist",
        tags=tags,
        intent=call.intent,
        lead_score=call.lead_score,
        custom_fields=custom_fields or {},
    )


def contact_from_lead(
    lead: Any, *, tags: tuple[str, ...] = (), custom_fields: dict[str, Any] | None = None
) -> NormalizedContact:
    """Build a contact from a `Lead` row."""
    first, last = NormalizedContact.split_name(lead.name or "")
    return NormalizedContact(
        first_name=first,
        last_name=last,
        phone=lead.phone,
        email=lead.email,
        company=lead.company,
        source="VoxDesk Lead",
        tags=tags,
        lead_score=lead.score,
        notes=lead.notes or "",
        custom_fields=custom_fields or {},
    )


def contact_from_appointment(
    appointment: Any, *, tags: tuple[str, ...] = (),
    custom_fields: dict[str, Any] | None = None,
) -> NormalizedContact:
    first, last = NormalizedContact.split_name(appointment.customer_name or "")
    return NormalizedContact(
        first_name=first,
        last_name=last,
        phone=appointment.customer_phone,
        source="VoxDesk Booking",
        tags=tags,
        custom_fields=custom_fields or {},
    )


def call_tags(call: Any) -> tuple[str, ...]:
    """
    Tags derived from how the call went.

    Kept small and predictable. A tag list that varies per call fills a CRM
    with single-use tags and makes segmentation useless, so this only emits
    values from a fixed vocabulary.
    """
    tags = ["voxdesk", "ai-call"]
    if call.intent:
        tags.append(str(call.intent))
    if getattr(call, "booked", False):
        tags.append("booked")
    if getattr(call, "escalated", False):
        tags.append("transferred")
    # dict.fromkeys: dedupe while keeping order, so `intent="booked"` does not
    # produce the tag twice.
    return tuple(dict.fromkeys(t for t in tags if t))

