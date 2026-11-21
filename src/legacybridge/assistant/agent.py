"""Optional LLM assistant (Mode B). Provider-specific code lives only here.

Mode A never constructs a chat model. Real-model behavior has not been evaluated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..config import BridgeSettings

SYSTEM_PROMPT = """You are the Brightwater Industrial Supply operations assistant for the HALDEN ERP 3.1 order desk.
Use the tools for every business fact. Never state a price, quantity, status or account that a tool did not return.
When find_customers returns several matches, list them and ask the user to choose; never pick one yourself.
State availability only from check_availability; the legacy availability flag is unreliable.
You cannot place orders. To help with an order, call prepare_order_draft and tell the user to review and
approve the draft in the console's Order Desk. Never say that an order was placed or booked.
Draft prices are estimates; HALDEN prices the booking.
If a tool returns an error, report it plainly and do not guess.
Keep answers short and factual."""


@dataclass
class ToolCallRecord:
    name: str
    args: dict[str, Any]
    ok: bool
    data: Any = None
    error: dict[str, Any] | None = None


@dataclass
class AssistantRun:
    question: str
    answer_text: str = ""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    draft_ids: list[str] = field(default_factory=list)
    error: str | None = None


def assistant_available(settings: BridgeSettings) -> tuple[bool, str]:
    model = settings.llm_model
    if model.startswith("anthropic:"):
        if settings.anthropic_api_key_present:
            return True, f"AI assistant · {model}"
        return False, "ANTHROPIC_API_KEY is not set; the deterministic Mode A remains fully available."
    if settings.llm_enable:
        return True, f"AI assistant · {model} (non-Anthropic provider; untested)"
    return False, f"LLM_MODEL {model!r} is not an Anthropic model and LLM_ENABLE is not true."


def build_chat_model(settings: BridgeSettings):
    from langchain.chat_models import init_chat_model

    return init_chat_model(settings.llm_model, max_tokens=4096, timeout=60, max_retries=1)


def build_agent(model, tools):
    from langchain.agents import create_agent

    return create_agent(model=model, tools=tools, system_prompt=SYSTEM_PROMPT)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        ) + "".join(block for block in content if isinstance(block, str))
    return ""


def run_assistant(agent, question: str) -> AssistantRun:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    run = AssistantRun(question=question)
    try:
        state = agent.invoke({"messages": [HumanMessage(question)]}, {"recursion_limit": 12})
        messages = state.get("messages", []) if isinstance(state, dict) else []
        results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
        for message in messages:
            if isinstance(message, AIMessage):
                for call in message.tool_calls or []:
                    tool_message = results.get(call.get("id"))
                    envelope: dict[str, Any] = {}
                    if tool_message is not None:
                        try:
                            envelope = json.loads(_text_of(tool_message.content))
                        except ValueError:
                            envelope = {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "unparseable tool result", "retryable": False}}
                    record = ToolCallRecord(
                        name=call.get("name", ""),
                        args=dict(call.get("args") or {}),
                        ok=bool(envelope.get("ok")),
                        data=envelope.get("data"),
                        error=envelope.get("error"),
                    )
                    run.tool_calls.append(record)
                    if record.ok and record.name == "prepare_order_draft" and isinstance(record.data, dict):
                        run.draft_ids.append(record.data.get("draft_id"))
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        run.answer_text = _text_of(last_ai.content) if last_ai is not None else ""
    except Exception as exc:  # never raised to the UI
        run.error = f"{type(exc).__name__}: {exc}"
    return run
