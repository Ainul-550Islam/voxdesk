"""
Application-level encryption for CRM credentials at rest.

Threat model, stated plainly, because "encrypted" is meaningless without one:

* **Defends against** a database dump. A stolen backup, a misconfigured
  read replica, a support engineer with SELECT, an ORM logger printing rows
  — none of those yield a usable provider token.
* **Does not defend against** an attacker who already runs the application
  process. They have the key, by construction. Nothing short of an HSM or a
  remote KMS changes that, and neither is in scope for this step.

The design is deliberately boring:

* **AES-256-GCM.** Authenticated, so a tampered ciphertext fails to decrypt
  rather than silently producing garbage that then gets sent to a provider.
* **A random 96-bit nonce per encryption.** Never derived, never reused.
* **Key ids in the envelope.** Every ciphertext records which key produced
  it, so a key can be rotated by adding the new one and leaving the old one
  readable, rather than by a synchronised flag day.
* **AAD binds the ciphertext to its owner.** The tenant id and provider are
  authenticated-but-not-encrypted, so a row copied from Tenant A's
  integration into Tenant B's will not decrypt. Moving ciphertext between
  rows is a real attack against naive column encryption; this closes it.

There is no home-grown cipher and no fallback that quietly stores plaintext.
If the key is not configured the application refuses to encrypt, and
`Settings.validate_security()` refuses to boot production.
"""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass

#: Envelope format: v1.<key_id>.<base64url nonce>.<base64url ciphertext>
#: Versioned so a future scheme can be introduced without guessing at the
#: shape of existing rows.
_ENVELOPE = re.compile(r"^v1\.(?P<key_id>[A-Za-z0-9_-]{1,64})\.(?P<nonce>[A-Za-z0-9_-]+)\.(?P<ct>[A-Za-z0-9_-]+)$")

_NONCE_BYTES = 12   # 96 bits, the value AES-GCM is specified for
_KEY_BYTES = 32     # AES-256


class CredentialCryptoError(Exception):
    """Encryption is misconfigured, or a ciphertext will not open."""


class CredentialDecryptionError(CredentialCryptoError):
    """
    The ciphertext did not authenticate.

    Distinct from a configuration error because the response differs: a
    configuration error is fixed by an operator, whereas this means the row
    was tampered with, was written by a key that is no longer loaded, or was
    copied from another tenant.
    """


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise CredentialCryptoError(
            "the 'cryptography' package is required to handle CRM credentials; "
            "install it (see requirements.txt)"
        ) from exc
    return AESGCM


@dataclass(frozen=True)
class KeyRing:
    """
    The keys this process can use.

    `active_id` is what new ciphertext is written with. Everything in `keys`
    can still be read, which is the whole point: rotation is "add a key, make
    it active, re-encrypt at leisure", not an outage.
    """

    keys: dict[str, bytes]
    active_id: str

    def __post_init__(self) -> None:
        if not self.keys:
            raise CredentialCryptoError("key ring is empty")
        if self.active_id not in self.keys:
            raise CredentialCryptoError(
                f"active key id {self.active_id!r} is not in the key ring"
            )
        for key_id, material in self.keys.items():
            if len(material) != _KEY_BYTES:
                raise CredentialCryptoError(
                    f"key {key_id!r} is {len(material)} bytes; AES-256 needs {_KEY_BYTES}"
                )

    def __repr__(self) -> str:
        # Never let key material reach a traceback, a log line, or a REPL.
        return f"KeyRing(active_id={self.active_id!r}, key_ids={sorted(self.keys)!r})"


def parse_key_ring(raw: str) -> KeyRing:
    """
    Build a key ring from the `CRM_ENCRYPTION_KEYS` environment variable.

    Format is `key_id:base64key` entries separated by commas; the first entry
    is the active key::

        CRM_ENCRYPTION_KEYS="2026a:<base64>,2025b:<base64>"

    Generate one with::

        python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"

    A single bare base64 key with no id is accepted and given the id
    ``default``, because requiring operators to invent an id before they can
    start is friction that encourages leaving encryption off entirely.
    """
    raw = (raw or "").strip()
    if not raw:
        raise CredentialCryptoError("CRM_ENCRYPTION_KEYS is not set")

    keys: dict[str, bytes] = {}
    order: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            key_id, _, material = entry.partition(":")
        else:
            key_id, material = "default", entry
        key_id = key_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key_id):
            raise CredentialCryptoError(f"invalid key id {key_id!r}")
        try:
            decoded = _b64d(material.strip())
        except Exception as exc:
            raise CredentialCryptoError(f"key {key_id!r} is not valid base64") from exc
        if key_id in keys:
            raise CredentialCryptoError(f"duplicate key id {key_id!r}")
        keys[key_id] = decoded
        order.append(key_id)

    if not order:
        raise CredentialCryptoError("CRM_ENCRYPTION_KEYS contained no keys")
    return KeyRing(keys=keys, active_id=order[0])


def generate_key() -> str:
    """A fresh base64 AES-256 key. Used by tests and by operator tooling."""
    return _b64e(os.urandom(_KEY_BYTES))


def _aad(tenant_id: str, provider: str) -> bytes:
    """
    Additional authenticated data.

    Binds the ciphertext to the row that owns it. Ciphertext lifted out of
    Tenant A's row and pasted into Tenant B's will fail authentication rather
    than handing B a working token — the isolation guarantee reaching all the
    way down to the cipher.
    """
    return f"voxdesk:crm:v1:{tenant_id}:{provider}".encode()


def encrypt_credentials(
    credentials: dict, *, tenant_id: str, provider: str, key_ring: KeyRing
) -> tuple[str, str]:
    """
    Encrypt a credential bundle. Returns ``(envelope, key_id)``.

    The key id is returned separately so it can be stored in its own column
    and queried — "which integrations still use the retired key" should be a
    SQL question, not a string-parsing exercise.
    """
    if not isinstance(credentials, dict):
        raise CredentialCryptoError("credentials must be a dict")

    AESGCM = _aesgcm()
    plaintext = json.dumps(credentials, separators=(",", ":"), sort_keys=True).encode()
    nonce = os.urandom(_NONCE_BYTES)
    key_id = key_ring.active_id
    ciphertext = AESGCM(key_ring.keys[key_id]).encrypt(
        nonce, plaintext, _aad(tenant_id, provider)
    )
    return f"v1.{key_id}.{_b64e(nonce)}.{_b64e(ciphertext)}", key_id


def decrypt_credentials(
    envelope: str, *, tenant_id: str, provider: str, key_ring: KeyRing
) -> dict:
    """Open an envelope. Raises `CredentialDecryptionError` if it will not."""
    match = _ENVELOPE.match(envelope or "")
    if not match:
        raise CredentialDecryptionError("credential envelope is malformed")

    key_id = match.group("key_id")
    key = key_ring.keys.get(key_id)
    if key is None:
        raise CredentialDecryptionError(
            f"no key with id {key_id!r} is loaded; it may have been retired "
            f"before the row was re-encrypted"
        )

    AESGCM = _aesgcm()
    try:
        plaintext = AESGCM(key).decrypt(
            _b64d(match.group("nonce")),
            _b64d(match.group("ct")),
            _aad(tenant_id, provider),
        )
    except Exception as exc:
        # Deliberately does not echo the underlying error: for AEAD the only
        # information it carries is "did not authenticate", and repeating the
        # library's message into a log adds nothing but noise.
        raise CredentialDecryptionError(
            "credentials failed to decrypt (wrong key, tampered row, or a row "
            "copied between tenants)"
        ) from exc

    try:
        data = json.loads(plaintext)
    except json.JSONDecodeError as exc:
        raise CredentialDecryptionError("decrypted credentials are not JSON") from exc
    if not isinstance(data, dict):
        raise CredentialDecryptionError("decrypted credentials are not an object")
    return data


def key_ring_from_settings():
    """
    The process key ring, or `None` when encryption is not configured.

    Returns `None` rather than raising so that a development machine with no
    key can still import the module and run every test that does not touch
    credentials. Production is protected at a different layer:
    `Settings.validate_security()` treats a missing key as a boot failure.
    """
    from app.core.config import settings

    if not settings.crm_encryption_keys:
        return None
    return parse_key_ring(settings.crm_encryption_keys)