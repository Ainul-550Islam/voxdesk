"""A2P 10DLC rules. Getting these wrong means silently filtered SMS."""
import pytest

from app.core.compliance import (
    check_message,
    count_segments,
    ensure_opt_out,
    is_sendable,
    registration_checklist,
    sample_messages,
)

# ------------------------------------------------------------- content ---

@pytest.mark.parametrize("body", [
    "Get your CBD oil today",
    "Fast payday loan approval",
    "Join our casino tonight",
    "Best vape deals",
])
def test_banned_content_is_blocked(body):
    assert is_sendable(body) is False


def test_normal_appointment_message_passes():
    assert is_sendable("Your appointment is confirmed for Tuesday at 2:30 PM.") is True


def test_url_shortener_is_blocked():
    issues = check_message("Book here: bit.ly/abc123")
    assert any("shortener" in i.message for i in issues)


def test_branded_domain_is_allowed():
    assert is_sendable("Book here: brightsmile.com/book") is True


def test_empty_message_is_an_error():
    assert is_sendable("   ") is False


def test_first_message_without_stop_is_a_warning_not_a_block():
    issues = check_message("Hello from us", is_first_of_thread=True)
    assert any(i.severity == "warning" for i in issues)
    assert is_sendable("Hello from us", is_first_of_thread=True) is True


def test_oversized_message_is_blocked():
    assert is_sendable("a" * 1601) is False


# ------------------------------------------------------------- segments ---

def test_short_gsm_message_is_one_segment():
    result = count_segments("Hello there")
    assert result["segments"] == 1 and result["encoding"] == "GSM-7"


def test_161_gsm_chars_becomes_two_segments():
    assert count_segments("a" * 161)["segments"] == 2


def test_emoji_switches_to_ucs2_and_shrinks_capacity():
    result = count_segments("Hello \U0001F600")
    assert result["encoding"] == "UCS-2" and result["per_segment"] == 70


def test_71_unicode_chars_becomes_two_segments():
    """Bengali is outside GSM-7, so capacity drops to 70/67 per segment."""
    assert count_segments("\u0995" * 71)["segments"] == 2


def test_accented_latin_stays_gsm7():
    """e-acute IS in the GSM-7 alphabet -- it must not force UCS-2 billing."""
    assert count_segments("\u00e9" * 71)["encoding"] == "GSM-7"


def test_empty_message_is_zero_segments():
    assert count_segments("")["segments"] == 0


# --------------------------------------------------------------- opt-out ---

def test_opt_out_footer_is_appended_when_missing():
    out = ensure_opt_out("Your appointment is confirmed.", business="Acme")
    assert "STOP" in out and out.startswith("Acme")


def test_opt_out_footer_is_not_duplicated():
    original = "Confirmed. Reply STOP to unsubscribe."
    assert ensure_opt_out(original) == original


# ---------------------------------------------------------- registration ---

def test_checklist_is_ordered_and_assigns_owners():
    steps = registration_checklist()
    assert [s["step"] for s in steps] == list(range(1, len(steps) + 1))
    assert {s["owner"] for s in steps} == {"client", "you", "carrier"}


def test_sample_messages_pass_our_own_validator():
    for msg in sample_messages("Bright Smile Dental"):
        assert is_sendable(msg, is_first_of_thread=True) is True
        assert "STOP" in msg

