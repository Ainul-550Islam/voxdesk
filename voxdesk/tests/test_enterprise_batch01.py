"""Batch 01 enterprise expansion — full test suite.

Covers the eight domain layers, the eight services, the three new API route
files, and the cross-cutting security guarantees. Deterministic and offline:
no external provider is ever contacted. Where a feature has no table yet, the
test asserts the *documented* behaviour (ephemeral overlay + tenant isolation)
rather than pretending durability.

The API routes are not registered in ``app/main.py`` (that file is outside the
allowed set for this batch), so the API tests build a small FastAPI app that
includes the three new routers and overrides the session dependency — exactly
the wiring ``app/main.py`` will perform once the integration dependency is
resolved.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.jwt import create_access_token
from app.core.errors import BadRequestError, NotFoundError
from app.db.models import CallStatus, LeadStatus, Speaker, UsageEvent, UsageEventType, UsageMetric
from app.db.session import get_session
from app.domain.agent_models import (
    AgentConfig, HandoffConfig, HandoffMode, LanguageConfig, ModelConfig,
    OperatingHours, SafetyPolicy, VoiceConfig,
    can_transition as agent_can_transition,
)
from app.domain.automation_models import (
    AutomationDefinition, AutomationSchedule, AutomationStatus, ExecutionPolicy,
    FilterRule, ScheduleKind, TriggerEvent, run_idempotency_key,
)
from app.domain.campaign_models import (
    Audience, CampaignChannel, CampaignDefinition, CampaignGoal, CampaignSchedule,
    CampaignState, ComplianceGate, Throttle,
)
from app.domain.conversation_models import (
    ConversationState, EscalationState, Sentiment,
    can_transition as conversation_can_transition,
)
from app.domain.inbox_models import (
    InboxChannel, MessageDirection, Thread, ThreadStatus, reopen_allowed,
)
from app.domain.notification_models import (
    DeliveryState, EventSource, NotificationChannel, NotificationTemplate,
    PreferenceSet, Recipient,
)
from app.domain.workflow_models import (
    CONDITION_OPERATORS, CONTROLLED_ACTIONS, Condition, ExecutionStatus, NodeType,
    WorkflowAction, WorkflowDefinition, WorkflowNode, evaluate_condition,
)
from app.services import (
    agent_service, analytics_service, automation_service, campaign_service,
    conversation_service, inbox_service, notification_service, workflow_service,
)
from tests.conftest import make_call, make_lead


# ============================================================ test fixtures ===

@pytest_asyncio.fixture
async def enterprise_app(sessionmaker_):
    """The three new routers wired into a test app with a real session override."""
    from app.api.agent_management_routes import router as agents_router
    from app.api.campaign_routes import router as campaigns_router
    from app.api.workflow_routes import router as workflows_router
    from app.core.errors import install_error_handling

    app = FastAPI()
    app.include_router(agents_router)
    app.include_router(workflows_router)
    app.include_router(campaigns_router)
    install_error_handling(app)

    async def _override():
        async with sessionmaker_() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def enterprise_client(enterprise_app):
    async with AsyncClient(
        transport=ASGITransport(app=enterprise_app), base_url="http://test"
    ) as ac:
        yield ac


def _auth(user) -> dict:
    token, _ = create_access_token(
        user_id=user.id, tenant_id=user.tenant_id,
        role=user.role.value, token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


def _agent(name="Alex", tenant_id="t"):
    return AgentConfig(
        tenant_id=tenant_id, name=name, greeting="Thanks for calling.",
        language=LanguageConfig(primary="en-US"),
        model=ModelConfig(provider="anthropic", model="claude-haiku-4-5"),
    )


def _terminal_workflow(tenant_id, name="wf"):
    return WorkflowDefinition(
        id=f"wf-{tenant_id}-{name}", tenant_id=tenant_id, name=name,
        entry_node="end", nodes=(WorkflowNode(id="end", type=NodeType.TERMINAL),),
    )


def _campaign(tenant_id, name="Spring outreach"):
    return CampaignDefinition(
        id=f"campaign-{tenant_id}-{name}", tenant_id=tenant_id, name=name,
        goal=CampaignGoal.QUALIFY, channel=CampaignChannel.VOICE,
        audience=Audience(tenant_id=tenant_id),
        schedule=CampaignSchedule(daily_start=time(9, 0), daily_end=time(20, 0)),
        throttle=Throttle(), compliance=ComplianceGate(), state=CampaignState.DRAFT,
    )


def _bounded_range(days_back: int = 30):
    """A now-anchored, 400-day-safe range that brackets ``make_call``'s clock."""
    now = datetime.utcnow()
    return now - timedelta(days=days_back), now + timedelta(days=1)


def _open_window(tenant) -> None:
    """Open the tenant's outbound window for the whole day (tests only)."""
    tenant.outbound_window_open = time(0, 0)
    tenant.outbound_window_close = time(23, 59, 59)


# ==================================================================== AGENTS ===

class TestAgentDomain:
    def test_validation_rejects_unknown_provider(self):
        config = AgentConfig(tenant_id="t", name="A", model=ModelConfig(provider="mystery"))
        assert any("provider" in p for p in config.validate())

    def test_validation_rejects_out_of_range_speed(self):
        config = AgentConfig(tenant_id="t", name="A", voice=VoiceConfig(speech_speed=9.0))
        assert any("speech_speed" in p for p in config.validate())

    def test_id_stable_across_content_edits(self):
        a = _agent("Alex")
        b = AgentConfig(tenant_id="t", name="Alex", greeting="Different greeting",
                        language=LanguageConfig(primary="en-US"),
                        model=ModelConfig(provider="anthropic"))
        assert a.id == b.id                       # identity is (tenant, name), not content

    def test_publish_transition_rules(self):
        from app.domain.agent_models import AgentStatus

        assert agent_can_transition(AgentStatus.DRAFT, AgentStatus.PUBLISHED)
        assert agent_can_transition(AgentStatus.PUBLISHED, AgentStatus.PUBLISHED)
        assert agent_can_transition(AgentStatus.PUBLISHED, AgentStatus.RETIRED)
        assert not agent_can_transition(AgentStatus.RETIRED, AgentStatus.PUBLISHED)


class TestAgentService:
    async def test_draft_tenant_isolation(self, tenant_a, tenant_b):
        config_a = _agent("Alex", str(tenant_a.id))
        agent_service.create_draft(tenant_a, config_a)
        assert agent_service.list_agents(tenant_a)
        assert agent_service.list_agents(tenant_b) == []

    async def test_publish_mints_immutable_version(self, db, tenant_a):
        config = _agent("Alex", str(tenant_a.id))
        agent_service.create_draft(tenant_a, config)
        v1 = await agent_service.publish_async(db, tenant_a, config)
        v2 = await agent_service.publish_async(db, tenant_a, config, changelog="bump")
        assert v1.version == 1 and v2.version == 2
        history = agent_service.version_history(tenant_a, config.id)
        assert history[0].version == 2
        assert history[0].config_hash == history[1].config_hash

    async def test_publish_persists_tenant_columns(self, db, tenant_a):
        config = AgentConfig(
            tenant_id=str(tenant_a.id), name="Rex", greeting="Hello there",
            language=LanguageConfig(primary="fr-FR"),
            model=ModelConfig(provider="google", model="gemini-2.0-flash", temperature=0.5),
            voice=VoiceConfig(voice_id="v1", speech_speed=1.1),
            operating_hours=OperatingHours(timezone="Europe/Paris"),
            handoff=HandoffConfig(mode=HandoffMode.NUMBER, destination="+15550001111"),
            safety=SafetyPolicy(record_calls=True),
        )
        agent_service.create_draft(tenant_a, config)
        await agent_service.publish_async(db, tenant_a, config)
        assert tenant_a.agent_name == "Rex"
        assert tenant_a.language == "fr-FR"
        assert tenant_a.llm_provider == "google"
        assert tenant_a.escalation_number == "+15550001111"

    async def test_rollback_reapplies_history(self, db, tenant_a):
        v1 = _agent("Alex", str(tenant_a.id))
        agent_service.create_draft(tenant_a, v1)
        await agent_service.publish_async(db, tenant_a, v1)
        v2 = AgentConfig(tenant_id=str(tenant_a.id), name="Alex", greeting="New greeting",
                         language=LanguageConfig(primary="en-US"),
                         model=ModelConfig(provider="anthropic"))
        agent_service.update_draft(tenant_a, v2)
        await agent_service.publish_async(db, tenant_a, v2)
        assert tenant_a.greeting == "New greeting"
        rolled = await agent_service.rollback_async(db, tenant_a, v1.id, 1)
        assert rolled.version == 3
        assert tenant_a.greeting == "Thanks for calling."

    async def test_configure_tools_validation(self, tenant_a):
        config = _agent("Alex", str(tenant_a.id))
        agent_service.create_draft(tenant_a, config)
        with pytest.raises(ValueError):
            agent_service.configure_tools(tenant_a, config.id, enabled=("not_a_tool",))

    async def test_test_configuration_offline(self, tenant_a):
        config = _agent("Alex", str(tenant_a.id))
        result = agent_service.test_configuration(tenant_a, config)
        assert result["ok"] is True
        assert isinstance(result["checks"], dict)


# ============================================================== CONVERSATIONS ===

class TestConversationDomain:
    def test_transition_table(self):
        assert conversation_can_transition(ConversationState.ACTIVE, ConversationState.COMPLETED)
        assert conversation_can_transition(ConversationState.COMPLETED, ConversationState.ACTIVE)
        assert not conversation_can_transition(ConversationState.FAILED, ConversationState.ACTIVE)

    def test_invalid_transition_raises(self):
        from app.domain.conversation_models import Conversation, ConversationChannel

        conversation = Conversation(id="c1", tenant_id="t", channel=ConversationChannel.VOICE,
                                    state=ConversationState.FAILED)
        with pytest.raises(ValueError):
            conversation.transition(ConversationState.COMPLETED)


class TestConversationService:
    async def test_start_and_project(self, db, tenant_a):
        call = await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        view = conversation_service.project(tenant_a, call)
        assert view.intent == "booking"
        assert view.state is ConversationState.ACTIVE

    async def test_append_turn_and_classify(self, db, tenant_a):
        call = await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        await conversation_service.append_turn(db, tenant_a, call, speaker=Speaker.USER,
                                               text="I need a cleaning")
        await conversation_service.classify(db, tenant_a, call, intent="booking",
                                            topic="cleaning", sentiment=Sentiment.NEUTRAL)
        view = conversation_service.project(tenant_a, call)
        assert view.intent == "booking"
        assert view.sentiment is Sentiment.NEUTRAL

    async def test_escalate_records_intent_without_dialing(self, db, tenant_a):
        call = await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        # ``escalate`` only records the intent; the transfer service owns the
        # provider dial, so no outbound call can be triggered from this module.
        view = await conversation_service.escalate(
            db, tenant_a, call, destination="+15550002222", reason="caller upset")
        assert call.escalated is True
        assert call.transfer_reason == "caller upset"
        assert call.transfer_destination == "+15550002222"
        assert view.escalation is EscalationState.REQUESTED

    async def test_close_and_reopen_policy(self, db, tenant_a):
        call = await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        await conversation_service.close(db, tenant_a, call)
        assert call.status is CallStatus.COMPLETED
        reopened = await conversation_service.reopen(db, tenant_a, call)
        assert reopened.state is ConversationState.ACTIVE

    async def test_reopen_outside_window_rejected(self, db, tenant_a):
        call = await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        await conversation_service.close(db, tenant_a, call)
        call.ended_at = datetime.now(timezone.utc) - timedelta(days=3)
        await db.commit()
        with pytest.raises(BadRequestError):
            await conversation_service.reopen(db, tenant_a, call)

    async def test_summarize_persists(self, db, tenant_a):
        call = await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        await conversation_service.summarize(db, tenant_a, call, short="Booked Tuesday",
                                             topics=("cleaning",))
        assert call.summary == "Booked Tuesday"

    async def test_search_tenant_scoped(self, db, tenant_a, tenant_b):
        await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        await conversation_service.start_conversation(
            db, tenant_b, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15559998888", intent="booking")
        mine = await conversation_service.search(db, tenant_a, intent="booking")
        assert all(str(c.tenant_id) == str(tenant_a.id) for c in mine)
        assert len(mine) == 1


# ================================================================== WORKFLOWS ===

class TestWorkflowDomain:
    def test_controlled_actions_are_bounded(self):
        assert "update_lead_status" in CONTROLLED_ACTIONS
        for bad in ("eval", "exec", "import", "os.system"):
            assert bad not in CONTROLLED_ACTIONS
        for op in ("eq", "in", "contains", "gt"):
            assert op in CONDITION_OPERATORS

    def test_evaluate_condition_operators(self):
        assert evaluate_condition(5, "gt", 3) is True
        assert evaluate_condition("hello", "contains", "ell") is True
        assert evaluate_condition(None, "exists", True) is False
        assert evaluate_condition(3, "in", [1, 2, 3]) is True

    def test_validation_rejects_unknown_action(self):
        node = WorkflowNode(id="a", type=NodeType.ACTION,
                            action=WorkflowAction(name="rm -rf /"), next="end")
        definition = WorkflowDefinition(id="w", tenant_id="t", name="bad",
                                        entry_node="a",
                                        nodes=(node, WorkflowNode(id="end", type=NodeType.TERMINAL)))
        assert any("not a controlled action" in p for p in definition.validate())

    def test_validation_rejects_code_markers(self):
        node = WorkflowNode(id="a", type=NodeType.ACTION,
                            action=WorkflowAction(name="__import__"), next="end")
        definition = WorkflowDefinition(id="w", tenant_id="t", name="bad", entry_node="a",
                                        nodes=(node, WorkflowNode(id="end", type=NodeType.TERMINAL)))
        assert any("forbidden" in p for p in definition.validate())

    def test_validation_requires_terminal(self):
        node = WorkflowNode(id="a", type=NodeType.ACTION, action=WorkflowAction("mark_resolved"))
        definition = WorkflowDefinition(id="w", tenant_id="t", name="bad", entry_node="a",
                                        nodes=(node,))
        assert any("terminal" in p for p in definition.validate())


class TestWorkflowService:
    async def test_publish_required_before_execute(self, tenant_a):
        definition = _terminal_workflow(str(tenant_a.id))
        workflow_service.create_workflow(str(tenant_a.id), definition)
        with pytest.raises(BadRequestError):
            await workflow_service.execute_workflow(str(tenant_a.id), definition.id, {})

    async def test_execution_deterministic_and_idempotent(self, tenant_a):
        definition = _terminal_workflow(str(tenant_a.id))
        workflow_service.create_workflow(str(tenant_a.id), definition)
        workflow_service.publish_workflow(str(tenant_a.id), definition.id)
        first = await workflow_service.execute_workflow(str(tenant_a.id), definition.id, {"x": 1})
        second = await workflow_service.execute_workflow(str(tenant_a.id), definition.id, {"x": 1})
        assert first.id == second.id                    # idempotent replay
        assert first.status is ExecutionStatus.COMPLETED

    async def test_condition_branching(self, tenant_a):
        nodes = (
            WorkflowNode(id="gate", type=NodeType.CONDITION,
                         condition=Condition(field="ok", operator="eq", value=True), next="end"),
            WorkflowNode(id="end", type=NodeType.TERMINAL),
        )
        definition = WorkflowDefinition(id="wf-cond", tenant_id=str(tenant_a.id), name="cond",
                                        entry_node="gate", nodes=nodes)
        workflow_service.create_workflow(str(tenant_a.id), definition)
        workflow_service.publish_workflow(str(tenant_a.id), definition.id)
        ok = await workflow_service.execute_workflow(str(tenant_a.id), definition.id, {"ok": True})
        bad = await workflow_service.execute_workflow(str(tenant_a.id), definition.id, {"ok": False})
        assert ok.status is ExecutionStatus.COMPLETED
        assert bad.status is ExecutionStatus.FAILED

    async def test_approval_gate_waits_then_cancel(self, tenant_a):
        nodes = (
            WorkflowNode(id="gate", type=NodeType.APPROVAL, approver_role="manager", next="end"),
            WorkflowNode(id="end", type=NodeType.TERMINAL),
        )
        definition = WorkflowDefinition(id="wf-appr", tenant_id=str(tenant_a.id), name="appr",
                                        entry_node="gate", nodes=nodes)
        workflow_service.create_workflow(str(tenant_a.id), definition)
        workflow_service.publish_workflow(str(tenant_a.id), definition.id)
        execution = await workflow_service.execute_workflow(str(tenant_a.id), definition.id, {})
        assert execution.status is ExecutionStatus.WAITING_APPROVAL
        cancelled = workflow_service.cancel_execution(str(tenant_a.id), execution.id)
        assert cancelled.status is ExecutionStatus.CANCELLED

    async def test_retry_after_failure(self, tenant_a):
        nodes = (
            WorkflowNode(id="gate", type=NodeType.CONDITION,
                         condition=Condition(field="ok", operator="eq", value=True), next="end"),
            WorkflowNode(id="end", type=NodeType.TERMINAL),
        )
        definition = WorkflowDefinition(id="wf-retry", tenant_id=str(tenant_a.id), name="retry",
                                        entry_node="gate", nodes=nodes)
        workflow_service.create_workflow(str(tenant_a.id), definition)
        workflow_service.publish_workflow(str(tenant_a.id), definition.id)
        failed = await workflow_service.execute_workflow(str(tenant_a.id), definition.id, {"ok": False})
        assert failed.status is ExecutionStatus.FAILED
        retried = await workflow_service.retry_execution(str(tenant_a.id), failed.id, {"ok": True})
        assert retried.status is ExecutionStatus.COMPLETED

    async def test_tenant_isolation(self, tenant_a, tenant_b):
        definition = _terminal_workflow(str(tenant_a.id))
        workflow_service.create_workflow(str(tenant_a.id), definition)
        with pytest.raises(NotFoundError):
            workflow_service.get_workflow(str(tenant_b.id), definition.id)

    async def test_lead_status_action_persists(self, db, tenant_a):
        lead = await make_lead(db, tenant_a)
        nodes = (
            WorkflowNode(id="act", type=NodeType.ACTION,
                         action=WorkflowAction("update_lead_status", {"status": "qualified"}),
                         next="end"),
            WorkflowNode(id="end", type=NodeType.TERMINAL),
        )
        definition = WorkflowDefinition(id="wf-lead", tenant_id=str(tenant_a.id), name="lead",
                                        entry_node="act", nodes=nodes)
        workflow_service.create_workflow(str(tenant_a.id), definition)
        workflow_service.publish_workflow(str(tenant_a.id), definition.id)
        await workflow_service.execute_workflow(str(tenant_a.id), definition.id,
                                                {"lead_id": str(lead.id)}, session=db)
        await db.refresh(lead)
        assert lead.status is LeadStatus.QUALIFIED


# ================================================================ AUTOMATIONS ===

class TestAutomationService:
    def _automation(self, tenant_id, *, filters=(), cooldown=0):
        return AutomationDefinition(
            id=f"auto-{tenant_id}", tenant_id=tenant_id, name="Negative sentiment alert",
            event=TriggerEvent.CALL_COMPLETED, filters=tuple(filters),
            actions=(WorkflowAction("record_escalation_intent", {"destination": "+15550009999"}),),
            schedule=AutomationSchedule(kind=ScheduleKind.ON_EVENT),
            policy=ExecutionPolicy(cooldown_seconds=cooldown, max_per_event=1),
            status=AutomationStatus.ENABLED,
        )

    async def test_filter_matching(self, tenant_a):
        automation = self._automation(str(tenant_a.id), filters=(FilterRule("sentiment", "eq", "negative"),))
        automation_service.register_automation(str(tenant_a.id), automation)
        hits = automation_service.evaluate(str(tenant_a.id), TriggerEvent.CALL_COMPLETED,
                                           {"sentiment": "negative"})
        assert len(hits) == 1
        misses = automation_service.evaluate(str(tenant_a.id), TriggerEvent.CALL_COMPLETED,
                                             {"sentiment": "positive"})
        assert misses == []

    async def test_disabled_not_evaluated(self, tenant_a):
        automation = self._automation(str(tenant_a.id))
        automation_service.register_automation(str(tenant_a.id), automation)
        automation_service.set_enabled(str(tenant_a.id), automation.id, False)
        assert automation_service.evaluate(str(tenant_a.id), TriggerEvent.CALL_COMPLETED,
                                           {"sentiment": "negative"}) == []

    async def test_deduplication_idempotency(self, tenant_a):
        automation = self._automation(str(tenant_a.id))
        automation_service.register_automation(str(tenant_a.id), automation)
        event_id = "call-123"
        first = await automation_service.execute_automation(str(tenant_a.id), automation,
                                                            event_id, {"sentiment": "negative"})
        assert first.status == "completed"
        second = await automation_service.execute_automation(str(tenant_a.id), automation,
                                                             event_id, {"sentiment": "negative"})
        assert second.status == "cancelled"          # deduplicated, not double-executed
        assert second.last_error == "max_per_event budget exhausted"

    async def test_cooldown_suppresses(self, tenant_a):
        automation = self._automation(str(tenant_a.id), cooldown=3600)
        automation_service.register_automation(str(tenant_a.id), automation)
        first = await automation_service.execute_automation(str(tenant_a.id), automation,
                                                            "call-1", {"sentiment": "negative"})
        assert first.status == "completed"
        ok, reason = automation_service.should_run(str(tenant_a.id), automation, "call-2")
        assert ok is False and reason == "cooldown active"

    async def test_run_idempotency_key_deterministic(self):
        k1 = run_idempotency_key("t", "a", TriggerEvent.CALL_COMPLETED, "evt-1")
        k2 = run_idempotency_key("t", "a", TriggerEvent.CALL_COMPLETED, "evt-1")
        k3 = run_idempotency_key("t", "a", TriggerEvent.CALL_COMPLETED, "evt-2")
        assert k1 == k2 and k1 != k3


# ================================================================== CAMPAIGNS ===

class TestCampaignDomain:
    def test_state_transitions(self):
        assert campaign_service._can_transition(CampaignState.DRAFT, CampaignState.SCHEDULED)
        assert campaign_service._can_transition(CampaignState.SCHEDULED, CampaignState.RUNNING)
        assert campaign_service._can_transition(CampaignState.RUNNING, CampaignState.PAUSED)
        assert not campaign_service._can_transition(CampaignState.CANCELLED, CampaignState.RUNNING)

    def test_compliance_gate_cannot_be_weakened(self):
        weakened = ComplianceGate(require_dnc_check=False)
        assert any("may not be disabled" in p for p in weakened.validate())
        strict = ComplianceGate()
        assert strict.validate() == []
        assert strict.require_dnc_check is True and strict.require_call_window is True


class TestCampaignService:
    async def test_create_and_state_lifecycle(self, db, tenant_a):
        definition = _campaign(str(tenant_a.id))
        row = await campaign_service.create_campaign(db, tenant_a, definition)
        assert row.is_active is False
        await campaign_service.schedule_campaign(db, tenant_a, str(row.id))
        _open_window(tenant_a)
        await campaign_service.resume_campaign(db, tenant_a, str(row.id))
        current = await campaign_service.get_campaign(db, tenant_a, str(row.id))
        assert current.state is CampaignState.RUNNING
        await campaign_service.pause_campaign(db, tenant_a, str(row.id))
        current = await campaign_service.get_campaign(db, tenant_a, str(row.id))
        assert current.state is CampaignState.PAUSED

    async def test_dnc_lead_skipped(self, db, tenant_a):
        lead = await make_lead(db, tenant_a, status=LeadStatus.DNC)
        definition = _campaign(str(tenant_a.id))
        definition = CampaignDefinition(**{**definition.__dict__,
                                           "audience": Audience(tenant_id=str(tenant_a.id),
                                                                lead_ids=(str(lead.id),))})
        ok, reason = await campaign_service.eligibility_check(db, tenant_a, lead, definition)
        assert ok is False and reason == "lead is on do-not-call"

    async def test_window_check_respects_tenant(self, db, tenant_a):
        lead = await make_lead(db, tenant_a)
        tenant_a.outbound_window_open = time(1, 0)
        tenant_a.outbound_window_close = time(2, 0)
        await db.commit()
        definition = _campaign(str(tenant_a.id))
        ok, reason = await campaign_service.eligibility_check(db, tenant_a, lead, definition)
        assert ok is False and reason == "outside the outbound call window"

    async def test_attempt_limit(self, db, tenant_a):
        lead = await make_lead(db, tenant_a, attempts=3)
        definition = _campaign(str(tenant_a.id))
        definition = CampaignDefinition(**{**definition.__dict__,
                                           "throttle": Throttle(max_attempts_per_lead=3)})
        _open_window(tenant_a)
        ok, reason = await campaign_service.eligibility_check(db, tenant_a, lead, definition)
        assert ok is False and reason == "attempt limit reached"

    async def test_execution_plan_never_dials(self, db, tenant_a):
        lead = await make_lead(db, tenant_a)
        definition = _campaign(str(tenant_a.id))
        definition = CampaignDefinition(**{**definition.__dict__,
                                           "audience": Audience(tenant_id=str(tenant_a.id),
                                                                lead_ids=(str(lead.id),))})
        row = await campaign_service.create_campaign(db, tenant_a, definition)
        fetched = await campaign_service.get_campaign(db, tenant_a, str(row.id))
        _open_window(tenant_a)
        # ``execution_plan`` only builds safe intents — it has no dial path at
        # all, so nothing can ever be dialled from here.
        intents = await campaign_service.execution_plan(db, tenant_a, fetched)
        assert len(intents) == 1 and not intents[0].skipped
        assert intents[0].lead_id == str(lead.id)
        assert intents[0].tenant_id == str(tenant_a.id)

    async def test_progress_counts_leads(self, db, tenant_a):
        row = await campaign_service.create_campaign(db, tenant_a, _campaign(str(tenant_a.id)))
        definition = await campaign_service.get_campaign(db, tenant_a, str(row.id))
        await make_lead(db, tenant_a, campaign_id=row.id, status=LeadStatus.QUALIFIED)
        await make_lead(db, tenant_a, campaign_id=row.id, status=LeadStatus.DNC)
        metrics = await campaign_service.progress(db, tenant_a, definition)
        assert metrics.total_leads == 2
        assert metrics.conversions == 1
        assert metrics.dnc_skipped == 1


# ================================================================= ANALYTICS ===

class TestAnalyticsService:
    async def test_aggregate_correctness(self, db, tenant_a):
        start, end = _bounded_range()
        await make_call(db, tenant_a, status=CallStatus.COMPLETED, duration_seconds=120)
        await make_call(db, tenant_a, status=CallStatus.COMPLETED, duration_seconds=60)
        await make_call(db, tenant_a, status=CallStatus.FAILED)
        kpi = await analytics_service.call_kpis(db, str(tenant_a.id), start=start, end=end)
        assert kpi.total == 3 and kpi.answered == 2 and kpi.failed == 1
        assert kpi.rates()["answer_rate"] == 66.7

    async def test_tenant_isolation(self, db, tenant_a, tenant_b):
        start, end = _bounded_range()
        await make_call(db, tenant_a, status=CallStatus.COMPLETED)
        kpi_a = await analytics_service.call_kpis(db, str(tenant_a.id), start=start, end=end)
        kpi_b = await analytics_service.call_kpis(db, str(tenant_b.id), start=start, end=end)
        assert kpi_a.total == 1 and kpi_b.total == 0

    async def test_range_validation(self, db, tenant_a):
        with pytest.raises(BadRequestError):
            await analytics_service.call_kpis(db, str(tenant_a.id),
                                              start=datetime(2025, 1, 2), end=datetime(2025, 1, 1))

    async def test_empty_results(self, db, tenant_a):
        start, end = _bounded_range(days_back=5)
        snapshot = await analytics_service.snapshot(db, str(tenant_a.id),
                                                    kind=analytics_service.KpiKind.CALL,
                                                    start=start, end=end)
        assert snapshot.points[0].metrics["total"] == 0

    async def test_snapshot_has_no_pii(self, db, tenant_a):
        start, end = _bounded_range()
        await make_call(db, tenant_a)
        snapshot = await analytics_service.snapshot(db, str(tenant_a.id),
                                                    kind=analytics_service.KpiKind.CALL,
                                                    start=start, end=end)
        assert snapshot.assert_no_pii() == []

    async def test_cost_estimate_from_usage(self, db, tenant_a, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings.__class__, "cost_unit_prices",
                            property(lambda self: {"voice_minute": 1300, "sms_segment": 790}))
        event = UsageEvent(
            tenant_id=tenant_a.id, billing_period="2026-09", metric=UsageMetric.VOICE_MINUTE,
            event_type=UsageEventType.VOICE_MINUTE_USED, quantity=120, unit="seconds",
            idempotency_key="usage-test-1", event_metadata={},
        )
        db.add(event)
        await db.commit()
        cost = await analytics_service.cost_kpis(db, str(tenant_a.id),
                                                 start=datetime(2026, 9, 1), end=datetime(2026, 10, 1))
        assert cost.minutes == 2.0
        assert cost.estimated_cost_millicents == 2600


# ============================================================== NOTIFICATIONS ===

class TestNotificationService:
    def _template(self, tenant_id="t"):
        return NotificationTemplate(
            id="tmpl-1", tenant_id=tenant_id, name="Appointment reminder",
            channel=NotificationChannel.SMS, body="Hi {customer_name}, see you at {appointment_time}.",
            variables=("customer_name", "appointment_time"),
        )

    def test_strict_rendering(self):
        template = self._template()
        body = template.render({"customer_name": "Jane", "appointment_time": "2pm"})
        assert body == "Hi Jane, see you at 2pm."
        body_unknown = template.render({"customer_name": "Jane"})
        assert "{appointment_time}" in body_unknown       # never swallowed, never evaluated

    def test_undeclared_variable_flagged(self):
        template = NotificationTemplate(id="t", tenant_id="t", name="x",
                                        channel=NotificationChannel.IN_APP,
                                        body="Hello {injected}", variables=())
        assert any("undeclared" in p for p in template.validate())

    def test_preference_quiet_hours(self):
        pref = PreferenceSet(channel=NotificationChannel.SMS, quiet_start=time(22, 0),
                             quiet_end=time(8, 0))
        moment = datetime(2026, 9, 14, 23, 0)
        ok, reason = notification_service.resolve_preferences(
            pref, NotificationChannel.SMS, now=moment)
        assert ok is False and reason == "recipient is in quiet hours"

    async def test_deduplication(self, tenant_a):
        template = self._template(str(tenant_a.id))
        notification_service.create_template(str(tenant_a.id), template)
        recipient = Recipient(kind="phone", target="+15550001111")
        n1 = notification_service.create_notification(
            str(tenant_a.id), template=template, recipient=recipient,
            event_source=EventSource.APPOINTMENT_REMINDER, business_key="appt-1")
        n2 = notification_service.create_notification(
            str(tenant_a.id), template=template, recipient=recipient,
            event_source=EventSource.APPOINTMENT_REMINDER, business_key="appt-1")
        assert n1.id == n2.id                            # same business fact, one notification

    async def test_retry_backoff_advances(self, tenant_a):
        template = self._template(str(tenant_a.id))
        notification_service.create_template(str(tenant_a.id), template)
        notification = notification_service.create_notification(
            str(tenant_a.id), template=template, recipient=Recipient(kind="user", target="u1"),
            event_source=EventSource.SYSTEM, business_key="b1")
        retried = notification_service.retry(str(tenant_a.id), notification.id)
        assert retried.attempts == 1
        assert retried.delivery_state is DeliveryState.RETRYING

    async def test_in_app_delivery(self, tenant_a):
        template = NotificationTemplate(
            id="tmpl-inapp", tenant_id=str(tenant_a.id), name="Alert",
            channel=NotificationChannel.IN_APP, body="You have a new message.",
            variables=())
        notification_service.create_template(str(tenant_a.id), template)
        notification = notification_service.create_notification(
            str(tenant_a.id), template=template, recipient=Recipient(kind="user", target="u1"),
            event_source=EventSource.SYSTEM, business_key="b2")
        result = await notification_service.deliver(notification)
        assert result.delivered is True

    async def test_email_channel_suppressed_honestly(self, tenant_a):
        template = NotificationTemplate(
            id="tmpl-mail", tenant_id=str(tenant_a.id), name="Email",
            channel=NotificationChannel.EMAIL, body="Hello.", variables=())
        notification_service.create_template(str(tenant_a.id), template)
        notification = notification_service.create_notification(
            str(tenant_a.id), template=template, recipient=Recipient(kind="email", target="a@b.c"),
            event_source=EventSource.SYSTEM, business_key="b3")
        result = await notification_service.deliver(notification)
        assert result.state is DeliveryState.SUPPRESSED


# ===================================================================== INBOX ===

class TestInboxService:
    async def test_thread_isolation(self, db, tenant_a, tenant_b):
        thread_a = await inbox_service.find_or_create_thread(
            db, tenant_a, channel=InboxChannel.WEB, customer="cust-a")
        with pytest.raises(NotFoundError):
            await inbox_service.project(db, tenant_b, thread_a)

    async def test_message_ordering(self, db, tenant_a):
        thread = await inbox_service.find_or_create_thread(
            db, tenant_a, channel=InboxChannel.WEB, customer="cust-a")
        m1 = await inbox_service.append_message(db, tenant_a, thread,
                                                direction=MessageDirection.INBOUND, body="hi")
        m2 = await inbox_service.append_message(db, tenant_a, thread,
                                                direction=MessageDirection.OUTBOUND, body="hello")
        assert m2.sequence > m1.sequence
        view = await inbox_service.project(db, tenant_a, thread)
        assert [m.sequence for m in view.messages] == sorted(m.sequence for m in view.messages)

    async def test_read_unread(self, db, tenant_a):
        thread = await inbox_service.find_or_create_thread(
            db, tenant_a, channel=InboxChannel.WEB, customer="cust-a")
        await inbox_service.append_message(db, tenant_a, thread,
                                           direction=MessageDirection.INBOUND, body="hi")
        inbox_service.mark_unread(tenant_a, thread)
        view = inbox_service.mark_read(tenant_a, thread)
        assert view.unread_count == 0

    async def test_assignment(self, db, tenant_a):
        thread = await inbox_service.find_or_create_thread(
            db, tenant_a, channel=InboxChannel.WEB, customer="cust-a")
        view = inbox_service.assign(tenant_a, thread, "agent-1")
        assert view.assignee_id == "agent-1"

    async def test_escalation(self, db, tenant_a):
        thread = await inbox_service.find_or_create_thread(
            db, tenant_a, channel=InboxChannel.WEB, customer="cust-a")
        await inbox_service.escalate(db, tenant_a, thread, reason="needs human")
        assert thread.escalated is True

    async def test_close_and_reopen_policy(self, db, tenant_a):
        thread = await inbox_service.find_or_create_thread(
            db, tenant_a, channel=InboxChannel.WEB, customer="cust-a")
        await inbox_service.append_message(db, tenant_a, thread,
                                           direction=MessageDirection.INBOUND, body="hi")
        await inbox_service.close(db, tenant_a, thread)
        view = await inbox_service.project(db, tenant_a, thread)
        assert view.status is ThreadStatus.CLOSED
        await inbox_service.reopen(db, tenant_a, thread)
        view = await inbox_service.project(db, tenant_a, thread)
        assert view.status is ThreadStatus.OPEN

    def test_reopen_window_rule(self):
        recent = Thread(id="t", tenant_id="x", channel=InboxChannel.WEB,
                        status=ThreadStatus.CLOSED,
                        last_message_at=datetime.now(timezone.utc).isoformat())
        assert reopen_allowed(recent) is True
        old = Thread(id="t2", tenant_id="x", channel=InboxChannel.WEB,
                     status=ThreadStatus.CLOSED,
                     last_message_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
        assert reopen_allowed(old) is False


# ======================================================================= API ===

class TestAgentApi:
    async def test_owner_can_create_and_list(self, enterprise_client, owner_a):
        response = await enterprise_client.post(
            "/api/agents", json={"name": "Alex", "greeting": "Thanks for calling."},
            headers=_auth(owner_a))
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Alex"
        listing = await enterprise_client.get("/api/agents", headers=_auth(owner_a))
        assert listing.status_code == 200 and any(a["name"] == "Alex" for a in listing.json())

    async def test_viewer_forbidden(self, enterprise_client, viewer_a):
        response = await enterprise_client.post(
            "/api/agents", json={"name": "Alex"}, headers=_auth(viewer_a))
        assert response.status_code == 403

    async def test_mass_assignment_protected(self, enterprise_client, owner_a):
        response = await enterprise_client.post(
            "/api/agents",
            json={"name": "Alex", "admin": True, "api_key": "sk-live-secret"},
            headers=_auth(owner_a))
        assert response.status_code == 422          # extra fields rejected

    async def test_no_secret_leakage(self, enterprise_client, owner_a):
        await enterprise_client.post(
            "/api/agents", json={"name": "Alex"}, headers=_auth(owner_a))
        listing = await enterprise_client.get("/api/agents", headers=_auth(owner_a))
        serialized = str(listing.json())
        assert "sk-live" not in serialized
        assert "api_key" not in listing.json()[0]

    async def test_publish_flow(self, enterprise_client, owner_a):
        created = await enterprise_client.post(
            "/api/agents", json={"name": "Rex", "greeting": "Hello"}, headers=_auth(owner_a))
        agent_id = created.json()["id"]
        published = await enterprise_client.post(
            f"/api/agents/{agent_id}/publish", json={"changelog": "first"}, headers=_auth(owner_a))
        assert published.status_code == 200
        assert published.json()["version"] == 1
        versions = await enterprise_client.get(
            f"/api/agents/{agent_id}/versions", headers=_auth(owner_a))
        assert len(versions.json()) == 1


class TestWorkflowApi:
    async def test_create_and_execute(self, enterprise_client, owner_a):
        created = await enterprise_client.post(
            "/api/workflows",
            json={"name": "Onboarding", "entry_node": "end",
                  "nodes": [{"id": "end", "type": "terminal"}]},
            headers=_auth(owner_a))
        assert created.status_code == 201, created.text
        workflow_id = created.json()["id"]
        await enterprise_client.post(f"/api/workflows/{workflow_id}/publish", headers=_auth(owner_a))
        execution = await enterprise_client.post(
            f"/api/workflows/{workflow_id}/execute", json={"payload": {"x": 1}},
            headers=_auth(owner_a))
        assert execution.status_code == 200, execution.text
        assert execution.json()["status"] == "completed"

    async def test_rejects_arbitrary_action(self, enterprise_client, owner_a):
        response = await enterprise_client.post(
            "/api/workflows",
            json={"name": "Bad", "entry_node": "a",
                  "nodes": [
                      {"id": "a", "type": "action", "action_name": "eval",
                       "action_params": {"code": "os.system('rm -rf /')"}, "next": "end"},
                      {"id": "end", "type": "terminal"},
                  ]},
            headers=_auth(owner_a))
        assert response.status_code == 422

    async def test_tenant_isolation(self, enterprise_client, owner_a, owner_b):
        created = await enterprise_client.post(
            "/api/workflows",
            json={"name": "Secret workflow", "entry_node": "end",
                  "nodes": [{"id": "end", "type": "terminal"}]},
            headers=_auth(owner_a))
        workflow_id = created.json()["id"]
        probe = await enterprise_client.get(f"/api/workflows/{workflow_id}", headers=_auth(owner_b))
        assert probe.status_code == 404


class TestCampaignApi:
    async def test_create_requires_write_permission(self, enterprise_client, viewer_a):
        response = await enterprise_client.post(
            "/api/campaigns", json={"name": "Spring"}, headers=_auth(viewer_a))
        assert response.status_code == 403

    async def test_create_and_plan(self, enterprise_client, owner_a):
        created = await enterprise_client.post(
            "/api/campaigns", json={"name": "Spring", "goal": "qualify"},
            headers=_auth(owner_a))
        assert created.status_code == 201, created.text
        campaign_id = created.json()["id"]
        plan = await enterprise_client.post(
            f"/api/campaigns/{campaign_id}/plan", headers=_auth(owner_a))
        assert plan.status_code == 200
        assert plan.json() == []                     # no leads yet, still safe

    async def test_mass_assignment_protected(self, enterprise_client, owner_a):
        response = await enterprise_client.post(
            "/api/campaigns",
            json={"name": "Spring", "bypass_dnc": True, "skip_safety": True},
            headers=_auth(owner_a))
        assert response.status_code == 422


# =================================================================== SECURITY ===

class TestSecurityGuarantees:
    async def test_workflow_cannot_execute_arbitrary_code(self, tenant_a):
        for bad in ("eval", "__import__", "os.system", "exec", "lambda"):
            node = WorkflowNode(id="a", type=NodeType.ACTION, action=WorkflowAction(bad), next="end")
            definition = WorkflowDefinition(id="w", tenant_id=str(tenant_a.id), name="x",
                                            entry_node="a",
                                            nodes=(node, WorkflowNode(id="end", type=NodeType.TERMINAL)))
            assert definition.validate(), f"{bad} should be rejected"

    async def test_automation_never_duplicates_side_effects(self, tenant_a):
        automation = AutomationDefinition(
            id="auto-x", tenant_id=str(tenant_a.id), name="x",
            event=TriggerEvent.LEAD_CREATED,
            actions=(WorkflowAction("enqueue_notification", {"template_id": "nope"}),),
            policy=ExecutionPolicy(max_per_event=1), status=AutomationStatus.ENABLED,
        )
        automation_service.register_automation(str(tenant_a.id), automation)
        r1 = await automation_service.execute_automation(str(tenant_a.id), automation, "lead-1", {})
        r2 = await automation_service.execute_automation(str(tenant_a.id), automation, "lead-1", {})
        assert r1.status == "completed"
        assert r2.status == "cancelled"

    async def test_no_cross_tenant_conversation_visibility(self, db, tenant_a, tenant_b):
        call = await conversation_service.start_conversation(
            db, tenant_a, channel=conversation_service.ConversationChannel.VOICE,
            from_number="+15551234567", intent="booking")
        with pytest.raises(NotFoundError):
            conversation_service.project(tenant_b, call)

    async def test_analytics_never_exposes_phone_numbers(self, db, tenant_a):
        start, end = _bounded_range()
        await make_call(db, tenant_a, from_number="+15551234567")
        snapshot = await analytics_service.snapshot(db, str(tenant_a.id),
                                                    kind=analytics_service.KpiKind.CALL,
                                                    start=start, end=end)
        serialized = str(snapshot.points[0].metrics)
        assert "+15551234567" not in serialized

    async def test_api_requires_authentication(self, enterprise_client):
        response = await enterprise_client.get("/api/agents")
        assert response.status_code == 401
