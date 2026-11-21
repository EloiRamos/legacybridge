"""Atomic approval, lifecycle and the eleven outcome rows, offline (F02-DS1…DS9, F01-T1 unit)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from urllib.parse import parse_qsl

import httpx
import pytest

from legacybridge.bridge.errors import DraftStateError
from legacybridge.bridge.models import (
    PERMITTED_ACTIONS,
    Approval,
    DraftAction,
    DraftLifecycle,
    OutcomeEvidence,
    ResultKind,
    SubmissionOutcome,
)
from legacybridge.service import REJECTED_BY_OPERATOR_MESSAGE, BridgeService
from tests.conftest import FakeHalden, json_response

ERROR_500 = "<html><body><h1>HALDEN ERP - Internal Error</h1></body></html>"


def approval(draft_id: str, n: int) -> Approval:
    return Approval(draft_id=draft_id, attempt_no=n, approved_at=datetime.now(timezone.utc))


def ready(service: BridgeService, account="00417", lines=(("BW-1001", 10),)):
    draft = service.prepare_order(account, list(lines))
    assert draft.lifecycle == DraftLifecycle.READY and draft.eligible
    return draft


def form_of(request: httpx.Request) -> list[tuple[str, str]]:
    return list(parse_qsl(request.content.decode()))


def commit_then_500(fake: FakeHalden):
    def handler(request):
        fake.book(form_of(request))
        return httpx.Response(500, text=ERROR_500)
    return handler


def reply(response: httpx.Response):
    return lambda request: response


def raise_(exc):
    def handler(request):
        raise exc
    return handler


def matching_order(order_no: str, draft, ref=None, lines=None) -> dict:
    return {
        "OrderNo": order_no, "Acct": str(int(draft.account_id)), "Date": "20.11.2026", "Stat": "OP", "Total": "48.50",
        "Ref": draft.draft_id if ref is None else ref,
        "lines": lines if lines is not None else [
            {"ln": n, "item": l.sku, "qty": str(l.quantity), "price": "4.85"} for n, l in enumerate(draft.lines, 1)],
    }


def assert_result(service, draft_id, outcome, evidence, lifecycle, e8):
    draft = service.get_draft(draft_id)
    result = draft.history[-1]
    assert (result.outcome, result.evidence, draft.lifecycle) == (outcome, evidence, lifecycle)
    assert result.e8_requests == e8 and draft.attempts[-1].e8_requests == e8
    assert PERMITTED_ACTIONS[draft.lifecycle] == {
        DraftLifecycle.CONFIRMED: (DraftAction.VIEW_ORDER,),
        DraftLifecycle.REJECTED_BY_ERP: (),
        DraftLifecycle.NOT_APPLIED: (DraftAction.APPROVE, DraftAction.REJECT),
        DraftLifecycle.UNKNOWN: (DraftAction.RECHECK,),
    }[lifecycle]
    assert result.kind == ResultKind.SUBMIT and draft.history == [draft.attempts[-1].result]
    return result


# =============================================================================== F01-T1 (unit)


def test_f01_t1_unit_pre_send_connect_error_sends_no_e8(fake, fake_service):
    draft = ready(fake_service)
    fake.e3_error = httpx.ConnectError("refused")
    result = fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    assert (result.outcome, result.evidence, result.e8_requests) == (SubmissionOutcome.NOT_APPLIED, OutcomeEvidence.NOT_SENT, 0)
    assert fake.e8_count == 0 and fake_service.client.e8_request_count == 0


def test_f01_t1_unit_wrong_secret_sends_no_e8(fake, fake_service):
    draft = ready(fake_service)
    fake_service.client = fake.client(secret="wrong-secret")
    result = fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    assert (result.outcome, result.evidence, result.e8_requests) == (SubmissionOutcome.NOT_APPLIED, OutcomeEvidence.NOT_SENT, 0)
    assert fake.e8_count == 0


# =============================================================================== F02-DS6: the eleven rows


def test_row6_pre_send_failure(fake, fake_service):
    draft = ready(fake_service)
    fake.customers = []  # zero rows for the account
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    assert_result(fake_service, draft.draft_id, SubmissionOutcome.NOT_APPLIED, OutcomeEvidence.NOT_SENT, DraftLifecycle.NOT_APPLIED, 0)
    assert fake.e8_count == 0


def test_row6_connect_error_on_e8(fake, fake_service):
    draft = ready(fake_service)
    fake.e8 = raise_(httpx.ConnectError("refused"))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    assert_result(fake_service, draft.draft_id, SubmissionOutcome.NOT_APPLIED, OutcomeEvidence.NOT_SENT, DraftLifecycle.NOT_APPLIED, 1)
    assert fake.e8_count == 1


@pytest.mark.parametrize("status", [401, 403, 440, 429])
def test_row7_pre_handler_refusal(fake, fake_service, status):
    draft = ready(fake_service)
    fake.e8 = reply(httpx.Response(status, text="refused", headers={"X-Halden-Wait": "2"}))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    assert_result(fake_service, draft.draft_id, SubmissionOutcome.NOT_APPLIED, OutcomeEvidence.PRE_HANDLER_REJECTION, DraftLifecycle.NOT_APPLIED, 1)
    assert fake.e8_count == 1


def test_row4_legacy_rc_rejection(fake, fake_service):
    draft = ready(fake_service)
    fake.e8 = reply(json_response({"rc": 21, "msg": "UNKNOWN_ITEM"}))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    result = assert_result(fake_service, draft.draft_id, SubmissionOutcome.REJECTED, OutcomeEvidence.LEGACY_REJECTION, DraftLifecycle.REJECTED_BY_ERP, 1)
    assert result.legacy_code == "UNKNOWN_ITEM"


def test_row5_short(fake, fake_service):
    draft = ready(fake_service)
    fake.e8 = reply(httpx.Response(409, text="SHORT:BW-1001:0"))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    result = assert_result(fake_service, draft.draft_id, SubmissionOutcome.REJECTED, OutcomeEvidence.LEGACY_REJECTION, DraftLifecycle.REJECTED_BY_ERP, 1)
    assert result.legacy_code == "INSUFFICIENT_STOCK"
    assert result.message == "Rejected by HALDEN: insufficient stock for BW-1001 (available 0)."


def test_row1_read_back(fake, fake_service):
    draft = ready(fake_service)
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    result = assert_result(fake_service, draft.draft_id, SubmissionOutcome.CONFIRMED, OutcomeEvidence.READ_BACK, DraftLifecycle.CONFIRMED, 1)
    assert result.order_no == "SO-240111" and result.order is not None and str(result.erp_booked_total) == "48.50"


def test_row2_read_back_transport_failure_is_acknowledged_not_absence(fake, fake_service):
    draft = ready(fake_service)
    fake.e7["SO-240111"] = raise_(httpx.ReadTimeout("slow"))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    result = assert_result(fake_service, draft.draft_id, SubmissionOutcome.CONFIRMED, OutcomeEvidence.ACKNOWLEDGED, DraftLifecycle.CONFIRMED, 1)
    assert result.order_no == "SO-240111" and result.order is None
    assert fake.count("GET", "/halden/orders/SO-240111") == 3  # ordinary read retries


def test_row3_read_back_404_is_mismatch_not_confirmation(fake, fake_service):
    draft = ready(fake_service)
    fake.e7["SO-240111"] = reply(json_response({"rc": 404, "msg": "ORDER NOT ON FILE"}))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    result = assert_result(fake_service, draft.draft_id, SubmissionOutcome.UNKNOWN, OutcomeEvidence.READBACK_MISMATCH, DraftLifecycle.UNKNOWN, 1)
    assert result.order_no == "SO-240111"


def test_row3_read_back_different_content(fake, fake_service):
    draft = ready(fake_service)
    fake.e7["SO-240111"] = reply(json_response({"rc": 0, "order": matching_order("SO-240111", draft, lines=[{"ln": 1, "item": "BW-1001", "qty": "9", "price": "4.85"}])}))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    assert_result(fake_service, draft.draft_id, SubmissionOutcome.UNKNOWN, OutcomeEvidence.READBACK_MISMATCH, DraftLifecycle.UNKNOWN, 1)


def test_row11_listing_fails(fake, fake_service):
    draft = ready(fake_service)
    fake.e8 = reply(httpx.Response(500, text=ERROR_500))
    fake.e6_error = True
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    assert_result(fake_service, draft.draft_id, SubmissionOutcome.UNKNOWN, OutcomeEvidence.RECONCILIATION_FAILED, DraftLifecycle.UNKNOWN, 1)


def test_row10_two_matching_refs(fake, fake_service):
    draft = ready(fake_service)
    fake.orders["SO-240200"] = matching_order("SO-240200", draft)
    fake.e8 = commit_then_500(fake)
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    result = assert_result(fake_service, draft.draft_id, SubmissionOutcome.UNKNOWN, OutcomeEvidence.AMBIGUOUS_MATCH, DraftLifecycle.UNKNOWN, 1)
    assert "SO-240200" in result.evidence_detail and "SO-240111" in result.evidence_detail


def test_row9_no_match_after_uncertain(fake, fake_service):
    draft = ready(fake_service)
    fake.e8 = reply(httpx.Response(500, text=ERROR_500))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    result = assert_result(fake_service, draft.draft_id, SubmissionOutcome.UNKNOWN, OutcomeEvidence.NO_MATCH_AFTER_UNCERTAIN, DraftLifecycle.UNKNOWN, 1)
    assert "does not prove it was not created" in result.evidence_detail


def test_row11_sole_candidate_detail_fails(fake, fake_service):
    draft = ready(fake_service)
    fake.e8 = commit_then_500(fake)
    fake.e7["SO-240111"] = reply(httpx.Response(500, text=ERROR_500))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    assert_result(fake_service, draft.draft_id, SubmissionOutcome.UNKNOWN, OutcomeEvidence.RECONCILIATION_FAILED, DraftLifecycle.UNKNOWN, 1)


@pytest.mark.parametrize("variant", ["missing", "different_lines"])
def test_row10_sole_candidate_missing_or_different(fake, fake_service, variant):
    draft = ready(fake_service)
    fake.e8 = commit_then_500(fake)
    if variant == "missing":
        fake.e7["SO-240111"] = reply(json_response({"rc": 404, "msg": "ORDER NOT ON FILE"}))
    else:
        fake.e7["SO-240111"] = reply(json_response({"rc": 0, "order": matching_order("SO-240111", draft, lines=[{"ln": 1, "item": "BW-1004", "qty": "10", "price": "1"}])}))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    assert_result(fake_service, draft.draft_id, SubmissionOutcome.UNKNOWN, OutcomeEvidence.AMBIGUOUS_MATCH, DraftLifecycle.UNKNOWN, 1)


def test_row8_reconciled(fake, fake_service):
    draft = ready(fake_service)
    fake.e8 = commit_then_500(fake)
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    result = assert_result(fake_service, draft.draft_id, SubmissionOutcome.CONFIRMED, OutcomeEvidence.RECONCILED, DraftLifecycle.CONFIRMED, 1)
    assert result.order_no == "SO-240111" and result.order is not None
    assert fake.e8_count == 1


# =============================================================================== F02-DS1 / DS2: lifecycle transitions


def bring_to(fake: FakeHalden, service: BridgeService, lifecycle: DraftLifecycle):
    if lifecycle == DraftLifecycle.BLOCKED:
        return service.prepare_order("10871", [("BW-1001", 1)])
    draft = ready(service)
    if lifecycle == DraftLifecycle.READY:
        return draft
    if lifecycle == DraftLifecycle.REJECTED_BY_USER:
        return service.reject_draft(draft.draft_id)
    fake.e8 = {
        DraftLifecycle.CONFIRMED: None,
        DraftLifecycle.NOT_APPLIED: reply(httpx.Response(403, text="forbidden")),
        DraftLifecycle.REJECTED_BY_ERP: reply(json_response({"rc": 22, "msg": "ITEM_DISCONTINUED"})),
        DraftLifecycle.UNKNOWN: reply(httpx.Response(500, text=ERROR_500)),
    }[lifecycle]
    service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    assert service.get_draft(draft.draft_id).lifecycle == lifecycle
    fake.e8 = None
    return service.get_draft(draft.draft_id)


ALLOWED = {
    (DraftLifecycle.READY, "approve"), (DraftLifecycle.READY, "reject"),
    (DraftLifecycle.BLOCKED, "reject"),
    (DraftLifecycle.NOT_APPLIED, "approve"), (DraftLifecycle.NOT_APPLIED, "reject"),
    (DraftLifecycle.UNKNOWN, "recheck"),
}
STATES = [DraftLifecycle.READY, DraftLifecycle.BLOCKED, DraftLifecycle.CONFIRMED, DraftLifecycle.REJECTED_BY_ERP,
          DraftLifecycle.NOT_APPLIED, DraftLifecycle.UNKNOWN, DraftLifecycle.REJECTED_BY_USER]


@pytest.mark.parametrize("lifecycle", STATES)
@pytest.mark.parametrize("action", ["approve", "reject", "recheck"])
def test_ds1_transitions(fake, fake_service, lifecycle, action):
    draft = bring_to(fake, fake_service, lifecycle)
    before_io = fake.io_count()
    e8_before = fake.e8_count

    def act():
        if action == "approve":
            return fake_service.submit_order(draft.draft_id, approval(draft.draft_id, len(draft.attempts) + 1))
        if action == "reject":
            return fake_service.reject_draft(draft.draft_id)
        return fake_service.recheck_submission(draft.draft_id)

    if (lifecycle, action) in ALLOWED:
        act()
        after = fake_service.get_draft(draft.draft_id).lifecycle
        if action == "reject":
            assert after == DraftLifecycle.REJECTED_BY_USER and fake.io_count() == before_io
        elif action == "recheck":
            assert after in (DraftLifecycle.UNKNOWN, DraftLifecycle.CONFIRMED) and fake.e8_count == e8_before
        else:
            assert after != DraftLifecycle.SUBMITTING and fake.e8_count == e8_before + 1
    else:
        with pytest.raises(DraftStateError):
            act()
        assert fake.io_count() == before_io  # a refused action performs no I/O
        assert fake_service.get_draft(draft.draft_id).lifecycle == lifecycle


def test_ds1_blocked_permits_discard_only_and_unknown_recheck_only():
    assert PERMITTED_ACTIONS[DraftLifecycle.BLOCKED] == (DraftAction.DISCARD,)
    assert PERMITTED_ACTIONS[DraftLifecycle.UNKNOWN] == (DraftAction.RECHECK,)
    assert PERMITTED_ACTIONS[DraftLifecycle.REJECTED_BY_USER] == ()
    assert PERMITTED_ACTIONS[DraftLifecycle.REJECTED_BY_ERP] == ()
    assert PERMITTED_ACTIONS[DraftLifecycle.SUBMITTING] == ()


def test_ds1_submitting_refuses_every_action(fake, fake_service):
    draft = ready(fake_service)
    fake.gate = threading.Event()
    worker = threading.Thread(target=fake_service.submit_order, args=(draft.draft_id, approval(draft.draft_id, 1)))
    worker.start()
    assert fake.gate_entered.wait(5)
    assert fake_service.get_draft(draft.draft_id).lifecycle == DraftLifecycle.SUBMITTING
    for act in (lambda: fake_service.reject_draft(draft.draft_id), lambda: fake_service.recheck_submission(draft.draft_id),
                lambda: fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 2))):
        with pytest.raises(DraftStateError):
            act()
    fake.gate.set()
    worker.join(5)
    assert fake_service.get_draft(draft.draft_id).lifecycle == DraftLifecycle.CONFIRMED


def test_ds2_eligible_is_fixed_and_lifecycle_never_returns_to_ready(fake, fake_service):
    draft = ready(fake_service)
    rejected = fake_service.reject_draft(draft.draft_id)
    assert rejected.eligible and rejected.status == "READY" and rejected.lifecycle == DraftLifecycle.REJECTED_BY_USER
    assert all(not line.blocking_reasons for line in rejected.lines)
    for setup in (reply(httpx.Response(403, text="x")), reply(httpx.Response(500, text=ERROR_500)), None):
        d = ready(fake_service)
        fake.e8 = setup
        fake_service.submit_order(d.draft_id, approval(d.draft_id, 1))
        after = fake_service.get_draft(d.draft_id)
        assert after.eligible and after.lifecycle != DraftLifecycle.READY
    blocked = fake_service.prepare_order("10871", [("BW-1001", 1)])
    assert not blocked.eligible and blocked.status == "BLOCKED" and blocked.lifecycle == DraftLifecycle.BLOCKED


# =============================================================================== F02-DS3 / DS4 / DS5: races and stale approvals


def race(fake: FakeHalden, service: BridgeService, draft_id: str, n: int):
    fake.gate = threading.Event()
    fake.gate_entered = threading.Event()
    barrier = threading.Barrier(2)
    outcomes: list = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            result = service.submit_order(draft_id, approval(draft_id, n))
            with lock:
                outcomes.append(result)
        except DraftStateError as exc:
            with lock:
                outcomes.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    assert fake.gate_entered.wait(5)
    # the loser has already failed while the winner's E8 is still held open
    for _ in range(100):
        with lock:
            if any(isinstance(o, DraftStateError) for o in outcomes):
                break
        threading.Event().wait(0.01)
    with lock:
        assert sum(isinstance(o, DraftStateError) for o in outcomes) == 1
    fake.gate.set()
    for t in threads:
        t.join(5)
    fake.gate = None
    return outcomes


@pytest.mark.parametrize("repeat", range(20))
def test_ds3_race_from_ready(repeat):
    fake = FakeHalden()
    service = BridgeService(fake.client(), None)
    draft = ready(service)
    e3_before = fake.count("GET", "/halden/customers")
    outcomes = race(fake, service, draft.draft_id, 1)
    winners = [o for o in outcomes if not isinstance(o, DraftStateError)]
    assert len(winners) == 1 and winners[0].outcome == SubmissionOutcome.CONFIRMED
    assert fake.e8_count == 1
    assert fake.count("GET", "/halden/customers") - e3_before == 1  # only the winner's pre-send read
    assert service.get_draft(draft.draft_id).lifecycle == DraftLifecycle.CONFIRMED
    assert len(service.get_draft(draft.draft_id).attempts) == 1


@pytest.mark.parametrize("repeat", range(20))
def test_ds4_race_after_not_applied(repeat):
    fake = FakeHalden()
    service = BridgeService(fake.client(), None)
    draft = ready(service)
    fake.e8 = reply(httpx.Response(403, text="forbidden"))
    service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    assert service.get_draft(draft.draft_id).lifecycle == DraftLifecycle.NOT_APPLIED
    fake.e8 = None
    outcomes = race(fake, service, draft.draft_id, 2)
    winners = [o for o in outcomes if not isinstance(o, DraftStateError)]
    assert len(winners) == 1 and winners[0].attempt_no == 2
    assert fake.e8_count == 2  # exactly one new E8
    assert len(service.get_draft(draft.draft_id).attempts) == 2


def test_ds5_stale_and_foreign_approvals(fake, fake_service):
    draft = ready(fake_service)
    other = ready(fake_service)
    fake.e8 = reply(httpx.Response(403, text="forbidden"))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    before = fake.io_count()
    with pytest.raises(DraftStateError):
        fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))  # stale attempt number
    with pytest.raises(DraftStateError):
        fake_service.submit_order(draft.draft_id, approval(other.draft_id, 2))  # approval for another draft
    with pytest.raises(DraftStateError):
        fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 3))  # skipped attempt number
    assert fake.io_count() == before
    assert len(fake_service.get_draft(draft.draft_id).attempts) == 1


# =============================================================================== F02-DS7: anchored re-check


def test_ds7_anchored_recheck_never_confirms_another_order(fake, fake_service):
    draft = ready(fake_service)
    x = "SO-240111"
    good_x = matching_order(x, draft)
    bad_x = matching_order(x, draft, lines=[{"ln": 1, "item": "BW-1001", "qty": "9", "price": "4.85"}])
    fake.e8 = reply(json_response({"rc": 0, "order_no": x, "total": "48.50"}))
    fake.e7[x] = reply(json_response({"rc": 0, "order": bad_x}))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))

    def check(evidence):
        d = fake_service.get_draft(draft.draft_id)
        assert d.lifecycle == DraftLifecycle.UNKNOWN and d.history[-1].evidence == evidence
        assert d.attempts[-1].acknowledged_order_no == x
        assert all("SO-240300" not in (r.order_no or "") and "SO-240300" not in r.evidence_detail for r in d.history)
        assert fake.count("GET", "/halden/orders") - fake.count("GET", "/halden/orders/") == 0  # zero E6 requests
        assert PERMITTED_ACTIONS[d.lifecycle] == (DraftAction.RECHECK,)
        assert fake.e8_count == 1

    check(OutcomeEvidence.READBACK_MISMATCH)
    fake.e7[x] = raise_(httpx.ReadTimeout("slow"))
    fake_service.recheck_submission(draft.draft_id)
    check(OutcomeEvidence.RECONCILIATION_FAILED)
    assert x in fake_service.get_draft(draft.draft_id).history[-1].evidence_detail
    y = matching_order("SO-240300", draft)
    fake.orders["SO-240300"] = y
    fake.e6_rows = lambda: [{k: v for k, v in y.items() if k != "lines"}]
    fake.e7[x] = reply(json_response({"rc": 0, "order": bad_x}))
    fake_service.recheck_submission(draft.draft_id)
    check(OutcomeEvidence.READBACK_MISMATCH)
    d = fake_service.get_draft(draft.draft_id)
    assert [r.kind for r in d.history] == [ResultKind.SUBMIT, ResultKind.RECHECK, ResultKind.RECHECK]
    assert [r.e8_requests for r in d.history] == [1, 0, 0]
    fake.e7[x] = reply(json_response({"rc": 0, "order": good_x}))
    final = fake_service.recheck_submission(draft.draft_id)
    assert (final.outcome, final.evidence, final.order_no) == (SubmissionOutcome.CONFIRMED, OutcomeEvidence.READ_BACK, x)
    assert final.order is not None and final.order.order_no == x
    assert fake_service.get_draft(draft.draft_id).lifecycle == DraftLifecycle.CONFIRMED
    assert fake.e8_count == 1


# =============================================================================== F02-DS8a–d: null references


def null_row(order_no="SO-240101", acct="417"):
    return {"OrderNo": order_no, "Acct": acct, "Date": "2025-09-02", "Stat": "DL", "Total": "97.00", "Ref": None}


def test_ds8a_null_only_listing_is_row9_without_e7(fake, fake_service):
    draft = ready(fake_service)
    fake.e8 = reply(httpx.Response(500, text=ERROR_500))
    fake.e6_rows = lambda: [null_row(), null_row("SO-240150")]
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    assert fake_service.get_draft(draft.draft_id).history[-1].evidence == OutcomeEvidence.NO_MATCH_AFTER_UNCERTAIN
    assert fake.count("GET", "/halden/orders/") == 0


def test_ds8b_sole_candidate_with_null_detail_ref_is_row10(fake, fake_service):
    draft = ready(fake_service)
    fake.e8 = reply(httpx.Response(500, text=ERROR_500))
    candidate = matching_order("SO-240150", draft)
    fake.e6_rows = lambda: [{k: v for k, v in candidate.items() if k != "lines"}]
    fake.e7["SO-240150"] = reply(json_response({"rc": 0, "order": dict(candidate, Ref=None)}))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    d = fake_service.get_draft(draft.draft_id)
    assert (d.history[-1].outcome, d.history[-1].evidence) == (SubmissionOutcome.UNKNOWN, OutcomeEvidence.AMBIGUOUS_MATCH)


def test_ds8c_acknowledged_order_with_null_ref_is_row3(fake, fake_service):
    draft = ready(fake_service)
    fake.e8 = reply(json_response({"rc": 0, "order_no": "SO-240111", "total": "48.50"}))
    fake.e7["SO-240111"] = reply(json_response({"rc": 0, "order": matching_order("SO-240111", draft, ref=None) | {"Ref": None}}))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    d = fake_service.get_draft(draft.draft_id)
    assert (d.history[-1].outcome, d.history[-1].evidence) == (SubmissionOutcome.UNKNOWN, OutcomeEvidence.READBACK_MISMATCH)
    assert d.attempts[-1].acknowledged_order_no == "SO-240111"


def test_ds8d_null_plus_matching_listing_is_row8_on_matching_row(fake, fake_service):
    draft = ready(fake_service)
    fake.e8 = commit_then_500(fake)
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    d = fake_service.get_draft(draft.draft_id)
    assert (d.history[-1].evidence, d.history[-1].order_no) == (OutcomeEvidence.RECONCILED, "SO-240111")
    assert fake.count("GET", "/halden/orders/SO-240101") == 0  # the null-Ref seed row is never read
    assert fake.count("GET", "/halden/orders/SO-240111") == 1


# =============================================================================== F02-DS9: rejection after NOT_APPLIED


def test_ds9_rejection_after_not_applied(fake, fake_service):
    draft = ready(fake_service)
    fake.e8 = reply(httpx.Response(429, text="THROTTLED", headers={"X-Halden-Wait": "3"}))
    fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    history_before = list(fake_service.get_draft(draft.draft_id).history)
    io_before = fake.io_count()
    rejected = fake_service.reject_draft(draft.draft_id)
    assert rejected.lifecycle == DraftLifecycle.REJECTED_BY_USER
    assert fake.e8_count == 1 and fake.io_count() == io_before
    assert rejected.history == history_before and len(rejected.attempts) == 1
    assert fake_service.rejection_message(rejected) == (
        REJECTED_BY_OPERATOR_MESSAGE
        + " Earlier attempts: 1, each recorded as NOT_APPLIED (see history). No additional request was sent."
    )
    for act in (lambda: fake_service.submit_order(draft.draft_id, approval(draft.draft_id, 2)),
                lambda: fake_service.recheck_submission(draft.draft_id)):
        with pytest.raises(DraftStateError):
            act()
    assert fake.io_count() == io_before
