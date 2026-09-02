"""
HubSpot adapter (CRM v3).

Contract:

* Base URL ``https://api.hubapi.com``
* ``Authorization: Bearer <private app token>``
* Errors carry a ``category`` field (``VALIDATION_ERROR``, ``RATE_LIMIT``,
  ``OBJECT_NOT_FOUND``, ...) which is more reliable than the status code alone.

**Why upsert is not simply "call the upsert endpoint".**

HubSpot has ``POST /crm/v3/objects/contacts/batch/upsert`` with
``idProperty: "email"``, and on a portal where email is configured as a unique
property it works. On portals where it is not, the same request returns
``400 VALIDATION_ERROR: Unable to perform update/upsert by non-unique property
email`` — a well-documented and frequently-reported inconsistency. HubSpot
also states that partial upserts are unsupported when using email.

And the case that matters most here: **a voice product usually has a phone
number and no email at all.** HubSpot deduplicates contacts on email; phone is
not a unique identifier. An adapter that only knew how to upsert by email
would create a fresh contact on every single inbound call.

So the strategy is explicit rather than hopeful:

1. Email present → try the batch upsert by email.
2. No email, or the upsert was rejected as non-unique → search on phone, then
   ``PATCH`` the match or ``POST`` a new contact.

Step 2 is the common path for this product, not the exception.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.integrations.crm.base import Capability, CrmProvider
from app.integrations.crm.errors import (
    CrmConfigurationError,
    CrmNotFound,
    CrmValidationError,
)
from app.integrations.crm.models import (
    CrmResult,
    HealthResult,
    NormalizedActivity,
    NormalizedContact,
)

BASE_URL = "https://api.hubapi.com"

#: HUBSPOT_DEFINED association type for "note -> contact". 201 is the inverse
#: direction and silently produces an orphaned note, so the number matters.
NOTE_TO_CONTACT_ASSOCIATION = 202


class HubSpotProvider(CrmProvider):
    name = "hubspot"
    capabilities = frozenset({
        Capability.UPSERT_CONTACT,
        Capability.CREATE_CONTACT,
        Capability.UPDATE_CONTACT,
        Capability.GET_CONTACT,
        Capability.CREATE_NOTE,
        Capability.CREATE_ACTIVITY,
        Capability.ADD_CUSTOM_FIELDS,
        Capability.HEALTH_CHECK,
    })
    # Deliberately absent: ADD_TAG (HubSpot has no tags -- the equivalent is a
    # list membership or a property, and pretending otherwise would silently
    # drop data), CREATE_APPOINTMENT (meetings need an owner and a meeting
    # link this layer does not have).

    @property
    def _token(self) -> str:
        token = (self.context.credentials or {}).get("access_token") or ""
        if not token:
            raise CrmConfigurationError(
                "HubSpot access token is not configured", provider=self.name
            )
        return token

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _base(self) -> str:
        return (self.context.config or {}).get("base_url") or BASE_URL

    # -------------------------------------------------------------- mapping ---

    def contact_properties(self, contact: NormalizedContact) -> dict[str, Any]:
        """
        VoxDesk contact -> HubSpot properties.

        HubSpot property names are lowercase with no separators
        (`firstname`, not `first_name` or `firstName`) — a detail that fails
        silently, because HubSpot accepts unknown properties on some plans and
        simply drops them.
        """
        properties: dict[str, Any] = {
            "firstname": contact.first_name or "Unknown",
            "lastname": contact.last_name or "Caller",
        }
        if contact.phone:
            properties["phone"] = contact.phone
        if contact.email:
            properties["email"] = contact.email.strip().lower()
        if contact.company:
            properties["company"] = contact.company
        if contact.source:
            properties["hs_lead_status"] = "OPEN"
        if contact.lead_score is not None:
            properties["hs_predictivecontactscore_v2"] = contact.lead_score
        # Tenant-configured custom properties last, so a tenant can override
        # anything above deliberately rather than being silently overruled.
        for key, value in (contact.custom_fields or {}).items():
            properties[key] = value
        return properties

    # ----------------------------------------------------------- operations ---

    async def upsert_contact(self, contact: NormalizedContact) -> CrmResult:
        if contact.email:
            try:
                return await self._upsert_by_email(contact)
            except CrmValidationError:
                # The portal does not treat email as unique. Documented
                # HubSpot behaviour, not an error in our payload -- fall
                # through to the search path instead of failing the sync.
                pass
        return await self._upsert_by_search(contact)

    async def _upsert_by_email(self, contact: NormalizedContact) -> CrmResult:
        body = {
            "inputs": [{
                "idProperty": "email",
                "id": contact.email.strip().lower(),
                "properties": self.contact_properties(contact),
            }]
        }
        _, data = await self.request(
            "POST", f"{self._base()}/crm/v3/objects/contacts/batch/upsert",
            json_body=body,
        )
        results = (data or {}).get("results") or []
        if not results or not results[0].get("id"):
            raise CrmValidationError(
                "HubSpot accepted the upsert but returned no contact id",
                provider=self.name,
            )
        first = results[0]
        return CrmResult(
            external_id=str(first["id"]),
            # `new` is absent on older responses; treat unknown as existing,
            # which is the safe direction -- it never causes a second create.
            already_existed=not bool(first.get("new", False)),
            details={"endpoint": "contacts/batch/upsert"},
        )

    async def _upsert_by_search(self, contact: NormalizedContact) -> CrmResult:
        existing = await self.get_contact(phone=contact.phone, email=contact.email)
        if existing is not None:
            await self.update_contact(existing.external_id, contact)
            return CrmResult(
                external_id=existing.external_id, already_existed=True,
                details={"endpoint": "contacts/search+patch"},
            )
        created = await self.create_contact(contact)
        return CrmResult(
            external_id=created.external_id, already_existed=False,
            details={"endpoint": "contacts/search+create"},
        )

    async def create_contact(self, contact: NormalizedContact) -> CrmResult:
        _, data = await self.request(
            "POST", f"{self._base()}/crm/v3/objects/contacts",
            json_body={"properties": self.contact_properties(contact)},
        )
        contact_id = (data or {}).get("id")
        if not contact_id:
            raise CrmValidationError(
                "HubSpot accepted the contact but returned no id", provider=self.name
            )
        return CrmResult(external_id=str(contact_id), details={"endpoint": "contacts"})

    async def update_contact(
        self, external_id: str, contact: NormalizedContact
    ) -> CrmResult:
        await self.request(
            "PATCH", f"{self._base()}/crm/v3/objects/contacts/{external_id}",
            json_body={"properties": self.contact_properties(contact)},
        )
        return CrmResult(
            external_id=external_id, already_existed=True,
            details={"endpoint": "contacts/{id}"},
        )

    async def get_contact(
        self, *, external_id: str | None = None, phone: str | None = None,
        email: str | None = None,
    ) -> CrmResult | None:
        if external_id:
            try:
                _, data = await self.request(
                    "GET", f"{self._base()}/crm/v3/objects/contacts/{external_id}"
                )
            except CrmNotFound:
                return None
            found = (data or {}).get("id")
            return CrmResult(external_id=str(found), already_existed=True) if found else None

        filters = self._search_filters(phone=phone, email=email)
        if not filters:
            return None

        _, data = await self.request(
            "POST", f"{self._base()}/crm/v3/objects/contacts/search",
            json_body={
                "filterGroups": [{"filters": filters}],
                "properties": ["email", "phone"],
                "limit": 1,
            },
        )
        results = (data or {}).get("results") or []
        if not results:
            return None
        return CrmResult(external_id=str(results[0]["id"]), already_existed=True)

    def _search_filters(
        self, *, phone: str | None, email: str | None
    ) -> list[dict[str, Any]]:
        """
        Search predicate, email first.

        One filter, not two ORed together: HubSpot's `filterGroups` ANDs
        within a group, so putting both in would look for a contact matching
        *both*, which is almost never what exists.
        """
        if email:
            return [{
                "propertyName": "email", "operator": "EQ",
                "value": email.strip().lower(),
            }]
        if phone:
            return [{"propertyName": "phone", "operator": "EQ", "value": phone}]
        return []

    async def create_note(
        self, external_contact_id: str, activity: NormalizedActivity
    ) -> CrmResult:
        occurred = activity.occurred_at or datetime.now(timezone.utc)
        body = {
            "properties": {
                # HubSpot wants epoch milliseconds. An ISO string is accepted
                # by some endpoints and rejected by this one.
                "hs_timestamp": int(occurred.timestamp() * 1000),
                "hs_note_body": _note_body(activity),
            },
            "associations": [{
                "to": {"id": external_contact_id},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": NOTE_TO_CONTACT_ASSOCIATION,
                }],
            }],
        }
        _, data = await self.request(
            "POST", f"{self._base()}/crm/v3/objects/notes", json_body=body
        )
        note_id = (data or {}).get("id")
        if not note_id:
            raise CrmValidationError(
                "HubSpot accepted the note but returned no id", provider=self.name
            )
        return CrmResult(external_id=str(note_id), details={"endpoint": "notes"})

    async def create_activity(
        self, external_contact_id: str, activity: NormalizedActivity
    ) -> CrmResult:
        return await self.create_note(external_contact_id, activity)

    async def add_custom_fields(
        self, external_contact_id: str, fields: dict[str, Any]
    ) -> CrmResult:
        if not fields:
            return CrmResult(external_id=external_contact_id, already_existed=True)
        await self.request(
            "PATCH", f"{self._base()}/crm/v3/objects/contacts/{external_contact_id}",
            json_body={"properties": dict(fields)},
        )
        return CrmResult(external_id=external_contact_id, already_existed=True)

    async def health_check(self) -> HealthResult:
        async def probe():
            await self.request(
                "GET", f"{self._base()}/crm/v3/objects/contacts", params={"limit": 1}
            )

        return await self._timed_health_check(probe)


def _note_body(activity: NormalizedActivity) -> str:
    parts = [f"<b>{_escape(activity.title)}</b>"] if activity.title else []
    if activity.body:
        # HubSpot notes render HTML; a plain newline collapses.
        parts.append(_escape(activity.body).replace("\n", "<br>"))
    return "<br><br>".join(parts)


def _escape(text: str) -> str:
    """
    Minimal HTML escaping for note bodies.

    Note bodies contain call summaries, which contain whatever the caller
    said. Without this, a caller saying something with an angle bracket in it
    injects markup into the CRM record.
    """
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
