"""Deterministic PII redaction for transcripts and exports (Phase 4, GDPR).

Data-minimisation and the right-of-access responses both need the same thing:
a way to scrub personal data out of free text *before* it leaves the system or
gets handed to a human. This module provides conservative, deterministic
redaction for the highest-risk identifiers found in call transcripts:

* email addresses,
* phone numbers (international-ish, US-style),
* credit-card numbers — validated with the **Luhn checksum**, so an innocent
  16-digit account ID that is not a valid card is left alone.

Redaction replaces matches with fixed tokens (``[EMAIL]``, ``[PHONE]``,
``[CARD]``) — never with a marker that leaks the original — and returns a
count, so callers can log *that* data was scrubbed without logging the data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: US-style phone numbers with an optional +country code; conservative about
#: not eating into surrounding digits.
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?\d{1,3}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)"
)

#: 13–19 digit runs, with optional spaces/dashes, that pass the Luhn check.
CARD_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[\s\-]?){12,18}\d(?!\d)")

TOKEN_EMAIL = "[EMAIL]"
TOKEN_PHONE = "[PHONE]"
TOKEN_CARD = "[CARD]"


def luhn_valid(digits: str) -> bool:
    """True when ``digits`` (digits only) passes the Luhn checksum."""
    if not digits or not digits.isdigit() or len(digits) < 2:
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _card_digits(match: re.Match[str]) -> str | None:
    digits = re.sub(r"[^\d]", "", match.group(0))
    if 13 <= len(digits) <= 19 and luhn_valid(digits):
        return digits
    return None


def redact_emails(text: str) -> tuple[str, int]:
    matches = EMAIL_RE.findall(text)
    return EMAIL_RE.sub(TOKEN_EMAIL, text), len(matches)


def redact_phones(text: str) -> tuple[str, int]:
    matches = PHONE_RE.findall(text)
    return PHONE_RE.sub(TOKEN_PHONE, text), len(matches)


def redact_cards(text: str) -> tuple[str, int]:
    matches = [m for m in CARD_CANDIDATE_RE.finditer(text) if _card_digits(m)]
    return CARD_CANDIDATE_RE.sub(_card_replacer, text), len(matches)


def _card_replacer(match: re.Match[str]) -> str:
    return TOKEN_CARD if _card_digits(match) else match.group(0)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    emails: int = 0
    phones: int = 0
    cards: int = 0

    @property
    def total(self) -> int:
        return self.emails + self.phones + self.cards


def redact(
    text: str,
    *,
    emails: bool = True,
    phones: bool = True,
    cards: bool = True,
) -> RedactionResult:
    """Scrub the enabled identifier kinds and report counts per kind."""
    working = text
    email_count = phone_count = card_count = 0
    if emails:
        working, email_count = redact_emails(working)
    if phones:
        working, phone_count = redact_phones(working)
    if cards:
        working, card_count = redact_cards(working)
    return RedactionResult(
        text=working,
        emails=email_count,
        phones=phone_count,
        cards=card_count,
    )
