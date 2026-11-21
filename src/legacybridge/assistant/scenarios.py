"""Deterministic Guided Scenarios (Mode A). No AI model is involved.

Each step calls the real ``BridgeService`` against the Java ERP. Scenarios never submit: pending
drafts are approved only through the console's approve handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from ..bridge.errors import BridgeError
from ..service import BridgeService

RESET_NOTE = "For reproducible results, use Reset demo data in the sidebar before running this scenario."
SIM_LABEL = "Simulation setup — not visible to the integration"
REQUIRES_SIM_NOTE = "Requires simulator control"


@dataclass
class StepRecord:
    label: str
    operation: str
    inputs: dict[str, Any]
    ok: bool
    result: Any
    message: str


@dataclass
class ScenarioRun:
    steps: list[StepRecord] = field(default_factory=list)
    pending_draft_id: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    workflow: Literal["A", "B", "C"]
    summary: str
    run: Callable[[BridgeService], ScenarioRun]


def _step(run: ScenarioRun, label: str, operation: str, inputs: dict[str, Any], call: Callable[[], Any],
          describe: Callable[[Any], str]) -> Any:
    try:
        result = call()
    except BridgeError as exc:
        run.steps.append(StepRecord(label, operation, inputs, False, None, f"{exc.code}: {exc.message}"))
        return None
    run.steps.append(StepRecord(label, operation, inputs, True, result, describe(result)))
    return result


def _draft_text(draft) -> str:
    reasons = list(draft.blocking_reasons) + [r for line in draft.lines for r in line.blocking_reasons]
    estimate = f"€{draft.estimated_total}" if draft.estimated_total is not None else "no estimate"
    text = f"Draft {draft.draft_id} is {draft.lifecycle.value} (estimate {estimate})."
    return text + (" Blocking reasons: " + "; ".join(reasons) if reasons else "")


def s1(service: BridgeService) -> ScenarioRun:
    run = ScenarioRun(notes=[RESET_NOTE])
    _step(run, "Find the customer", "search_customers", {"query": "nordkap"},
          lambda: service.search_customers("nordkap"), lambda r: f"{len(r)} match(es): " + ", ".join(f"{c.account_id} {c.name}" for c in r))
    _step(run, "Customer overview", "get_customer_overview", {"account_id": "00417"},
          lambda: service.get_customer_overview("00417"),
          lambda r: f"{len(r.orders)} orders; outstanding: " + ", ".join(f"{o.order_no} ({o.status.value})" for o in r.outstanding))
    _step(run, "Order detail", "get_order", {"order_no": "SO-240102"},
          lambda: service.get_order("SO-240102"),
          lambda r: f"{r.order_no} {r.status.value}, total €{r.total}, {len(r.lines)} line(s).")
    return run


def s2(service: BridgeService) -> ScenarioRun:
    run = ScenarioRun(notes=[RESET_NOTE])
    result = _step(run, "Search an ambiguous name", "search_customers", {"query": "alder"},
                   lambda: service.search_customers("alder"),
                   lambda r: f"{len(r)} matches: " + ", ".join(
                       f"{c.account_id} {c.name}{' (credit hold)' if c.credit_hold else ''}" for c in r))
    if result is not None:
        run.notes.append("Selection required — the console never auto-picks.")
    return run


def s3(service: BridgeService) -> ScenarioRun:
    run = ScenarioRun(notes=[RESET_NOTE])
    for sku in ("BW-1002", "BW-1009", "BW-1005", "BW-1006"):
        _step(run, f"Stock truth for {sku}", "check_availability", {"sku": sku},
              lambda sku=sku: service.check_availability(sku),
              lambda r: f"{r.status.value}"
              + (f" {r.available_quantity}" if r.available_quantity is not None else "")
              + (f" (known minimum {r.known_minimum})" if r.status.value == "UNDETERMINED" and r.known_minimum is not None else "")
              + (" · issues: " + ", ".join(i.code.value for i in r.issues) if r.issues else "")
              + f" — {r.explanation}")
    return run


def s4(service: BridgeService) -> ScenarioRun:
    run = ScenarioRun(notes=[RESET_NOTE])
    draft = _step(run, "Prepare a draft for review", "prepare_order",
                  {"account_id": "00417", "lines": [["BW-1001", 10]]},
                  lambda: service.prepare_order("00417", [("BW-1001", 10)]), _draft_text)
    if draft is not None and draft.lifecycle.value == "READY":
        run.pending_draft_id = draft.draft_id
        run.notes.append("Review the draft below and Approve or Reject it. The scenario itself never submits.")
    return run


def s5(service: BridgeService) -> ScenarioRun:
    run = ScenarioRun(notes=[RESET_NOTE])
    _step(run, "Customer on credit hold", "prepare_order", {"account_id": "10871", "lines": [["BW-1001", 1]]},
          lambda: service.prepare_order("10871", [("BW-1001", 1)]), _draft_text)
    lines = [("BW-1007", 1), ("BW-1008", 1), ("BW-1009", 1), ("BW-1010", 5)]
    _step(run, "Unsafe lines", "prepare_order", {"account_id": "52290", "lines": [list(x) for x in lines]},
          lambda: service.prepare_order("52290", lines), _draft_text)
    return run


def s6(service: BridgeService) -> ScenarioRun:
    run = ScenarioRun(notes=[RESET_NOTE])
    if service.sim is None:
        run.notes.append(REQUIRES_SIM_NOTE)
        return run
    draft = _step(run, "Prepare a draft while 2 units are available", "prepare_order",
                  {"account_id": "52290", "lines": [["BW-1010", 2]]},
                  lambda: service.prepare_order("52290", [("BW-1010", 2)]), _draft_text)
    if draft is None:
        return run
    try:
        order_no = service.simulate_external_order("31002", "BW-1010", 2)
    except BridgeError as exc:
        if exc.code == "SIMULATION_UNAVAILABLE":
            service.reject_draft(draft.draft_id)
            run.notes.append(REQUIRES_SIM_NOTE)
            run.notes.append("The prepared draft was discarded; nothing was sent to HALDEN.")
            return run
        run.steps.append(StepRecord(f"{SIM_LABEL}: a counter-sales terminal books 2 × BW-1010 directly in HALDEN ERP",
                                    "simulate_external_order", {"account_id": "31002", "sku": "BW-1010", "quantity": 2},
                                    False, None, f"{exc.code}: {exc.message}"))
        return run
    run.steps.append(StepRecord(
        f"{SIM_LABEL}: a counter-sales terminal books 2 × BW-1010 directly in HALDEN ERP",
        "simulate_external_order", {"account_id": "31002", "sku": "BW-1010", "quantity": 2}, True, order_no,
        f"Simulation: a counter-sales terminal books 2 × BW-1010 directly in HALDEN ERP (order {order_no})."))
    if draft.lifecycle.value == "READY":
        run.pending_draft_id = draft.draft_id
        run.notes.append("The draft was prepared while 2 units were available. Approve it to see how HALDEN answers.")
    return run


def s7(service: BridgeService) -> ScenarioRun:
    run = ScenarioRun(notes=[RESET_NOTE])
    if service.sim is None:
        run.notes.append(REQUIRES_SIM_NOTE)
        return run
    try:
        armed = service.arm_fault("POST", "/halden/orders", "commit_then_500", 1)
    except BridgeError as exc:
        if exc.code == "SIMULATION_UNAVAILABLE":
            run.notes.append(REQUIRES_SIM_NOTE)
            return run
        run.steps.append(StepRecord(f"{SIM_LABEL}: the next order post commits and then fails with HTTP 500",
                                    "arm_fault", {"mode": "commit_then_500"}, False, None, f"{exc.code}: {exc.message}"))
        return run
    run.steps.append(StepRecord(
        f"{SIM_LABEL}: the next order post commits and then fails with HTTP 500", "arm_fault",
        {"method": "POST", "path_prefix": "/halden/orders", "mode": "commit_then_500", "count": 1}, True, armed,
        "Simulation: one commit-then-500 fault armed on POST /halden/orders."))
    draft = _step(run, "Prepare a draft for review", "prepare_order", {"account_id": "52290", "lines": [["BW-1004", 1]]},
                  lambda: service.prepare_order("52290", [("BW-1004", 1)]), _draft_text)
    if draft is not None and draft.lifecycle.value == "READY":
        run.pending_draft_id = draft.draft_id
        run.notes.append("Approve the draft: the reply is lost, and the outcome is established from ERP evidence only.")
    return run


SCENARIOS: list[Scenario] = [
    Scenario("S1", "Customer 360 — Nordkap Marine", "A", "Find the account, its orders and an order detail.", s1),
    Scenario("S2", "Ambiguous account — Alder & Finch", "A", "Two similar accounts; one is on credit hold.", s2),
    Scenario("S3", "Stock truth check", "B", "Verdicts the legacy flag gets wrong, and what cannot be determined.", s3),
    Scenario("S4", "Order with human approval", "C", "Prepare a draft; a human approves or rejects it.", s4),
    Scenario("S5", "Pre-checks block unsafe orders", "C", "Credit hold, discontinued, no price, undetermined, short.", s5),
    Scenario("S6", "Stale draft rejected by the ERP", "C", "Another sales channel takes the stock before approval.", s6),
    Scenario("S7", "Unreliable ERP write", "C", "The booking commits but the reply is lost.", s7),
]


def get_scenario(scenario_id: str) -> Scenario:
    return next(s for s in SCENARIOS if s.id == scenario_id)
