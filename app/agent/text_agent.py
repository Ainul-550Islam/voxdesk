"""
The text brain: one LLM loop shared by SMS, WhatsApp and web chat.

The voice pipeline (pipecat) cannot be reused here -- it is a streaming audio
graph. Text channels are request/response, so they get their own small agent
that reuses the *same* tools, the same prompt rules and the same LLM presets.
That way a business gets identical answers whether the customer calls or texts.
"""
from __future__ import annotations

import json
from typing import Any

import structlog

from app.agent.functions import TOOL_SCHEMAS as FUNCTION_SCHEMAS, FunctionHandlers
from app.agent.llm_factory import resolve
from app.agent.prompts import build_system_prompt

log = structlog.get_logger()

MAX_TOOL_ROUNDS = 4          # stop runaway tool loops
SMS_SAFE_LENGTH = 300        # keep replies inside 2 SMS segments


# ------------------------------------------------------------ text shaping ---

def shape_for_channel(text: str, channel: str) -> str:
    """Voice rules and text rules differ. Do not send 25-word voice text to SMS."""
    text = (text or "").strip()
    if channel == "sms":
        if len(text) > SMS_SAFE_LENGTH:
            cut = text[:SMS_SAFE_LENGTH].rsplit(". ", 1)[0]
            text = (cut + "." if cut else text[:SMS_SAFE_LENGTH - 1] + "\u2026")
    elif channel == "whatsapp":
        # WhatsApp allows 4096 chars and renders *bold*; nothing to trim.
        pass
    return text


def channel_rules(channel: str, business: str) -> str:
    if channel == "sms":
        return (
            "\n\nCHANNEL: SMS.\n"
            "- Keep replies under 300 characters. Texts cost money.\n"
            "- No markdown, no emoji, no bullet lists.\n"
            "- Never send more than one message per reply.\n"
            f"- Sign off only on the first message of a thread: '- {business}'.\n"
        )
    if channel == "whatsapp":
        return (
            "\n\nCHANNEL: WhatsApp.\n"
            "- Conversational and warm, but under 4 sentences.\n"
            "- You may use *bold* for a time or a price. No headers, no tables.\n"
            "- One question at a time.\n"
        )
    return (
        "\n\nCHANNEL: Web chat.\n"
        "- Short paragraphs. You may use short bullet lists.\n"
    )


# ------------------------------------------------------------------ client ---

def _openai_style_client(provider: str, api_key: str):
    """OpenAI and Google (via its OpenAI-compatible endpoint) share this path."""
    from openai import AsyncOpenAI
    if provider == "google":
        return AsyncOpenAI(
            api_key=api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
    return AsyncOpenAI(api_key=api_key)


async def _complete_openai(client, model, messages, tools, temperature):
    resp = await client.chat.completions.create(
        model=model, messages=messages, tools=tools,
        tool_choice="auto", temperature=temperature, max_tokens=400,
    )
    msg = resp.choices[0].message
    calls = [
        {"id": c.id, "name": c.function.name, "args": json.loads(c.function.arguments or "{}")}
        for c in (msg.tool_calls or [])
    ]
    return msg.content or "", calls, msg


async def _complete_anthropic(api_key, model, system, messages, tools, temperature):
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=api_key)
    resp = await client.messages.create(
        model=model, system=system, messages=messages, max_tokens=400,
        temperature=temperature,
        tools=[
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "input_schema": t["function"]["parameters"],
            }
            for t in tools
        ],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    calls = [
        {"id": b.id, "name": b.name, "args": b.input}
        for b in resp.content if b.type == "tool_use"
    ]
    return text, calls, resp


# ------------------------------------------------------------------- agent ---

class TextAgent:
    """Stateless per-request; history is passed in and returned."""

    def __init__(self, tenant, handlers: FunctionHandlers, channel: str = "sms"):
        self.tenant = tenant
        self.handlers = handlers
        self.channel = channel
        self.provider, self.model, self.api_key = resolve(tenant)

    def system_prompt(self) -> str:
        base = build_system_prompt(self.tenant, provider=self.provider)
        # The voice prompt forbids long answers in *words*; text needs its own cap.
        base = base.replace("Maximum 25 words", "Maximum 45 words")
        return base + channel_rules(self.channel, self.tenant.name)

    async def reply(self, history: list[dict[str, Any]], user_text: str) -> dict:
        """history is [{'role':'user'|'assistant','content':str}, ...]"""
        system = self.system_prompt()
        messages = list(history) + [{"role": "user", "content": user_text}]
        used_tools: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS):
            if self.provider == "anthropic":
                text, calls, _raw = await _complete_anthropic(
                    self.api_key, self.model, system, messages,
                    FUNCTION_SCHEMAS, self.tenant.temperature,
                )
                if not calls:
                    break
                messages.append({"role": "assistant", "content": [
                    {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                    for c in calls
                ]})
                results = []
                for c in calls:
                    used_tools.append(c["name"])
                    out = await self.handlers.dispatch(c["name"], c["args"])
                    results.append({
                        "type": "tool_result", "tool_use_id": c["id"],
                        "content": json.dumps(out),
                    })
                messages.append({"role": "user", "content": results})
            else:
                client = _openai_style_client(self.provider, self.api_key)
                convo = [{"role": "system", "content": system}] + messages
                text, calls, raw = await _complete_openai(
                    client, self.model, convo, FUNCTION_SCHEMAS, self.tenant.temperature
                )
                if not calls:
                    break
                messages.append(raw.model_dump(exclude_none=True))
                for c in calls:
                    used_tools.append(c["name"])
                    out = await self.handlers.dispatch(c["name"], c["args"])
                    messages.append({
                        "role": "tool", "tool_call_id": c["id"],
                        "content": json.dumps(out),
                    })
        else:
            text = "Let me have someone get back to you on that."

        reply_text = shape_for_channel(text, self.channel)
        log.info("text_agent.reply", channel=self.channel,
                 provider=self.provider, tools=used_tools)
        return {
            "reply": reply_text,
            "tools_used": used_tools,
            "provider": self.provider,
            "model": self.model,
        }

