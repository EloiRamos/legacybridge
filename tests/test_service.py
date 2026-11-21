"""BridgeService against the running Java ERP (F01-*, F02-LV, F03-*)."""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from legacybridge.bridge.client import LegacyClient
from legacybridge.bridge.errors import LegacyMalformedResponse, NotFoundError
from legacybridge.bridge.models import (
    Approval,
    DraftLifecycle,
    DraftLine,
    IssueCode,
    OrderStatus,
    OutcomeEvidence,
    ResultKind,
    StockStatus,
    SubmissionOutcome,
)
from legacybridge.service import BridgeService
from tests.conftest import (
    ERP_URL,
    LOGIN,
    SECRET,
    SEED_STOCK,
    CountingTransport,
    erp_orders,
    override_transport,
    raw_e8,
    stock_response,
    verification_client,
)

pytestmark = pytest.mark.live


def approve(service: BridgeService, draft_id: str, n: int = 1):
    return service.submit_order(draft_id, Approval(draft_id=draft_id, attempt_no=n, approved_at=datetime.now(timezone.utc)))


def make_service(sim, *, timeout=5.0, transport=None) -> tuple[BridgeService, CountingTransport]:
    transport = transport or CountingTransport()
    return BridgeService(LegacyClient(ERP_URL, LOGIN, SECRET, timeout=timeout, transport=transport), sim), transport


def refs(order_no_ref: str) -> list[dict]:
    return [o for o in erp_orders() if (o["Ref"] or "").strip() == order_no_ref]


def all_reasons(draft) -> list[str]:
    return list(draft.blocking_reasons) + [r for line in draft.lines for r in line.blocking_reasons]


# =============================================================================== oracle facts


def test_customers_and_names(live_service):
    customers = live_service.search_customers("")
    assert len(customers) == 8
    assert sorted(c.account_id for c in customers if c.credit_hold) == ["04450", "10871"]
    nordkap = live_service.get_customer("417")
    assert (nordkap.name, nordkap.contact_name, nordkap.email) == ("Nordkap Marine Services AS", "Erik Holm", "erik.holm@nordkap-marine.example")
    alder = live_service.get_customer("10871")
    assert (alder.name, alder.contact_name) == ("Alder & Finch Fabrication Ltd", "Priya Raman")
    castell = live_service.get_customer("20514")
    assert castell.email is None and [i.code for i in castell.issues] == [IssueCode.INVALID_EMAIL] and castell.city == "Düsseldorf"
    with pytest.raises(NotFoundError):
        live_service.get_customer("99999")


def test_ambiguous_and_numeric_search(live_service):
    assert [c.account_id for c in live_service.search_customers("alder")] == ["10233", "10871"]
    assert [c.account_id for c in live_service.search_customers("417")] == ["00417"]


def test_orders_statuses_outstanding_and_totals(live_service):
    orders = {o.order_no: o for o in live_service.list_orders()}
    expected = {101: "DELIVERED", 102: "SHIPPED", 103: "OPEN", 104: "DELIVERED", 105: "ALLOCATED", 106: "CANCELLED",
                107: "ON_HOLD", 108: "SHIPPED", 109: "OPEN", 110: "UNKNOWN"}
    assert {k: o.status.value for k, o in orders.items()} == {f"SO-240{n}": v for n, v in expected.items()}
    assert [i.code for i in orders["SO-240110"].issues] == [IssueCode.UNKNOWN_STATUS_CODE]
    assert sorted(o for o, s in orders.items() if s.is_outstanding) == ["SO-240102", "SO-240103", "SO-240105", "SO-240107", "SO-240108", "SO-240109"]
    overview = live_service.get_customer_overview("00417")
    assert len(overview.orders) == 3
    assert [(o.order_no, o.status) for o in overview.outstanding] == [("SO-240102", OrderStatus.SHIPPED), ("SO-240103", OrderStatus.OPEN)]
    assert live_service.get_order("SO-240104").total == Decimal("1249.00")
    assert live_service.get_order("SO-240102").total == Decimal("172.40")
    mismatch = live_service.get_order("so-240109")
    assert (mismatch.total, mismatch.lines_total) == (Decimal("100.00"), Decimal("92.40"))
    assert [i.code for i in mismatch.issues] == [IssueCode.TOTAL_MISMATCH]
    assert orders["SO-240101"].order_date == date(2025, 9, 2)
    with pytest.raises(NotFoundError):
        live_service.get_order("SO-999999")


PRICES = {"BW-1001": "4.85", "BW-1002": "12.40", "BW-1003": "1249.00", "BW-1004": "86.20", "BW-1005": "9.90", "BW-1006": "6.75",
          "BW-1007": "189.00", "BW-1008": None, "BW-1009": None, "BW-1010": "118.50", "BW-1011": "23.10"}
CATEGORIES = {"BW-1001": "Bearings", "BW-1002": "Bearings", "BW-1003": "Hydraulics", "BW-1009": "Hydraulics", "BW-1004": "Valves",
              "BW-1005": "PPE", "BW-1006": "PPE", "BW-1007": "Power Tools", "BW-1008": "Hand Tools", "BW-1010": "Lubricants",
              "BW-1011": "Uncategorized"}
AVAILABILITY = {"BW-1001": ("IN_STOCK", 300, []), "BW-1002": ("OUT_OF_STOCK", 0, ["FLAG_MISMATCH"]),
                "BW-1003": ("IN_STOCK", 3, ["FLAG_MISMATCH"]), "BW-1004": ("IN_STOCK", 19, []),
                "BW-1005": ("UNDETERMINED", None, ["UNKNOWN_QUANTITY"]), "BW-1006": ("IN_STOCK", 1700, []),
                "BW-1007": ("IN_STOCK", 12, []), "BW-1008": ("IN_STOCK", 7, []), "BW-1009": ("UNDETERMINED", None, ["DUPLICATE_SKU"]),
                "BW-1010": ("IN_STOCK", 2, []), "BW-1011": ("UNDETERMINED", None, ["NEGATIVE_QUANTITY"])}


def test_products_prices_categories_and_availability(live_service):
    products = {p.sku: p for p in live_service.search_products()}
    assert len(products) == 11
    assert {k: (str(p.unit_price) if p.unit_price is not None else None) for k, p in products.items()} == PRICES
    assert {k: p.category for k, p in products.items()} == CATEGORIES
    assert [i.code for i in products["BW-1008"].issues] == [IssueCode.MISSING_PRICE]
    assert {i.code for i in products["BW-1009"].issues} == {IssueCode.DUPLICATE_SKU, IssueCode.CONFLICTING_PRICE}
    assert [i.code for i in products["BW-1011"].issues] == [IssueCode.MISSING_CATEGORY]
    assert products["BW-1007"].discontinued
    for sku, (status, available, issues) in AVAILABILITY.items():
        report = live_service.check_availability(sku)
        assert (report.status.value, report.available_quantity, [i.code.value for i in report.issues]) == (status, available, issues), sku
    assert live_service.check_availability("BW-1005").known_minimum == 45
    catalogue = live_service.product_catalogue()
    assert {p.sku: r.status.value for p, r in catalogue} == {k: v[0] for k, v in AVAILABILITY.items()}


def test_overview_exact(live_service):
    m = live_service.overview()
    assert (m.customer_count, m.credit_hold_count, m.order_count, m.outstanding_order_count, m.outstanding_order_value,
            m.needs_review_order_count, m.product_count) == (8, 2, 10, 6, Decimal("943.50"), 1, 11)
    assert m.stock_status_counts == {StockStatus.IN_STOCK: 7, StockStatus.OUT_OF_STOCK: 1, StockStatus.UNDETERMINED: 3}
    assert {k.value: v for k, v in m.issue_counts.items()} == {
        "FLAG_MISMATCH": 2, "DUPLICATE_SKU": 1, "CONFLICTING_PRICE": 1, "MISSING_PRICE": 1, "UNKNOWN_QUANTITY": 1,
        "NEGATIVE_QUANTITY": 1, "MISSING_CATEGORY": 1, "INVALID_EMAIL": 1, "UNKNOWN_STATUS_CODE": 1}
    assert len(m.outstanding_orders) == 6


def test_request_budget(sim):
    service, transport = make_service(sim)
    service.check_availability("BW-1001")
    assert (transport.count("GET", "/halden/items"), transport.count("GET", "/halden/stock")) == (3, 1)
    transport.reset()
    service.prepare_order("00417", [("BW-1001", 10)])
    assert (transport.count("GET", "/halden/customers"), transport.count("GET", "/halden/items"), transport.count("GET", "/halden/stock")) == (1, 3, 1)
    transport.reset()
    service.overview()
    assert (transport.count("GET", "/halden/customers"), transport.count("GET", "/halden/items"),
            transport.count("GET", "/halden/orders"), transport.count("GET", "/halden/stock")) == (2, 3, 4, 1)


# =============================================================================== identity, preparation, rejection


def test_f03_1_complete_snapshot_identity(live_service):
    found = live_service.search_products("legacy item")
    assert [p.sku for p in found] == ["BW-1009"]
    assert found[0].record_ids == [9, 12] and found[0].unit_price is None
    assert {IssueCode.DUPLICATE_SKU, IssueCode.CONFLICTING_PRICE} <= {i.code for i in found[0].issues}
    assert found[0] == live_service.get_product("BW-1009")
    assert live_service.check_availability(found[0].sku).status == StockStatus.UNDETERMINED
    blocked = live_service.prepare_order("52290", [("BW-1009", 1)])
    assert blocked.lifecycle == DraftLifecycle.BLOCKED
    reasons = all_reasons(blocked)
    assert "No reliable price on file." in reasons
    assert any(r.startswith("Availability cannot be established: ") for r in reasons)


def test_prepare_ready_and_each_blocked_reason(live_service):
    draft = live_service.prepare_order("00417", [("BW-1001", 4), ("bw-1001 ", 6)])  # duplicates merged
    assert draft.lifecycle == DraftLifecycle.READY and draft.eligible and draft.estimated_total == Decimal("48.50")
    assert [(l.sku, l.quantity) for l in draft.lines] == [("BW-1001", 10)]
    assert draft.draft_id.startswith("LB-") and len(draft.draft_id) == 11
    hold = live_service.prepare_order("10871", [("BW-1001", 1)])
    assert hold.lifecycle == DraftLifecycle.BLOCKED and hold.blocking_reasons == ["Customer account is on credit hold."]
    unsafe = live_service.prepare_order("52290", [("BW-1007", 1), ("BW-1008", 1), ("BW-1009", 1), ("BW-1010", 5), ("BW-9999", 1)])
    by_sku = {l.sku: l.blocking_reasons for l in unsafe.lines}
    assert by_sku["BW-1007"] == ["Product is discontinued."]
    assert by_sku["BW-1008"] == ["No reliable price on file."]
    assert by_sku["BW-1009"][0] == "No reliable price on file." and by_sku["BW-1009"][1].startswith("Availability cannot be established: ")
    assert by_sku["BW-1010"] == ["Insufficient stock: requested 5, available 2."]
    assert by_sku["BW-9999"] == ["Unknown SKU"]
    assert unsafe.lifecycle == DraftLifecycle.BLOCKED and unsafe.estimated_total is None
    with pytest.raises(NotFoundError):
        live_service.prepare_order("99999", [("BW-1001", 1)])
    with pytest.raises(ValueError):
        live_service.prepare_order("00417", [("BW-1001", 0)])
    with pytest.raises(ValueError):
        live_service.prepare_order("00417", [("BW-1001", 1)] * 11)


def test_reject_sends_nothing(sim):
    service, transport = make_service(sim)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    transport.reset()
    rejected = service.reject_draft(draft.draft_id)
    assert rejected.lifecycle == DraftLifecycle.REJECTED_BY_USER and transport.records == []
    assert len(erp_orders()) == 10


# =============================================================================== F01: one approval, one order POST


def test_f01_t1_wrong_secret_pre_send_failure_sends_no_e8(sim):
    service, healthy = make_service(sim)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    wrong = CountingTransport()
    service.client = LegacyClient(ERP_URL, LOGIN, "wrong-secret", transport=wrong)
    result = approve(service, draft.draft_id)
    assert (result.outcome, result.evidence, result.e8_requests) == (SubmissionOutcome.NOT_APPLIED, OutcomeEvidence.NOT_SENT, 0)
    assert wrong.e8_count == 0 and healthy.e8_count == 0
    assert service.get_draft(draft.draft_id).lifecycle == DraftLifecycle.NOT_APPLIED
    assert len(erp_orders()) == 10


def test_f01_t2_and_t6_pre_handler_429_then_fresh_approval(sim):
    service, transport = make_service(sim)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    assert sim.reset(rate_limit=1, rate_window_sec=60) is True
    transport.reset()
    result = approve(service, draft.draft_id)
    assert (result.outcome, result.evidence, result.e8_requests) == (SubmissionOutcome.NOT_APPLIED, OutcomeEvidence.PRE_HANDLER_REJECTION, 1)
    assert transport.e8_count == 1
    e8_index = next(i for i, r in enumerate(transport.records) if (r.method, r.path) == ("POST", "/halden/orders"))
    assert transport.records[e8_index].status == 429
    assert transport.records[e8_index + 1:] == []  # no login and no E8 after the refusal
    check = verification_client()
    assert check.get_order_raw("SO-240111")["rc"] == 404  # 0 new bookings (one request within the verifier's own window)
    # F01-T6: reset(rate_limit=0); approve attempt 2 → exactly one new E8.
    assert sim.reset(rate_limit=0) is True
    second = approve(service, draft.draft_id, 2)
    assert (second.outcome, second.evidence, second.order_no) == (SubmissionOutcome.CONFIRMED, OutcomeEvidence.READ_BACK, "SO-240111")
    assert transport.e8_count == 2 and second.e8_requests == 1
    history = service.get_draft(draft.draft_id).history
    assert [(r.attempt_no, r.outcome) for r in history] == [(1, SubmissionOutcome.NOT_APPLIED), (2, SubmissionOutcome.CONFIRMED)]


def test_f01_t3_t5_late_commit_is_unknown_then_reconciled(sim):
    service, transport = make_service(sim, timeout=1.0)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    assert sim.arm_fault("POST", "/halden/orders", "delay", 1, 2500) == 1
    result = approve(service, draft.draft_id)
    assert (result.outcome, result.evidence, result.e8_requests) == (SubmissionOutcome.UNKNOWN, OutcomeEvidence.NO_MATCH_AFTER_UNCERTAIN, 1)
    attempt = service.get_draft(draft.draft_id).attempts[-1]
    assert attempt.e8_observation_kind == "UNCERTAIN" and attempt.acknowledged_order_no is None
    time.sleep(3)
    recheck = service.recheck_submission(draft.draft_id)
    assert (recheck.outcome, recheck.evidence, recheck.order_no, recheck.e8_requests) == (
        SubmissionOutcome.CONFIRMED, OutcomeEvidence.RECONCILED, "SO-240111", 0)
    assert transport.e8_count == 1
    assert not any(p.startswith("/_sim/") for p in transport.paths())


def test_f01_t4_t5_pre_handler_error_500_stays_unknown(sim):
    service, transport = make_service(sim)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    sim.arm_fault("POST", "/halden/orders", "error_500", 1)
    result = approve(service, draft.draft_id)
    assert (result.outcome, result.evidence, transport.e8_count) == (SubmissionOutcome.UNKNOWN, OutcomeEvidence.NO_MATCH_AFTER_UNCERTAIN, 1)
    recheck = service.recheck_submission(draft.draft_id)
    assert (recheck.outcome, recheck.evidence) == (SubmissionOutcome.UNKNOWN, OutcomeEvidence.NO_MATCH_AFTER_UNCERTAIN)
    assert transport.e8_count == 1 and len(erp_orders()) == 10
    assert not any(p.startswith("/_sim/") for p in transport.paths())


def test_f01_t4_commit_then_500_is_reconciled(sim):
    service, transport = make_service(sim)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    sim.arm_fault("POST", "/halden/orders", "commit_then_500", 1)
    result = approve(service, draft.draft_id)
    assert (result.outcome, result.evidence, result.order_no) == (SubmissionOutcome.CONFIRMED, OutcomeEvidence.RECONCILED, "SO-240111")
    assert transport.e8_count == 1 and len(refs(draft.draft_id)) == 1
    assert not any(p.startswith("/_sim/") for p in transport.paths())


# =============================================================================== F02-LV: live outcome rows


def test_lv_row1_read_back(sim):
    service, transport = make_service(sim)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    result = approve(service, draft.draft_id)
    assert (result.outcome, result.evidence, result.order_no, str(result.erp_booked_total)) == (
        SubmissionOutcome.CONFIRMED, OutcomeEvidence.READ_BACK, "SO-240111", "48.50")
    assert result.order.lines[0].sku == "BW-1001" and transport.e8_count == 1
    assert [h.kind for h in service.get_draft(draft.draft_id).history] == [ResultKind.SUBMIT]


def test_lv_row4_legacy_rejection(sim):
    service, transport = make_service(sim)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    with service.store.lock:  # test-only mutation of the draft line before approval
        draft.lines[0] = DraftLine(**{**draft.lines[0].model_dump(), "sku": "BW-9999"})
    result = approve(service, draft.draft_id)
    assert (result.outcome, result.evidence, result.legacy_code) == (SubmissionOutcome.REJECTED, OutcomeEvidence.LEGACY_REJECTION, "UNKNOWN_ITEM")
    assert service.get_draft(draft.draft_id).lifecycle == DraftLifecycle.REJECTED_BY_ERP and transport.e8_count == 1
    assert len(erp_orders()) == 10


def test_lv_row5_stale_draft_short(sim):
    service, transport = make_service(sim)
    draft = service.prepare_order("52290", [("BW-1010", 2)])
    assert draft.lifecycle == DraftLifecycle.READY
    assert service.simulate_external_order("31002", "BW-1010", 2) == "SO-240111"
    result = approve(service, draft.draft_id)
    assert (result.outcome, result.evidence, result.legacy_code) == (SubmissionOutcome.REJECTED, OutcomeEvidence.LEGACY_REJECTION, "INSUFFICIENT_STOCK")
    assert result.message == "Rejected by HALDEN: insufficient stock for BW-1010 (available 0)."
    assert transport.e8_count == 1 and len(erp_orders()) == 11


def test_lv_row6_pre_send_failure(sim):
    service, _ = make_service(sim)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    service.client = LegacyClient("http://127.0.0.1:9", LOGIN, SECRET, sleep=lambda s: None)  # nothing listens
    result = approve(service, draft.draft_id)
    assert (result.outcome, result.evidence, result.e8_requests) == (SubmissionOutcome.NOT_APPLIED, OutcomeEvidence.NOT_SENT, 0)
    assert len(erp_orders()) == 10


def test_lv_row8_and_row9_to_8(sim):
    service, transport = make_service(sim, timeout=1.0)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    sim.arm_fault("POST", "/halden/orders", "delay", 1, 2500)
    first = approve(service, draft.draft_id)
    assert first.evidence == OutcomeEvidence.NO_MATCH_AFTER_UNCERTAIN  # row 9
    time.sleep(3)
    second = service.recheck_submission(draft.draft_id)
    assert (second.evidence, second.order_no) == (OutcomeEvidence.RECONCILED, "SO-240111")  # 9 → 8
    history = service.get_draft(draft.draft_id).history
    assert [(h.kind, h.e8_requests) for h in history] == [(ResultKind.SUBMIT, 1), (ResultKind.RECHECK, 0)]
    assert transport.e8_count == 1


def test_lv_row11_listing_failure_after_commit_then_500(sim):
    service, transport = make_service(sim)
    service.client._sleep = lambda s: None
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    sim.arm_fault("POST", "/halden/orders", "commit_then_500", 1)
    sim.arm_fault("GET", "/halden/orders", "error_500", 3)
    result = approve(service, draft.draft_id)
    assert (result.outcome, result.evidence) == (SubmissionOutcome.UNKNOWN, OutcomeEvidence.RECONCILIATION_FAILED)
    assert transport.e8_count == 1
    recheck = service.recheck_submission(draft.draft_id)
    assert (recheck.evidence, recheck.order_no) == (OutcomeEvidence.RECONCILED, "SO-240111")
    assert transport.e8_count == 1


def test_lv_row10_two_refs(sim):
    service, transport = make_service(sim)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    assert raw_e8([("acct", "00417"), ("ref", draft.draft_id), ("item", "BW-1001"), ("qty", "10")]).json()["rc"] == 0
    sim.arm_fault("POST", "/halden/orders", "commit_then_500", 1)
    result = approve(service, draft.draft_id)
    assert (result.outcome, result.evidence) == (SubmissionOutcome.UNKNOWN, OutcomeEvidence.AMBIGUOUS_MATCH)
    assert "SO-240111" in result.evidence_detail and "SO-240112" in result.evidence_detail
    assert transport.e8_count == 1


def test_lv_row10_one_mismatching_ref(sim):
    service, transport = make_service(sim)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    assert raw_e8([("acct", "00417"), ("ref", draft.draft_id), ("item", "BW-1004"), ("qty", "1")]).json()["rc"] == 0
    sim.arm_fault("POST", "/halden/orders", "error_500", 1)
    result = approve(service, draft.draft_id)
    assert (result.outcome, result.evidence) == (SubmissionOutcome.UNKNOWN, OutcomeEvidence.AMBIGUOUS_MATCH)
    assert transport.e8_count == 1


def _e7_override(handler):
    return override_transport([("GET", r"/halden/orders/SO-240111", handler)])


def test_lv_row2_read_back_transport_failure_via_override(sim):
    def timeout(request):
        raise httpx.ReadTimeout("simulated read-back failure", request=request)

    transport = _e7_override(timeout)
    service = BridgeService(LegacyClient(ERP_URL, LOGIN, SECRET, transport=transport, sleep=lambda s: None), sim)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    result = approve(service, draft.draft_id)
    assert (result.outcome, result.evidence, result.order_no, result.order) == (
        SubmissionOutcome.CONFIRMED, OutcomeEvidence.ACKNOWLEDGED, "SO-240111", None)
    assert transport.e8_count == 1 and transport.count("GET", "/halden/orders/SO-240111") == 3


def test_lv_row3_read_back_404_via_override(sim):
    transport = _e7_override(lambda request: httpx.Response(200, json={"rc": 404, "msg": "ORDER NOT ON FILE"}))
    service = BridgeService(LegacyClient(ERP_URL, LOGIN, SECRET, transport=transport), sim)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    result = approve(service, draft.draft_id)
    assert (result.outcome, result.evidence, result.order_no) == (SubmissionOutcome.UNKNOWN, OutcomeEvidence.READBACK_MISMATCH, "SO-240111")
    attempt = service.get_draft(draft.draft_id).attempts[-1]
    assert attempt.acknowledged_order_no == "SO-240111" and transport.e8_count == 1


# =============================================================================== F03: strict stock evidence


def service_with_stock(sim, body: str) -> tuple[BridgeService, CountingTransport]:
    transport = override_transport([("GET", r"/halden/stock", stock_response(body))])
    return BridgeService(LegacyClient(ERP_URL, LOGIN, SECRET, transport=transport), sim), transport


HEADER = "ITEM;WH;ON_HAND;ALLOCATED\r\n"


def assert_no_verdict(service, sku):
    with pytest.raises(LegacyMalformedResponse):
        service.check_availability(sku)
    with pytest.raises(LegacyMalformedResponse):
        service.product_catalogue()


def assert_blocked(service, account, sku):
    draft = service.prepare_order(account, [(sku, 1)])
    assert draft.lifecycle == DraftLifecycle.BLOCKED and not draft.eligible
    assert any(r.startswith("Availability cannot be established") for r in all_reasons(draft))
    assert all(line.availability not in (StockStatus.IN_STOCK, StockStatus.OUT_OF_STOCK) for line in draft.lines)
    return draft


def test_f03_3_three_field_row(sim):
    service, _ = service_with_stock(sim, HEADER + "BW-1001;DUS;240;0\r\nBW-1001;MAN;60\r\n")
    assert_no_verdict(service, "BW-1001")
    assert_blocked(service, "00417", "BW-1001")


def test_f03_5_missing_sku(sim):
    service, _ = service_with_stock(sim, HEADER + "BW-1005;DUS;50;5\r\n ;MAN;?;0\r\n")
    assert_no_verdict(service, "BW-1005")
    assert_blocked(service, "00417", "BW-1005")


def test_f03_6_missing_warehouse(sim):
    service, _ = service_with_stock(sim, HEADER + "BW-1001;DUS;240;0\r\nBW-1001;;60;0\r\n")
    assert_no_verdict(service, "BW-1001")


def test_f03_7_negative_reserved_quantity(sim):
    service, _ = service_with_stock(sim, HEADER + "BW-1001;DUS;0;-2\r\n")
    report = service.check_availability("BW-1001")
    assert report.status == StockStatus.UNDETERMINED and report.available_quantity is None
    assert [(i.code, i.field) for i in report.issues] == [(IssueCode.NEGATIVE_QUANTITY, "allocated")]


def test_f03_8_valid_rows_with_invalid_identity(sim):
    service, _ = service_with_stock(sim, SEED_STOCK + " ;MAN;?;0\r\n")
    for sku in ("BW-1001", "BW-1006", "BW-1010"):
        assert_no_verdict(service, sku)
    assert_blocked(service, "00417", "BW-1001")


def test_f03_9_valid_rows_with_invalid_allocation(sim):
    service, _ = service_with_stock(sim, SEED_STOCK + "BW-1001;MAN;5;-2\r\n")
    assert service.check_availability("BW-1001").status == StockStatus.UNDETERMINED
    report = service.check_availability("BW-1006")
    assert (report.status, report.available_quantity) == (StockStatus.IN_STOCK, 1700)
    draft = service.prepare_order("00417", [("BW-1001", 1)])
    assert draft.lifecycle == DraftLifecycle.BLOCKED and not draft.eligible


def test_f03_10_draft_preparation_with_affected_extracts(sim):
    for body, sku in ((HEADER + "BW-1005;DUS;50;5\r\n ;MAN;?;0\r\n", "BW-1005"), (SEED_STOCK + " ;MAN;?;0\r\n", "BW-1001")):
        service, _ = service_with_stock(sim, body)
        draft = assert_blocked(service, "00417", sku)
        assert draft.lifecycle == DraftLifecycle.BLOCKED
    service, _ = service_with_stock(sim, SEED_STOCK + "BW-1001;MAN;5;-2\r\n")
    draft = service.prepare_order("00417", [("BW-1001", 1)])
    assert (draft.eligible, draft.lifecycle, draft.lines[0].availability) == (False, DraftLifecycle.BLOCKED, StockStatus.UNDETERMINED)


def test_f03_11_every_path_reads_the_full_extract_without_item(sim):
    from legacybridge.assistant.tools import make_tools
    import json

    service, transport = service_with_stock(sim, SEED_STOCK + " ;MAN;?;0\r\n")
    with pytest.raises(LegacyMalformedResponse):
        service.check_availability("BW-1001")
    tool = {t.name: t for t in make_tools(service)}["check_availability"]
    envelope = json.loads(tool.invoke({"sku": "BW-1001"}))
    assert envelope["ok"] is False and envelope["error"]["code"] == "MALFORMED_RESPONSE"
    with pytest.raises(LegacyMalformedResponse):
        service.product_catalogue()
    draft = service.prepare_order("00417", [("BW-1001", 1)])
    assert draft.lifecycle == DraftLifecycle.BLOCKED and draft.lines[0].available_quantity != 300
    stock_requests = [r for r in transport.records if r.path == "/halden/stock"]
    assert len(stock_requests) == 4
    assert all("item" not in dict(r.params) for r in stock_requests)
    # the controlled transport honors the query as written: a request WITH item reaches the live ERP
    direct = transport.inner.handle_request(httpx.Request("GET", f"{ERP_URL}/halden/stock?item=BW-1001", headers={"X-Halden-Ticket": service.client._ticket}))
    direct.read()
    assert direct.content.decode().count("\r\n") == 3  # header + the two live BW-1001 rows
