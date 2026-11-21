"""Console views with Streamlit AppTest against the Java ERP."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from legacybridge.bridge.client import LegacyClient
from legacybridge.ui import runtime
from tests.conftest import ERP_URL, LOGIN, SECRET, CountingTransport, erp_orders

pytestmark = pytest.mark.live
APP = str(Path(__file__).resolve().parents[1] / "src" / "legacybridge" / "ui" / "app.py")
VIEWS = ["Overview", "Customers & Orders", "Inventory", "Order Desk", "Assistant"]


@pytest.fixture
def shared_service(sim, monkeypatch):
    from legacybridge.service import BridgeService

    transport = CountingTransport()
    service = BridgeService(LegacyClient(ERP_URL, LOGIN, SECRET, transport=transport), sim)
    monkeypatch.setattr(runtime, "build_service", lambda: service)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    runtime._service_resource.clear()
    yield service, transport
    runtime._service_resource.clear()


def app() -> AppTest:
    return AppTest.from_file(APP, default_timeout=30).run(timeout=30)


def goto(at: AppTest, view: str) -> AppTest:
    at.sidebar.radio(key="view").set_value(view).run(timeout=30)
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_five_views_render(shared_service):
    at = app()
    assert not at.exception
    assert at.sidebar.radio(key="view").options == VIEWS
    sidebar_text = " ".join(m.value for m in at.sidebar.markdown)
    assert "HALDEN ERP 3.1 — simulated legacy system (Java 8 · Tomcat 9 · JDBC/SQLite)" in sidebar_text
    assert "Local single-operator demo — approvals are not tied to an authenticated user." in [i.value for i in at.sidebar.info]
    for view in VIEWS:
        goto(at, view)
        assert at.header[0].value == view


def test_overview_metrics_and_label(shared_service):
    at = app()
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Outstanding orders"] == "6"
    assert metrics["Undelivered order value (header totals)"] == "€943.50"
    assert (metrics["Customers"], metrics["On credit hold"], metrics["Products"], metrics["Stock undetermined"]) == ("8", "2", "11", "3")


def test_inventory_shows_pooled_caption(shared_service):
    at = goto(app(), "Inventory")
    assert any("pooled allocation across DUS and MAN" in c.value for c in at.caption)


def test_order_desk_approve_and_reused_approval(shared_service):
    service, transport = shared_service
    first = goto(app(), "Order Desk")
    first.button(key="prepare_draft").click().run(timeout=30)
    approve = [b for b in first.button if b.label == "Approve attempt 1 and send to HALDEN"]
    assert len(approve) == 1
    second = goto(app(), "Order Desk")  # a second browser session showing the same READY draft
    stale = [b for b in second.button if b.label == "Approve attempt 1 and send to HALDEN"]
    assert len(stale) == 1
    approve[0].click().run(timeout=30)
    assert not first.exception
    draft = service.list_drafts()[-1]
    assert draft.lifecycle.value == "CONFIRMED" and draft.submission.order_no == "SO-240111"
    assert any("SO-240111" in s.value for s in first.success)
    assert "SO-240111" in [o["OrderNo"] for o in erp_orders("00417")]
    stale[0].click().run(timeout=30)
    assert "This approval was already used." in [e.value for e in second.error]
    assert transport.e8_count == 1


def test_assistant_view_mode_a_without_key(shared_service, monkeypatch):
    from legacybridge.assistant import agent

    def boom(*args, **kwargs):
        raise AssertionError("chat model constructed in Mode A")

    monkeypatch.setattr(agent, "build_chat_model", boom)
    at = goto(app(), "Assistant")
    assert "Deterministic scenario — no AI model involved." in [i.value for i in at.info]
    assert any(i.value.startswith("AI assistant unavailable:") for i in at.info)
    at.button(key="run-S1").click().run(timeout=30)
    assert not at.exception
