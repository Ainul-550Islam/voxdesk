"""Language wiring: a wrong model choice produces an English accent in Spanish."""
import pytest

from app.core.i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    deepgram_options,
    elevenlabs_options,
    get_profile,
    is_english,
    llm_language_instruction,
    supported_languages,
)


def test_known_language_resolves_exactly():
    assert get_profile("es-MX").code == "es-MX"


def test_region_falls_back_to_base_language():
    """es-AR is not in the table but must not become English."""
    assert get_profile("es-AR").code.startswith("es")


def test_unknown_language_degrades_to_english_not_crash():
    assert get_profile("xx-YY").code == DEFAULT_LANGUAGE


def test_none_language_degrades_to_default():
    assert get_profile(None).code == DEFAULT_LANGUAGE


@pytest.mark.parametrize("code,expected", [
    ("en-US", True), ("en-GB", True), ("es-MX", False), ("bn-BD", False),
])
def test_is_english(code, expected):
    assert is_english(code) is expected


# --------------------------------------------------------------- deepgram ---

def test_nova3_used_for_covered_language():
    assert deepgram_options("es-MX")["model"] == "nova-3"


def test_nova2_fallback_for_uncovered_language():
    """Bengali is not on nova-3, so it must silently fall back, not fail."""
    assert deepgram_options("bn-BD")["model"] == "nova-2"


def test_filler_words_only_enabled_for_english():
    assert deepgram_options("en-US")["filler_words"] is True
    assert deepgram_options("fr-FR")["filler_words"] is False


# ------------------------------------------------------------- elevenlabs ---

def test_english_uses_the_fast_english_model():
    assert elevenlabs_options("en-US")["model"] == "eleven_flash_v2"


def test_non_english_forces_multilingual_model():
    assert elevenlabs_options("de-DE")["model"] == "eleven_flash_v2_5"


def test_voice_id_is_passed_through():
    assert elevenlabs_options("en-US", "abc123")["voice_id"] == "abc123"


# ------------------------------------------------------------------ prompt ---

def test_default_language_adds_no_prompt_tokens():
    assert llm_language_instruction("en-US") == ""


def test_british_english_gets_a_region_note():
    note = llm_language_instruction("en-GB")
    assert "British" in note and "day before month" in note


def test_non_english_instruction_forbids_drifting_to_english():
    note = llm_language_instruction("es-MX")
    assert "ONLY" in note and "Espa\u00f1ol" in note


def test_rtl_flag_present_for_arabic():
    assert get_profile("ar-AE").rtl is True


def test_supported_languages_feeds_the_dropdown():
    rows = supported_languages()
    assert len(rows) == len(LANGUAGES)
    assert all({"code", "label", "native", "stt_model"} <= set(r) for r in rows)
