"""Draft identity and re-check serialization, offline (T1 draft-ID collision, T2 overlapping re-checks).

Every scenario is deterministic. ID collisions are forced by replacing ``secrets.token_hex``. Thread
interleavings are fixed with events and a barrier: the HALDEN order listing (E6) is held open per call,
and an instrumented per-draft lock signals when a second re-check is waiting for it. No test sleeps.
"""

from __future__ import annotations

import re
import threading

import httpx
import pytest

from legacybridge import service as service_module
from legacybridge.bridge.errors import BridgeError, DraftStateError
from legacybridge.bridge.models import (
    PERMITTED_ACTIONS,
    DraftAction,
    DraftLifecycle,
    OutcomeEvidence,
    ResultKind,
    SubmissionOutcome,
)
from legacybridge.service import BridgeService
from tests.conftest import FakeHalden
from tests.test_draft_state import ERROR_500, approval, matching_order, ready, reply

WAIT = 10  # upper bound for a hang only; no assertion depends on elapsed time


# =============================================================================== helpers


class ScriptedIds:
    """Thread-safe replacement for ``secrets.token_hex`` returning a fixed sequence (last value repeats)."""

    def __init__(self, *values: str) -> None:
        self.values = list(values)
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, nbytes: int = 4) -> str:
        assert nbytes == 4
        with self._lock:
            value = self.values[min(self.calls, len(self.values) - 1)]
            self.calls += 1
            return value


def force_ids(monkeypatch, *values: str) -> ScriptedIds:
    ids = ScriptedIds(*values)
    monkeypatch.setattr(service_module.secrets, "token_hex", ids)
    return ids


def booked_quantities(fake: FakeHalden, draft_id: str) -> list[tuple[str, int]]:
    order = next(o for o in fake.orders.values() if o["Ref"] == draft_id)
    return [(line["item"], int(line["qty"])) for line in order["lines"]]


class Runner(threading.Thread):
    """Runs ``fn`` and keeps its result or exception; sets ``done`` (and an optional extra event) on exit."""

    def __init__(self, fn, also_set: threading.Event | None = None) -> None:
        super().__init__(daemon=True)
        self.fn, self.also_set = fn, also_set
        self.result = self.error = None
        self.done = threading.Event()

    def run(self) -> None:
        try:
            self.result = self.fn()
        except Exception as exc:  # recorded for assertions
            self.error = exc
        finally:
            self.done.set()
            if self.also_set is not None:
                self.also_set.set()


class ProbeLock:
    """A per-draft re-check lock that reports contention: ``contended`` is set when a second caller
    has to wait. Installed into ``service.store.recheck_locks``; unused by an implementation without it."""

    def __init__(self, contended: threading.Event) -> None:
        self._lock = threading.Lock()
        self.contended = contended

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.acquire(blocking=False):
            return True
        self.contended.set()
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> "ProbeLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class ScriptedListing:
    """``FakeHalden.e6_rows`` replacement. Call *i* follows ``plan[i]``: ``hold-empty`` / ``hold-visible``
    block until ``release[i]`` is set; ``empty`` returns no order; ``visible`` returns the ERP's orders."""

    def __init__(self, fake: FakeHalden, plan: list[str]) -> None:
        self.fake, self.plan = fake, plan
        self.entered = [threading.Event() for _ in plan]
        self.release = [threading.Event() for _ in plan]
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self) -> list[dict]:
        with self._lock:
            index = self.calls
            self.calls += 1
        step = self.plan[min(index, len(self.plan) - 1)]
        if index < len(self.plan):
            self.entered[index].set()
            if step.startswith("hold-"):
                assert self.release[index].wait(WAIT), "listing was never released"
        if step.endswith("empty"):
            return []
        with self.fake.lock:
            return [{k: v for k, v in o.items() if k != "lines"} for o in self.fake.orders.values()]


def unknown_with_late_commit(fake: FakeHalden, service: BridgeService):
    """An approved draft whose E8 reply was a 500 with nothing visible (row 9, UNKNOWN). The order
    then appears in HALDEN, as after a late commit, so a later re-check can reconcile it."""
    draft = ready(service)
    fake.e8 = reply(httpx.Response(500, text=ERROR_500))
    service.submit_order(draft.draft_id, approval(draft.draft_id, 1))
    fake.e8 = None
    assert service.get_draft(draft.draft_id).lifecycle == DraftLifecycle.UNKNOWN
    with fake.lock:
        fake.orders["SO-240111"] = matching_order("SO-240111", draft)
    return draft


def assert_consistent_confirmation(service: BridgeService, fake: FakeHalden, draft_id: str) -> None:
    draft = service.get_draft(draft_id)
    latest = draft.submission
    assert draft.lifecycle == DraftLifecycle.CONFIRMED
    assert latest is not None and latest.outcome == SubmissionOutcome.CONFIRMED
    assert latest.order_no == "SO-240111" and latest.order is not None
    assert PERMITTED_ACTIONS[draft.lifecycle] == (DraftAction.VIEW_ORDER,)
    first_confirmed = next(i for i, r in enumerate(draft.history) if r.outcome == SubmissionOutcome.CONFIRMED)
    assert all(r.outcome == SubmissionOutcome.CONFIRMED for r in draft.history[first_confirmed:])
    assert all(r.e8_requests == 0 for r in draft.history if r.kind == ResultKind.RECHECK)
    assert fake.e8_count == 1 and len(draft.attempts) == 1


# =============================================================================== T1 — draft identity


def test_t1_generated_ids_keep_the_halden_reference_format(fake_service):
    draft = fake_service.prepare_order("00417", [("BW-1001", 1)])
    assert re.fullmatch(r"LB-[0-9A-F]{8}", draft.draft_id)
    assert len(draft.draft_id) <= 12  # HALDEN's reference field width


def test_t1_forced_collision_keeps_original_and_allocates_another_id(fake, fake_service, monkeypatch):
    force_ids(monkeypatch, "aaaaaaaa", "aaaaaaaa", "bbbbbbbb")
    first = fake_service.prepare_order("00417", [("BW-1001", 1)])
    second = fake_service.prepare_order("00417", [("BW-1001", 2)])
    assert first.draft_id == "LB-AAAAAAAA" and second.draft_id == "LB-BBBBBBBB"
    assert [line.quantity for line in fake_service.get_draft("LB-AAAAAAAA").lines] == [1]
    assert [line.quantity for line in fake_service.get_draft("LB-BBBBBBBB").lines] == [2]
    assert fake_service.get_draft("LB-AAAAAAAA") is first
    assert len(fake_service.list_drafts()) == 2
    assert fake.e8_count == 0


def test_t1_collision_preserves_existing_attempts_and_history(fake, fake_service, monkeypatch):
    force_ids(monkeypatch, "aaaaaaaa", "aaaaaaaa", "cccccccc")
    first = ready(fake_service, lines=(("BW-1001", 1),))
    fake.e8 = reply(httpx.Response(500, text=ERROR_500))
    fake_service.submit_order(first.draft_id, approval(first.draft_id, 1))
    fake.e8 = None
    history_before = list(fake_service.get_draft(first.draft_id).history)
    attempts_before = list(fake_service.get_draft(first.draft_id).attempts)
    assert len(history_before) == 1 and history_before[0].outcome == SubmissionOutcome.UNKNOWN

    second = fake_service.prepare_order("00417", [("BW-1001", 2)])

    assert second.draft_id == "LB-CCCCCCCC"
    kept = fake_service.get_draft(first.draft_id)
    assert kept is first and kept.lifecycle == DraftLifecycle.UNKNOWN
    assert kept.history == history_before and kept.attempts == attempts_before
    assert [line.quantity for line in kept.lines] == [1]
    assert [d.draft_id for d in fake_service.unresolved_drafts()] == [first.draft_id]
    assert fake.e8_count == 1  # preparation sent nothing


def test_t1_stale_approval_cannot_authorize_different_content(fake, fake_service, monkeypatch):
    force_ids(monkeypatch, "aaaaaaaa", "aaaaaaaa", "bbbbbbbb")
    reviewed = ready(fake_service, lines=(("BW-1001", 1),))  # the operator reviews quantity 1
    click = approval(reviewed.draft_id, 1)  # the button captured (draft id, attempt 1)
    other = ready(fake_service, lines=(("BW-1001", 2),))  # a second draft draws the same candidate id

    result = fake_service.submit_order(reviewed.draft_id, click)

    assert result.outcome == SubmissionOutcome.CONFIRMED
    assert booked_quantities(fake, reviewed.draft_id) == [("BW-1001", 1)]  # what was reviewed was sent
    assert other.draft_id != reviewed.draft_id
    untouched = fake_service.get_draft(other.draft_id)
    assert untouched.lifecycle == DraftLifecycle.READY and untouched.attempts == [] and untouched.history == []
    with pytest.raises(DraftStateError):
        fake_service.submit_order(reviewed.draft_id, click)  # the consumed approval stays consumed
    assert fake.e8_count == 1


@pytest.mark.parametrize("repeat", range(10))
def test_t1_concurrent_preparation_with_repeated_candidate_ids(monkeypatch, repeat):
    fake = FakeHalden()
    service = BridgeService(fake.client(), None)
    ids = force_ids(monkeypatch, "dddddddd", "dddddddd", "eeeeeeee")
    barrier = threading.Barrier(2)
    stock_extract = service._full_stock_extract

    def stock_after_both_arrive():
        rows = stock_extract()
        barrier.wait(WAIT)  # both preparations finish their reads, then allocate together
        return rows

    monkeypatch.setattr(service, "_full_stock_extract", stock_after_both_arrive)
    runners = [Runner(lambda q=q: service.prepare_order("00417", [("BW-1001", q)])) for q in (1, 2)]
    for runner in runners:
        runner.start()
    for runner in runners:
        assert runner.done.wait(WAIT)
        assert runner.error is None

    drafts = {d.draft_id: d for d in service.list_drafts()}
    assert set(drafts) == {"LB-DDDDDDDD", "LB-EEEEEEEE"}
    for runner in runners:  # each caller's returned draft is the stored one, with its own content
        assert drafts[runner.result.draft_id] is runner.result
    assert sorted(d.lines[0].quantity for d in drafts.values()) == [1, 2]
    assert ids.calls == 3 and fake.e8_count == 0


def test_t1_bounded_retry_exhaustion_fails_explicitly_and_stores_nothing(fake, fake_service, monkeypatch):
    ids = force_ids(monkeypatch, "ffffffff")
    first = fake_service.prepare_order("00417", [("BW-1001", 1)])
    calls_after_first = ids.calls

    with pytest.raises(BridgeError) as raised:
        fake_service.prepare_order("00417", [("BW-1001", 2)])

    attempts = getattr(service_module, "DRAFT_ID_ATTEMPTS", 8)
    assert raised.value.code == "DRAFT_ID_UNAVAILABLE"
    assert ids.calls - calls_after_first == attempts  # bounded, not unbounded
    assert fake_service.list_drafts() == [first]
    assert [line.quantity for line in fake_service.get_draft(first.draft_id).lines] == [1]
    assert fake.e8_count == 0


# =============================================================================== T2 — overlapping re-checks


@pytest.mark.parametrize("repeat", range(5))
def test_t2_first_recheck_completing_last_cannot_supersede_confirmation(repeat):
    """A starts first and holds an empty listing; B would confirm. A stale UNKNOWN must never follow
    the confirmation, and the latest displayed result must match the lifecycle."""
    fake = FakeHalden()
    service = BridgeService(fake.client(), None)
    draft = unknown_with_late_commit(fake, service)
    listing = ScriptedListing(fake, ["hold-empty", "visible"])
    fake.e6_rows = listing
    b_progress = threading.Event()  # B finished, or B is waiting for the per-draft re-check lock
    service.store.recheck_locks = {draft.draft_id: ProbeLock(b_progress)}

    a = Runner(lambda: service.recheck_submission(draft.draft_id))
    a.start()
    assert listing.entered[0].wait(WAIT)  # A is inside its E6 listing
    b = Runner(lambda: service.recheck_submission(draft.draft_id), also_set=b_progress)
    b.start()
    assert b_progress.wait(WAIT)
    listing.release[0].set()
    assert a.done.wait(WAIT) and b.done.wait(WAIT)

    assert a.error is None and b.error is None
    assert (a.result.outcome, a.result.evidence) == (SubmissionOutcome.UNKNOWN, OutcomeEvidence.NO_MATCH_AFTER_UNCERTAIN)
    assert (b.result.outcome, b.result.evidence) == (SubmissionOutcome.CONFIRMED, OutcomeEvidence.RECONCILED)
    assert_consistent_confirmation(service, fake, draft.draft_id)
    history = service.get_draft(draft.draft_id).history
    assert [(r.kind, r.outcome) for r in history] == [
        (ResultKind.SUBMIT, SubmissionOutcome.UNKNOWN),
        (ResultKind.RECHECK, SubmissionOutcome.UNKNOWN),
        (ResultKind.RECHECK, SubmissionOutcome.CONFIRMED),
    ]


@pytest.mark.parametrize("repeat", range(5))
def test_t2_second_recheck_waits_and_cannot_reopen_a_confirmed_draft(repeat):
    """A starts first and will confirm; B would see an empty listing. B must not reconcile while A is
    in flight, and once A has confirmed, B is refused without any HALDEN request."""
    fake = FakeHalden()
    service = BridgeService(fake.client(), None)
    draft = unknown_with_late_commit(fake, service)
    listing = ScriptedListing(fake, ["hold-visible", "empty"])
    fake.e6_rows = listing
    b_progress = threading.Event()
    service.store.recheck_locks = {draft.draft_id: ProbeLock(b_progress)}

    a = Runner(lambda: service.recheck_submission(draft.draft_id))
    a.start()
    assert listing.entered[0].wait(WAIT)
    b = Runner(lambda: service.recheck_submission(draft.draft_id), also_set=b_progress)
    b.start()
    assert b_progress.wait(WAIT)
    assert listing.calls == 1  # B has not listed while A's reconciliation is in flight
    listing.release[0].set()
    assert a.done.wait(WAIT) and b.done.wait(WAIT)

    assert a.error is None and (a.result.outcome, a.result.evidence) == (SubmissionOutcome.CONFIRMED, OutcomeEvidence.RECONCILED)
    assert isinstance(b.error, DraftStateError) and "CONFIRMED" in b.error.message
    assert listing.calls == 1
    assert_consistent_confirmation(service, fake, draft.draft_id)
    assert [(r.kind, r.outcome) for r in service.get_draft(draft.draft_id).history] == [
        (ResultKind.SUBMIT, SubmissionOutcome.UNKNOWN),
        (ResultKind.RECHECK, SubmissionOutcome.CONFIRMED),
    ]


def test_t2_unknown_then_valid_confirmation_sequentially(fake, fake_service):
    draft = unknown_with_late_commit(fake, fake_service)
    listing = ScriptedListing(fake, ["empty", "visible"])
    fake.e6_rows = listing

    first = fake_service.recheck_submission(draft.draft_id)
    assert (first.outcome, fake_service.get_draft(draft.draft_id).lifecycle) == (SubmissionOutcome.UNKNOWN, DraftLifecycle.UNKNOWN)
    assert fake_service.get_draft(draft.draft_id).submission is first
    second = fake_service.recheck_submission(draft.draft_id)
    assert (second.outcome, second.evidence) == (SubmissionOutcome.CONFIRMED, OutcomeEvidence.RECONCILED)
    assert_consistent_confirmation(fake_service, fake, draft.draft_id)
    with pytest.raises(DraftStateError):
        fake_service.recheck_submission(draft.draft_id)
    assert listing.calls == 2 and len(fake_service.get_draft(draft.draft_id).history) == 3


def test_t2_recheck_does_not_hold_the_store_lock_during_io(fake, fake_service):
    draft = unknown_with_late_commit(fake, fake_service)
    listing = ScriptedListing(fake, ["hold-empty", "visible"])
    fake.e6_rows = listing
    a = Runner(lambda: fake_service.recheck_submission(draft.draft_id))
    a.start()
    assert listing.entered[0].wait(WAIT)

    reader = Runner(lambda: (fake_service.get_draft(draft.draft_id), fake_service.list_drafts(),
                             fake_service.unresolved_drafts()))
    reader.start()
    assert reader.done.wait(WAIT) and reader.error is None  # store reads proceed while A is in I/O
    assert fake_service.get_draft(draft.draft_id).lifecycle == DraftLifecycle.UNKNOWN

    listing.release[0].set()
    assert a.done.wait(WAIT) and a.error is None
    assert fake.e8_count == 1
