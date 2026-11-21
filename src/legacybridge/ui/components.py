"""Rendering helpers and the single approve handler of the console."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
import streamlit as st

from ..bridge.errors import BridgeError
from ..bridge.models import (
    PERMITTED_ACTIONS,
    Approval,
    AvailabilityReport,
    Customer,
    DraftAction,
    DraftLifecycle,
    Order,
    OrderDraft,
    OrderSummary,
    OutcomeEvidence,
    Product,
    StockStatus,
    SubmissionOutcome,
    SubmissionResult,
)
from ..service import APPROVAL_USED_MESSAGE, BridgeService

ERROR_MESSAGES = {
    "AUTH_FAILED": "HALDEN ERP rejected the integration credentials. Check LEGACY_LOGIN/LEGACY_SECRET.",
    "RATE_LIMITED": "HALDEN ERP is throttling requests; try again shortly.",
    "MALFORMED_RESPONSE": "HALDEN ERP returned data the bridge cannot interpret.",
}


def approve_draft(service: BridgeService, draft_id: str, attempt_no: int) -> SubmissionResult:
    """The only place that constructs an ``Approval``: one explicit click, one attempt number."""
    approval = Approval(draft_id=draft_id, attempt_no=attempt_no, approved_at=datetime.now(timezone.utc))
    return service.submit_order(draft_id, approval)


def error_message(exc: BaseException) -> str:
    if isinstance(exc, BridgeError):
        if exc.code == "LEGACY_UNAVAILABLE":
            return exc.message if exc.message.startswith("HALDEN ERP is not responding") else "HALDEN ERP is not responding."
        if exc.code == "DRAFT_STATE" and exc.message == APPROVAL_USED_MESSAGE:
            return APPROVAL_USED_MESSAGE
        return ERROR_MESSAGES.get(exc.code, exc.message)
    if isinstance(exc, ValueError):
        return f"Invalid input: {exc}"
    return f"Unexpected error: {type(exc).__name__}: {exc}"


def render_error(exc: BaseException) -> None:
    st.error(error_message(exc))


# --------------------------------------------------------------------------- badges


def money(value: Any) -> str:
    return "—" if value is None else f"€{value}"


def stock_badge(status: StockStatus | None) -> str:
    if status is None:
        return ":gray[—]"
    color = {StockStatus.IN_STOCK: "green", StockStatus.OUT_OF_STOCK: "red", StockStatus.UNDETERMINED: "orange"}[status]
    return f":{color}[{status.value}]"


LIFECYCLE_COLORS = {
    DraftLifecycle.READY: "green",
    DraftLifecycle.BLOCKED: "red",
    DraftLifecycle.SUBMITTING: "gray",
    DraftLifecycle.CONFIRMED: "green",
    DraftLifecycle.REJECTED_BY_ERP: "red",
    DraftLifecycle.NOT_APPLIED: "orange",
    DraftLifecycle.UNKNOWN: "gray",
    DraftLifecycle.REJECTED_BY_USER: "gray",
}
OUTCOME_COLORS = {
    SubmissionOutcome.CONFIRMED: "green",
    SubmissionOutcome.REJECTED: "red",
    SubmissionOutcome.NOT_APPLIED: "orange",
    SubmissionOutcome.UNKNOWN: "gray",
}


def lifecycle_badge(lifecycle: DraftLifecycle) -> str:
    return f":{LIFECYCLE_COLORS[lifecycle]}[{lifecycle.value}]"


def outcome_badge(outcome: SubmissionOutcome) -> str:
    return f":{OUTCOME_COLORS[outcome]}[{outcome.value}]"


# --------------------------------------------------------------------------- tables


def customers_frame(customers: list[Customer]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Account": c.account_id,
                "Name": c.name,
                "City": c.city or "—",
                "Country": c.country or "—",
                "Credit hold": "ON HOLD" if c.credit_hold else "no recorded hold",
                "Issues": ", ".join(i.code.value for i in c.issues),
            }
            for c in customers
        ]
    )


def orders_frame(orders: list[OrderSummary]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Order": o.order_no,
                "Account": o.account_id,
                "Date": o.order_date.isoformat() if o.order_date else "—",
                "Status": o.status.value,
                "Raw status": o.raw_status,
                "Total": money(o.total),
                "Reference": o.customer_reference or "—",
                "Issues": ", ".join(i.code.value for i in o.issues),
            }
            for o in orders
        ]
    )


def render_customer_card(customer: Customer) -> None:
    hold = ":red[CREDIT HOLD]" if customer.credit_hold else ":gray[no recorded hold]"
    st.markdown(f"#### {customer.name} · `{customer.account_id}` {hold}")
    cols = st.columns(4)
    cols[0].markdown(f"**Contact**  \n{customer.contact_name or '—'}")
    cols[1].markdown(f"**Email**  \n{customer.email or '—'}")
    cols[2].markdown(f"**Phone**  \n{customer.phone or '—'}")
    cols[3].markdown(f"**Location**  \n{customer.city or '—'}, {customer.country or '—'}")
    for issue in customer.issues:
        st.markdown(f":orange[{issue.code.value}] {issue.message}")


def render_order_detail(order: Order) -> None:
    st.markdown(f"#### {order.order_no} · {order.status.value} (raw `{order.raw_status}`)")
    st.dataframe(
        pd.DataFrame(
            [
                {"Line": l.line_no, "SKU": l.sku, "Quantity": l.quantity, "Unit price": money(l.unit_price), "Line total": money(l.line_total)}
                for l in order.lines
            ]
        ),
        hide_index=True,
    )
    st.markdown(f"Header total **{money(order.total)}** · lines total **{money(order.lines_total)}**")
    for issue in order.issues:
        st.warning(f"{issue.code.value}: {issue.message}")


def catalogue_frame(rows: list[tuple[Product, AvailabilityReport | None]]) -> pd.DataFrame:
    data = []
    for product, report in rows:
        issues = [i.code.value for i in product.issues] + ([i.code.value for i in report.issues] if report else [])
        data.append(
            {
                "SKU": product.sku,
                "Name": product.name,
                "Category": product.category,
                "Unit price": money(product.unit_price),
                "Legacy flag": {True: "Y", False: "N", None: "—"}[product.legacy_available_flag],
                "Availability": report.status.value if report else "—",
                "Available": str(report.available_quantity) if report and report.available_quantity is not None else "—",
                "Issues": ", ".join(dict.fromkeys(issues)),
            }
        )
    return pd.DataFrame(data)


def render_availability(report: AvailabilityReport) -> None:
    quantity = report.available_quantity if report.available_quantity is not None else "Cannot be determined"
    st.markdown(f"#### {report.sku} · {report.product_name or ''} {stock_badge(report.status)}")
    cols = st.columns(3)
    cols[0].metric("Available", str(quantity))
    cols[1].metric("Known minimum", "—" if report.known_minimum is None else str(report.known_minimum))
    cols[2].metric("Legacy flag", {True: "Y", False: "N", None: "—"}[report.legacy_available_flag])
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Warehouse": w.warehouse,
                    "Raw ON_HAND": repr(w.raw_on_hand),
                    "Raw ALLOCATED": repr(w.raw_allocated),
                    "On hand": "?" if w.on_hand is None else str(w.on_hand),
                    "Allocated": "?" if w.allocated is None else str(w.allocated),
                    "Available": "?" if w.available is None else str(w.available),
                }
                for w in report.warehouses
            ]
        ),
        hide_index=True,
    )
    for issue in report.issues:
        st.markdown(f":orange[{issue.code.value}] {issue.message}")
    st.info(report.explanation)


# --------------------------------------------------------------------------- order desk


def render_result(result: SubmissionResult, draft: OrderDraft) -> None:
    st.markdown(f"**Outcome** {outcome_badge(result.outcome)} · **Evidence** `{result.evidence.value}` · {result.kind.value}")
    text = result.message
    if result.outcome == SubmissionOutcome.CONFIRMED:
        st.success(text)
    elif result.outcome == SubmissionOutcome.REJECTED:
        st.error(text)
    elif result.outcome == SubmissionOutcome.NOT_APPLIED:
        st.warning(text)
    else:
        st.info(text)
    cols = st.columns(4)
    attempt = draft.attempts[result.attempt_no - 1] if 0 < result.attempt_no <= len(draft.attempts) else None
    cols[0].metric("E8 requests for this attempt", attempt.e8_requests if attempt else result.e8_requests)
    cols[1].metric("Order no", result.order_no or "—")
    cols[2].metric("ERP booked total", money(result.erp_booked_total))
    cols[3].metric("Estimate", money(draft.estimated_total))
    st.caption(f"Evidence detail: {result.evidence_detail}")
    if result.evidence == OutcomeEvidence.ACKNOWLEDGED:
        st.caption("Acknowledged by HALDEN — not verified by read-back.")
    if result.erp_booked_total is not None and draft.estimated_total is not None and result.erp_booked_total != draft.estimated_total:
        st.caption("The booked total differs from the estimate. HALDEN prices the booking; the price is reported, not verified.")


def history_frame(draft: OrderDraft) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Attempt": r.attempt_no,
                "Kind": r.kind.value,
                "Outcome": r.outcome.value,
                "Evidence": r.evidence.value,
                "E8 requests": r.e8_requests,
                "Order": r.order_no or "—",
                "Recorded (UTC)": r.recorded_at.strftime("%H:%M:%S"),
                "Detail": r.evidence_detail,
            }
            for r in draft.history
        ]
    )


def permitted_actions(draft: OrderDraft) -> tuple[DraftAction, ...]:
    return PERMITTED_ACTIONS[draft.lifecycle]


def approve_label(draft: OrderDraft) -> str:
    return f"Approve attempt {draft.next_attempt_no} and send to HALDEN"
