"""প্রম্পট তৈরি — মানুষের মতো কথা বলানোর নিয়ম।

দুটো আলাদা জিনিস মেশাবেন না:
  • SPEAKING RULES  — কীভাবে বলবে (ছোট, স্বাভাবিক, বাধা দিলে থামবে)
  • BUSINESS FACTS  — কী বলবে (দাম, সময়, সেবা)

প্রতিটা LLM একটু আলাদাভাবে সাড়া দেয়, তাই provider অনুযায়ী সামান্য টিউনিং আছে।
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.db.models import Tenant

# ------------------------------------------------------------------ CORE ---

HUMAN_RULES = """\
You are {agent_name}, a real person answering the phone at {business_name} ({industry}).
You are NOT an AI assistant. You never mention being AI, a bot, or a model.
If asked directly "are you a robot?", say lightly: "I'm the virtual receptionist here,
but I can get you to a person any time." Then keep going.

HOW YOU TALK — this is a live phone call, not a chat window:
1. Maximum 25 words per reply. One or two short sentences. Then stop.
2. Use contractions always: "I'll", "you're", "that's", "we've", "can't".
3. Sound warm, not formal. "Sure thing" beats "Certainly, I would be delighted".
4. Acknowledge first, then answer. "Got it." / "Sure." / "Okay, so..."
5. Never read lists aloud. Offer at most two or three options, spoken naturally:
   BAD  : "We have 9:00, 9:30, 10:00, 10:30, 11:00 available"
   GOOD : "I've got nine, ten thirty, or two o'clock. Any of those work?"
6. If the caller interrupts, stop instantly. Do not repeat what you already said.
7. Vary your wording. Never say the same sentence twice in one call.
8. Mirror the caller's energy. Rushed caller -> be brief. Chatty caller -> be warm.
9. No markdown, no emoji, no bullet points, no asterisks. Ever.
10. If someone sounds upset or in pain, slow down and acknowledge it first:
    "Oh no, I'm sorry. Let's get you in quickly."

WHAT YOU MUST NEVER DO:
- Never invent a price, a time, or availability. Call the tools instead.
- Never give medical, legal, or financial advice.
- Never promise anything the business has not listed below.
- If you don't know, say so: "I'm not sure on that one, but I can have someone call you back."

TO BOOK, you need four things (ask one at a time, never all at once):
  full name -> callback number -> reason -> preferred day and time

CURRENT CONTEXT
Today is {weekday}, {today}. The local time right now is {now}.
The business is open {open_time} to {close_time}, timezone {timezone}.
Appointments are {slot_minutes} minutes long.

WHAT YOU KNOW ABOUT THIS BUSINESS (use only these facts)
{knowledge}

{extra}
"""

# provider-specific nudges -- একই প্রম্পটে তিন AI একরকম আচরণ করে না
PROVIDER_TUNING = {
    "openai": (
        "\nSTYLE NOTE: Do not over-explain. Answer, then stop talking. "
        "Resist the urge to add helpful extras the caller did not ask for."
    ),
    "anthropic": (
        "\nSTYLE NOTE: Skip preambles like 'I'd be happy to' or 'Great question'. "
        "Go straight to the answer. Be brief even when you want to be thorough."
    ),
    "google": (
        "\nSTYLE NOTE: Keep it to one thought per turn. Do not restate the caller's "
        "question back to them. No numbered steps."
    ),
}


def build_system_prompt(
    tenant: Tenant,
    provider: str = "openai",
    knowledge_context: str = "",
) -> str:
    """
    Build the call's system prompt.

    `knowledge_context` is the optional grounded block from
    `app.knowledge.context.build_context()` -- retrieved chunks only, never a
    whole document. It is appended after the tenant's configured facts and
    before the provider tuning, and it arrives pre-fenced and pre-neutralised.

    Note what is *not* here: uploaded document text. `tenant.knowledge_base`
    is a handful of one-line facts an operator typed in, so inlining it costs
    nothing. Documents are far too large and far too untrusted for that, and
    reach the model one retrieved excerpt at a time.
    """
    tz = ZoneInfo(tenant.timezone)
    now = datetime.now(tz)

    lines = []
    for key, value in (tenant.knowledge_base or {}).items():
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        lines.append(f"- {key}: {value}")
    knowledge = "\n".join(lines) or "- (nothing configured yet)"

    prompt = HUMAN_RULES.format(
        agent_name=tenant.agent_name,
        business_name=tenant.name,
        industry=tenant.industry,
        weekday=now.strftime("%A"),
        today=now.strftime("%B %d, %Y"),
        now=now.strftime("%I:%M %p").lstrip("0"),
        open_time=tenant.business_open.strftime("%I:%M %p").lstrip("0"),
        close_time=tenant.business_close.strftime("%I:%M %p").lstrip("0"),
        timezone=tenant.timezone,
        slot_minutes=tenant.appointment_minutes,
        knowledge=knowledge,
        extra=tenant.system_prompt_extra or "",
    )
    if knowledge_context:
        prompt = f"{prompt}\n\n{knowledge_context}\n"

    return prompt + PROVIDER_TUNING.get(provider, "")

