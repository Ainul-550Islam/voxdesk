"""
Password hashing and policy.

bcrypt only -- no custom cryptography. `bcrypt.checkpw` is constant-time, and
`hashpw` generates a per-password salt, so identical passwords do not produce
identical hashes.
"""
from __future__ import annotations

import re
import unicodedata

import bcrypt

# 12 rounds is the common 2026 default: ~250ms on commodity hardware, which is
# slow enough to matter for offline cracking and fast enough for a login form.
BCRYPT_ROUNDS = 12

# bcrypt silently truncates at 72 bytes. Rejecting longer input is safer than
# letting a user believe a 200-character passphrase is fully used.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 12

# A hash of a throwaway value, used to burn the same CPU time on a login
# attempt for an address that does not exist. Without this, response timing
# reveals which emails are registered.
_DUMMY_HASH = bcrypt.hashpw(b"voxdesk-timing-equalizer", bcrypt.gensalt(rounds=BCRYPT_ROUNDS))

_COMMON = {
    # Original 12 (Step 2).
    "password", "password1", "passw0rd", "12345678", "123456789", "qwertyuiop",
    "letmein", "welcome", "iloveyou", "admin123", "changeme", "voxdesk",
    # STEP 9: expanded from public breach-corpus head entries that still pass the
    # 12-character / 3-of-4-classes gate above, so they would otherwise be
    # accepted. Kept to a short, reviewable set — this is a tripwire against the
    # most-sprayed credentials, not a substitute for a breached-password API.
    "password123", "password1234", "password12345", "password123456",
    "qwerty123", "qwerty12345", "qwertyuiop123", "1234567890",
    "123456789012", "12345678910", "123456789a", "abcdefghijkl",
    "qazwsxedcrfv", "1qaz2wsx3edc", "1q2w3e4r5t6y", "asdfghjkl123",
    "zxcvbnm12345", "admin12345", "admin1234", "administrator", "administrator1",
    "letmein123", "letmein1234", "welcome123", "welcome1", "welcome12345",
    "iloveyou123", "iloveyou1", "iloveyou1234", "monkey123", "dragon123",
    "sunshine123", "princess123", "football123", "baseball123", "superman123",
    "batman123", "trustno1", "trustno1123", "master123", "shadow123",
    "password!", "password1!", "password123!", "passw0rd!", "p@ssw0rd",
    "p@ssword", "p@ssword1", "p@ssword123", "changeme123", "changeme1",
    "abc123456789", "qwerty1234", "computer123", "internet123", "whatever123",
}


class PasswordPolicyError(ValueError):
    """Raised when a candidate password fails policy. Message is user-safe."""


def normalize(password: str) -> str:
    """
    NFKC so a password typed on a different keyboard layout still matches.
    Deliberately does NOT strip whitespace: a leading space is a real
    character the user chose.
    """
    return unicodedata.normalize("NFKC", password)


def validate_policy(password: str, *, email: str = "") -> None:
    """Raise PasswordPolicyError with an actionable message, or return None."""
    password = normalize(password)

    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes."
        )
    if password.lower() in _COMMON:
        raise PasswordPolicyError("That password is too common.")
    if email:
        local = email.split("@")[0].lower()
        if local and len(local) >= 3 and local in password.lower():
            raise PasswordPolicyError("Password must not contain your email address.")

    classes = sum(bool(rx.search(password)) for rx in (
        re.compile(r"[a-z]"), re.compile(r"[A-Z]"),
        re.compile(r"\d"), re.compile(r"[^\w\s]"),
    ))
    if classes < 3:
        raise PasswordPolicyError(
            "Password must mix at least three of: lowercase, uppercase, "
            "digits, symbols."
        )


def hash_password(password: str) -> str:
    """Validate nothing here -- callers run `validate_policy` explicitly."""
    encoded = normalize(password).encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes."
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    """
    Constant-time comparison via bcrypt. Never raises: a malformed or missing
    hash is a failed login, not a 500.
    """
    if not password_hash:
        verify_dummy()
        return False
    try:
        return bcrypt.checkpw(
            normalize(password).encode("utf-8")[:MAX_PASSWORD_BYTES],
            password_hash.encode("ascii"),
        )
    except (ValueError, TypeError):
        return False


def verify_dummy() -> None:
    """Spend a comparable amount of CPU when the account does not exist."""
    bcrypt.checkpw(b"voxdesk-timing-equalizer", _DUMMY_HASH)


def needs_rehash(password_hash: str) -> bool:
    """True when a stored hash used fewer rounds than the current setting."""
    try:
        cost = int(password_hash.split("$")[2])
    except (IndexError, ValueError):
        return True
    return cost < BCRYPT_ROUNDS