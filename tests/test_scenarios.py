"""Guided Scenarios S1–S7 against the Java ERP."""

import inspect

import pytest

from legacybridge.assistant import scenarios as scenarios_module
from legacybridge.assistant.scenarios import REQUIRES_SIM_NOTE, SCENARIOS, SIM_LABEL, get_scenario
from legacybridge.bridge.client import LegacyClient
from legacybridge.bridge.models import DraftLifecycle, OutcomeEvidence, StockStatus, SubmissionOutcome
from legacybridge.service import BridgeService
from legacybridge.ui import components
from tests.conftest import ERP_URL, LOGIN, SECRET, CountingTransport, erp_orders

pytestmark = pytest.mark.live


@pytest.fixture
def no_submit(live_service, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("scenarios must never submit")

    monkeypatch.setattr(live_service, "submit_order", forbidden)
    return live_service


def test_scenarios_never_call_submit_order_statically():
    source = inspect.getsource(scenarios_module)
    assert "submit_order" not in source and "Approval(" not in source and "approve_draft" not in source
    assert [s.id for s in SCENARIOS] == ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    assert [s.workflow for s in SCENARIOS] == ["A", "A", "B", "C", "C", "C", "C"]


def test_s1_customer_360(no_submit):
    run = get_scenario("S1").run(no_submit)
    assert all(step.ok for step in run.steps) and len(run.steps) == 3
    assert [c.account_id for c in run.steps[0].result] == ["00417"]
    assert len(run.steps[1].result.orders) == 3
    assert [o.order_no for o in run.steps[1].result.outstanding] == ["SO-240102", "SO-240103"]
    assert run.steps[2].result.order_no == "SO-240102"


def test_s2_ambiguous_account(no_submit):
    run = get_scenario("S2").run(no_submit)
    matches = run.steps[0].result
    assert [(c.account_id, c.credit_hold) for c in matches] == [("10233", False), ("10871", True)]
    assert "Selection required — the console never auto-picks." in run.notes


def test_s3_stock_truth(no_submit):
    reports = [step.result for step in get_scenario("S3").run(no_submit).steps]
    assert (reports[0].status, reports[0].issues[0].code.value) == (StockStatus.OUT_OF_STOCK, "FLAG_MISMATCH")
    assert (reports[1].status, reports[1].issues[0].code.value) == (StockStatus.UNDETERMINED, "DUPLICATE_SKU")
    assert (reports[2].status, reports[2].known_minimum) == (StockStatus.UNDETERMINED, 45)
    assert (reports[3].status, reports[3].available_quantity) == (StockStatus.IN_STOCK, 1700)
    assert reports[3].warehouses[1].raw_on_hand == "1.200"


def test_s4_pending_draft_then_ui_approval(live_service):
    run = get_scenario("S4").run(live_service)
    draft = live_service.get_draft(run.pending_draft_id)
    assert draft.lifecycle == DraftLifecycle.READY and str(draft.estimated_total) == "48.50"
    result = components.approve_draft(live_service, draft.draft_id, 1)
    assert (result.outcome, result.evidence, result.order_no) == (SubmissionOutcome.CONFIRMED, OutcomeEvidence.READ_BACK, "SO-240111")


def test_s5_prechecks_block(no_submit):
    run = get_scenario("S5").run(no_submit)
    hold, unsafe = (step.result for step in run.steps)
    assert hold.lifecycle == DraftLifecycle.BLOCKED and hold.blocking_reasons == ["Customer account is on credit hold."]
    reasons = [r for line in unsafe.lines for r in line.blocking_reasons]
    assert unsafe.lifecycle == DraftLifecycle.BLOCKED
    assert "Product is discontinued." in reasons and "No reliable price on file." in reasons
    assert any(r.startswith("Availability cannot be established") for r in reasons)
    assert "Insufficient stock: requested 5, available 2." in reasons


def test_s6_stale_draft_rejected(live_service):
    run = get_scenario("S6").run(live_service)
    assert all(step.ok for step in run.steps)
    assert run.steps[1].label.startswith(SIM_LABEL) and run.steps[1].result == "SO-240111"
    assert [o["Ref"] for o in erp_orders("31002") if o["OrderNo"] == "SO-240111"] == ["EXT-000001"]
    result = components.approve_draft(live_service, run.pending_draft_id, 1)
    assert (result.outcome, result.evidence, result.legacy_code) == (SubmissionOutcome.REJECTED, OutcomeEvidence.LEGACY_REJECTION, "INSUFFICIENT_STOCK")
    assert "SHORT:BW-1010:0" in result.evidence_detail


def test_s7_unreliable_write(live_service):
    run = get_scenario("S7").run(live_service)
    assert run.steps[0].label.startswith(SIM_LABEL)
    result = components.approve_draft(live_service, run.pending_draft_id, 1)
    assert (result.outcome, result.evidence, result.e8_requests) == (SubmissionOutcome.CONFIRMED, OutcomeEvidence.RECONCILED, 1)
    assert len([o for o in erp_orders("52290") if (o["Ref"] or "").strip() == run.pending_draft_id]) == 1


@pytest.mark.parametrize("scenario_id", ["S6", "S7"])
def test_s6_s7_without_simulator(scenario_id):
    service = BridgeService(LegacyClient(ERP_URL, LOGIN, SECRET, transport=CountingTransport()), None)
    run = get_scenario(scenario_id).run(service)
    assert REQUIRES_SIM_NOTE in run.notes and run.pending_draft_id is None
    assert all(step.ok for step in run.steps)
