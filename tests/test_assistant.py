"""Tools and the optional agent."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from legacybridge.assistant import agent as agent_module
from legacybridge.assistant.agent import assistant_available, build_agent, run_assistant
from legacybridge.assistant.tools import make_tools
from legacybridge.config import BridgeSettings
from tests.conftest import ERP_URL, LOGIN, SECRET, CountingTransport

SRC = Path(__file__).resolve().parents[1] / "src" / "legacybridge" / "assistant"
NAMES = ["find_customers", "get_customer_overview", "get_order", "search_products", "check_availability", "prepare_order_draft"]
FORBIDDEN = ["submit_order", "create_order", "simulate_external_order", "sim_", "reset_demo", "arm_fault", "SimControl",
             "Approval(", "post_order_once"]
DRAFT_KEYS = ["draft_id", "account_id", "customer_name", "lines", "estimated_total", "status", "blocking_reasons",
              "created_at", "submission", "history", "lifecycle", "eligible"]
LINE_KEYS = ["sku", "product_name", "quantity", "unit_price", "line_total", "availability", "available_quantity", "blocking_reasons"]


class ScriptedChatModel(BaseChatModel):
    """Replays a fixed list of AI messages; used to test the agent wiring without a real model."""

    script: list[AIMessage]
    state: dict = Field(default_factory=lambda: {"i": 0})

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = self.script[self.state["i"]]
        self.state["i"] += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


def call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])


def tools_by_name(service):
    return {t.name: t for t in make_tools(service)}


# --------------------------------------------------------------------------- static / offline


def test_tool_names_and_schemas(fake_service):
    tools = make_tools(fake_service)
    assert [t.name for t in tools] == NAMES
    schemas = {t.name: t.args_schema.model_json_schema() for t in tools}
    assert list(schemas["find_customers"]["properties"]) == ["query"]
    assert list(schemas["get_customer_overview"]["properties"]) == ["account_id"]
    assert list(schemas["get_order"]["properties"]) == ["order_no"]
    assert list(schemas["search_products"]["properties"]) == ["query", "category"]
    assert list(schemas["check_availability"]["properties"]) == ["sku"]
    assert list(schemas["prepare_order_draft"]["properties"]) == ["account_id", "lines"]
    for tool in tools:
        assert "submit" not in tool.name.lower() and "submit" not in tool.description.lower()


def test_forbidden_strings_absent():
    for name in ("tools.py", "agent.py"):
        text = (SRC / name).read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            assert needle not in text, (name, needle)


@pytest.mark.parametrize(
    "name, args",
    [("find_customers", {"query": ""}), ("get_order", {"order_no": "X"}),
     ("prepare_order_draft", {"account_id": "00417", "lines": [{"sku": "BW-1001", "quantity": 0}]}),
     ("get_customer_overview", {"account_id": "ABC"}), ("search_products", {"category": "Snacks"})],
)
def test_invalid_input_envelopes(fake_service, name, args):
    envelope = json.loads(tools_by_name(fake_service)[name].invoke(args))
    assert envelope["ok"] is False and envelope["error"]["code"] == "INVALID_INPUT" and envelope["error"]["retryable"] is False


def test_assistant_available_without_key():
    ok, reason = assistant_available(BridgeSettings())
    assert ok is False and "ANTHROPIC_API_KEY" in reason
    assert assistant_available(BridgeSettings(anthropic_api_key_present=True))[0] is True
    assert assistant_available(BridgeSettings(llm_model="openai:x"))[0] is False
    assert assistant_available(BridgeSettings(llm_model="openai:x", llm_enable=True))[0] is True


def test_system_prompt_is_short_and_has_no_sampling_parameters():
    assert len(agent_module.SYSTEM_PROMPT.splitlines()) <= 25
    source = (SRC / "agent.py").read_text(encoding="utf-8")
    for needle in ("temperature", "top_p", "top_k", "tool_choice", "response_format", "checkpointer", "middleware", "thinking"):
        assert needle not in source, needle
    assert "max_tokens=4096, timeout=60, max_retries=1" in source


# --------------------------------------------------------------------------- live


@pytest.fixture
def mode_a_guard(monkeypatch):
    """Mode A must never construct a chat model."""
    import langchain.chat_models

    def boom(*args, **kwargs):
        raise AssertionError("a chat model was constructed in Mode A")

    monkeypatch.setattr(agent_module, "build_chat_model", boom)
    monkeypatch.setattr(langchain.chat_models, "init_chat_model", boom)


@pytest.mark.live
def test_legacy_unavailable_envelope_is_retryable(live_service, sim, mode_a_guard):
    live_service.client._sleep = lambda s: None
    sim.arm_fault("GET", "/halden/customers", "error_500", 3)
    envelope = json.loads(tools_by_name(live_service)["find_customers"].invoke({"query": "nordkap"}))
    assert envelope == {"ok": False, "error": {"code": "LEGACY_UNAVAILABLE", "message": envelope["error"]["message"], "retryable": True}}


@pytest.mark.live
def test_f02_tool_exact_envelopes(live_service, mode_a_guard):
    tool = tools_by_name(live_service)["prepare_order_draft"]
    ready = json.loads(tool.invoke({"account_id": "00417", "lines": [{"sku": "BW-1001", "quantity": 10}]}))
    blocked = json.loads(tool.invoke({"account_id": "10871", "lines": [{"sku": "BW-1001", "quantity": 1}]}))
    for envelope, status in ((ready, "READY"), (blocked, "BLOCKED")):
        assert list(envelope) == ["ok", "data"] and envelope["ok"] is True
        data = envelope["data"]
        assert list(data) == DRAFT_KEYS
        assert [list(line) for line in data["lines"]] == [LINE_KEYS]
        assert data["status"] == data["lifecycle"] == status and data["eligible"] is (status == "READY")
        assert data["submission"] is None and data["history"] == []
        assert re.fullmatch(r"LB-[0-9A-F]{8}", data["draft_id"])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00", data["created_at"])
    r = ready["data"]
    assert (r["account_id"], r["customer_name"], r["estimated_total"], r["blocking_reasons"]) == ("00417", "Nordkap Marine Services AS", "48.50", [])
    assert r["lines"][0] == {"sku": "BW-1001", "product_name": "Deep Groove Ball Bearing 6205-2RS", "quantity": 10, "unit_price": "4.85",
                             "line_total": "48.50", "availability": "IN_STOCK", "available_quantity": 300, "blocking_reasons": []}
    b = blocked["data"]
    assert (b["account_id"], b["customer_name"], b["blocking_reasons"]) == ("10871", "Alder & Finch Fabrication Ltd", ["Customer account is on credit hold."])


@pytest.mark.live
def test_f03_2_tools_share_identity_and_verdict(live_service, mode_a_guard):
    tools = tools_by_name(live_service)
    products = json.loads(tools["search_products"].invoke({"query": "legacy item"}))["data"]["products"]
    assert [p["sku"] for p in products] == ["BW-1009"] and products[0]["record_ids"] == [9, 12]
    assert products[0] == json.loads(json.dumps(live_service.get_product("BW-1009").model_dump(mode="json")))
    report = json.loads(tools["check_availability"].invoke({"sku": products[0]["sku"]}))["data"]
    assert report["status"] == "UNDETERMINED"
    draft = json.loads(tools["prepare_order_draft"].invoke({"account_id": "52290", "lines": [{"sku": "BW-1009", "quantity": 1}]}))
    assert draft["ok"] is True and draft["data"]["status"] == "BLOCKED"


@pytest.mark.live
def test_scripted_replay_matches_direct_service_results(live_service, mode_a_guard):
    model = ScriptedChatModel(script=[
        call("find_customers", {"query": "nordkap"}, "c1"),
        call("get_customer_overview", {"account_id": "00417"}, "c2"),
        AIMessage(content="Nordkap Marine Services AS (00417) has 2 outstanding orders."),
    ])
    run = run_assistant(build_agent(model, make_tools(live_service)), "What is outstanding for Nordkap?")
    assert run.error is None
    assert [r.name for r in run.tool_calls] == ["find_customers", "get_customer_overview"]
    assert all(r.ok for r in run.tool_calls)
    assert run.tool_calls[0].data == {"matches": [c.model_dump(mode="json") for c in live_service.search_customers("nordkap")], "ambiguous": False}
    assert run.tool_calls[1].data == live_service.get_customer_overview("00417").model_dump(mode="json")
    assert run.answer_text == "Nordkap Marine Services AS (00417) has 2 outstanding orders."


@pytest.mark.live
def test_scripted_prepare_creates_shared_draft_without_e8(sim, mode_a_guard):
    from legacybridge.bridge.client import LegacyClient
    from legacybridge.service import BridgeService

    transport = CountingTransport()
    service = BridgeService(LegacyClient(ERP_URL, LOGIN, SECRET, transport=transport), sim)
    model = ScriptedChatModel(script=[
        call("prepare_order_draft", {"account_id": "00417", "lines": [{"sku": "BW-1001", "quantity": 10}]}, "c1"),
        AIMessage(content="I prepared a draft. Please review and approve it in the Order Desk."),
    ])
    run = run_assistant(build_agent(model, make_tools(service)), "Book 10 BW-1001 for Nordkap")
    assert len(run.draft_ids) == 1
    assert service.get_draft(run.draft_ids[0]).lifecycle.value == "READY"
    assert transport.e8_count == 0 and service.client.e8_request_count == 0
