"""
Field mapping, custom-field validation, and the normalized domain.

Requirements 16 and 17. The theme: tenant-supplied configuration is turned
into an outbound provider payload, so it is validated at the boundary and
bounded at the point of use.
"""
from __future__ import annotations

import pytest

from app.integrations.crm.mapping import (
    ALLOWED_SOURCES,
    MAX_CUSTOM_FIELDS,
    MAX_KEY_LENGTH,
    MAX_VALUE_LENGTH,
    MappingError,
    call_tags,
    caller_number,
    contact_from_lead,
    resolve_custom_fields,
    validate_field_mappings,
)
from app.integrations.crm.models import (
    NormalizedContact,
    identity_hash,
    normalize_email,
    normalize_phone,
)


# =============================================== custom field validation ===

class TestMappingValidation:
    def test_a_valid_mapping_is_accepted(self):
        assert validate_field_mappings({
            "my_score": "lead_score", "my_summary": "call_summary",
        }) == {"my_score": "lead_score", "my_summary": "call_summary"}

    def test_an_empty_mapping_is_fine(self):
        assert validate_field_mappings(None) == {}
        assert validate_field_mappings({}) == {}

    def test_an_unknown_source_is_rejected(self):
        """
        An allowlist, not "any attribute of the payload". Otherwise a tenant
        could route an internal field into their CRM by guessing its name.
        """
        with pytest.raises(MappingError) as caught:
            validate_field_mappings({"x": "credentials_encrypted"})
        assert "not a mappable" in str(caught.value)

    def test_the_allowlist_covers_the_examples_the_brief_named(self):
        for named in (
            "lead_score", "call_summary", "call_outcome", "appointment_type",
            "source_campaign",
        ):
            assert named in ALLOWED_SOURCES

    def test_too_many_mappings_are_rejected(self):
        """
        Requirement 17: unbounded JSON must not become a provider payload.
        Unbounded means a tenant can post a huge body on our worker's time.
        """
        with pytest.raises(MappingError):
            validate_field_mappings(
                {f"f{i}": "lead_score" for i in range(MAX_CUSTOM_FIELDS + 1)}
            )

    def test_an_overlong_key_is_rejected(self):
        with pytest.raises(MappingError):
            validate_field_mappings({"k" * (MAX_KEY_LENGTH + 1): "lead_score"})

    def test_a_non_string_source_is_rejected(self):
        with pytest.raises(MappingError):
            validate_field_mappings({"k": {"nested": "object"}})
        with pytest.raises(MappingError):
            validate_field_mappings({"k": ["a"]})

    def test_an_empty_key_is_rejected(self):
        with pytest.raises(MappingError):
            validate_field_mappings({"": "lead_score"})
        with pytest.raises(MappingError):
            validate_field_mappings({"   ": "lead_score"})

    def test_a_non_dict_is_rejected(self):
        with pytest.raises(MappingError):
            validate_field_mappings(["lead_score"])


class TestMappingResolution:
    def test_values_are_pulled_from_the_named_sources(self):
        resolved = resolve_custom_fields(
            {"crm_score": "lead_score", "crm_summary": "call_summary"},
            {"lead_score": 80, "call_summary": "Booked Tuesday."},
        )
        assert resolved == {"crm_score": 80, "crm_summary": "Booked Tuesday."}

    def test_a_missing_source_is_skipped_not_sent_as_null(self):
        """
        A CRM field that silently becomes empty on every call without a
        summary is worse than one that is simply not written.
        """
        assert resolve_custom_fields(
            {"crm_summary": "call_summary"}, {"lead_score": 80}
        ) == {}

    def test_an_empty_value_is_skipped(self):
        assert resolve_custom_fields(
            {"a": "call_summary"}, {"call_summary": ""}
        ) == {}
        assert resolve_custom_fields(
            {"a": "call_summary"}, {"call_summary": None}
        ) == {}

    def test_booleans_become_provider_friendly_strings(self):
        assert resolve_custom_fields(
            {"was_booked": "booked"}, {"booked": True}
        ) == {"was_booked": "true"}
        assert resolve_custom_fields(
            {"was_booked": "booked"}, {"booked": False}
        ) == {"was_booked": "false"}

    def test_numbers_stay_numbers(self):
        assert resolve_custom_fields(
            {"s": "lead_score"}, {"lead_score": 80}
        ) == {"s": 80}

    def test_an_enormous_value_is_truncated_at_send_time(self):
        """
        Write-time validation bounds the *configuration*; this bounds the
        *data*. A summary long enough to matter is generated at runtime.
        """
        resolved = resolve_custom_fields(
            {"s": "call_summary"}, {"call_summary": "x" * 10_000}
        )
        assert len(resolved["s"]) == MAX_VALUE_LENGTH
        assert resolved["s"].endswith("…")

    def test_an_unmapped_source_is_never_sent(self):
        assert resolve_custom_fields({}, {"lead_score": 80, "secret": "x"}) == {}


# ================================================================ identity ===

class TestIdentity:
    @pytest.mark.parametrize("raw,expected", [
        ("+1 (555) 010-2000", "5550102000"),
        ("+15550102000", "5550102000"),
        ("555-010-2000", "5550102000"),
        ("15550102000", "5550102000"),
        ("", None),
        (None, None),
    ])
    def test_phone_normalization_makes_formats_comparable(self, raw, expected):
        """
        `(555) 010-2000` and `+15550102000` must not become two CRM contacts.
        """
        assert normalize_phone(raw) == expected

    def test_a_non_us_number_keeps_its_country_code(self):
        """
        Only a leading `1` on an 11-digit number is stripped. Stripping any
        leading digit would collide numbers from different countries.
        """
        assert normalize_phone("+44 20 7946 0958") == "442079460958"

    def test_email_normalization(self):
        assert normalize_email("  Jane@Example.COM ") == "jane@example.com"
        assert normalize_email("") is None

    def test_the_same_person_hashes_the_same_within_a_tenant(self):
        tenant = "tenant-1"
        assert identity_hash(
            tenant_id=tenant, phone="+1 (555) 010-2000", email=None
        ) == identity_hash(tenant_id=tenant, phone="+15550102000", email=None)

    def test_the_same_person_hashes_differently_across_tenants(self):
        """
        Requirement 19: never match contacts across tenants. Salting with the
        tenant id makes that true of the *value*, so even a query missing its
        `WHERE tenant_id` could not join two tenants' contacts.
        """
        assert identity_hash(
            tenant_id="tenant-a", phone="+15550102000", email=None
        ) != identity_hash(tenant_id="tenant-b", phone="+15550102000", email=None)

    def test_phone_is_preferred_over_email(self):
        """
        A voice product always has a phone and often has no email; preferring
        email would fragment the same caller across two links.
        """
        with_both = identity_hash(
            tenant_id="t", phone="+15550102000", email="jane@example.com"
        )
        phone_only = identity_hash(tenant_id="t", phone="+15550102000", email=None)
        assert with_both == phone_only

    def test_no_identity_at_all_hashes_to_none(self):
        assert identity_hash(tenant_id="t", phone=None, email=None) is None
        assert identity_hash(tenant_id="t", phone="", email="") is None

    def test_the_hash_is_not_the_raw_number(self):
        """The table should not be a searchable index of every phone number."""
        digest = identity_hash(tenant_id="t", phone="+15550102000", email=None)
        assert "5550102000" not in digest
        assert len(digest) == 64


# ============================================================ name splitting ===

class TestNameSplitting:
    @pytest.mark.parametrize("full,expected", [
        ("Jane Doe", ("Jane", "Doe")),
        ("Jane van der Berg", ("Jane", "van der Berg")),
        ("Madonna", ("Madonna", "Caller")),
        ("", ("Unknown", "Caller")),
        ("   ", ("Unknown", "Caller")),
    ])
    def test_split(self, full, expected):
        assert NormalizedContact.split_name(full) == expected

    def test_full_name_reassembles(self):
        contact = NormalizedContact(first_name="Jane", last_name="Doe")
        assert contact.full_name == "Jane Doe"


# ============================================================= call mapping ===

class TestCallMapping:
    def test_inbound_uses_the_caller_as_the_contact(self):
        assert caller_number("inbound", "+15551110000", "+15559990000") == "+15551110000"

    def test_outbound_uses_the_person_we_rang(self):
        """
        Getting this backwards files the business's own Twilio number as a
        lead. The pre-STEP-5 code got it right and the behaviour is preserved.
        """
        assert caller_number("outbound", "+15559990000", "+15551110000") == "+15551110000"

    def test_tags_come_from_a_fixed_vocabulary(self):
        class FakeCall:
            intent = "booking"
            booked = True
            escalated = False

        assert call_tags(FakeCall()) == ("voxdesk", "ai-call", "booking", "booked")

    def test_tags_are_deduplicated(self):
        """`intent="booked"` must not produce the tag twice."""
        class FakeCall:
            intent = "booked"
            booked = True
            escalated = False

        tags = call_tags(FakeCall())
        assert len(tags) == len(set(tags))

    def test_a_transferred_call_is_tagged(self):
        class FakeCall:
            intent = None
            booked = False
            escalated = True

        assert "transferred" in call_tags(FakeCall())


class TestLeadMapping:
    def test_contact_from_lead(self):
        class FakeLead:
            name = "Jane Doe"
            phone = "+15551230000"
            email = "jane@example.com"
            company = "Doe Plumbing"
            notes = "Burst pipe"
            score = 70

        contact = contact_from_lead(FakeLead(), tags=("lead",))
        assert contact.first_name == "Jane"
        assert contact.last_name == "Doe"
        assert contact.company == "Doe Plumbing"
        assert contact.lead_score == 70
        assert contact.tags == ("lead",)