"""মানুষের মতো শোনানোর লেয়ারের টেস্ট। কোনো API key লাগে না।"""
import pytest

from app.agent.humanize import (
    add_natural_opener,
    normalize_for_speech,
    num_to_words,
    vary_greeting,
)


@pytest.mark.parametrize("n,expected", [
    (0, "zero"), (7, "seven"), (13, "thirteen"), (30, "thirty"),
    (42, "forty two"), (100, "one hundred"), (150, "one hundred fifty"),
    (1250, "one thousand two hundred fifty"),
])
def test_num_to_words(n, expected):
    assert num_to_words(n) == expected


def test_money_is_spoken():
    out = normalize_for_speech("It costs $150.")
    assert "one hundred fifty dollars" in out
    assert "$" not in out


def test_money_with_cents():
    out = normalize_for_speech("Total $1,250.50")
    assert "one thousand two hundred fifty dollars and fifty cents" in out


def test_time_is_spoken():
    out = normalize_for_speech("See you at 2:30 PM")
    assert "two thirty P M" in out
    assert ":" not in out


def test_bare_meridiem_time():
    out = normalize_for_speech("We open at 9 AM")
    assert "nine A M" in out


def test_percent():
    assert "twenty percent" in normalize_for_speech("20% off")


def test_markdown_is_stripped():
    out = normalize_for_speech("**Great!** Here is a `list`:\n- one\n- two")
    for ch in "*`#-":
        assert ch not in out


def test_emoji_is_stripped():
    assert "\u2705" not in normalize_for_speech("Booked \u2705")


def test_abbreviations_expanded():
    out = normalize_for_speech("Dr. Smith on Main St.")
    assert "Doctor" in out and "Street" in out


def test_phone_number_digit_by_digit():
    out = normalize_for_speech("Call 555-123-4567")
    assert out.count("five") >= 4          # 555 + 5 in 4567
    assert "-" not in out


def test_always_ends_with_punctuation():
    assert normalize_for_speech("We open at 9 AM").endswith(".")


def test_empty_input_is_safe():
    assert normalize_for_speech("") == ""


def test_natural_opener_never_breaks_text():
    for _ in range(50):
        out = add_natural_opener("I can book that for you.", probability=1.0)
        assert "book that for you" in out


def test_greeting_varies():
    seen = {vary_greeting("Thanks for calling.", "Acme Dental", "Alex") for _ in range(60)}
    assert len(seen) > 1, "greeting প্রতিবার একই হলে রোবট ধরা পড়ে যাবে"


def test_greeting_contains_business_name():
    for _ in range(20):
        g = vary_greeting("Thanks for calling Acme Dental.", "Acme Dental", "Alex")
        assert "Acme Dental" in g
