"""Phone number normalization. Ambiguous input must be rejected, not guessed."""
from __future__ import annotations

import pytest

from app.telephony import phone


@pytest.mark.parametrize("raw,expected", [
    ("+15550101234", "+15550101234"),
    ("+1 (555) 010-1234", "+15550101234"),
    ("+1.555.010.1234", "+15550101234"),
    ("  +15550101234  ", "+15550101234"),
    ("0015550101234", "+15550101234"),        # 00 international prefix
    ("+44 20 7946 0958", "+442079460958"),
    ("+8801712345678", "+8801712345678"),
    ("+1\u2013555\u20130101234"[:13] + "4", "+15550101234"),  # en-dash
])
def test_valid_numbers_normalize(raw, expected):
    assert phone.normalize(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "   ",
    "5550101234",        # no country code -- must NOT become +1...
    "555-0101",
    "+1555",             # too short
    "+1555abc0101",      # letters
    "+0155501012",       # E.164 cannot start with 0
    "not a number",
    "+1234567890123456", # too long
])
def test_invalid_numbers_are_rejected(raw):
    with pytest.raises(phone.InvalidPhoneNumber):
        phone.normalize(raw)


def test_local_number_is_never_silently_given_a_country_code():
    """Guessing +1 would dial the wrong continent for a non-US tenant."""
    assert phone.try_normalize("5550101234") is None


def test_twilio_endpoints_pass_through_untouched():
    assert phone.normalize("client:alice") == "client:alice"
    assert phone.normalize("sip:alice@example.com") == "sip:alice@example.com"


def test_is_valid_and_try_normalize():
    assert phone.is_valid("+15550101234") is True
    assert phone.is_valid("5550101234") is False
    assert phone.is_valid(None) is False
    assert phone.try_normalize("+1 555 010 1234") == "+15550101234"


def test_same_number_is_format_insensitive():
    assert phone.same_number("+1 (555) 010-1234", "+15550101234") is True
    assert phone.same_number("+15550101234", "+15550109999") is False
    assert phone.same_number("garbage", "garbage") is False
    assert phone.same_number(None, None) is False


def test_redact_keeps_correlation_but_not_the_number():
    redacted = phone.redact("+15550101234")
    assert redacted.startswith("+1")
    assert redacted.endswith("34")
    assert "5550101" not in redacted
    assert phone.redact(None) == ""
    assert phone.redact("client:alice") == "client:***"
