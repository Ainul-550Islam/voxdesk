"""
Credential storage and secret handling.

The single worst outcome in this whole step is a tenant's CRM token leaving
the system — in an API response, a log line, an audit row, or a database dump.
These tests are the ones that would have to fail before that could happen.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.integrations.crm import crypto
from app.integrations.crm.crypto import (
    CredentialCryptoError,
    CredentialDecryptionError,
    decrypt_credentials,
    encrypt_credentials,
    generate_key,
    parse_key_ring,
)

TENANT_A = str(uuid.uuid4())
TENANT_B = str(uuid.uuid4())
SECRET = {"access_token": "pit-super-secret-token-value-9876543210"}


@pytest.fixture
def ring():
    return parse_key_ring(f"k1:{generate_key()}")


# ============================================================ round trip ===

def test_credentials_survive_a_round_trip(ring):
    envelope, key_id = encrypt_credentials(
        SECRET, tenant_id=TENANT_A, provider="gohighlevel", key_ring=ring
    )
    recovered = decrypt_credentials(
        envelope, tenant_id=TENANT_A, provider="gohighlevel", key_ring=ring
    )
    assert recovered == SECRET
    assert key_id == "k1"


def test_the_plaintext_token_is_not_present_in_the_ciphertext(ring):
    """The obvious test, and the one that catches "encryption" that is base64."""
    envelope, _ = encrypt_credentials(
        SECRET, tenant_id=TENANT_A, provider="gohighlevel", key_ring=ring
    )
    assert SECRET["access_token"] not in envelope
    # ...and not merely base64-hidden either.
    import base64
    for part in envelope.split("."):
        try:
            decoded = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        except Exception:
            continue
        assert SECRET["access_token"].encode() not in decoded


def test_every_encryption_uses_a_fresh_nonce(ring):
    """
    Identical plaintext must not produce identical ciphertext.

    Nonce reuse in GCM is catastrophic — it leaks the XOR of two plaintexts
    and breaks the authentication key. This is the test that catches a
    "deterministic for easier testing" change.
    """
    envelopes = {
        encrypt_credentials(
            SECRET, tenant_id=TENANT_A, provider="gohighlevel", key_ring=ring
        )[0]
        for _ in range(20)
    }
    assert len(envelopes) == 20


def test_the_envelope_records_which_key_wrote_it(ring):
    envelope, key_id = encrypt_credentials(
        SECRET, tenant_id=TENANT_A, provider="hubspot", key_ring=ring
    )
    assert envelope.startswith(f"v1.{key_id}.")


# ============================================================== tampering ===

def test_a_tampered_ciphertext_fails_to_decrypt(ring):
    """
    AES-GCM is authenticated, so this must raise rather than returning
    corrupted data that then gets sent to a provider.
    """
    envelope, _ = encrypt_credentials(
        SECRET, tenant_id=TENANT_A, provider="hubspot", key_ring=ring
    )
    prefix, key_id, nonce, ciphertext = envelope.split(".")
    flipped = ciphertext[:-4] + ("AAAA" if not ciphertext.endswith("AAAA") else "BBBB")

    with pytest.raises(CredentialDecryptionError):
        decrypt_credentials(
            f"{prefix}.{key_id}.{nonce}.{flipped}",
            tenant_id=TENANT_A, provider="hubspot", key_ring=ring,
        )


def test_a_malformed_envelope_is_rejected(ring):
    for bad in ("", "not-an-envelope", "v1.k1.onlythree", "v2.k1.a.b"):
        with pytest.raises(CredentialDecryptionError):
            decrypt_credentials(
                bad, tenant_id=TENANT_A, provider="hubspot", key_ring=ring
            )


def test_a_ciphertext_from_a_retired_key_reports_why(ring):
    envelope, _ = encrypt_credentials(
        SECRET, tenant_id=TENANT_A, provider="hubspot", key_ring=ring
    )
    other = parse_key_ring(f"k2:{generate_key()}")

    with pytest.raises(CredentialDecryptionError) as caught:
        decrypt_credentials(
            envelope, tenant_id=TENANT_A, provider="hubspot", key_ring=other
        )
    assert "k1" in str(caught.value)


# ================================================= AAD / tenant binding ===

def test_ciphertext_copied_to_another_tenant_will_not_decrypt(ring):
    """
    **The isolation guarantee reaching down to the cipher.**

    An attacker with database write access copies Tenant A's
    `credentials_encrypted` into Tenant B's row, hoping B's integration will
    then use A's token. The tenant id is in the AES-GCM associated data, so
    the copy fails authentication instead.
    """
    envelope, _ = encrypt_credentials(
        SECRET, tenant_id=TENANT_A, provider="gohighlevel", key_ring=ring
    )
    with pytest.raises(CredentialDecryptionError):
        decrypt_credentials(
            envelope, tenant_id=TENANT_B, provider="gohighlevel", key_ring=ring
        )


def test_ciphertext_moved_to_another_provider_will_not_decrypt(ring):
    """
    Same defence, other axis: a HubSpot token pasted into the same tenant's
    GoHighLevel row would otherwise be sent to the wrong vendor.
    """
    envelope, _ = encrypt_credentials(
        SECRET, tenant_id=TENANT_A, provider="hubspot", key_ring=ring
    )
    with pytest.raises(CredentialDecryptionError):
        decrypt_credentials(
            envelope, tenant_id=TENANT_A, provider="gohighlevel", key_ring=ring
        )


# ================================================================ key ring ===

def test_key_rotation_keeps_old_ciphertext_readable():
    """
    Rotation must not be a flag day. Old envelopes stay readable while new
    ones are written with the new key.
    """
    old_key, new_key = generate_key(), generate_key()

    old_ring = parse_key_ring(f"y2025:{old_key}")
    envelope, _ = encrypt_credentials(
        SECRET, tenant_id=TENANT_A, provider="jobber", key_ring=old_ring
    )

    rotated = parse_key_ring(f"y2026:{new_key},y2025:{old_key}")
    assert rotated.active_id == "y2026"
    assert decrypt_credentials(
        envelope, tenant_id=TENANT_A, provider="jobber", key_ring=rotated
    ) == SECRET

    fresh, key_id = encrypt_credentials(
        SECRET, tenant_id=TENANT_A, provider="jobber", key_ring=rotated
    )
    assert key_id == "y2026"


def test_a_bare_key_with_no_id_is_accepted():
    ring = parse_key_ring(generate_key())
    assert ring.active_id == "default"


@pytest.mark.parametrize("bad", [
    "",
    "   ",
    "k1:not-valid-base64!!!",
    "k1:" + "c2hvcnQ=",           # valid base64, but only 5 bytes
    "bad id:" + generate_key(),   # space in the key id
])
def test_invalid_key_configuration_is_rejected_loudly(bad):
    with pytest.raises(CredentialCryptoError):
        parse_key_ring(bad)


def test_duplicate_key_ids_are_rejected():
    key = generate_key()
    with pytest.raises(CredentialCryptoError):
        parse_key_ring(f"k1:{key},k1:{generate_key()}")


def test_a_key_ring_never_prints_key_material():
    """A KeyRing in a traceback or a REPL must not reveal the keys."""
    ring = parse_key_ring(f"k1:{generate_key()},k2:{generate_key()}")
    printed = repr(ring)

    assert "k1" in printed and "k2" in printed
    for material in ring.keys.values():
        assert material.hex() not in printed
        assert str(material) not in printed


def test_generated_keys_are_the_right_size_and_random():
    keys = {generate_key() for _ in range(50)}
    assert len(keys) == 50
    for key in keys:
        ring = parse_key_ring(f"k:{key}")
        assert len(ring.keys["k"]) == 32


# ========================================================= configuration ===

def test_production_refuses_to_boot_without_an_encryption_key(monkeypatch):
    """
    Requirement 4: fail safely if encryption configuration is invalid.
    Storing a provider token with no key would mean plaintext in the database.
    """
    from app.core.config import Settings

    settings = Settings(
        app_env="production", crm_encryption_keys="",
        jwt_secret="x" * 40, secret_key="a-real-secret",
        public_base_url="https://api.example.com", twilio_auth_token="t",
        knowledge_embedding_provider="openai",
    )
    problems = settings.validate_security()
    assert any("CRM_ENCRYPTION_KEYS" in p for p in problems), problems


def test_a_malformed_encryption_key_is_a_boot_failure(monkeypatch):
    from app.core.config import Settings

    settings = Settings(
        app_env="development", crm_encryption_keys="k1:!!!not-base64!!!"
    )
    problems = settings.validate_security()
    assert any("CRM_ENCRYPTION_KEYS is invalid" in p for p in problems), problems


def test_development_without_a_key_is_allowed():
    """
    A developer with no key should still be able to run the app; they simply
    cannot connect an integration. Requiring one to boot locally is friction
    that gets worked around by disabling the check.
    """
    from app.core.config import Settings

    settings = Settings(app_env="development", crm_encryption_keys="")
    assert not any("CRM_ENCRYPTION_KEYS" in p for p in settings.validate_security())


def test_key_ring_from_settings_returns_none_when_unconfigured(monkeypatch):
    from app.core.config import settings as live

    monkeypatch.setattr(live, "crm_encryption_keys", "", raising=False)
    assert crypto.key_ring_from_settings() is None


# ============================================== no secrets in serialization ===

async def test_the_stored_row_contains_ciphertext_not_the_token(db, tenant_a):
    """End to end: what actually lands in the database column."""
    from tests.conftest import make_integration

    integration = await make_integration(
        db, tenant_a, "hubspot", credentials={"access_token": "pat-na1-REALTOKEN-42"}
    )
    assert integration.credentials_encrypted
    assert "pat-na1-REALTOKEN-42" not in integration.credentials_encrypted
    assert integration.credentials_encrypted.startswith("v1.")
    assert integration.credentials_key_id


async def test_no_secret_survives_json_serialization_of_the_api_model(db, tenant_a):
    from app.api.integration_routes import to_out
    from tests.conftest import make_integration

    integration = await make_integration(
        db, tenant_a, "jobber", credentials={"access_token": "jobber-REALTOKEN-42"}
    )
    body = json.dumps(to_out(integration).model_dump())

    assert "jobber-REALTOKEN-42" not in body
    assert "credentials_encrypted" not in body
    assert integration.credentials_encrypted not in body
    # But it must still tell the tenant that they *are* connected.
    assert '"connected": true' in body.replace(", ", ", ").lower()


async def test_the_api_model_has_no_field_that_could_carry_a_secret():
    """
    Structural. A future field named `credentials` or `token` added to the
    response model would be caught here rather than in production.
    """
    from app.api.integration_routes import IntegrationOut

    forbidden = (
        "credential", "token", "secret", "password", "key", "auth",
    )
    for name in IntegrationOut.model_fields:
        assert not any(word in name.lower() for word in forbidden), (
            f"IntegrationOut.{name} is named like a secret; if it is not one, "
            f"rename it, and if it is, remove it"
        )