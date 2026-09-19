"""Unit tests for app.orchestration.workflow.

Co-located with the package; run with: python -m pytest app/orchestration/ -q
"""

from __future__ import annotations

import pytest

from app.orchestration.workflow import (
    ACTION,
    APPROVAL,
    COMPLETED,
    CONDITION,
    DELAY,
    FAILED,
    RUNNING,
    TERMINAL,
    TIMED_OUT,
    TRIGGER,
    WAITING_APPROVAL,
    Action,
    Condition,
    Node,
    RunResult,
    Workflow,
    WorkflowEngine,
    WorkflowError,
)


def action_node(node_id, action_name, *, params=None, next_id="", retry_limit=3):
    return Node(
        id=node_id, type=ACTION,
        action=Action(name=action_name, params=params or {}),
        next=next_id, retry_limit=retry_limit,
    )


def terminal_node(node_id):
    return Node(id=node_id, type=TERMINAL)


def test_workflow_validation_requires_terminal():
    wf = Workflow("w", "t", "no terminal", entry_node="a",
                  nodes=(action_node("a", "mark_resolved", next_id=""),))
    problems = wf.validate()
    assert any("terminal" in p for p in problems)
    assert not wf.is_valid()


def test_workflow_validation_catches_unknown_next():
    wf = Workflow("w", "t", "dangling", entry_node="a",
                  nodes=(action_node("a", "mark_resolved", next_id="zzz"), terminal_node("end")))
    assert any("unknown node" in p for p in wf.validate())


def test_controlled_action_allowlist_and_markers():
    problems = Action(name="rm_rf", params={}).validate()
    assert any("not a controlled action" in p for p in problems)

    problems = Action(name="mark_resolved", params={}).validate()
    assert not problems

    # A name that somehow contains a code marker is rejected even if listed.
    node = Node(id="n", type=ACTION, action=Action(name="update_lead_status", params={}))
    assert not node.action.validate()  # legit name is fine

    bad = Action(name="eval_something", params={})
    assert any("forbidden" in p for p in bad.validate())


def test_linear_run_completes_and_records_steps():
    calls: list[str] = []
    engine = WorkflowEngine(handlers={
        "mark_resolved": lambda name, params: calls.append(name),
    })
    wf = Workflow("w", "t", "linear", entry_node="start",
                  nodes=(
                      Node(id="start", type=TRIGGER, next="act"),
                      action_node("act", "mark_resolved", next_id="end"),
                      terminal_node("end"),
                  ))
    result = engine.run(wf, {})
    assert result.status == COMPLETED
    assert result.terminal
    assert calls == ["mark_resolved"]
    assert [s.node_id for s in result.steps] == ["act", "end"]


def test_condition_branch_selects_matching_target():
    wf = Workflow("w", "t", "branch", entry_node="c",
                  nodes=(
                      Node(
                          id="c", type=CONDITION,
                          branches=(
                              (Condition("status", "eq", "vip"), "vip_path"),
                              (Condition("status", "eq", "normal"), "normal_path"),
                          ),
                          default_next="other_path",
                      ),
                      terminal_node("vip_path"),
                      terminal_node("normal_path"),
                      terminal_node("other_path"),
                  ))
    engine = WorkflowEngine()
    result = engine.run(wf, {"status": "normal"})
    assert result.status == COMPLETED
    assert result.steps[0].detail == "branch -> normal_path"


def test_condition_if_else_single_condition():
    wf = Workflow("w", "t", "ifelse", entry_node="c",
                  nodes=(
                      Node(id="c", type=CONDITION, condition=Condition("hot", "eq", True),
                           next="yes", default_next="no"),
                      terminal_node("yes"),
                      terminal_node("no"),
                  ))
    engine = WorkflowEngine()
    assert engine.run(wf, {"hot": True}).steps[0].detail == "branch -> yes"
    assert engine.run(wf, {"hot": False}).steps[0].detail == "branch -> no"


def test_condition_no_match_fails():
    wf = Workflow("w", "t", "no match", entry_node="c",
                  nodes=(
                      Node(id="c", type=CONDITION, branches=((Condition("x", "eq", 1), "yes"),)),
                      terminal_node("yes"),
                  ))
    result = WorkflowEngine().run(wf, {"x": 2})
    assert result.status == FAILED
    assert "matched no branch" in result.error


def test_action_failure_retries_then_fails():
    attempts: list[int] = []

    def flaky(name, params):
        attempts.append(name)
        raise RuntimeError("boom")

    engine = WorkflowEngine(handlers={"mark_resolved": flaky})
    wf = Workflow("w", "t", "retry", entry_node="a",
                  nodes=(
                      action_node("a", "mark_resolved", next_id="end", retry_limit=3),
                      terminal_node("end"),
                  ))
    result = engine.run(wf, {})
    assert result.status == FAILED
    assert len(attempts) == 3
    assert result.steps[0].status == "failed"
    assert result.steps[0].attempt == 3


def test_action_without_handler_fails():
    engine = WorkflowEngine()
    wf = Workflow("w", "t", "no handler", entry_node="a",
                  nodes=(action_node("a", "mark_resolved", next_id="end"), terminal_node("end")))
    result = engine.run(wf, {})
    assert result.status == FAILED
    assert "no handler" in result.error


def test_approval_pauses_run():
    wf = Workflow("w", "t", "approval", entry_node="a",
                  nodes=(
                      Node(id="a", type=APPROVAL, approver_role="manager", next="end"),
                      terminal_node("end"),
                  ))
    result = WorkflowEngine().run(wf, {})
    assert result.status == WAITING_APPROVAL
    assert result.current_node == "a"
    assert not result.terminal


def test_delay_and_timeout_are_scheduled_not_slept():
    wf = Workflow("w", "t", "timers", entry_node="d",
                  nodes=(
                      Node(id="d", type=DELAY, delay_seconds=60, next="t"),
                      Node(id="t", type="timeout", timeout_seconds=30, next="end"),
                      terminal_node("end"),
                  ))
    result = WorkflowEngine().run(wf, {})
    assert result.status == COMPLETED
    assert result.steps[0].status == "scheduled"
    assert result.steps[1].status == "scheduled"


def test_idempotency_key_replays_without_reexecuting():
    calls: list[str] = []
    engine = WorkflowEngine(handlers={"mark_resolved": lambda n, p: calls.append(n)})
    wf = Workflow("w", "t", "idem", entry_node="a",
                  nodes=(action_node("a", "mark_resolved", next_id="end"), terminal_node("end")))
    first = engine.run(wf, {}, idempotency_key="evt-1")
    second = engine.run(wf, {}, idempotency_key="evt-1")
    assert first is second
    assert calls == ["mark_resolved"]
    # A different key runs again.
    engine.run(wf, {}, idempotency_key="evt-2")
    assert calls == ["mark_resolved", "mark_resolved"]


def test_cycle_is_capped():
    wf = Workflow("w", "t", "loop", entry_node="a",
                  nodes=(Node(id="a", type=TRIGGER, next="b"), Node(id="b", type=TRIGGER, next="a")))
    # Validation needs a terminal; add one unreachable node to satisfy it.
    wf = Workflow("w", "t", "loop", entry_node="a",
                  nodes=(Node(id="a", type=TRIGGER, next="b"), Node(id="b", type=TRIGGER, next="a"),
                         terminal_node("end")))
    result = WorkflowEngine().run(wf, {})
    assert result.status == TIMED_OUT
    assert "step limit" in result.error


def test_dangling_end_without_terminal_fails():
    wf = Workflow("w", "t", "dangling", entry_node="a",
                  nodes=(action_node("a", "mark_resolved", next_id=""), terminal_node("end")))
    # Reach the dangling node by pointing entry at "a"; "end" exists but is unreachable.
    result = WorkflowEngine(handlers={"mark_resolved": lambda n, p: None}).run(wf, {})
    assert result.status == FAILED
    assert "terminal" in result.error


def test_invalid_workflow_raises_on_run():
    wf = Workflow("w", "t", "broken", entry_node="missing",
                  nodes=(terminal_node("end"),))
    with pytest.raises(WorkflowError):
        WorkflowEngine().run(wf, {})


def test_run_result_is_typed():
    result = RunResult("w", "t", RUNNING, current_node="a")
    assert isinstance(result, RunResult)
    assert not result.terminal
