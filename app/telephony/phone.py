"""
Phone number normalization and validation.

Before this module the codebase passed raw strings straight to Twilio:
`lead.phone`, `tenant.escalation_number`, `tenant.outbound_caller_id`. A
mistyped escalation number meant the transfer silently dialled nothing, and a
number stored as "(555) 010-1234" in one place and "+15550101234" in another
compared unequal.

Design rules, deliberately conservative:

* We normalize *formatting* only -- spaces, dashes, brackets, dots, a leading
  "00" international prefix, and a leading "+" that is already there.
* We NEVER guess a country code. "5550101234" is not silently turned into
  "+15550101234"; a US default would quietly dial the wrong continent for a
  UK client. Ambiguous input is rejected so the caller sees a clear error.
* `+` followed by 8-15 digits is the E.164 shape. Twilio also accepts a few
  non-E.164 endpoint forms (`client:`, `sip:`), which we pass through
  untouched rather than mangling.

`redact()` exists because full phone numbers are personal data and there is no
reason for them to sit in a log aggregator forever.
"""
from __future__ import annotations

import re

# Everything that is decoration rather than digits.
_PUNCTUATION = re.compile(r"[\s\-().\u00a0\u2010-\u2015]")
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")

# Endpoint schemes Twilio understands that are not phone numbers at all.
_PASSTHROUGH_SCHEMES = ("client:", "sip:")


class InvalidPhoneNumber(ValueError):
    """Raised when a destination cannot be used for dialling."""


def looks_like_endpoint(value: str) -> bool:
    """True for `client:alice` / `sip:...` style Twilio endpoints."""
    return value.strip().lower().startswith(_PASSTHROUGH_SCHEMES)


def normalize(value: str | None) -> str:
    """
    Return a dialable destination, or raise `InvalidPhoneNumber`.

    Accepts:
        "+1 (555) 010-1234"   -> "+15550101234"
        "0015550101234"       -> "+15550101234"   (00 = international prefix)
        "client:alice"        -> "client:alice"   (passed through)

    Rejects (rather than guessing):
        ""             -- nothing to dial
        "5550101234"   -- no country code; we will not assume one
        "+1555"        -- too short to be a real number
        "+1555abc0101" -- letters
    """
    if value is None:
        raise InvalidPhoneNumber("no destination configured")

    raw = value.strip()
    if not raw:
        raise InvalidPhoneNumber("no destination configured")

    if looks_like_endpoint(raw):
        return raw

    cleaned = _PUNCTUATION.sub("", raw)

    # "00" is the international access prefix in most of the world.
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]

    if not cleaned.startswith("+"):
        # Deliberately not guessed. See the module docstring.
        raise InvalidPhoneNumber(
            "number must be in international format starting with '+' "
            "(for example +15550101234)"
        )

    if not _E164.match(cleaned):
        raise InvalidPhoneNumber("not a valid E.164 phone number")

    return cleaned


def try_normalize(value: str | None) -> str | None:
    """Non-raising variant: returns None instead of raising."""
    try:
        return normalize(value)
    except InvalidPhoneNumber:
        return None


def is_valid(value: str | None) -> bool:
    return try_normalize(value) is not None


def same_number(a: str | None, b: str | None) -> bool:
    """Format-insensitive comparison. Two unparseable values are never equal."""
    na, nb = try_normalize(a), try_normalize(b)
    return na is not None and na == nb


def redact(value: str | None) -> str:
    """
    Log-safe rendering: keeps the country code and the last two digits.

        "+15550101234" -> "+1*******34"

    Enough to correlate a support ticket, not enough to be a phone directory.
    """
    if not value:
        return ""
    normalized = try_normalize(value) or value.strip()
    if looks_like_endpoint(normalized):
        return normalized.split(":", 1)[0] + ":***"
    if len(normalized) <= 5:
        return "*" * len(normalized)
    head = normalized[:2]
    tail = normalized[-2:]
    return f"{head}{'*' * (len(normalized) - 4)}{tail}"