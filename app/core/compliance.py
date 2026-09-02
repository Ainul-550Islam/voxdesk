"""
A2P 10DLC registration + messaging compliance.

In the US, an unregistered business sending SMS over a 10-digit long code gets
its messages filtered by the carriers -- silently. Clients blame you, not the
carrier. Every serious voice/SMS gig has to handle this, which is why the top
Fiverr agencies list "A2P 10DLC" as a selling point.

This module does two things:
  1. Drives Twilio Trust Hub registration (Brand -> Campaign -> number pool).
  2. Enforces the content rules that get campaigns rejected, *before* sending.

Note: registration is part paperwork, part API. The API half lives here; the
paperwork half (EIN, business address, opt-in screenshot) is the client's, and
`registration_checklist()` is what you send them so it is not your bottleneck.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

from app.core.config import settings

log = structlog.get_logger()

# Carriers reject campaigns whose sample messages omit these.
REQUIRED_OPT_OUT_PHRASE = "Reply STOP to unsubscribe"
REQUIRED_HELP_PHRASE = "Reply HELP for help"

# "SHAFT" content is banned outright on US 10DLC.
BANNED_PATTERNS = [
    (r"\b(cannabis|marijuana|weed|cbd|thc)\b", "cannabis content is prohibited on 10DLC"),
    (r"\b(loan|payday|debt\s+relief|credit\s+repair)\b", "high-risk financial content"),
    (r"\b(casino|betting|gambling|sportsbook)\b", "gambling content is prohibited"),
    (r"\b(firearm|handgun|ammo|ammunition)\b", "firearms content is prohibited"),
    (r"\b(vape|e-?cigarette|tobacco|nicotine)\b", "tobacco content is prohibited"),
]

# Public URL shorteners are a spam signal and get campaigns rejected.
SHORTENER_PATTERN = re.compile(
    r"\b(bit\.ly|tinyurl\.com|goo\.gl|t\.co|ow\.ly|is\.gd|buff\.ly)\b", re.I
)

SEGMENT_GSM = 160
SEGMENT_GSM_MULTI = 153
SEGMENT_UCS2 = 70
SEGMENT_UCS2_MULTI = 67

GSM_CHARS = set(
    "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
    "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u0398\u039e\u00c6\u00e6\u00df\u00c9"
    " !\"#\u00a4%&'()*+,-./0123456789:;<=>?"
    "\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
    "\u00bfabcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0"
    "^{}\\[~]|\u20ac"
)


@dataclass
class ComplianceIssue:
    severity: str      # "error" blocks sending, "warning" is advisory
    message: str


# --------------------------------------------------------------- validation ---

def check_message(body: str, *, is_first_of_thread: bool = False) -> list[ComplianceIssue]:
    """Run before any outbound marketing/notification SMS."""
    issues: list[ComplianceIssue] = []
    text = body or ""
    lowered = text.lower()

    for pattern, reason in BANNED_PATTERNS:
        if re.search(pattern, lowered):
            issues.append(ComplianceIssue("error", reason))

    if SHORTENER_PATTERN.search(text):
        issues.append(ComplianceIssue(
            "error",
            "public URL shorteners are blocked by carriers -- use a branded domain",
        ))

    if is_first_of_thread and "stop" not in lowered:
        issues.append(ComplianceIssue(
            "warning",
            f"first message of a thread should include '{REQUIRED_OPT_OUT_PHRASE}'",
        ))

    if len(text) > 1600:
        issues.append(ComplianceIssue("error", "message exceeds 1600 characters"))

    if not text.strip():
        issues.append(ComplianceIssue("error", "message is empty"))

    return issues


def is_sendable(body: str, **kw) -> bool:
    return not any(i.severity == "error" for i in check_message(body, **kw))


def count_segments(body: str) -> dict:
    """Billing surprises are a real support cost. Show the client the count."""
    text = body or ""
    unicode_needed = any(ch not in GSM_CHARS for ch in text)
    length = len(text)

    if unicode_needed:
        single, multi = SEGMENT_UCS2, SEGMENT_UCS2_MULTI
    else:
        single, multi = SEGMENT_GSM, SEGMENT_GSM_MULTI

    if length == 0:
        segments = 0
    elif length <= single:
        segments = 1
    else:
        segments = -(-length // multi)      # ceiling division

    return {
        "characters": length,
        "encoding": "UCS-2" if unicode_needed else "GSM-7",
        "segments": segments,
        "per_segment": single if segments <= 1 else multi,
    }


def ensure_opt_out(body: str, *, business: str = "") -> str:
    """Append the legally expected footer if it is not already there."""
    if "stop" in (body or "").lower():
        return body
    prefix = f"{business}: " if business and not body.startswith(business) else ""
    return f"{prefix}{body} {REQUIRED_OPT_OUT_PHRASE}."


# ------------------------------------------------------------- registration ---

def registration_checklist() -> list[dict]:
    """Send this to the client on day one. It is the longest pole in the tent."""
    return [
        {"step": 1, "item": "Legal business name exactly as registered",
         "owner": "client"},
        {"step": 2, "item": "EIN / Tax ID (or company number outside the US)",
         "owner": "client"},
        {"step": 3, "item": "Registered business address and website URL",
         "owner": "client"},
        {"step": 4, "item": "Authorised contact: name, email, phone, job title",
         "owner": "client"},
        {"step": 5, "item": "Screenshot of where customers opt in (web form, paper form)",
         "owner": "client"},
        {"step": 6, "item": "Two sample messages including STOP wording",
         "owner": "you"},
        {"step": 7, "item": "Submit Brand to Twilio Trust Hub",
         "owner": "you"},
        {"step": 8, "item": "Submit Campaign use-case and attach the phone number",
         "owner": "you"},
        {"step": 9, "item": "Wait for carrier vetting (typically days, not hours)",
         "owner": "carrier"},
    ]


def sample_messages(business: str) -> list[str]:
    """Carrier-safe samples that pass vetting. Reuse these in the submission."""
    return [
        f"{business}: Your appointment is confirmed for Tuesday at 2:30 PM. "
        f"Reply C to confirm or R to reschedule. Reply STOP to unsubscribe.",
        f"{business}: This is a reminder of your appointment tomorrow at 10:00 AM. "
        f"Reply HELP for help, STOP to unsubscribe.",
    ]


def _client():
    from twilio.rest import Client
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


async def register_brand(
    *,
    legal_name: str,
    ein: str,
    website: str,
    email: str,
    phone: str,
    street: str,
    city: str,
    region: str,
    postal_code: str,
    country: str = "US",
    dry_run: bool = False,
) -> dict:
    """
    Create the Trust Hub customer profile that a 10DLC brand hangs off.

    Twilio's Trust Hub API surface changes; this returns a structured result
    either way so the dashboard can show progress instead of a stack trace.
    """
    payload = {
        "legal_name": legal_name, "ein": ein, "website": website,
        "email": email, "phone": phone,
        "address": {
            "street": street, "city": city, "region": region,
            "postal_code": postal_code, "country": country,
        },
    }
    missing = [k for k, v in payload.items() if isinstance(v, str) and not v.strip()]
    if missing:
        return {"ok": False, "reason": "missing_fields", "fields": missing}

    if dry_run:
        return {"ok": True, "dry_run": True, "payload": payload}

    try:
        client = _client()
        profile = client.trusthub.v1.customer_profiles.create(
            friendly_name=legal_name,
            email=email,
            policy_sid="RNdfbf3fae0e1107f8aded0e7cead80bf5",   # Twilio's primary CP policy
        )
    except Exception as exc:
        log.error("a2p.brand_failed", error=str(exc))
        return {"ok": False, "reason": "twilio_error", "error": str(exc)}

    log.info("a2p.brand_submitted", sid=profile.sid)
    return {"ok": True, "customer_profile_sid": profile.sid, "status": "pending_review"}

