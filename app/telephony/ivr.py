"""
IVR: a JSON-defined call flow that runs *before* the AI picks up.

Why a business still wants a menu in 2026: pressing 1 for an emergency is
instant and never mis-hears, while an AI has to listen to a sentence first.
The winning pattern is a very short menu whose default branch is the AI --
"press 1 for emergencies, or just tell me what you need".

A flow is stored as JSON on the tenant so it can be edited from the dashboard
without a deploy. That is the thing Twilio Studio gives people, done in a way
you own.
"""
from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger()

# A node is:
#   {"say": "...", "gather": {"1": "node_id", ...}, "timeout_goto": "node_id"}
#   {"say": "...", "transfer": "+1555..."}
#   {"say": "...", "ai": true}            -> hand off to the voice pipeline
#   {"say": "...", "voicemail": true}
#   {"say": "...", "hangup": true}

DEFAULT_FLOW: dict[str, Any] = {
    "start": "menu",
    "nodes": {
        "menu": {
            "say": (
                "Thanks for calling. If this is a medical emergency, press 1. "
                "Otherwise, just tell me what you need and I'll help."
            ),
            "gather": {"1": "emergency", "2": "hours"},
            "timeout_goto": "ai",
            "timeout_seconds": 3,
        },
        "emergency": {"say": "Connecting you now.", "transfer": "{escalation_number}"},
        "hours": {"say": "{hours_line}", "goto": "ai"},
        "ai": {"ai": True},
    },
}


class FlowError(ValueError):
    pass


# --------------------------------------------------------------- validation ---

def validate_flow(flow: dict[str, Any]) -> list[str]:
    """Return a list of problems. Empty list means the flow is safe to run."""
    problems: list[str] = []
    nodes = flow.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return ["flow has no nodes"]

    start = flow.get("start")
    if start not in nodes:
        problems.append(f"start node '{start}' does not exist")

    for name, node in nodes.items():
        if not isinstance(node, dict):
            problems.append(f"node '{name}' is not an object")
            continue

        targets = list((node.get("gather") or {}).values())
        for key in ("goto", "timeout_goto"):
            if node.get(key):
                targets.append(node[key])
        for target in targets:
            if target not in nodes:
                problems.append(f"node '{name}' points at missing node '{target}'")

        terminal = any(node.get(k) for k in ("ai", "hangup", "voicemail", "transfer"))
        if not terminal and not targets:
            problems.append(f"node '{name}' is a dead end")

    # A flow that can never reach the AI or a human is a trap for the caller.
    reachable = _reachable(flow)
    if not any(
        nodes[n].get(k) for n in reachable for k in ("ai", "transfer", "voicemail")
        if n in nodes
    ):
        problems.append("no reachable node hands off to the AI, a human, or voicemail")

    orphans = set(nodes) - reachable
    if orphans:
        problems.append(f"unreachable nodes: {', '.join(sorted(orphans))}")

    return problems


def _reachable(flow: dict[str, Any]) -> set[str]:
    nodes = flow.get("nodes", {})
    start = flow.get("start")
    if start not in nodes:
        return set()
    seen, stack = set(), [start]
    while stack:
        current = stack.pop()
        if current in seen or current not in nodes:
            continue
        seen.add(current)
        node = nodes[current]
        stack.extend((node.get("gather") or {}).values())
        for key in ("goto", "timeout_goto"):
            if node.get(key):
                stack.append(node[key])
    return seen


# ------------------------------------------------------------- interpolation ---

def interpolate(text: str, tenant) -> str:
    """Fill {placeholders} from the tenant so flows stay reusable across clients."""
    values = {
        "business": tenant.name,
        "agent": tenant.agent_name,
        "escalation_number": tenant.escalation_number or "",
        "hours_line": (
            f"We're open {tenant.business_open:%-I:%M %p} "
            f"to {tenant.business_close:%-I:%M %p}."
        ),
    }
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


# --------------------------------------------------------------------- run ---

def render_node(flow: dict[str, Any], node_id: str, tenant, *, ws_url: str) -> str:
    """Turn one node into TwiML."""
    from twilio.twiml.voice_response import Connect, Gather, VoiceResponse

    nodes = flow.get("nodes", {})
    node = nodes.get(node_id)
    response = VoiceResponse()

    if node is None:
        log.warning("ivr.missing_node", node=node_id)
        response.say("Sorry, something went wrong.")
        response.hangup()
        return str(response)

    say_text = interpolate(node.get("say", ""), tenant) if node.get("say") else ""

    # Terminal: hand the call to the AI voice pipeline.
    if node.get("ai"):
        if say_text:
            response.say(say_text)
        connect = Connect()
        connect.stream(url=ws_url)
        response.append(connect)
        return str(response)

    if node.get("transfer"):
        target = interpolate(str(node["transfer"]), tenant)
        if say_text:
            response.say(say_text)
        if target:
            response.dial(target, answer_on_bridge=True)
        else:
            response.say("No one is available. Please leave a message.")
            response.record(max_length=90, play_beep=True)
        response.hangup()
        return str(response)

    if node.get("voicemail"):
        response.say(say_text or "Please leave a message after the tone.")
        response.record(max_length=120, play_beep=True)
        response.hangup()
        return str(response)

    if node.get("hangup"):
        if say_text:
            response.say(say_text)
        response.hangup()
        return str(response)

    # Menu node.
    if node.get("gather"):
        gather = Gather(
            num_digits=1,
            timeout=int(node.get("timeout_seconds", 4)),
            action=f"?node={node_id}",
            method="POST",
        )
        if say_text:
            gather.say(say_text)
        response.append(gather)
        # No key pressed -> fall through instead of hanging up on the caller.
        fallback = node.get("timeout_goto") or flow.get("start")
        response.redirect(f"?node={fallback}", method="POST")
        return str(response)

    # Plain say-then-continue node.
    if say_text:
        response.say(say_text)
    if node.get("goto"):
        response.redirect(f"?node={node['goto']}", method="POST")
    else:
        response.hangup()
    return str(response)


def next_node(flow: dict[str, Any], node_id: str, digit: str) -> str:
    """Resolve a keypress to the next node id."""
    node = flow.get("nodes", {}).get(node_id, {})
    gather = node.get("gather") or {}
    return gather.get(str(digit)) or node.get("timeout_goto") or flow.get("start", "start")

