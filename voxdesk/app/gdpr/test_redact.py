"""Unit tests for app.gdpr.redact.

Co-located with the package; run with: python -m pytest app/gdpr/ -q
"""

from __future__ import annotations

from app.gdpr.redact import (
    TOKEN_CARD,
    TOKEN_EMAIL,
    TOKEN_PHONE,
    luhn_valid,
    redact,
    redact_cards,
    redact_emails,
    redact_phones,
)


def test_luhn_valid():
    assert luhn_valid("4111111111111111")           # Visa test number
    assert luhn_valid("5555555555554444")           # Mastercard test number
    assert not luhn_valid("4111111111111112")       # one digit off
    assert not luhn_valid("")
    assert not luhn_valid("1234")


def test_redact_emails():
    text = "email me at alice@example.com or BOB@sub.example.co.uk thanks"
    redacted, count = redact_emails(text)
    assert count == 2
    assert "alice@example.com" not in redacted
    assert "BOB@sub.example.co.uk" not in redacted
    assert redacted.count(TOKEN_EMAIL) == 2


def test_redact_phones():
    text = "call +1 (555) 123-4567 or 555-123-4567 today"
    redacted, count = redact_phones(text)
    assert count == 2
    assert "555" not in redacted.replace(TOKEN_PHONE, "")
    assert redacted.count(TOKEN_PHONE) == 2


def test_redact_cards_only_luhn_valid():
    text = "card 4111 1111 1111 1111 and id 4111111111111112"
    redacted, count = redact_cards(text)
    assert count == 1
    assert TOKEN_CARD in redacted
    # The non-Luhn 16-digit id is left untouched.
    assert "4111111111111112" in redacted


def test_full_redact_counts_and_tokens():
    result = redact("reach me at alice@example.com or 555-123-4567, card 4111111111111111")
    assert result.emails == 1
    assert result.phones == 1
    assert result.cards == 1
    assert result.total == 3
    assert "alice@example.com" not in result.text
    assert TOKEN_PHONE in result.text
    assert TOKEN_CARD in result.text


def test_redact_disabled_kinds_are_untouched():
    result = redact("alice@example.com", emails=False)
    assert result.emails == 0
    assert "alice@example.com" in result.text


def test_redact_no_matches_unchanged():
    result = redact("nothing to see here")
    assert result.text == "nothing to see here"
    assert result.total == 0
