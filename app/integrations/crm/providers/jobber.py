"""
Jobber adapter (GraphQL API).

Strategically the most important adapter here: Jobber is what home-service
businesses — the exact market for an AI receptionist — actually run on.

Contract, from Jobber's developer documentation:

* A single endpoint: ``POST https://api.getjobber.com/api/graphql``
* Headers: ``Authorization: Bearer <OAuth token>``,
  ``X-JOBBER-GRAPHQL-VERSION: <active version>``, ``Content-Type: application/json``
* Mutations return ``userErrors { message path }``. **An empty array means
  success; a non-empty one means the mutation was rejected.** This arrives
  with HTTP 200, so an adapter that only checks status codes reports every
  rejection as a success. Requirement 12 forbids claiming a sync happened
  when it did not, and this is the exact shape that trap takes on Jobber.
* Transport-level GraphQL problems arrive as a top-level ``errors`` array,
  also with HTTP 200.

**What this adapter deliberately does not do**, because requirement 9 says not
to invent operations:

* ``clientUpsert`` **was removed** from the Jobber schema in the 2023-08-18
  version. Upsert is therefore implemented as query-then-create/edit. Calling
  a mutation that no longer exists would fail on every account.
* No appointment or calendar sync. Jobber models scheduled work as jobs and
  visits, and creating one requires a property, line items and a schedule that
  this layer has no way to supply. `Capability.CREATE_APPOINTMENT` is
  therefore not declared, and the service layer skips appointment events for
  Jobber rather than half-writing a job. That is a real limitation and it is
  documented as one rather than papered over.
* Note the mutation name: ``clientCreateNote``, not ``clientNoteCreate``. The
  latter was removed in the same 2023-08-18 version. They are easy to
  transpose and the failure is a permissions-shaped error message that sends
  you looking in the wrong place.
"""
from __future__ import annotations

from typing import Any

from app.integrations.crm.base import Capability, CrmProvider
from app.integrations.crm.errors import (
    CrmAuthError,
    CrmConfigurationError,
    CrmError,
    CrmRateLimited,
    CrmServerError,
    CrmValidationError,
    safe_message,
)
from app.integrations.crm.models import (
    CrmResult,
    HealthResult,
    NormalizedActivity,
    NormalizedContact,
)

BASE_URL = "https://api.getjobber.com/api/graphql"
API_VERSION = "2025-04-16"

#: GraphQL `extensions.code` values that mean "try again later" rather than
#: "this request was wrong". Jobber throttles on query cost, not request count.
_TRANSIENT_CODES = {"THROTTLED", "TIMEOUT", "INTERNAL_SERVER_ERROR", "SERVICE_UNAVAILABLE"}
_AUTH_CODES = {"UNAUTHENTICATED", "UNAUTHORIZED", "FORBIDDEN"}

_CLIENT_FIELDS = "id firstName lastName companyName"

MUTATION_CLIENT_CREATE = """
mutation VoxDeskClientCreate($input: ClientCreateInput!) {
  clientCreate(input: $input) {
    client { %s }
    userErrors { message path }
  }
}
""" % _CLIENT_FIELDS

MUTATION_CLIENT_EDIT = """
mutation VoxDeskClientEdit($clientId: EncodedId!, $input: ClientEditInput!) {
  clientEdit(clientId: $clientId, input: $input) {
    client { %s }
    userErrors { message path }
  }
}
""" % _CLIENT_FIELDS

MUTATION_CLIENT_NOTE = """
mutation VoxDeskClientNote($clientId: EncodedId!, $input: ClientCreateNoteInput!) {
  clientCreateNote(clientId: $clientId, input: $input) {
    clientNote { id }
    userErrors { message path }
  }
}
"""

QUERY_CLIENT_SEARCH = """
query VoxDeskClientSearch($searchTerm: String!) {
  clients(searchTerm: $searchTerm, first: 1) {
    nodes { %s }
  }
}
""" % _CLIENT_FIELDS

QUERY_ACCOUNT = "query VoxDeskAccount { account { id name } }"


class JobberProvider(CrmProvider):
    name = "jobber"
    capabilities = frozenset({
        Capability.UPSERT_CONTACT,
        Capability.CREATE_CONTACT,
        Capability.UPDATE_CONTACT,
        Capability.GET_CONTACT,
        Capability.CREATE_NOTE,
        Capability.CREATE_ACTIVITY,
        Capability.HEALTH_CHECK,
    })

    @property
    def _token(self) -> str:
        token = (self.context.credentials or {}).get("access_token") or ""
        if not token:
            raise CrmConfigurationError(
                "Jobber access token is not configured", provider=self.name
            )
        return token

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["Authorization"] = f"Bearer {self._token}"
        headers["X-JOBBER-GRAPHQL-VERSION"] = (
            (self.context.config or {}).get("api_version") or API_VERSION
        )
        return headers

    def _base(self) -> str:
        return (self.context.config or {}).get("base_url") or BASE_URL

    # ------------------------------------------------------------- transport ---

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """
        One GraphQL round trip, with Jobber's three failure shapes normalized.

        Everything here is HTTP 200, which is why this cannot be left to the
        shared `request()` classification:

        1. top-level ``errors`` — the query itself was bad, or the service is
           throttling/unavailable;
        2. ``userErrors`` inside the mutation payload — the query was valid
           but the business rules rejected it;
        3. a null payload with no errors at all — treated as a failure, not a
           success, because we have nothing to record an external id from.
        """
        _, data = await self.request(
            "POST", self._base(), json_body={"query": query, "variables": variables}
        )
        if not isinstance(data, dict):
            raise CrmServerError(
                "Jobber returned a non-JSON body", provider=self.name
            )

        errors = data.get("errors") or []
        if errors:
            raise self._classify_graphql_errors(errors)

        payload = data.get("data")
        if not isinstance(payload, dict):
            raise CrmServerError(
                "Jobber returned no data and no errors", provider=self.name
            )
        return payload

    def _classify_graphql_errors(self, errors: list) -> CrmError:
        codes = {
            str(((e or {}).get("extensions") or {}).get("code") or "").upper()
            for e in errors
        }
        message = safe_message(
            "; ".join(str((e or {}).get("message") or "") for e in errors)[:300]
        )

        if codes & _AUTH_CODES:
            return CrmAuthError(
                f"Jobber rejected the credentials: {message}", provider=self.name
            )
        if "THROTTLED" in codes:
            return CrmRateLimited(
                f"Jobber throttled the query: {message}", provider=self.name
            )
        if codes & _TRANSIENT_CODES:
            return CrmServerError(f"Jobber: {message}", provider=self.name)
        # A schema or argument error. Retrying an invalid query forever is
        # exactly the loop requirement 13 forbids.
        return CrmValidationError(f"Jobber rejected the query: {message}", provider=self.name)

    def _check_user_errors(self, node: dict[str, Any], operation: str) -> None:
        user_errors = (node or {}).get("userErrors") or []
        if not user_errors:
            return
        detail = "; ".join(
            f"{'.'.join(str(p) for p in (e.get('path') or []))}: {e.get('message', '')}".strip(": ")
            for e in user_errors
        )
        # Business-rule rejections are permanent by definition: "Last name is
        # required" will still be true on the fifth attempt.
        raise CrmValidationError(
            f"Jobber rejected {operation}: {safe_message(detail)}", provider=self.name
        )

    # --------------------------------------------------------------- mapping ---

    def client_input(self, contact: NormalizedContact) -> dict[str, Any]:
        """
        VoxDesk contact -> Jobber `ClientCreateInput`.

        Jobber wants emails and phones as arrays of typed objects, not
        scalars, and requires `lastName` — hence the `"Caller"` fallback that
        `NormalizedContact.split_name` provides.
        """
        payload: dict[str, Any] = {
            "firstName": contact.first_name or "Unknown",
            "lastName": contact.last_name or "Caller",
        }
        if contact.company:
            payload["companyName"] = contact.company
        if contact.email:
            payload["emails"] = [{
                "description": "MAIN",
                "primary": True,
                "address": contact.email.strip().lower(),
            }]
        if contact.phone:
            payload["phones"] = [{
                "description": "MAIN",
                "primary": True,
                "number": contact.phone,
            }]
        return payload

    # ------------------------------------------------------------ operations ---

    async def upsert_contact(self, contact: NormalizedContact) -> CrmResult:
        """
        Query, then create or edit.

        Not a native upsert: `clientUpsert` was removed from Jobber's schema.
        The race this leaves — two concurrent syncs both finding nothing and
        both creating — is closed a layer up, where `CrmSync`'s unique
        constraint on (event, integration) means only one worker ever runs
        this for a given event.
        """
        existing = await self.get_contact(phone=contact.phone, email=contact.email)
        if existing is not None:
            await self.update_contact(existing.external_id, contact)
            return CrmResult(
                external_id=existing.external_id, already_existed=True,
                details={"operation": "clientEdit"},
            )
        created = await self.create_contact(contact)
        return CrmResult(
            external_id=created.external_id, already_existed=False,
            details={"operation": "clientCreate"},
        )

    async def create_contact(self, contact: NormalizedContact) -> CrmResult:
        data = await self.graphql(
            MUTATION_CLIENT_CREATE, {"input": self.client_input(contact)}
        )
        node = data.get("clientCreate") or {}
        self._check_user_errors(node, "clientCreate")

        client_id = ((node.get("client") or {}).get("id"))
        if not client_id:
            raise CrmValidationError(
                "Jobber reported no errors but returned no client id",
                provider=self.name,
            )
        return CrmResult(external_id=str(client_id), details={"operation": "clientCreate"})

    async def update_contact(
        self, external_id: str, contact: NormalizedContact
    ) -> CrmResult:
        payload = self.client_input(contact)
        data = await self.graphql(
            MUTATION_CLIENT_EDIT, {"clientId": external_id, "input": payload}
        )
        node = data.get("clientEdit") or {}
        self._check_user_errors(node, "clientEdit")
        return CrmResult(
            external_id=external_id, already_existed=True,
            details={"operation": "clientEdit"},
        )

    async def get_contact(
        self, *, external_id: str | None = None, phone: str | None = None,
        email: str | None = None,
    ) -> CrmResult | None:
        term = email or phone or external_id
        if not term:
            return None
        data = await self.graphql(QUERY_CLIENT_SEARCH, {"searchTerm": str(term)})
        nodes = ((data.get("clients") or {}).get("nodes")) or []
        if not nodes or not nodes[0].get("id"):
            return None
        return CrmResult(external_id=str(nodes[0]["id"]), already_existed=True)

    async def create_note(
        self, external_contact_id: str, activity: NormalizedActivity
    ) -> CrmResult:
        body = "\n\n".join(p for p in (activity.title, activity.body) if p)
        data = await self.graphql(
            MUTATION_CLIENT_NOTE,
            {"clientId": external_contact_id, "input": {"message": body}},
        )
        node = data.get("clientCreateNote") or {}
        self._check_user_errors(node, "clientCreateNote")

        note_id = ((node.get("clientNote") or {}).get("id"))
        if not note_id:
            raise CrmValidationError(
                "Jobber reported no errors but returned no note id", provider=self.name
            )
        return CrmResult(
            external_id=str(note_id), details={"operation": "clientCreateNote"}
        )

    async def create_activity(
        self, external_contact_id: str, activity: NormalizedActivity
    ) -> CrmResult:
        return await self.create_note(external_contact_id, activity)

    async def health_check(self) -> HealthResult:
        async def probe():
            data = await self.graphql(QUERY_ACCOUNT, {})
            if not (data.get("account") or {}).get("id"):
                raise CrmAuthError(
                    "Jobber returned no account; the token may lack scopes",
                    provider=self.name,
                )

        return await self._timed_health_check(probe)
