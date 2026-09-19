"""SMS / WhatsApp channel logic: prefixes, opt-out keywords, reply shaping."""
import pytest

from app.agent.text_agent import SMS_SAFE_LENGTH, channel_rules, shape_for_channel
from app.channels.messaging import (
    HELP_WORDS,
    START_WORDS,
    STOP_WORDS,
    detect_channel,
    help_text,
    normalize_keyword,
    opt_out_confirmation,
    strip_channel_prefix,
)

# ------------------------------------------------------------- addressing ---

def test_strip_whatsapp_prefix():
    assert strip_channel_prefix("whatsapp:+15551234567") == "+15551234567"


def test_strip_leaves_plain_sms_alone():
    assert strip_channel_prefix("+15551234567") == "+15551234567"


@pytest.mark.parametrize("addr,expected", [
    ("whatsapp:+15551234567", "whatsapp"),
    ("+15551234567", "sms"),
])
def test_detect_channel(addr, expected):
    assert detect_channel(addr) == expected


# ---------------------------------------------------------------- keywords ---

@pytest.mark.parametrize("raw", ["STOP", " stop ", "Stop!", "STOPALL", "unsubscribe"])
def test_stop_variants_are_recognised(raw):
    assert normalize_keyword(raw) in STOP_WORDS


def test_start_and_help_are_recognised():
    assert normalize_keyword("START") in START_WORDS
    assert normalize_keyword("Help") in HELP_WORDS


def test_normal_sentence_is_not_a_keyword():
    kw = normalize_keyword("Can I book for tomorrow at 3?")
    assert kw not in STOP_WORDS and kw not in HELP_WORDS


def test_stop_inside_a_sentence_is_not_an_opt_out():
    """'please stop by at 3' must NOT unsubscribe the customer."""
    assert normalize_keyword("please stop by at 3") not in STOP_WORDS


def test_opt_out_confirmation_tells_them_how_to_return():
    text = opt_out_confirmation("Bright Smile Dental")
    assert "Bright Smile Dental" in text and "START" in text


def test_help_text_includes_stop_instruction():
    assert "STOP" in help_text("Acme")


# ----------------------------------------------------------- reply shaping ---

def test_long_sms_is_trimmed_at_a_sentence_boundary():
    long_text = ("We are open nine to five. " * 30).strip()
    out = shape_for_channel(long_text, "sms")
    assert len(out) <= SMS_SAFE_LENGTH
    assert out.endswith(".")


def test_short_sms_is_untouched():
    assert shape_for_channel("Booked for 2 PM.", "sms") == "Booked for 2 PM."


def test_whatsapp_is_not_trimmed():
    long_text = "a" * 1000
    assert len(shape_for_channel(long_text, "whatsapp")) == 1000


def test_sms_rules_forbid_markdown_and_cap_length():
    rules = channel_rules("sms", "Acme")
    assert "300 characters" in rules and "No markdown" in rules


def test_whatsapp_rules_allow_bold_but_not_tables():
    rules = channel_rules("whatsapp", "Acme")
    assert "*bold*" in rules and "No headers, no tables" in rules