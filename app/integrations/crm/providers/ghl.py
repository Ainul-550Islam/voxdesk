"""
GoHighLevel adapter (API v2).

Contract this is written against, from HighLevel's developer documentation:

* Base URL ``https://services.leadconnectorhq.com``
* ``Authorization: Bearer <token>`` — an OAuth access token or a Private
  Integration Token
* ``Version: 2021-07-28`` on **every** request. Omitting it produces errors
  that blame the payload instead of the missing header, which is worth a
  comment because it is the single most common GHL integration failure.
* ``locationId`` on write payloads. An agency-level token cannot write to
  location-scoped resources.
* Rate limit: 500 requests / 10 seconds per sub-account.

**Upsert.** HighLevel documents ``POST /contacts/upsert``, and it is the right
call: it matches on email or phone within the location and returns the
existing record instead of duplicating. But community reports disagree about
whether it is present on every account and API version, so a 404 here is
treated as "this account does not have the endpoint" and falls back to
search-then-create/update rather than being reported as a permanent failure.
That fallback is the difference between a working integration and a tenant
whose syncs all say "404" for reasons nobody can reproduce.

**Idempotency.** GHL accepts no idempotency key header — noted explicitly in
their integration guidance. Duplicate protection therefore comes from two
places we control: the upsert semantics above, and `CrmContactLink`, which
remembers the external id so a retry updates instead of creating.
"""
from __future__ import annotations

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
    NormalizedAppointment,
    NormalizedContact,
)

BASE_URL = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"


class GoHighLevelProvider(CrmProvider):
    name = "gohighlevel"
    capabilities = frozenset({
        Capability.UPSERT_CONTACT,
        Capability.CREATE_CONTACT,
        Capability.UPDATE_CONTACT,
        Capability.GET_CONTACT,
        Capability.CREATE_NOTE,
        Capability.CREATE_ACTIVITY,
        Capability.CREATE_APPOINTMENT,
        Capability.ADD_TAG,
        Capability.ADD_CUSTOM_FIELDS,
        Capability.HEALTH_CHECK,
    })

    # ------------------------------------------------------------- plumbing ---

    @property
    def _token(self) -> str:
        token = (self.context.credentials or {}).get("access_token") or ""
        if not token:
            raise CrmConfigurationError(
                "GoHighLevel access token is not configured", provider=self.name
            )
        return token

    @property
    def _location_id(self) -> str:
        location = (self.context.config or {}).get("location_id") or ""
        if not location:
            raise CrmConfigurationError(
                "GoHighLevel location_id is not configured; agency tokens cannot "
                "write location-scoped resources",
                provider=self.name,
            )
        return location

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["Authorization"] = f"Bearer {self._token}"
        headers["Version"] = API_VERSION
        headers["Accept"] = "application/json"
        return headers

    def _base(self) -> str:
        # Overridable so tests can point at a local stub without patching httpx.
        return (self.context.config or {}).get("base_url") or BASE_URL

    # -------------------------------------------------------------- mapping ---

    def contact_payload(self, contact: NormalizedContact) -> dict[str, Any]:
        """
        VoxDesk contact -> GHL contact body.

        Public and pure so the payload can be asserted in a test without any
        HTTP at all, which is what requirement 26's "payload mapping" tests do.
        """
        body: dict[str, Any] = {
            "locationId": self._location_id,
            "firstName": contact.first_name or "Unknown",
            "lastName": contact.last_name or "Caller",
            "source": contact.source,
        }
        if contact.phone:
            body["phone"] = contact.phone
        if contact.email:
            # GHL matches on email; normalising the case here is what stops
            # "Jane@x.com" and "jane@x.com" becoming two contacts.
            body["email"] = contact.email.strip().lower()
        if contact.company:
            body["companyName"] = contact.company
        if contact.tags:
            body["tags"] = list(contact.tags)

        custom = self._custom_fields(contact)
        if custom:
            body["customFields"] = custom
        return body

    def _custom_fields(self, contact: NormalizedContact) -> list[dict[str, Any]]:
        """
        GHL takes custom fields as a list of `{"key" | "id", "field_value"}`.

        A mapping value that looks like a GHL field id (their ids are long
        hex-ish strings) is sent as `id`; anything else is sent as `key`. Both
        forms are accepted by the API and tenants have both in front of them
        depending on where in the GHL UI they looked.
        """
        fields: list[dict[str, Any]] = []
        for key, value in (contact.custom_fields or {}).items():
            entry_key = "id" if _looks_like_ghl_id(key) else "key"
            fields.append({entry_key: key, "field_value": _as_text(value)})
        if contact.lead_score is not None and "lead_score" not in (
            contact.custom_fields or {}
        ):
            fields.append({"key": "lead_score", "field_value": str(contact.lead_score)})
        return fields

    # ----------------------------------------------------------- operations ---

    async def upsert_contact(self, contact: NormalizedContact) -> CrmResult:
        body = self.contact_payload(contact)
        try:
            _, data = await self.request(
                "POST", f"{self._base()}/contacts/upsert", json_body=body
            )
        except CrmNotFound:
            # The account or API version does not expose /contacts/upsert.
            # Fall back rather than failing the sync permanently.
            return await self._upsert_by_search(contact)

        contact_id = _contact_id(data)
        if not contact_id:
            raise CrmValidationError(
                "GoHighLevel accepted the upsert but returned no contact id",
                provider=self.name,
            )
        return CrmResult(
            external_id=contact_id,
            already_existed=bool(_dig(data, "new") is False),
            details={"endpoint": "contacts/upsert"},
        )

    async def _upsert_by_search(self, contact: NormalizedContact) -> CrmResult:
        existing = await self.get_contact(phone=contact.phone, email=contact.email)
        if existing is not None:
            result = await self.update_contact(existing.external_id, contact)
            return CrmResult(
                external_id=result.external_id, already_existed=True,
                details={"endpoint": "contacts/search+update"},
            )
        created = await self.create_contact(contact)
        return CrmResult(
            external_id=created.external_id, already_existed=False,
            details={"endpoint": "contacts/search+create"},
        )

    async def create_contact(self, contact: NormalizedContact) -> CrmResult:
        _, data = await self.request(
            "POST", f"{self._base()}/contacts/", json_body=self.contact_payload(contact)
        )
        contact_id = _contact_id(data)
        if not contact_id:
            raise CrmValidationError(
                "GoHighLevel accepted the contact but returned no id", provider=self.name
            )
        return CrmResult(external_id=contact_id, details={"endpoint": "contacts"})

    async def update_contact(
        self, external_id: str, contact: NormalizedContact
    ) -> CrmResult:
        body = self.contact_payload(contact)
        # locationId is rejected on update: it is fixed by the contact itself.
        body.pop("locationId", None)
        await self.request(
            "PUT", f"{self._base()}/contacts/{external_id}", json_body=body
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
                    "GET", f"{self._base()}/contacts/{external_id}"
                )
            except CrmNotFound:
                return None
            found = _contact_id(data)
            return CrmResult(external_id=found, already_existed=True) if found else None

        if not (phone or email):
            return None

        params: dict[str, Any] = {"locationId": self._location_id}
        if email:
            params["query"] = email.strip().lower()
        elif phone:
            params["query"] = phone
        try:
            _, data = await self.request(
                "GET", f"{self._base()}/contacts/", params=params
            )
        except CrmNotFound:
            return None

        contacts = (data or {}).get("contacts") or []
        if not contacts:
            return None
        first = contacts[0].get("id")
        return CrmResult(external_id=first, already_existed=True) if first else None

    async def create_note(
        self, external_contact_id: str, activity: NormalizedActivity
    ) -> CrmResult:
        body = {"body": _note_body(activity)}
        _, data = await self.request(
            "POST", f"{self._base()}/contacts/{external_contact_id}/notes",
            json_body=body,
        )
        note_id = _dig(data, "note", "id") or _dig(data, "id") or external_contact_id
        return CrmResult(external_id=str(note_id), details={"endpoint": "contacts/notes"})

    async def create_activity(
        self, external_contact_id: str, activity: NormalizedActivity
    ) -> CrmResult:
        # GHL has no distinct "activity" object for this purpose; a note on
        # the contact timeline is the honest equivalent, so this is an alias
        # rather than an invented endpoint.
        return await self.create_note(external_contact_id, activity)

    async def add_tag(self, external_contact_id: str, tags: list[str]) -> CrmResult:
        cleaned = [t for t in (tags or []) if t]
        if not cleaned:
            return CrmResult(external_id=external_contact_id, already_existed=True)
        await self.request(
            "POST", f"{self._base()}/contacts/{external_contact_id}/tags",
            json_body={"tags": cleaned},
        )
        return CrmResult(external_id=external_contact_id, already_existed=True)

    async def add_custom_fields(
        self, external_contact_id: str, fields: dict[str, Any]
    ) -> CrmResult:
        if not fields:
            return CrmResult(external_id=external_contact_id, already_existed=True)
        payload = [
            {("id" if _looks_like_ghl_id(k) else "key"): k, "field_value": _as_text(v)}
            for k, v in fields.items()
        ]
        await self.request(
            "PUT", f"{self._base()}/contacts/{external_contact_id}",
            json_body={"customFields": payload},
        )
        return CrmResult(external_id=external_contact_id, already_existed=True)

    async def create_appointment(
        self, appointment: NormalizedAppointment, *, external_contact_id: str | None = None
    ) -> CrmResult:
        calendar_id = (
            appointment.external_calendar_id
            or (self.context.config or {}).get("calendar_id")
        )
        if not calendar_id:
            raise CrmConfigurationError(
                "GoHighLevel calendar_id is not configured; appointment sync needs "
                "a calendar to write into",
                provider=self.name,
            )
        body = {
            "calendarId": calendar_id,
            "locationId": self._location_id,
            "title": appointment.title,
            "startTime": appointment.starts_at.isoformat(),
            "endTime": appointment.ends_at.isoformat(),
        }
        if external_contact_id:
            body["contactId"] = external_contact_id
        _, data = await self.request(
            "POST", f"{self._base()}/calendars/events/appointments", json_body=body
        )
        event_id = _dig(data, "id") or _dig(data, "appointment", "id")
        if not event_id:
            raise CrmValidationError(
                "GoHighLevel accepted the appointment but returned no id",
                provider=self.name,
            )
        return CrmResult(
            external_id=str(event_id), details={"endpoint": "calendars/events/appointments"}
        )

    async def health_check(self) -> HealthResult:
        async def probe():
            # A cheap, read-only, location-scoped call. Chosen because it
            # proves the three things that actually break: the token works,
            # the Version header is accepted, and the location id is real.
            await self.request(
                "GET", f"{self._base()}/contacts/",
                params={"locationId": self._location_id, "limit": 1},
            )

        return await self._timed_health_check(probe)


# ------------------------------------------------------------------ helpers ---

def _looks_like_ghl_id(value: str) -> bool:
    """GHL object ids are ~20+ chars of mixed alphanumerics with no spaces."""
    return len(value) >= 20 and value.isalnum()


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _contact_id(data: Any) -> str | None:
    """
    GHL returns the contact under several shapes depending on the endpoint:
    `{"contact": {"id": ...}}`, `{"id": ...}`, or the upsert flavour that
    nests it one deeper. Accept all of them rather than guessing one.
    """
    for path in (("contact", "id"), ("id",), ("contact", "contactId"), ("contactId",)):
        found = _dig(data, *path)
        if found:
            return str(found)
    return None


def _note_body(activity: NormalizedActivity) -> str:
    parts = [activity.title]
    if activity.body:
        parts.append(activity.body)
    return "\n\n".join(p for p in parts if p)

