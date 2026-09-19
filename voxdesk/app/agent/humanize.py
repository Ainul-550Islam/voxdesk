"""মানুষের মতো শোনানোর লেয়ার।

রোবট আর মানুষের পার্থক্য প্রম্পটে না — এই ৫টা জিনিসে:

  1. TextNormalizer   "$1,250" -> "twelve fifty"   TTS সংখ্যা পড়তে পারে না
  2. FillerInjector   ক্যালেন্ডার চেক করার ২ সেকেন্ড চুপ না থেকে "let me check that"
  3. Backchannel      কাস্টমার লম্বা কথা বললে মাঝে "mm-hmm" — মানুষ এটাই করে
  4. Disfluency       মাঝেমধ্যে "so...", "okay, um" — নিখুঁত কথা রোবটিক শোনায়
  5. VariedGreeting   প্রতিবার একই greeting = রোবট ধরা পড়ে যায়
"""
from __future__ import annotations

import random
import re
from datetime import datetime

from app.core.logging import log

# pipecat ছাড়াও যেন টেক্সট ফাংশনগুলো import করা যায় (টেস্ট/স্ক্রিপ্টের জন্য)
try:
    from pipecat.frames.frames import (
        Frame,
        FunctionCallInProgressFrame,
        TextFrame,
        TTSSpeakFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:                                   # pragma: no cover
    PIPECAT_AVAILABLE = False
    Frame = FunctionCallInProgressFrame = TextFrame = TTSSpeakFrame = object
    UserStartedSpeakingFrame = UserStoppedSpeakingFrame = object
    FrameDirection = None

    class FrameProcessor:                              # noqa: D401 - stub
        """pipecat ইনস্টল না থাকলে ব্যবহৃত ডামি।"""

        def __init__(self, *args, **kwargs):
            pass

# ============================================================== 1. NUMBERS ==

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]


def num_to_words(n: int) -> str:
    """0-999999 -> ইংরেজি শব্দ। TTS-এর জন্য অপরিহার্য।"""
    if n < 0:
        return "minus " + num_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        rest = "" if n % 100 == 0 else " " + num_to_words(n % 100)
        return _ONES[n // 100] + " hundred" + rest
    if n < 1_000_000:
        rest = "" if n % 1000 == 0 else " " + num_to_words(n % 1000)
        return num_to_words(n // 1000) + " thousand" + rest
    return str(n)


def _money(match: re.Match) -> str:
    raw = match.group(1).replace(",", "")
    if "." in raw:
        dollars, cents = raw.split(".", 1)
        cents = (cents + "0")[:2]
        out = f"{num_to_words(int(dollars))} dollars"
        if int(cents):
            out += f" and {num_to_words(int(cents))} cents"
        return out
    return f"{num_to_words(int(raw))} dollars"


def _clock(match: re.Match) -> str:
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").upper().replace(".", "")
    spoken = num_to_words(hour if hour else 12)
    if minute == 0:
        spoken += " o'clock" if not meridiem else ""
    elif minute < 10:
        spoken += " oh " + num_to_words(minute)
    else:
        spoken += " " + num_to_words(minute)
    if meridiem:
        spoken += " " + ("A M" if meridiem.startswith("A") else "P M")
    return spoken


def _phone(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return " ".join(num_to_words(int(d)) for d in digits)


ABBREVIATIONS = {
    r"\bDr\.": "Doctor", r"\bMr\.": "Mister", r"\bMrs\.": "Missus",
    r"\bMs\.": "Miss", r"\bSt\.": "Street", r"\bAve\.": "Avenue",
    r"\bappt\b": "appointment", r"\bASAP\b": "as soon as possible",
    r"\be\.g\.": "for example", r"\bi\.e\.": "that is", r"\bvs\.": "versus",
    r"\bapprox\.": "approximately", r"&": " and ",
}


def normalize_for_speech(text: str) -> str:
    """LLM-এর লেখা টেক্সটকে 'বলার মতো' টেক্সটে বদলায়।"""
    if not text:
        return text

    # markdown ফেলে দাও — TTS "asterisk asterisk" বলে ফেলবে
    text = re.sub(r"[*_`#>|]+", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)   # লিংক
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", text)  # emoji

    for pattern, replacement in ABBREVIATIONS.items():
        text = re.sub(pattern, replacement, text)

    # ক্রম গুরুত্বপূর্ণ: টাকা -> ফোন -> ঘড়ি -> বাকি সংখ্যা
    text = re.sub(r"\$\s?([\d,]+(?:\.\d{1,2})?)", _money, text)
    text = re.sub(r"\+?\d[\d\-\.\s\(\)]{8,}\d", _phone, text)
    text = re.sub(r"\b(\d{1,2}):(\d{2})\s*([APap]\.?[Mm]\.?)?", _clock, text)
    text = re.sub(
        r"\b(\d{1,2})\s*([APap])\.?[Mm]\.?",
        lambda m: f"{num_to_words(int(m.group(1)))} "
                  f"{'A M' if m.group(2).upper() == 'A' else 'P M'}",
        text,
    )
    text = re.sub(r"(\d+)%", lambda m: f"{num_to_words(int(m.group(1)))} percent", text)
    text = re.sub(r"\b(\d{1,4})\b", lambda m: num_to_words(int(m.group(1))), text)

    text = re.sub(r"\s{2,}", " ", text).strip()

    # "A.M." এর ডট খেয়ে ফেললে বাক্যের শেষ বিরামচিহ্ন হারায় -> TTS-এর সুর নষ্ট হয়
    if text and text[-1] not in ".?!,":
        text += "."
    return text


class TextNormalizer(FrameProcessor):
    """LLM -> TTS এর মাঝে বসে। সব টেক্সট বলার উপযোগী করে দেয়।"""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TextFrame) and frame.text:
            frame.text = normalize_for_speech(frame.text)
        await self.push_frame(frame, direction)


# ============================================================== 2. FILLERS ==

THINKING_FILLERS = [
    "Let me check that for you.",
    "One second, I'm looking at the calendar.",
    "Okay, let me see.",
    "Sure, give me just a moment.",
    "Alright, checking now.",
]


class FillerInjector(FrameProcessor):
    """Tool call চলাকালীন চুপ না থেকে কিছু বলে।

    এইটাই সবচেয়ে বড় 'মানুষ মনে হওয়ার' ট্রিক। ক্যালেন্ডার API-তে ৮০০ms লাগে;
    ওই ৮০০ms চুপ থাকলে কাস্টমার ভাবে লাইন কেটে গেছে।
    """

    def __init__(self, min_gap_seconds: float = 8.0):
        super().__init__()
        self._last_filler_at = 0.0
        self._min_gap = min_gap_seconds

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, FunctionCallInProgressFrame):
            now = datetime.now().timestamp()
            if now - self._last_filler_at > self._min_gap:
                self._last_filler_at = now
                filler = random.choice(THINKING_FILLERS)
                log.info("humanize.filler", text=filler)
                await self.push_frame(TTSSpeakFrame(filler), FrameDirection.DOWNSTREAM)

        await self.push_frame(frame, direction)


# ========================================================= 3. BACKCHANNEL ==

BACKCHANNELS = ["Mm-hmm.", "Right.", "Okay.", "Got it.", "I see."]


class Backchannel(FrameProcessor):
    """কাস্টমার লম্বা কথা বললে মাঝে ছোট সাড়া দেয় — মানুষ ঠিক এটাই করে।

    সাবধান: বেশি করলে বিরক্তিকর। তাই ৩ সেকেন্ডের বেশি কথা + ১৫s কুলডাউন।
    """

    def __init__(self, after_seconds: float = 3.0, cooldown: float = 15.0):
        super().__init__()
        self._after = after_seconds
        self._cooldown = cooldown
        self._speech_started_at: float | None = None
        self._last_ack_at = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        now = datetime.now().timestamp()

        if isinstance(frame, UserStartedSpeakingFrame):
            self._speech_started_at = now

        elif isinstance(frame, UserStoppedSpeakingFrame) and self._speech_started_at:
            spoke_for = now - self._speech_started_at
            if spoke_for > self._after and now - self._last_ack_at > self._cooldown:
                self._last_ack_at = now
                ack = random.choice(BACKCHANNELS)
                log.info("humanize.backchannel", text=ack, after_s=round(spoke_for, 1))
                await self.push_frame(TTSSpeakFrame(ack), FrameDirection.DOWNSTREAM)
            self._speech_started_at = None

        await self.push_frame(frame, direction)


# ========================================================== 4. DISFLUENCY ==

SENTENCE_OPENERS = ["So, ", "Okay, ", "Alright, ", "Sure, ", "Well, ", ""]


def add_natural_opener(text: str, probability: float = 0.25) -> str:
    """মাঝেমধ্যে বাক্যের শুরুতে স্বাভাবিক শব্দ বসায়।

    নিখুঁত ব্যাকরণ = রোবট। সামান্য অগোছালো = মানুষ।
    ২৫% এর বেশি করবেন না, নাহলে ন্যাকা শোনাবে।
    """
    if not text or random.random() > probability:
        return text
    if text[:6].lower() in ("so, ", "okay,", "alrig", "sure,", "well,"):
        return text
    return random.choice(SENTENCE_OPENERS) + text[0].lower() + text[1:]


# ====================================================== 5. VARIED GREETING ==

def vary_greeting(base: str, business_name: str, agent_name: str) -> str:
    """প্রতিবার হুবহু একই greeting = রোবট ধরা পড়ে। সময় অনুযায়ী বদলায়।"""
    hour = datetime.now().hour
    part = "Good morning" if hour < 12 else ("Good afternoon" if hour < 17 else "Good evening")

    templates = [
        base,
        f"{part}, {business_name}, this is {agent_name}. How can I help?",
        f"Thanks for calling {business_name}. {agent_name} speaking, what can I do for you?",
        f"Hi, {business_name}, this is {agent_name}. What can I help you with today?",
    ]
    return random.choice(templates)