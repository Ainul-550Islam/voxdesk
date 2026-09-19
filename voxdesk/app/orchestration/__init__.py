"""Orchestration engine (Phase 4 slice 3): workflow + campaign execution.

Pure, deterministic, stdlib-only. The domain layer (``app/domain``) defines
the *shapes* — workflows, campaigns, compliance gates — and this package adds
the *engines* that run them: a graph executor for workflows and a
compliance-gated dispatch engine for campaigns. Both mirror the domain
vocabulary exactly (controlled-action allowlist, condition operators, campaign
states) without importing the database or async stack.
"""

from app.orchestration.campaign import (
    CHANNEL_SMS,
    CHANNEL_VOICE,
    CHANNEL_WHATSAPP,
    Campaign,
    CampaignMetrics,
    ComplianceGate,
    DayState,
    Intent,
    REASON_ATTEMPT_LIMIT,
    REASON_DAILY_LIMIT,
    REASON_DNC,
    REASON_WINDOW,
    Schedule,
    Throttle,
    can_transition,
    dispatch,
    intent_key,
    is_within_window,
)
from app.orchestration.conditions import CONDITION_OPERATORS, compare, evaluate
from app.orchestration.workflow import (
    CONTROLLED_ACTIONS,
    FORBIDDEN_ACTION_MARKERS,
    Action,
    Condition,
    Node,
    RunResult,
    Step,
    Workflow,
    WorkflowEngine,
)

__all__ = [
    "CONTROLLED_ACTIONS",
    "FORBIDDEN_ACTION_MARKERS",
    "Action",
    "CHANNEL_SMS",
    "CHANNEL_VOICE",
    "CHANNEL_WHATSAPP",
    "CONDITION_OPERATORS",
    "Campaign",
    "CampaignMetrics",
    "ComplianceGate",
    "Condition",
    "DayState",
    "Intent",
    "Node",
    "REASON_ATTEMPT_LIMIT",
    "REASON_DAILY_LIMIT",
    "REASON_DNC",
    "REASON_WINDOW",
    "RunResult",
    "Schedule",
    "Step",
    "Throttle",
    "Workflow",
    "WorkflowEngine",
    "can_transition",
    "compare",
    "dispatch",
    "evaluate",
    "intent_key",
    "is_within_window",
]
