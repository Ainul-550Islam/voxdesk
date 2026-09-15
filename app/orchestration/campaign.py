"""Campaign orchestration: compliance-gated dispatch + state machine (Phase 4).

Mirrors the vocabulary of ``app/domain/campaign_models.py`` — the same
compliance gate (which may only ever get *stricter*), the same throttle
bounds, the same eligibility reasons and intent shape — and adds the missing
**dispatch engine**: the pure, deterministic decision of which leads are due
and which are skipped, and why.

The engine never dials anything. It produces ``Intent`` records (the domain
layer's "execution intent") plus aggregate-safe counters; the existing
outbound machinery is the only thing that may act on them. A skipped intent
still carries its idempotency key so a re-plan never double-queues a lead.

``now`` is always a caller-supplied ``datetime`` already localised to the
campaign's timezone, so window and day-of-week checks stay deterministic.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, time as _time
from typing import Any

# ------------------------------------------------------------- states -------

DRAFT = "draft"
SCHEDULED = "scheduled"
RUNNING = "running"
PAUSED = "paused"
COMPLETED = "completed"
CANCELLED = "cancelled"

CAMPAIGN_STATES = frozenset({DRAFT, SCHEDULED, RUNNING, PAUSED, COMPLETED, CANCELLED})

#: The documented lifecycle: draft -> scheduled -> running -> (paused <->
#: running) -> completed/cancelled.
CAMPAIGN_TRANSITIONS: dict[str, frozenset[str]] = {
    DRAFT: frozenset({SCHEDULED, CANCELLED}),
    SCHEDULED: frozenset({RUNNING, CANCELLED}),
    RUNNING: frozenset({PAUSED, COMPLETED, CANCELLED}),
    PAUSED: frozenset({RUNNING, CANCELLED}),
    COMPLETED: frozenset(),
    CANCELLED: frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    return target in CAMPAIGN_TRANSITIONS.get(current, frozenset())


# ------------------------------------------------------------ channels ------

CHANNEL_VOICE = "voice"
CHANNEL_SMS = "sms"
CHANNEL_WHATSAPP = "whatsapp"

CHANNELS = frozenset({CHANNEL_VOICE, CHANNEL_SMS, CHANNEL_WHATSAPP})


def _stable_id(*parts: object) -> str:
    payload = "|".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class CampaignError(ValueError):
    """Raised when a campaign fails validation or a dispatch is impossible."""


@dataclass(frozen=True)
class ComplianceGate:
    """The safety requirements a campaign must honour. Defaults are strict,
    and the legal controls (DNC, call window) may never be disabled."""

    require_dnc_check: bool = True
    require_call_window: bool = True
    require_attempt_limit: bool = True
    require_daily_limit: bool = True
    require_a2p_registration: bool = False  # only for SMS/WhatsApp

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.require_dnc_check:
            problems.append("require_dnc_check may not be disabled")
        if not self.require_call_window:
            problems.append("require_call_window may not be disabled")
        return problems


@dataclass(frozen=True)
class Throttle:
    calls_per_minute: int = 2
    daily_limit: int = 200
    max_attempts_per_lead: int = 3

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not 1 <= self.calls_per_minute <= 60:
            problems.append("calls_per_minute must be between 1 and 60")
        if not 1 <= self.daily_limit <= 100_000:
            problems.append("daily_limit must be between 1 and 100000")
        if not 1 <= self.max_attempts_per_lead <= 10:
            problems.append("max_attempts_per_lead must be between 1 and 10")
        return problems


@dataclass(frozen=True)
class Schedule:
    daily_start: _time = _time(9, 0)
    daily_end: _time = _time(20, 0)
    days_of_week: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)  # 0 = Monday
    timezone: str = "UTC"

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.daily_start >= self.daily_end:
            problems.append("daily_start must be earlier than daily_end")
        bad_days = sorted(set(self.days_of_week) - set(range(7)))
        if bad_days:
            problems.append(f"days_of_week contains invalid days: {bad_days}")
        if not self.days_of_week:
            problems.append("days_of_week must not be empty")
        return problems


@dataclass(frozen=True)
class Campaign:
    id: str
    tenant_id: str
    name: str
    channel: str = CHANNEL_VOICE
    schedule: Schedule = field(default_factory=Schedule)
    throttle: Throttle = field(default_factory=Throttle)
    compliance: ComplianceGate = field(default_factory=ComplianceGate)
    state: str = DRAFT

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.tenant_id or not self.tenant_id.strip():
            problems.append("campaign requires tenant_id")
        if not self.id or not self.id.strip():
            problems.append("campaign requires id")
        if not self.name.strip() or len(self.name) > 200:
            problems.append("campaign name must be 1-200 characters")
        if self.channel not in CHANNELS:
            problems.append(f"unknown channel {self.channel!r}")
        if self.state not in CAMPAIGN_STATES:
            problems.append(f"unknown campaign state {self.state!r}")
        problems += self.schedule.validate()
        problems += self.throttle.validate()
        problems += self.compliance.validate()
        if self.channel is not CHANNEL_VOICE and not self.compliance.require_a2p_registration:
            problems.append("non-voice campaigns must require A2P registration")
        return problems

    def is_valid(self) -> bool:
        return not self.validate()

    def identity(self) -> str:
        return _stable_id(self.tenant_id, self.name, self.channel)


# ------------------------------------------------------- window helpers -----

def is_allowed_day(now: datetime, days_of_week: tuple[int, ...]) -> bool:
    """True when ``now.weekday()`` (0 = Monday) is an allowed day."""
    return now.weekday() in days_of_week


def is_within_window(now: datetime, schedule: Schedule) -> bool:
    """True when the local time is within the daily window on an allowed day."""
    if not is_allowed_day(now, schedule.days_of_week):
        return False
    return schedule.daily_start <= now.time() <= schedule.daily_end


# ------------------------------------------------------------ dispatch ------

REASON_DNC = "dnc"
REASON_WINDOW = "outside_call_window"
REASON_ATTEMPT_LIMIT = "attempt_limit"
REASON_DAILY_LIMIT = "daily_limit"


def intent_key(campaign_id: str, lead_id: str) -> str:
    """One campaign + one lead ⇒ one key, so re-planning never double-queues."""
    return _stable_id(campaign_id, lead_id)


@dataclass(frozen=True)
class Intent:
    """A single, safe unit of work the outbound layer MAY pick up later.
    Creating one is not dialing — it only records eligibility."""

    campaign_id: str
    tenant_id: str
    lead_id: str
    channel: str
    idempotency_key: str = ""
    reason_skipped: str = ""

    @property
    def skipped(self) -> bool:
        return bool(self.reason_skipped)


@dataclass
class DayState:
    """The mutable, caller-owned counters a dispatch day needs. The service
    layer persists these; the engine only reads and advances them."""

    daily_calls_today: int = 0
    attempts: dict[str, int] = field(default_factory=dict)

    def attempts_for(self, lead_id: str) -> int:
        return self.attempts.get(lead_id, 0)

    def consume(self, lead_id: str) -> int:
        self.daily_calls_today += 1
        self.attempts[lead_id] = self.attempts_for(lead_id) + 1
        return self.attempts[lead_id]


@dataclass(frozen=True)
class CampaignMetrics:
    """Aggregate-safe counters. No phone numbers, no PII."""

    total_leads: int = 0
    attempted: int = 0
    dnc_skipped: int = 0
    window_skipped: int = 0
    attempt_limit_skipped: int = 0
    daily_limit_skipped: int = 0


def dispatch(
    campaign: Campaign,
    leads: Sequence[dict[str, Any]],
    state: DayState,
    now: datetime,
) -> tuple[tuple[Intent, ...], CampaignMetrics]:
    """Decide, per lead, whether it is due for a call — and why not.

    Checks run in a fixed order (DNC, window, attempt limit, daily limit) so
    the reason recorded is the *first* thing that blocked the lead, and the
    counters always add up to ``total_leads``. Each decision is deterministic
    in ``(campaign, leads, state, now)``.
    """
    problems = campaign.validate()
    if problems:
        raise CampaignError("; ".join(problems))

    intents: list[Intent] = []
    metrics = CampaignMetrics(total_leads=len(leads))

    for lead in leads:
        lead_id = str(lead["id"])
        key = intent_key(campaign.id, lead_id)

        if campaign.compliance.require_dnc_check and lead.get("dnc"):
            metrics = replace(metrics, dnc_skipped=metrics.dnc_skipped + 1)
            intents.append(Intent(campaign.id, campaign.tenant_id, lead_id,
                                  campaign.channel, key, REASON_DNC))
            continue

        if campaign.compliance.require_call_window and not is_within_window(now, campaign.schedule):
            metrics = replace(metrics, window_skipped=metrics.window_skipped + 1)
            intents.append(Intent(campaign.id, campaign.tenant_id, lead_id,
                                  campaign.channel, key, REASON_WINDOW))
            continue

        if (campaign.compliance.require_attempt_limit
                and state.attempts_for(lead_id) >= campaign.throttle.max_attempts_per_lead):
            metrics = replace(metrics, attempt_limit_skipped=metrics.attempt_limit_skipped + 1)
            intents.append(Intent(campaign.id, campaign.tenant_id, lead_id,
                                  campaign.channel, key, REASON_ATTEMPT_LIMIT))
            continue

        if (campaign.compliance.require_daily_limit
                and state.daily_calls_today >= campaign.throttle.daily_limit):
            metrics = replace(metrics, daily_limit_skipped=metrics.daily_limit_skipped + 1)
            intents.append(Intent(campaign.id, campaign.tenant_id, lead_id,
                                  campaign.channel, key, REASON_DAILY_LIMIT))
            continue

        state.consume(lead_id)
        metrics = replace(metrics, attempted=metrics.attempted + 1)
        intents.append(Intent(campaign.id, campaign.tenant_id, lead_id,
                              campaign.channel, key, ""))

    return tuple(intents), metrics
