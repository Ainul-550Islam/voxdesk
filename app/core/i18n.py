"""
Multi-language support, wired end to end.

Until now `Tenant.language` was a column nobody read. A language choice has to
propagate to four places or the call breaks in a subtle way:

  1. Deepgram  -- the STT model must be told the language, and Nova-3 does not
                  cover every language, so some tenants must fall back to Nova-2.
  2. ElevenLabs -- Flash v2.5 is multilingual, but the older Turbo model is
                  English-only; picking wrong produces an English accent
                  reading Spanish words.
  3. The LLM   -- needs an explicit instruction, otherwise it drifts to English
                  the moment the caller code-switches.
  4. Number/time formatting -- "2:30 PM" must not be spoken as "P M" in French.

Everything here is a pure lookup so it is trivially testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Deepgram Nova-3 language coverage is narrower than Nova-2's.
NOVA3_LANGUAGES = {"en", "es", "fr", "de", "hi", "ru", "pt", "ja", "it", "nl"}

# ElevenLabs: only the multilingual/flash-v2.5 models handle non-English well.
MULTILINGUAL_TTS_MODEL = "eleven_flash_v2_5"
ENGLISH_TTS_MODEL = "eleven_flash_v2"


@dataclass(frozen=True)
class LanguageProfile:
    code: str                 # BCP-47, e.g. "es-MX"
    name: str                 # human label for the dashboard
    native_name: str
    stt_language: str         # what Deepgram wants
    rtl: bool = False
    meridiem: tuple[str, str] = ("A M", "P M")   # spoken, not written
    currency_word: str = "dollars"
    aliases: list[str] = field(default_factory=list)


LANGUAGES: dict[str, LanguageProfile] = {
    "en-US": LanguageProfile("en-US", "English (US)", "English", "en-US"),
    "en-GB": LanguageProfile("en-GB", "English (UK)", "English", "en-GB",
                             currency_word="pounds"),
    "en-AU": LanguageProfile("en-AU", "English (Australia)", "English", "en-AU"),
    "es-US": LanguageProfile("es-US", "Spanish (US)", "Espa\u00f1ol", "es",
                             meridiem=("de la ma\u00f1ana", "de la tarde")),
    "es-MX": LanguageProfile("es-MX", "Spanish (Mexico)", "Espa\u00f1ol", "es",
                             meridiem=("de la ma\u00f1ana", "de la tarde"),
                             currency_word="pesos"),
    "fr-FR": LanguageProfile("fr-FR", "French", "Fran\u00e7ais", "fr",
                             meridiem=("du matin", "de l'apr\u00e8s-midi"),
                             currency_word="euros"),
    "de-DE": LanguageProfile("de-DE", "German", "Deutsch", "de",
                             meridiem=("Uhr vormittags", "Uhr nachmittags"),
                             currency_word="euros"),
    "pt-BR": LanguageProfile("pt-BR", "Portuguese (Brazil)", "Portugu\u00eas", "pt-BR",
                             currency_word="reais"),
    "it-IT": LanguageProfile("it-IT", "Italian", "Italiano", "it",
                             currency_word="euros"),
    "nl-NL": LanguageProfile("nl-NL", "Dutch", "Nederlands", "nl",
                             currency_word="euros"),
    "hi-IN": LanguageProfile("hi-IN", "Hindi", "\u0939\u093f\u0928\u094d\u0926\u0940", "hi",
                             currency_word="rupees"),
    "ar-AE": LanguageProfile("ar-AE", "Arabic", "\u0627\u0644\u0639\u0631\u0628\u064a\u0629", "ar",
                             rtl=True, currency_word="dirhams"),
    "bn-BD": LanguageProfile("bn-BD", "Bengali", "\u09ac\u09be\u0982\u09b2\u09be", "bn",
                             currency_word="taka"),
}

DEFAULT_LANGUAGE = "en-US"


def get_profile(code: str | None) -> LanguageProfile:
    """Never raises: an unknown code degrades to US English rather than crashing a call."""
    if not code:
        return LANGUAGES[DEFAULT_LANGUAGE]
    if code in LANGUAGES:
        return LANGUAGES[code]
    base = code.split("-")[0].lower()
    for profile in LANGUAGES.values():
        if profile.code.split("-")[0].lower() == base:
            return profile
    return LANGUAGES[DEFAULT_LANGUAGE]


def is_english(code: str | None) -> bool:
    return get_profile(code).code.startswith("en")


# ------------------------------------------------------------ provider wiring ---

def deepgram_options(code: str | None, preferred_model: str = "nova-3") -> dict:
    """STT settings. Falls back to nova-2 for languages nova-3 does not cover."""
    profile = get_profile(code)
    base = profile.stt_language.split("-")[0].lower()
    model = preferred_model if base in NOVA3_LANGUAGES else "nova-2"
    return {
        "model": model,
        "language": profile.stt_language,
        "smart_format": True,
        "punctuate": True,
        # Filler words only exist in the English models.
        "filler_words": profile.code.startswith("en"),
    }


def elevenlabs_options(code: str | None, voice_id: str | None = None) -> dict:
    """TTS settings. Non-English must use the multilingual model."""
    profile = get_profile(code)
    return {
        "model": ENGLISH_TTS_MODEL if profile.code.startswith("en") else MULTILINGUAL_TTS_MODEL,
        "language_code": profile.code.split("-")[0],
        "voice_id": voice_id,
    }


def llm_language_instruction(code: str | None) -> str:
    """Appended to the system prompt. Empty for US English so we waste no tokens."""
    profile = get_profile(code)
    if profile.code == DEFAULT_LANGUAGE:
        return ""
    if profile.code.startswith("en"):
        region = {"en-GB": "British", "en-AU": "Australian"}.get(profile.code, "")
        return (
            f"\n\nLANGUAGE: Speak {region} English. Use {region} spelling, "
            f"date order (day before month) and local phrasing.\n"
        )
    return (
        f"\n\nLANGUAGE: Speak ONLY {profile.name} ({profile.native_name}). "
        f"Even if the caller uses an English word, keep replying in "
        f"{profile.native_name}. Never apologise for the language.\n"
    )


def supported_languages() -> list[dict]:
    """Dashboard dropdown data."""
    return [
        {
            "code": p.code,
            "label": p.name,
            "native": p.native_name,
            "rtl": p.rtl,
            "stt_model": deepgram_options(p.code)["model"],
        }
        for p in LANGUAGES.values()
    ]

