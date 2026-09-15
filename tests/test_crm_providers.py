"""
Per-provider adapter tests: payload mapping and error handling.

The contract tests in `test_crm_contract.py` cover what every provider must
do identically. This file covers what each one does *differently* — the vendor
quirks that are the actual reason an adapter exists.
"""
from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from app.integrations.crm.base import Capability, ProviderContext
from app.integrations.crm.errors import (
    CrmAuthError,
    CrmConfigurationError,
    CrmRateLimited,
    CrmServerError,
    CrmValidationError,
)
from app.integrations.crm.models import (
    NormalizedActivity,
    NormalizedAppointment,
    NormalizedContact,
)
from app.integrations.crm.providers.ghl import GoHighLevelProvider
from app.integrations.crm.providers.hubspot import HubSpotProvider
from app.integrations.crm.providers.jobber import JobberProvider
from app.integrations.crm.providers.webhook import (
    IDEMPOTENCY_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookProvider,
    sign,
    verify,
)
from tests.conftest import FakeTransport

CONTACT = NormalizedContact(
    first_name="Jane", last_name="Doe", phone="+15551230000",
    email="Jane@Example.COM", company="Doe Plumbing",
    tags=("voxdesk", "booked"), lead_score=80,
    custom_fields={"call_summary": "Booked a drain cleaning."},
)
ACTIVITY = NormalizedActivity(
    title="VoxDesk AI call", body="Caller booked Tuesday.",
    occurred_at=datetime(2026, 3, 1, 10, 30),
)


def ctx(**over):
    base = dict(
        tenant_id="t-1", credentials={"access_token": "tok-secret-123456"},
        config={}, field_mappings={}, timeout_seconds=5.0,
    )
    base.update(over)
    return ProviderContext(**base)


# ============================================================ GoHighLevel ===

def ghl(**config):
    settings = {"location_id": "loc-1", "base_url": "https://ghl.test"}
    settings.update(config)
    return GoHighLevelProvider(ctx(config=settings))


class TestGoHighLevelMapping:
    def test_contact_payload_shape(self):
        body = ghl().contact_payload(CONTACT)

        assert body["locationId"] == "loc-1"
        assert body["firstName"] == "Jane"
        assert body["lastName"] == "Doe"
        assert body["phone"] == "+15551230000"
        assert body["companyName"] == "Doe Plumbing"
        assert body["tags"] == ["voxdesk", "booked"]

    def test_email_is_lowercased(self):
        """GHL matches on email; case variance would create two contacts."""
        assert ghl().contact_payload(CONTACT)["email"] == "jane@example.com"

    def test_a_nameless_caller_gets_a_placeholder_not_a_blank(self):
        """
        A CRM full of blank contacts is worse than one full of obviously
        placeholder ones, and GHL requires both name fields.
        """
        first, last = NormalizedContact.split_name("")
        body = ghl().contact_payload(
            NormalizedContact(first_name=first, last_name=last, phone="+1555")
        )
        assert body["firstName"] == "Unknown"
        assert body["lastName"] == "Caller"

    def test_custom_fields_use_key_form_for_readable_names(self):
        fields = ghl().contact_payload(CONTACT)["customFields"]
        summary = next(f for f in fields if f.get("key") == "call_summary")
        assert summary["field_value"] == "Booked a drain cleaning."

    def test_custom_fields_use_id_form_for_ghl_object_ids(self):
        """
        Tenants paste either a field key or a field id depending on where in
        the GHL UI they looked, and the API wants a different property name
        for each.
        """
        contact = NormalizedContact(
            first_name="A", last_name="B",
            custom_fields={"aBcDeF1234567890xyzQ": "value"},
        )
        fields = ghl().contact_payload(contact)["customFields"]
        assert fields[0]["id"] == "aBcDeF1234567890xyzQ"
        assert "key" not in fields[0]

    def test_lead_score_is_added_when_not_already_mapped(self):
        fields = ghl().contact_payload(CONTACT)["customFields"]
        assert any(f.get("key") == "lead_score" and f["field_value"] == "80"
                   for f in fields)

    def test_a_missing_location_id_is_a_configuration_error_not_a_crash(self):
        with pytest.raises(CrmConfigurationError) as caught:
            ghl(location_id="").contact_payload(CONTACT)
        assert "location_id" in str(caught.value)

    def test_a_missing_token_is_a_configuration_error(self):
        provider = GoHighLevelProvider(ctx(credentials={}, config={"location_id": "l"}))
        with pytest.raises(CrmConfigurationError):
            provider._headers()


class TestGoHighLevelRequests:
    async def test_the_version_header_is_always_sent(self, monkeypatch):
        """
        The single most common GHL integration failure: omitting `Version`
        produces errors that blame the payload instead of the header.
        """
        transport = FakeTransport((200, {"contact": {"id": "c1"}})).install(monkeypatch)
        await ghl().upsert_contact(CONTACT)

        assert transport.last()["headers"]["Version"] == "2021-07-28"
        assert transport.last()["headers"]["Authorization"].startswith("Bearer ")

    async def test_upsert_hits_the_upsert_endpoint(self, monkeypatch):
        transport = FakeTransport((200, {"contact": {"id": "c1"}})).install(monkeypatch)
        result = await ghl().upsert_contact(CONTACT)

        assert transport.last()["url"].endswith("/contacts/upsert")
        assert result.external_id == "c1"

    async def test_a_404_on_upsert_falls_back_to_search_then_create(
        self, monkeypatch
    ):
        """
        Community reports disagree about whether `/contacts/upsert` exists on
        every account and API version. A 404 must mean "use the other path",
        not "this tenant's syncs all fail forever for reasons nobody can
        reproduce".
        """
        transport = FakeTransport(
            (404, {"message": "not found"}),   # upsert missing
            (200, {"contacts": []}),           # search: nothing
            (200, {"contact": {"id": "c-new"}}),  # create
        ).install(monkeypatch)

        result = await ghl().upsert_contact(CONTACT)

        assert result.external_id == "c-new"
        assert result.already_existed is False
        assert transport.call_count == 3

    async def test_the_fallback_updates_when_the_contact_already_exists(
        self, monkeypatch
    ):
        transport = FakeTransport(
            (404, {}),
            (200, {"contacts": [{"id": "c-existing"}]}),
            (200, {"contact": {"id": "c-existing"}}),
        ).install(monkeypatch)

        result = await ghl().upsert_contact(CONTACT)

        assert result.external_id == "c-existing"
        assert result.already_existed is True
        assert transport.requests[-1]["method"] == "PUT"

    async def test_update_does_not_resend_location_id(self, monkeypatch):
        """GHL rejects locationId on update: it is fixed by the contact."""
        transport = FakeTransport((200, {"contact": {"id": "c1"}})).install(monkeypatch)
        await ghl().update_contact("c1", CONTACT)

        assert "locationId" not in transport.last()["json"]

    async def test_a_200_with_no_contact_id_is_treated_as_a_failure(
        self, monkeypatch
    ):
        FakeTransport((200, {"ok": True})).install(monkeypatch)
        with pytest.raises(CrmValidationError):
            await ghl().upsert_contact(CONTACT)

    async def test_appointment_needs_a_calendar(self, monkeypatch):
        appointment = NormalizedAppointment(
            title="Cleaning", starts_at=datetime(2026, 3, 2, 9),
            ends_at=datetime(2026, 3, 2, 9, 30), contact=CONTACT,
        )
        with pytest.raises(CrmConfigurationError):
            await ghl(calendar_id="").create_appointment(appointment)

    async def test_appointment_payload(self, monkeypatch):
        transport = FakeTransport((200, {"id": "evt-1"})).install(monkeypatch)
        appointment = NormalizedAppointment(
            title="Cleaning", starts_at=datetime(2026, 3, 2, 9),
            ends_at=datetime(2026, 3, 2, 9, 30), contact=CONTACT,
        )
        result = await ghl(calendar_id="cal-9").create_appointment(
            appointment, external_contact_id="c1"
        )

        body = transport.last()["json"]
        assert body["calendarId"] == "cal-9"
        assert body["contactId"] == "c1"
        assert body["startTime"] == "2026-03-02T09:00:00"
        assert result.external_id == "evt-1"

    async def test_tags_endpoint(self, monkeypatch):
        transport = FakeTransport((200, {})).install(monkeypatch)
        await ghl().add_tag("c1", ["urgent", "voxdesk"])

        assert transport.last()["url"].endswith("/contacts/c1/tags")
        assert transport.last()["json"] == {"tags": ["urgent", "voxdesk"]}

    async def test_empty_tags_makes_no_request(self, monkeypatch):
        transport = FakeTransport((200, {})).install(monkeypatch)
        await ghl().add_tag("c1", [])
        assert transport.call_count == 0

    @pytest.mark.parametrize("status,expected", [
        (401, CrmAuthError), (429, CrmRateLimited),
        (422, CrmValidationError), (500, CrmServerError),
    ])
    async def test_error_mapping(self, status, expected, monkeypatch):
        FakeTransport((status, {"message": "x"})).install(monkeypatch)
        with pytest.raises(expected):
            await ghl().upsert_contact(CONTACT)


# ================================================================ HubSpot ===

def hubspot():
    return HubSpotProvider(ctx(config={"base_url": "https://hubspot.test"}))


class TestHubSpotMapping:
    def test_property_names_are_hubspot_style(self):
        """
        `firstname`, not `first_name` or `firstName`. HubSpot accepts unknown
        properties on some plans and silently drops them, so getting this
        wrong produces contacts with no name and no error.
        """
        properties = hubspot().contact_properties(CONTACT)

        assert properties["firstname"] == "Jane"
        assert properties["lastname"] == "Doe"
        assert properties["phone"] == "+15551230000"
        assert properties["email"] == "jane@example.com"
        assert properties["company"] == "Doe Plumbing"
        assert "first_name" not in properties

    def test_custom_fields_can_override_defaults(self):
        contact = NormalizedContact(
            first_name="A", last_name="B",
            custom_fields={"hs_lead_status": "CONNECTED"},
        )
        assert hubspot().contact_properties(contact)["hs_lead_status"] == "CONNECTED"

    def test_hubspot_does_not_claim_tag_support(self):
        """
        HubSpot has no tags. Declaring the capability and quietly dropping the
        data would be worse than not having it.
        """
        assert not hubspot().supports(Capability.ADD_TAG)
        assert not hubspot().supports(Capability.CREATE_APPOINTMENT)


class TestHubSpotUpsert:
    async def test_with_an_email_it_uses_the_batch_upsert(self, monkeypatch):
        transport = FakeTransport(
            (200, {"results": [{"id": "hs-1", "new": True}]})
        ).install(monkeypatch)

        result = await hubspot().upsert_contact(CONTACT)

        assert transport.last()["url"].endswith("/contacts/batch/upsert")
        body = transport.last()["json"]["inputs"][0]
        assert body["idProperty"] == "email"
        assert body["id"] == "jane@example.com"
        assert result.external_id == "hs-1"
        assert result.already_existed is False

    async def test_without_an_email_it_searches_on_phone(self, monkeypatch):
        """
        **The common path for a voice product.**

        HubSpot deduplicates on email. A phone-only caller upserted by email
        would create a fresh contact on every single call.
        """
        transport = FakeTransport(
            (200, {"results": [{"id": "hs-existing"}]}),   # search hit
            (200, {"id": "hs-existing"}),                  # patch
        ).install(monkeypatch)

        phone_only = NormalizedContact(
            first_name="Jane", last_name="Doe", phone="+15551230000"
        )
        result = await hubspot().upsert_contact(phone_only)

        assert "batch/upsert" not in transport.requests[0]["url"]
        assert transport.requests[0]["url"].endswith("/contacts/search")
        filters = transport.requests[0]["json"]["filterGroups"][0]["filters"]
        assert filters == [
            {"propertyName": "phone", "operator": "EQ", "value": "+15551230000"}
        ]
        assert result.external_id == "hs-existing"
        assert result.already_existed is True

    async def test_a_non_unique_email_rejection_falls_back_to_search(
        self, monkeypatch
    ):
        """
        Documented HubSpot behaviour: portals where email is not a unique
        property answer the upsert with
        `400 VALIDATION_ERROR: Unable to perform update/upsert by non-unique
        property email`. That is the portal's configuration, not our payload,
        so it must not fail the sync.
        """
        transport = FakeTransport(
            (400, {"category": "VALIDATION_ERROR",
                   "message": "Unable to perform update/upsert by non-unique "
                              "0-1 property email"}),
            (200, {"results": []}),          # search: nothing
            (200, {"id": "hs-created"}),     # create
        ).install(monkeypatch)

        result = await hubspot().upsert_contact(CONTACT)

        assert result.external_id == "hs-created"
        assert transport.call_count == 3

    async def test_search_filters_are_not_anded_together(self, monkeypatch):
        """
        HubSpot ANDs filters within a group. Sending email AND phone would
        look for a contact matching both, which is almost never the row that
        exists.
        """
        transport = FakeTransport((200, {"results": []})).install(monkeypatch)
        await hubspot().get_contact(phone="+1555", email="a@b.com")

        filters = transport.last()["json"]["filterGroups"][0]["filters"]
        assert len(filters) == 1
        assert filters[0]["propertyName"] == "email"

    async def test_a_200_with_no_results_is_a_failure(self, monkeypatch):
        FakeTransport((200, {"results": []})).install(monkeypatch)
        with pytest.raises(CrmValidationError):
            await hubspot()._upsert_by_email(CONTACT)


class TestHubSpotNotes:
    async def test_note_uses_epoch_milliseconds_and_association_202(
        self, monkeypatch
    ):
        """
        `hs_timestamp` must be epoch ms -- an ISO string is accepted by some
        HubSpot endpoints and rejected by this one. And 202 is note->contact;
        201 is the inverse and produces an orphaned note.
        """
        transport = FakeTransport((200, {"id": "note-1"})).install(monkeypatch)
        await hubspot().create_note("hs-1", ACTIVITY)

        body = transport.last()["json"]
        assert isinstance(body["properties"]["hs_timestamp"], int)
        assert body["properties"]["hs_timestamp"] > 1_000_000_000_000
        association = body["associations"][0]["types"][0]
        assert association["associationTypeId"] == 202
        assert association["associationCategory"] == "HUBSPOT_DEFINED"

    async def test_note_bodies_are_html_escaped(self, monkeypatch):
        """
        Note bodies contain call summaries, which contain whatever the caller
        said. HubSpot renders notes as HTML.
        """
        transport = FakeTransport((200, {"id": "n"})).install(monkeypatch)
        nasty = NormalizedActivity(
            title="Call", body="<script>alert('x')</script> & more"
        )
        await hubspot().create_note("hs-1", nasty)

        html = transport.last()["json"]["properties"]["hs_note_body"]
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "&amp;" in html


# ================================================================= Jobber ===

def jobber():
    return JobberProvider(ctx(config={"base_url": "https://jobber.test/graphql"}))


def gql(payload):
    return (200, {"data": payload})


class TestJobberMapping:
    def test_emails_and_phones_are_typed_arrays_not_scalars(self):
        payload = jobber().client_input(CONTACT)

        assert payload["emails"] == [
            {"description": "MAIN", "primary": True, "address": "jane@example.com"}
        ]
        assert payload["phones"] == [
            {"description": "MAIN", "primary": True, "number": "+15551230000"}
        ]

    def test_last_name_is_always_present(self):
        """Jobber requires it, hence the `Caller` fallback."""
        first, last = NormalizedContact.split_name("Madonna")
        payload = jobber().client_input(
            NormalizedContact(first_name=first, last_name=last)
        )
        assert payload["lastName"] == "Caller"

    def test_the_version_header_is_sent(self):
        assert "X-JOBBER-GRAPHQL-VERSION" in jobber()._headers()

    def test_jobber_does_not_claim_appointment_support(self):
        """
        Jobber models scheduled work as jobs and visits, which need a
        property, line items and a schedule this layer cannot supply.
        Declaring the capability would produce permanent failures that look
        like a broken integration rather than an absent feature.
        """
        provider = jobber()
        assert not provider.supports(Capability.CREATE_APPOINTMENT)
        assert not provider.supports(Capability.CANCEL_APPOINTMENT)

    def test_the_removed_upsert_mutation_is_not_referenced(self):
        """
        `clientUpsert` was removed from Jobber's schema in 2023-08-18.
        Calling it would fail on every account.
        """
        import app.integrations.crm.providers.jobber as module

        # It may be *named* in a comment explaining the absence; it must not
        # appear inside a GraphQL document.
        for query in (
            module.MUTATION_CLIENT_CREATE, module.MUTATION_CLIENT_EDIT,
            module.MUTATION_CLIENT_NOTE, module.QUERY_CLIENT_SEARCH,
        ):
            assert "clientUpsert" not in query
            assert "clientNoteCreate" not in query   # also removed

    def test_the_surviving_note_mutation_name_is_used(self):
        import app.integrations.crm.providers.jobber as module

        assert "clientCreateNote" in module.MUTATION_CLIENT_NOTE


class TestJobberErrors:
    async def test_user_errors_at_http_200_are_a_failure_not_a_success(
        self, monkeypatch
    ):
        """
        **Jobber's defining trap.** A rejected mutation returns HTTP 200 with
        a populated `userErrors`. An adapter that only checks the status code
        reports every rejection as a success -- and the service layer would
        then write SYNCED with no external id.
        """
        FakeTransport(gql({
            "clientCreate": {
                "client": None,
                "userErrors": [{"message": "Last name is required",
                                "path": ["input", "lastName"]}],
            }
        })).install(monkeypatch)

        with pytest.raises(CrmValidationError) as caught:
            await jobber().create_contact(CONTACT)
        assert "Last name is required" in str(caught.value)

    async def test_user_errors_are_permanent_not_retried(self, monkeypatch):
        FakeTransport(gql({
            "clientCreate": {"client": None,
                             "userErrors": [{"message": "bad", "path": []}]}
        })).install(monkeypatch)

        with pytest.raises(CrmValidationError) as caught:
            await jobber().create_contact(CONTACT)
        assert caught.value.retryable is False

    async def test_throttling_arrives_as_a_graphql_error_and_is_retryable(
        self, monkeypatch
    ):
        """Jobber throttles on query cost, and says so at HTTP 200."""
        FakeTransport((200, {
            "errors": [{"message": "Throttled",
                        "extensions": {"code": "THROTTLED"}}]
        })).install(monkeypatch)

        with pytest.raises(CrmRateLimited) as caught:
            await jobber().create_contact(CONTACT)
        assert caught.value.retryable is True

    async def test_an_auth_error_at_200_is_permanent(self, monkeypatch):
        FakeTransport((200, {
            "errors": [{"message": "not authenticated",
                        "extensions": {"code": "UNAUTHENTICATED"}}]
        })).install(monkeypatch)

        with pytest.raises(CrmAuthError) as caught:
            await jobber().create_contact(CONTACT)
        assert caught.value.retryable is False

    async def test_a_schema_error_is_permanent(self, monkeypatch):
        FakeTransport((200, {
            "errors": [{"message": "Argument 'firstName' has an invalid value",
                        "extensions": {"code": "argumentLiteralsIncompatible"}}]
        })).install(monkeypatch)

        with pytest.raises(CrmValidationError):
            await jobber().create_contact(CONTACT)

    async def test_an_internal_server_error_code_is_retryable(self, monkeypatch):
        FakeTransport((200, {
            "errors": [{"message": "boom",
                        "extensions": {"code": "INTERNAL_SERVER_ERROR"}}]
        })).install(monkeypatch)

        with pytest.raises(CrmServerError) as caught:
            await jobber().create_contact(CONTACT)
        assert caught.value.retryable is True

    async def test_no_errors_and_no_id_is_still_a_failure(self, monkeypatch):
        FakeTransport(gql({
            "clientCreate": {"client": None, "userErrors": []}
        })).install(monkeypatch)

        with pytest.raises(CrmValidationError):
            await jobber().create_contact(CONTACT)

    async def test_upsert_queries_then_edits(self, monkeypatch):
        transport = FakeTransport(
            gql({"clients": {"nodes": [{"id": "jb-1"}]}}),
            gql({"clientEdit": {"client": {"id": "jb-1"}, "userErrors": []}}),
        ).install(monkeypatch)

        result = await jobber().upsert_contact(CONTACT)

        assert result.external_id == "jb-1"
        assert result.already_existed is True
        assert "VoxDeskClientSearch" in transport.requests[0]["json"]["query"]
        assert "clientEdit" in transport.requests[1]["json"]["query"]

    async def test_upsert_creates_when_the_search_finds_nothing(self, monkeypatch):
        FakeTransport(
            gql({"clients": {"nodes": []}}),
            gql({"clientCreate": {"client": {"id": "jb-new"}, "userErrors": []}}),
        ).install(monkeypatch)

        result = await jobber().upsert_contact(CONTACT)
        assert result.external_id == "jb-new"
        assert result.already_existed is False


# ================================================================ Webhook ===

def webhook(**config):
    settings = {"url": "https://hooks.test/in"}
    settings.update(config)
    return WebhookProvider(ctx(
        credentials={"signing_secret": "s" * 40}, config=settings
    ))


class TestWebhookSigning:
    def test_sign_and_verify_round_trip(self):
        secret, body, ts = "sekrit", '{"a":1}', 1_700_000_000
        signature = sign(secret, timestamp=ts, body=body)

        assert signature.startswith("sha256=")
        assert verify(secret, timestamp=ts, body=body, signature=signature, now=ts)

    def test_a_wrong_secret_fails(self):
        ts, body = 1_700_000_000, "{}"
        signature = sign("right", timestamp=ts, body=body)
        assert not verify("wrong", timestamp=ts, body=body, signature=signature, now=ts)

    def test_a_tampered_body_fails(self):
        ts = 1_700_000_000
        signature = sign("s", timestamp=ts, body='{"amount":10}')
        assert not verify(
            "s", timestamp=ts, body='{"amount":1000}', signature=signature, now=ts
        )

    def test_the_timestamp_is_inside_the_signature(self):
        """
        Signing only the body lets an attacker replay a captured request
        forever, because the receiver cannot tell an old valid request from a
        new one.
        """
        body = "{}"
        assert sign("s", timestamp=1, body=body) != sign("s", timestamp=2, body=body)

    def test_an_old_timestamp_is_rejected_even_with_a_valid_signature(self):
        old = 1_700_000_000
        signature = sign("s", timestamp=old, body="{}")

        assert verify("s", timestamp=old, body="{}", signature=signature, now=old)
        assert not verify(
            "s", timestamp=old, body="{}", signature=signature,
            now=old + 3600, tolerance_seconds=300,
        )

    def test_a_future_timestamp_beyond_tolerance_is_rejected(self):
        future = 1_700_000_000
        signature = sign("s", timestamp=future, body="{}")
        assert not verify(
            "s", timestamp=future, body="{}", signature=signature,
            now=future - 3600, tolerance_seconds=300,
        )

    def test_generated_secrets_are_unique_and_long(self):
        from app.integrations.crm.providers.webhook import generate_signing_secret

        secrets = {generate_signing_secret() for _ in range(50)}
        assert len(secrets) == 50
        assert all(len(s) >= 64 for s in secrets)


class TestWebhookDelivery:
    async def test_all_required_headers_are_sent(self, monkeypatch):
        transport = FakeTransport((200, {"id": "ok"})).install(monkeypatch)
        await webhook().deliver(
            "lead.created", {"lead_id": "1"}, idempotency_key="lead.created:1"
        )

        headers = transport.last()["headers"]
        assert headers[SIGNATURE_HEADER].startswith("sha256=")
        assert headers[TIMESTAMP_HEADER].isdigit()
        assert headers[IDEMPOTENCY_HEADER] == "lead.created:1"
        assert headers["X-VoxDesk-Event"] == "lead.created"
        assert headers["X-VoxDesk-Delivery"]

    async def test_the_signature_verifies_against_the_body_that_was_sent(
        self, monkeypatch
    ):
        """
        The signature must cover the exact bytes delivered. Signing a
        re-serialized copy is how signatures start failing for unicode
        payloads only.
        """
        import json

        transport = FakeTransport((200, {})).install(monkeypatch)
        await webhook().deliver(
            "call.completed", {"note": "café — naïve"}, idempotency_key="k"
        )

        request = transport.last()
        body = json.dumps(
            request["json"], separators=(",", ":"), sort_keys=True, default=str
        )
        assert verify(
            "s" * 40,
            timestamp=int(request["headers"][TIMESTAMP_HEADER]),
            body=body,
            signature=request["headers"][SIGNATURE_HEADER],
        )

    async def test_the_idempotency_key_is_stable_and_the_delivery_id_is_not(
        self, monkeypatch
    ):
        transport = FakeTransport((200, {})).install(monkeypatch)
        for _ in range(2):
            await webhook().deliver("lead.created", {}, idempotency_key="stable-key")

        first, second = transport.requests
        assert first["headers"][IDEMPOTENCY_HEADER] == second["headers"][IDEMPOTENCY_HEADER]
        assert first["headers"]["X-VoxDesk-Delivery"] != second["headers"]["X-VoxDesk-Delivery"]

    async def test_the_signing_secret_is_never_in_the_payload(self, monkeypatch):
        """
        Echoing the shared secret inside the payload it authenticates would be
        self-defeating.
        """
        import json

        transport = FakeTransport((200, {})).install(monkeypatch)
        await webhook().deliver("lead.created", {"x": 1}, idempotency_key="k")

        assert "s" * 40 not in json.dumps(transport.last()["json"])

    async def test_http_is_refused(self):
        with pytest.raises(CrmConfigurationError) as caught:
            await webhook(url="http://insecure.test/in").deliver(
                "lead.created", {}, idempotency_key="k"
            )
        assert "https" in str(caught.value)

    async def test_an_unsigned_integration_cannot_deliver(self):
        provider = WebhookProvider(ctx(
            credentials={}, config={"url": "https://hooks.test/in"}
        ))
        with pytest.raises(CrmConfigurationError):
            await provider.deliver("lead.created", {}, idempotency_key="k")

    async def test_a_receiver_reporting_ok_false_is_not_a_success(
        self, monkeypatch
    ):
        """
        Requirement 10 asks for response validation, and the standing rule is
        that we never claim a record synced unless the provider accepted it.
        A 200 with `{"ok": false}` is an explicit rejection.
        """
        FakeTransport((200, {"ok": False, "error": "queue full"})).install(monkeypatch)

        with pytest.raises(CrmValidationError):
            await webhook().deliver("lead.created", {}, idempotency_key="k")

    async def test_a_204_with_no_body_is_a_success(self, monkeypatch):
        FakeTransport((204, None)).install(monkeypatch)
        result = await webhook().deliver(
            "lead.created", {}, idempotency_key="the-key"
        )
        assert result.external_id == "the-key"

    async def test_the_envelope_is_versioned_and_carries_the_event_type(
        self, monkeypatch
    ):
        transport = FakeTransport((200, {})).install(monkeypatch)
        await webhook().deliver("appointment.booked", {"a": 1}, idempotency_key="k")

        body = transport.last()["json"]
        assert body["specversion"] == "1.0"
        assert body["source"] == "voxdesk"
        assert body["type"] == "appointment.booked"
        assert body["id"] == "k"
        assert body["data"] == {"a": 1}

    @pytest.mark.parametrize("status,expected", [
        (401, CrmAuthError), (429, CrmRateLimited),
        (400, CrmValidationError), (502, CrmServerError),
    ])
    async def test_error_mapping(self, status, expected, monkeypatch):
        FakeTransport((status, {"e": 1})).install(monkeypatch)
        with pytest.raises(expected):
            await webhook().deliver("lead.created", {}, idempotency_key="k")

    async def test_a_timeout_is_retryable(self, monkeypatch):
        FakeTransport(httpx.ReadTimeout("slow")).install(monkeypatch)
        from app.integrations.crm.errors import CrmTimeout

        with pytest.raises(CrmTimeout) as caught:
            await webhook().deliver("lead.created", {}, idempotency_key="k")
        assert caught.value.retryable is True