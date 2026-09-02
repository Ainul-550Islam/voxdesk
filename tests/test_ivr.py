"""IVR flow validation: a broken menu must never reach a real caller."""

from app.telephony.ivr import (
    DEFAULT_FLOW,
    interpolate,
    next_node,
    validate_flow,
)


class FakeTenant:
    name = "Bright Smile Dental"
    agent_name = "Alex"
    escalation_number = "+15551110000"

    class _T:
        def __format__(self, spec):
            return "9:00 AM"
    business_open = _T()
    business_close = _T()


# ------------------------------------------------------------- validation ---

def test_default_flow_is_valid():
    assert validate_flow(DEFAULT_FLOW) == []


def test_missing_nodes_is_reported():
    assert validate_flow({"start": "a", "nodes": {}}) == ["flow has no nodes"]


def test_bad_start_node_is_reported():
    flow = {"start": "nope", "nodes": {"ai": {"ai": True}}}
    assert any("start node" in p for p in validate_flow(flow))


def test_dangling_pointer_is_reported():
    flow = {"start": "m", "nodes": {
        "m": {"say": "hi", "gather": {"1": "ghost"}, "timeout_goto": "ai"},
        "ai": {"ai": True},
    }}
    assert any("ghost" in p for p in validate_flow(flow))


def test_dead_end_node_is_reported():
    flow = {"start": "m", "nodes": {
        "m": {"say": "hi", "gather": {"1": "dead"}, "timeout_goto": "ai"},
        "dead": {"say": "bye"},
        "ai": {"ai": True},
    }}
    assert any("dead end" in p for p in validate_flow(flow))


def test_flow_with_no_handoff_is_rejected():
    """A menu that loops forever and never reaches AI/human/voicemail is a trap."""
    flow = {"start": "m", "nodes": {"m": {"say": "hi", "gather": {"1": "m"}}}}
    assert any("hands off" in p for p in validate_flow(flow))


def test_unreachable_node_is_reported():
    flow = {"start": "ai", "nodes": {
        "ai": {"ai": True},
        "orphan": {"say": "nobody gets here", "hangup": True},
    }}
    assert any("unreachable" in p for p in validate_flow(flow))


def test_transfer_only_flow_is_valid():
    flow = {"start": "t", "nodes": {"t": {"say": "hold on", "transfer": "+15551110000"}}}
    assert validate_flow(flow) == []


# ---------------------------------------------------------------- routing ---

def test_next_node_follows_digit():
    assert next_node(DEFAULT_FLOW, "menu", "1") == "emergency"


def test_next_node_unknown_digit_falls_back_to_timeout_target():
    assert next_node(DEFAULT_FLOW, "menu", "9") == "ai"


def test_next_node_unknown_node_falls_back_to_start():
    assert next_node(DEFAULT_FLOW, "does-not-exist", "1") == "menu"


# ---------------------------------------------------------- interpolation ---

def test_interpolate_fills_business_and_agent():
    out = interpolate("Hi, this is {agent} from {business}.", FakeTenant())
    assert out == "Hi, this is Alex from Bright Smile Dental."


def test_interpolate_fills_escalation_number():
    assert interpolate("{escalation_number}", FakeTenant()) == "+15551110000"


def test_interpolate_leaves_unknown_placeholder_alone():
    assert "{mystery}" in interpolate("a {mystery} b", FakeTenant())

