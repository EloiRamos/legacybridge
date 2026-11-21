"""LEGACYBRIDGE console. Run with ``streamlit run src/legacybridge/ui/app.py``."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from legacybridge.assistant.agent import assistant_available, build_agent, build_chat_model, run_assistant
from legacybridge.assistant.scenarios import SCENARIOS
from legacybridge.assistant.tools import make_tools
from legacybridge.bridge.errors import BridgeError, DraftStateError, LegacyMalformedResponse
from legacybridge.bridge.models import DraftAction, DraftLifecycle, StockStatus
from legacybridge.bridge.normalize import CANONICAL_CATEGORIES, UNCATEGORIZED
from legacybridge.ui import components as ui
from legacybridge.ui import runtime

VIEWS = ["Overview", "Customers & Orders", "Inventory", "Order Desk", "Assistant"]
LEGACY_CARD = "HALDEN ERP 3.1 — simulated legacy system (Java 8 · Tomcat 9 · JDBC/SQLite)"
TRUST_NOTICE = "Local single-operator demo — approvals are not tied to an authenticated user."
POOLED_CAPTION = (
    "Available = free units in HALDEN's pooled allocation across DUS and MAN at read time. "
    "Not a delivery date or reservation."
)
STOCK_BANNER = "Stock extract unreadable — no availability shown"

st.set_page_config(page_title="LEGACYBRIDGE", layout="wide")
service = runtime.get_service()
settings = runtime.get_settings()
state = st.session_state
state.setdefault("flash", [])
state.setdefault("scenario_runs", {})


def invalidate_overview() -> None:
    state.pop("overview", None)


def flash(kind: str, text: str) -> None:
    state["flash"].append((kind, text))


def show_flash() -> None:
    for kind, text in state["flash"]:
        getattr(st, kind)(text)
    state["flash"] = []


# ----------------------------------------------------------------------------- callbacks


def on_approve(draft_id: str, attempt_no: int) -> None:
    try:
        result = ui.approve_draft(service, draft_id, attempt_no)
        flash("success" if result.outcome.value == "CONFIRMED" else "info", f"Attempt {attempt_no}: {result.message}")
    except DraftStateError:
        flash("error", "This approval was already used.")
    except BridgeError as exc:
        flash("error", ui.error_message(exc))
    invalidate_overview()


def on_reject(draft_id: str) -> None:
    try:
        draft = service.reject_draft(draft_id)
        flash("info", service.rejection_message(draft))
    except BridgeError as exc:
        flash("error", ui.error_message(exc))


def on_recheck(draft_id: str) -> None:
    try:
        result = service.recheck_submission(draft_id)
        flash("success" if result.outcome.value == "CONFIRMED" else "info", f"Re-check: {result.message}")
    except BridgeError as exc:
        flash("error", ui.error_message(exc))
    invalidate_overview()


def on_reset() -> None:
    try:
        ok = service.reset_demo()
        flash("success" if ok else "warning", "Demo data reset." if ok else "Simulation control is not available.")
    except BridgeError as exc:
        flash("error", ui.error_message(exc))
    state["scenario_runs"] = {}
    state.pop("selected_draft", None)
    state.pop("customer_options", None)
    invalidate_overview()


# ----------------------------------------------------------------------------- sidebar

with st.sidebar:
    st.title("LEGACYBRIDGE")
    st.caption("Order-desk integration for a simulated Java 8 legacy ERP")
    view = st.radio("View", VIEWS, key="view")
    st.divider()
    st.markdown(f"**{LEGACY_CARD}**")
    try:
        banner = service.client.ping()
        st.markdown(f":green[Connected] · `{banner}`")
    except BridgeError as exc:
        st.markdown(f":red[Not reachable] · {ui.error_message(exc)}")
    available, reason = assistant_available(settings)
    st.markdown("**Mode A** deterministic · always available")
    st.markdown(f"**Mode B** AI assistant · {'available' if available else 'unavailable'}")
    st.info(TRUST_NOTICE)
    if runtime.sim_control_responds(service):
        st.markdown("**Simulation control**")
        confirm = st.toggle("Confirm reset", key="confirm_reset")
        st.button("Reset demo data", on_click=on_reset, disabled=not confirm, key="reset_demo")

show_flash()


# ----------------------------------------------------------------------------- shared draft panel


def render_draft_panel(draft_id: str, key_prefix: str) -> None:
    try:
        draft = service.get_draft(draft_id)
    except BridgeError as exc:
        ui.render_error(exc)
        return
    st.markdown(f"### Draft `{draft.draft_id}` · {draft.customer_name} (`{draft.account_id}`) {ui.lifecycle_badge(draft.lifecycle)}")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "SKU": l.sku,
                    "Product": l.product_name or "—",
                    "Quantity": str(l.quantity),
                    "Unit price (estimate)": ui.money(l.unit_price),
                    "Line total (estimate)": ui.money(l.line_total),
                    "Availability": l.availability.value if l.availability else "—",
                    "Available": "—" if l.available_quantity is None else str(l.available_quantity),
                    "Blocking reasons": "; ".join(l.blocking_reasons),
                }
                for l in draft.lines
            ]
        ),
        hide_index=True,
    )
    st.markdown(f"Estimated total **{ui.money(draft.estimated_total)}** — Estimate — HALDEN prices the booking")
    for reason in draft.blocking_reasons:
        st.error(reason)
    actions = ui.permitted_actions(draft)
    cols = st.columns(4)
    if DraftAction.APPROVE in actions:
        n = draft.next_attempt_no
        cols[0].button(ui.approve_label(draft), type="primary", on_click=on_approve, args=(draft.draft_id, n),
                       key=f"{key_prefix}-approve-{draft.draft_id}-{n}")
    if DraftAction.REJECT in actions:
        cols[1].button("Reject", on_click=on_reject, args=(draft.draft_id,), key=f"{key_prefix}-reject-{draft.draft_id}")
    if DraftAction.DISCARD in actions:
        cols[1].button("Discard", on_click=on_reject, args=(draft.draft_id,), key=f"{key_prefix}-discard-{draft.draft_id}")
    if DraftAction.RECHECK in actions:
        cols[0].button("Re-check outcome", on_click=on_recheck, args=(draft.draft_id,), key=f"{key_prefix}-recheck-{draft.draft_id}")
    if draft.lifecycle == DraftLifecycle.SUBMITTING:
        st.info("Submitting…")
    if draft.lifecycle == DraftLifecycle.REJECTED_BY_USER:
        st.info(service.rejection_message(draft))
    if draft.submission is not None:
        ui.render_result(draft.submission, draft)
        if DraftAction.VIEW_ORDER in actions and draft.submission.order is not None:
            with st.expander(f"View order {draft.submission.order_no}"):
                ui.render_order_detail(draft.submission.order)
    if draft.history:
        st.markdown("**Attempt and re-check history**")
        st.dataframe(ui.history_frame(draft), hide_index=True)


# ----------------------------------------------------------------------------- views


def view_overview() -> None:
    st.header("Overview")
    if st.button("Refresh"):
        invalidate_overview()
    if "overview" not in state:
        try:
            state["overview"] = service.overview()
        except BridgeError as exc:
            ui.render_error(exc)
            return
    m = state["overview"]
    cols = st.columns(6)
    cols[0].metric("Customers", m.customer_count)
    cols[1].metric("On credit hold", m.credit_hold_count)
    cols[2].metric("Outstanding orders", m.outstanding_order_count)
    cols[3].metric("Undelivered order value (header totals)", ui.money(m.outstanding_order_value))
    cols[4].metric("Products", m.product_count)
    cols[5].metric("Stock undetermined", m.stock_status_counts.get(StockStatus.UNDETERMINED, 0))
    st.caption("On credit hold counts recorded holds; other accounts have no recorded hold, which is not a credit assurance.")
    st.caption("Figures are computed live from HALDEN via its published HTTP interface.")
    left, right = st.columns([1, 2])
    with left:
        st.subheader("Data-quality issues")
        st.dataframe(pd.DataFrame([{"Issue": k.value, "Count": v} for k, v in m.issue_counts.items()]), hide_index=True)
    with right:
        st.subheader("Outstanding orders")
        st.dataframe(ui.orders_frame(m.outstanding_orders), hide_index=True)


def view_customers() -> None:
    st.header("Customers & Orders")
    query = st.text_input("Search by company name or account number", value="nordkap", key="customer_query")
    try:
        matches = service.search_customers(query)
    except BridgeError as exc:
        ui.render_error(exc)
        return
    if not matches:
        st.info(f"No customer matches '{query}'.")
        return
    if len(matches) > 1:
        st.warning(f"{len(matches)} customers match — select the correct account")
    st.dataframe(ui.customers_frame(matches), hide_index=True)
    options = ["— select —"] + [f"{c.account_id} — {c.name}" for c in matches] if len(matches) > 1 else [f"{c.account_id} — {c.name}" for c in matches]
    choice = st.selectbox("Account", options, key="customer_choice")
    if choice == "— select —":
        return
    account_id = choice.split(" — ")[0]
    try:
        overview = service.get_customer_overview(account_id)
    except BridgeError as exc:
        ui.render_error(exc)
        return
    ui.render_customer_card(overview.customer)
    only_outstanding = st.toggle("Outstanding only", key="outstanding_only")
    orders = overview.outstanding if only_outstanding else overview.orders
    st.dataframe(ui.orders_frame(orders), hide_index=True)
    if orders:
        order_no = st.selectbox("Order", [o.order_no for o in orders], key="order_choice")
        try:
            ui.render_order_detail(service.get_order(order_no))
        except BridgeError as exc:
            ui.render_error(exc)


def view_inventory() -> None:
    st.header("Inventory")
    cols = st.columns(2)
    query = cols[0].text_input("Search SKU or description", key="product_query")
    category = cols[1].selectbox("Category", ["All", *CANONICAL_CATEGORIES, UNCATEGORIZED], key="product_category")
    category_filter = None if category == "All" else category
    rows: list = []
    try:
        rows = service.product_catalogue(query or None, category_filter)
    except LegacyMalformedResponse:
        st.error(STOCK_BANNER)
        try:
            rows = [(p, None) for p in service.search_products(query or None, category_filter)]
        except BridgeError as exc:
            ui.render_error(exc)
            return
    except BridgeError as exc:
        ui.render_error(exc)
        return
    st.caption(POOLED_CAPTION)
    st.dataframe(ui.catalogue_frame(rows), hide_index=True)
    if rows:
        sku = st.selectbox("Product", [p.sku for p, _ in rows], key="product_choice")
        try:
            ui.render_availability(service.check_availability(sku))
        except LegacyMalformedResponse:
            st.error(STOCK_BANNER)
        except BridgeError as exc:
            ui.render_error(exc)


def customer_options() -> list[str]:
    if "customer_options" not in state:
        state["customer_options"] = [f"{c.account_id} — {c.name}" for c in service.search_customers("")]
    return state["customer_options"]


def view_order_desk() -> None:
    st.header("Order Desk")
    try:
        options = customer_options()
    except BridgeError as exc:
        ui.render_error(exc)
        return
    default = next((i for i, o in enumerate(options) if o.startswith("00417")), 0)
    choice = st.selectbox("Customer", options, index=default, key="desk_customer")
    account_id = choice.split(" — ")[0]
    unresolved = service.unresolved_drafts(account_id)
    if unresolved:
        st.warning(
            "Unresolved draft(s) for this customer: "
            + ", ".join(d.draft_id for d in unresolved)
            + ". The outcome is unknown — use Re-check before preparing the same goods again."
        )
    lines = st.data_editor(
        pd.DataFrame([{"sku": "BW-1001", "quantity": 10}]),
        num_rows="dynamic",
        column_config={
            "sku": st.column_config.TextColumn("SKU", required=True),
            "quantity": st.column_config.NumberColumn("Quantity", min_value=1, max_value=999, step=1, required=True),
        },
        key="desk_lines",
    )
    if st.button("Prepare draft", key="prepare_draft"):
        try:
            records = [
                (str(row["sku"]), int(row["quantity"]))
                for _, row in lines.iterrows()
                if isinstance(row.get("sku"), str) and row["sku"].strip() and pd.notna(row.get("quantity"))
            ]
            draft = service.prepare_order(account_id, records)
            state["selected_draft"] = draft.draft_id
        except (BridgeError, ValueError) as exc:
            ui.render_error(exc)
    drafts = service.list_drafts()
    if not drafts:
        return
    ids = [d.draft_id for d in reversed(drafts)]
    selected = state.get("selected_draft")
    index = ids.index(selected) if selected in ids else 0
    draft_id = st.selectbox(
        "Draft", ids, index=index, key="desk_draft",
        format_func=lambda i: f"{i} · {service.get_draft(i).customer_name}",
    )
    state["selected_draft"] = draft_id
    render_draft_panel(draft_id, "desk")


def view_assistant() -> None:
    st.header("Assistant")
    scenarios_tab, ai_tab = st.tabs(["Guided scenarios", "AI assistant"])
    with scenarios_tab:
        st.info("Deterministic scenario — no AI model involved.")
        for scenario in SCENARIOS:
            with st.container(border=True):
                st.markdown(f"**{scenario.id} · {scenario.title}** · Workflow {scenario.workflow}  \n{scenario.summary}")
                if st.button(f"Run {scenario.id}", key=f"run-{scenario.id}"):
                    state["scenario_runs"][scenario.id] = scenario.run(service)
                    invalidate_overview()
                run = state["scenario_runs"].get(scenario.id)
                if run is None:
                    continue
                for note in run.notes:
                    st.caption(note)
                for step in run.steps:
                    mark = ":green[✓]" if step.ok else ":red[✗]"
                    st.markdown(f"{mark} **{step.label}** · `{step.operation}`  \n{step.message}")
                if run.pending_draft_id:
                    render_draft_panel(run.pending_draft_id, f"scenario-{scenario.id}")
    with ai_tab:
        available, reason = assistant_available(settings)
        if not available:
            st.info(f"AI assistant unavailable: {reason}")
            return
        st.markdown(f"**AI assistant · {settings.llm_model}**")
        st.caption("Model behavior has not been evaluated; tool calls are shown as evidence.")
        question = st.chat_input("Ask about customers, stock or a draft order")
        if question:
            try:
                agent = build_agent(build_chat_model(settings), make_tools(service))
            except Exception as exc:  # provider error or missing package
                st.error(f"AI assistant unavailable: {exc}")
                return
            run = run_assistant(agent, question)
            if run.error:
                st.error(f"AI assistant unavailable: {run.error}")
            for record in run.tool_calls:
                with st.expander(f"Tool call · {record.name} · {'ok' if record.ok else 'error'}"):
                    st.json({"args": record.args, "data": record.data, "error": record.error})
            if run.answer_text:
                st.caption("Model summary")
                st.markdown(run.answer_text)
            for draft_id in run.draft_ids:
                render_draft_panel(draft_id, "assistant")


{
    "Overview": view_overview,
    "Customers & Orders": view_customers,
    "Inventory": view_inventory,
    "Order Desk": view_order_desk,
    "Assistant": view_assistant,
}[view]()
