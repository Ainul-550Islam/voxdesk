"""Analytics core (Phase 4 slice 1): conversation metrics + usage forecasting.

Pure, deterministic, stdlib-only computation. No database, no network, no
model training — the numbers these modules produce are reproducible from
their inputs, which is what makes them auditable and cheap to test. The
SQLAlchemy/async adapters that feed these functions from the database belong
in ``app/services`` and are deliberately kept separate so this core carries
no infrastructure dependency.
"""

from app.analytics.conversation_metrics import (
    AggregateMetrics,
    ConversationMetrics,
    TurnRecord,
    aggregate,
    compute_conversation_metrics,
    sentiment_score,
)
from app.analytics.forecast import (
    ChurnRisk,
    Projection,
    UsagePoint,
    churn_risk,
    exponential_smoothing,
    linear_trend,
    moving_average,
    next_period_labels,
    project_usage,
)

__all__ = [
    "AggregateMetrics",
    "ChurnRisk",
    "ConversationMetrics",
    "Projection",
    "TurnRecord",
    "UsagePoint",
    "aggregate",
    "churn_risk",
    "compute_conversation_metrics",
    "exponential_smoothing",
    "linear_trend",
    "moving_average",
    "next_period_labels",
    "project_usage",
    "sentiment_score",
]
