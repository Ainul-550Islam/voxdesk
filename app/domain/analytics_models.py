"""Analytics domain objects (Batch 01 enterprise expansion).

These are *aggregate-safe* structures: they carry counts, rates, latencies and
money-shaped estimates — never a phone number, an email address, a full name
or a transcript. The ``PII_FIELDS`` allowlist and ``KpiSnapshot.assert_no_pii``
turn that rule into something a test can assert, so a future edit that slips a
raw customer field into a KPI fails the suite instead of shipping.

Money is represented honestly. ``CostKpi.estimated_cost_millicents`` is always
labelled an *estimate*: it is derived from configured unit prices and metered
usage, and it must never be presented as an invoiced amount — invoicing is
``app.billing``'s job, untouched here.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

#: Field names that would signal raw customer data leaking into an aggregate.
PII_FIELDS = frozenset({
    "phone", "phone_number", "from_number", "to_number", "email",
    "full_name", "customer_name", "name", "transcript", "recording_url",
    "address", "call_sid",
})


class KpiGranularity(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    CUSTOM = "custom"


class KpiKind(str, enum.Enum):
    CALL = "call"
    AGENT = "agent"
    PROVIDER = "provider"
    CAMPAIGN = "campaign"
    FUNNEL = "funnel"
    APPOINTMENT = "appointment"
    LEAD = "lead"
    COST = "cost"
    QUALITY = "quality"
    SLA = "sla"


def _pct(numerator: int | float, denominator: int | float) -> float:
    """0–100 percentage, 0.0 on a zero denominator. Mirrors the dashboard."""
    if not denominator:
        return 0.0
    return round(numerator / denominator * 100, 1)


@dataclass(frozen=True)
class CallKpi:
    total: int = 0
    answered: int = 0
    completed: int = 0
    failed: int = 0
    no_answer: int = 0
    transferred: int = 0
    escalated: int = 0
    avg_duration_seconds: float = 0.0

    def rates(self) -> dict[str, float]:
        return {
            "answer_rate": _pct(self.answered, self.total),
            "completion_rate": _pct(self.completed, self.total),
            "failure_rate": _pct(self.failed, self.total),
            "transfer_rate": _pct(self.transferred, self.answered),
            "escalation_rate": _pct(self.escalated, self.total),
        }


@dataclass(frozen=True)
class AgentKpi:
    agent_id: str = ""
    calls: int = 0
    bookings: int = 0
    escalations: int = 0
    avg_duration_seconds: float = 0.0
    csat: float | None = None

    def rates(self) -> dict[str, float]:
        return {
            "booking_rate": _pct(self.bookings, self.calls),
            "escalation_rate": _pct(self.escalations, self.calls),
        }


@dataclass(frozen=True)
class ProviderKpi:
    provider: str = ""
    calls: int = 0
    errors: int = 0
    avg_latency_ms: float | None = None

    def rates(self) -> dict[str, float]:
        return {"error_rate": _pct(self.errors, self.calls + self.errors)}


@dataclass(frozen=True)
class CampaignKpi:
    campaign_id: str = ""
    total: int = 0
    conversions: int = 0
    booked: int = 0
    estimated_cost_millicents: int = 0

    def rates(self) -> dict[str, float]:
        return {
            "conversion_rate": _pct(self.conversions, self.total),
            "booking_rate": _pct(self.booked, self.total),
        }


@dataclass(frozen=True)
class FunnelKpi:
    """Stage counts through the call funnel. Counts only, no identities."""

    total: int = 0
    answered: int = 0
    engaged: int = 0
    qualified: int = 0
    booked: int = 0

    def rates(self) -> dict[str, float]:
        return {
            "answer_rate": _pct(self.answered, self.total),
            "engagement_rate": _pct(self.engaged, self.answered),
            "qualification_rate": _pct(self.qualified, self.engaged),
            "booking_rate": _pct(self.booked, self.qualified),
        }


@dataclass(frozen=True)
class AppointmentKpi:
    total: int = 0
    confirmed: int = 0
    cancelled: int = 0
    no_show: int = 0

    def rates(self) -> dict[str, float]:
        return {
            "confirmation_rate": _pct(self.confirmed, self.total),
            "cancellation_rate": _pct(self.cancelled, self.total),
            "no_show_rate": _pct(self.no_show, self.total),
        }


@dataclass(frozen=True)
class LeadKpi:
    total: int = 0
    qualified: int = 0
    unqualified: int = 0
    dnc: int = 0
    converted: int = 0

    def rates(self) -> dict[str, float]:
        return {
            "qualification_rate": _pct(self.qualified, self.total),
            "conversion_rate": _pct(self.converted, self.total),
        }


@dataclass(frozen=True)
class CostKpi:
    """Usage × configured unit price. An estimate, never an invoice."""

    minutes: float = 0.0
    sms_segments: int = 0
    llm_tokens: int = 0
    estimated_cost_millicents: int = 0
    currency: str = "USD"


@dataclass(frozen=True)
class QualityKpi:
    avg_response_ms: float | None = None
    p95_response_ms: float | None = None
    positive_sentiment_rate: float = 0.0
    negative_sentiment_rate: float = 0.0
    resolved_rate: float = 0.0


@dataclass(frozen=True)
class SlaKpi:
    within_target: int = 0
    breached: int = 0
    target_seconds: int = 0

    def rates(self) -> dict[str, float]:
        return {"sla_attainment": _pct(self.within_target, self.within_target + self.breached)}


@dataclass(frozen=True)
class KpiPoint:
    """One kind of KPI for one period. ``metrics`` is aggregate-safe only."""

    kind: KpiKind
    period_start: str = ""
    period_end: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def rates(self) -> dict[str, float]:
        return self.metrics.get("rates", {})


@dataclass(frozen=True)
class KpiSnapshot:
    """A tenant-scoped collection of KPI points for a date range."""

    tenant_id: str
    range_start: str = ""
    range_end: str = ""
    granularity: KpiGranularity = KpiGranularity.DAILY
    points: tuple[KpiPoint, ...] = ()
    generated_at: str = ""

    def assert_no_pii(self) -> list[str]:
        """Return any PII-shaped field names found anywhere in the snapshot."""
        found: list[str] = []
        for point in self.points:
            for key in point.metrics:
                lowered = key.lower()
                if any(pi in lowered for pi in PII_FIELDS):
                    found.append(f"{point.kind.value}.{key}")
        return found

    def empty(self) -> bool:
        return not self.points

    @staticmethod
    def build(
        tenant_id: str,
        *,
        range_start: str = "",
        range_end: str = "",
        granularity: KpiGranularity = KpiGranularity.DAILY,
        points: tuple[KpiPoint, ...] = (),
    ) -> "KpiSnapshot":
        return KpiSnapshot(
            tenant_id=tenant_id,
            range_start=range_start,
            range_end=range_end,
            granularity=granularity,
            points=points,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
