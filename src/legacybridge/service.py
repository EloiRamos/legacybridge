"""Business orchestration for the three workflows.

``BridgeService`` is the single entry point used by the UI, the deterministic scenarios and the
read/draft tools. Submission and re-check implement ONE APPROVAL, ONE ORDER POST:

    ONE APPROVAL, ONE ORDER POST. One explicit human approval authorizes at most one E8 request
    (POST /halden/orders) handed to the HTTP transport. This is a wire-level guarantee, not a
    statement about successful business operations. Before that request, the integration may
    perform authentication preparation and validation reads. Once the E8 request has been handed
    to the transport, the approval is consumed and cannot authorize another E8, whatever the
    response: no re-login-and-resend, no wait-and-resend, no retry. A definitive pre-handler
    rejection is recorded as NOT_APPLIED and a further attempt requires a fresh explicit human
    approval. A timeout, lost response or ambiguous server error is never answered with another
    E8; only read-only reconciliation is allowed. Simulator fault-control state is never evidence:
    the integration does not read it. Read requests keep their own bounded retries.
"""

from __future__ import annotations

import secrets
import threading
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .bridge.client import LegacyClient, SimControlClient
from .bridge.errors import BridgeError, DraftStateError, LegacyMalformedResponse, NotFoundError
from .bridge.models import (
    LIFECYCLE_FOR_OUTCOME,
    Approval,
    AvailabilityReport,
    Customer,
    CustomerOverview,
    DraftLifecycle,
    DraftLine,
    E8Observation,
    IssueCode,
    Order,
    OrderDraft,
    OrderStatus,
    OrderSummary,
    OutcomeEvidence,
    OverviewMetrics,
    Product,
    RawStockRow,
    ResultKind,
    StockStatus,
    SubmissionAttempt,
    SubmissionOutcome,
    SubmissionResult,
    quantize_money,
)
from .bridge.normalize import (
    assess_stock,
    build_customer,
    build_order,
    build_order_summary,
    build_products,
    normalize_account_id,
    normalize_sku,
    parse_money,
    parse_quantity,
)

MAX_CUSTOMER_PAGES = 50
MAX_ORDER_PAGES = 100
MAX_ITEM_PAGES = 50
MAX_DRAFT_LINES = 10
DRAFT_ID_ATTEMPTS = 8  # bounded candidate draws before preparation fails without storing anything

REJECTED_BY_OPERATOR_MESSAGE = "Rejected by the operator. This action sent nothing to HALDEN."
APPROVAL_USED_MESSAGE = "This approval was already used."


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DraftStore:
    """In-memory draft store; every mutation of a draft happens under ``lock``.

    ``recheck_locks`` holds one lock per re-checked draft. It is created under ``lock`` and acquired
    without holding ``lock``, so overlapping re-checks of one draft run one after another while
    ``lock`` itself is never held across HALDEN I/O."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.drafts: dict[str, OrderDraft] = {}
        self.recheck_locks: dict[str, threading.Lock] = {}


class BridgeService:
    def __init__(self, client: LegacyClient, sim: SimControlClient | None = None) -> None:
        self.client = client
        self.sim = sim
        self.store = DraftStore()

    # ================================================================== Workflow A

    def search_customers(self, query: str) -> list[Customer]:
        text = (query or "").strip()
        if text.isdigit() and 1 <= len(text) <= 5:
            return self._customer_pages(acct=int(text))
        return self._customer_pages(name=text)

    def _customer_pages(self, name: str | None = None, acct: int | None = None) -> list[Customer]:
        customers: list[Customer] = []
        offset = 0
        for _ in range(MAX_CUSTOMER_PAGES):
            body = self.client.search_customers_raw(name=name, acct=acct, offset=offset)
            customers.extend(build_customer(row) for row in body["rows"])
            if body.get("next_offset") is None:
                return customers
            offset = body["next_offset"]
        raise LegacyMalformedResponse("HALDEN ERP customer paging did not terminate.")

    def get_customer(self, account_id: str) -> Customer:
        account = normalize_account_id(account_id)
        matches = [c for c in self._customer_pages(acct=int(account)) if c.account_id == account]
        if not matches:
            raise NotFoundError(f"No customer with account {account}.")
        return matches[0]

    def list_orders(self, account_id: str | None = None) -> list[OrderSummary]:
        acct = int(normalize_account_id(account_id)) if account_id is not None else None
        return [build_order_summary(row) for row in self._order_rows(acct)]

    def _order_rows(self, acct: int | None) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_ORDER_PAGES):
            body = self.client.list_orders_raw(acct=acct, cursor=cursor)
            rows.extend(body["data"]["orders"])
            cursor = body["continue"]
            if cursor == "":
                return rows
        raise LegacyMalformedResponse("HALDEN ERP order paging did not terminate.")

    def get_customer_overview(self, account_id: str) -> CustomerOverview:
        customer = self.get_customer(account_id)
        orders = self.list_orders(customer.account_id)
        return CustomerOverview(customer=customer, orders=orders, outstanding=[o for o in orders if o.is_outstanding])

    def get_order(self, order_no: str) -> Order:
        body = self.client.get_order_raw(order_no.strip().upper())
        if body["rc"] == 404:
            raise NotFoundError(f"Order {order_no.strip().upper()} is not on file.")
        if body["rc"] != 0:
            raise LegacyMalformedResponse(f"HALDEN ERP answered rc {body['rc']} for an order read.")
        try:
            return build_order(body["order"])
        except (ValueError, TypeError) as exc:
            raise LegacyMalformedResponse(f"HALDEN ERP returned an order the bridge cannot interpret ({exc}).") from exc

    # ================================================================== Workflow B

    def _catalogue_snapshot(self) -> list[Product]:
        """All E4 pages without q/cat, grouped by canonical SKU before any filtering."""
        records, pages = self.client.list_items_raw(1)
        if pages > MAX_ITEM_PAGES:
            raise LegacyMalformedResponse(f"HALDEN ERP reports {pages} item pages (limit {MAX_ITEM_PAGES}).")
        for page in range(2, pages + 1):
            more, _ = self.client.list_items_raw(page)
            records.extend(more)
        return build_products(records)

    @staticmethod
    def _filter(products: list[Product], query: str | None, category: str | None) -> list[Product]:
        result = products
        if query is not None and query.strip():
            needle = query.strip().casefold()
            result = [
                p
                for p in result
                if needle in p.sku.casefold() or any(needle in d.casefold() for d in p.record_descriptions)
            ]
        if category:
            result = [p for p in result if p.category == category]
        return sorted(result, key=lambda p: p.sku)

    def search_products(self, query: str | None = None, category: str | None = None) -> list[Product]:
        return self._filter(self._catalogue_snapshot(), query, category)

    @staticmethod
    def _find_product(products: Iterable[Product], sku: str) -> Product | None:
        wanted = normalize_sku(sku)
        return next((p for p in products if p.sku == wanted), None)

    def get_product(self, sku: str) -> Product:
        product = self._find_product(self._catalogue_snapshot(), sku)
        if product is None:
            raise NotFoundError(f"No product with SKU {normalize_sku(sku)}.")
        return product

    def _full_stock_extract(self) -> list[RawStockRow]:
        """One complete E5 extract read without ``item``, validated in full (F03)."""
        return self.client.stock_raw()

    def check_availability(self, sku: str) -> AvailabilityReport:
        product = self.get_product(sku)
        return assess_stock(product, self._full_stock_extract())

    def product_catalogue(
        self, query: str | None = None, category: str | None = None
    ) -> list[tuple[Product, AvailabilityReport]]:
        products = self._filter(self._catalogue_snapshot(), query, category)
        rows = self._full_stock_extract()
        return [(p, assess_stock(p, rows)) for p in products]

    # ================================================================== overview

    def overview(self) -> OverviewMetrics:
        customers = self._customer_pages()
        orders = self.list_orders()
        products = self._catalogue_snapshot()
        rows = self._full_stock_extract()
        reports = [assess_stock(p, rows) for p in products]
        outstanding = [o for o in orders if o.is_outstanding]
        pairs: set[tuple[str, str, IssueCode]] = set()
        for c in customers:
            pairs.update(("customer", c.account_id, i.code) for i in c.issues)
        for o in orders:
            pairs.update(("order", o.order_no, i.code) for i in o.issues)
        for p in products:
            pairs.update(("sku", p.sku, i.code) for i in p.issues)
        for r in reports:
            pairs.update(("sku", r.sku, i.code) for i in r.issues)
        issue_counts: dict[IssueCode, int] = {}
        for _, _, code in pairs:
            issue_counts[code] = issue_counts.get(code, 0) + 1
        status_counts = {status: 0 for status in StockStatus}
        for r in reports:
            status_counts[r.status] += 1
        return OverviewMetrics(
            customer_count=len(customers),
            credit_hold_count=sum(1 for c in customers if c.credit_hold),
            order_count=len(orders),
            outstanding_order_count=len(outstanding),
            outstanding_order_value=quantize_money(
                sum((o.total for o in outstanding if o.total is not None), Decimal("0"))
            ),
            needs_review_order_count=sum(1 for o in orders if o.status == OrderStatus.UNKNOWN),
            product_count=len(products),
            stock_status_counts=status_counts,
            issue_counts=dict(sorted(issue_counts.items(), key=lambda kv: kv[0].value)),
            outstanding_orders=outstanding,
        )

    # ================================================================== Workflow C — preparation

    @staticmethod
    def _merge_lines(lines: Iterable[Any]) -> list[tuple[str, int]]:
        merged: dict[str, int] = {}
        count = 0
        for line in lines:
            count += 1
            if isinstance(line, Mapping):
                sku, quantity = line.get("sku"), line.get("quantity")
            elif hasattr(line, "sku"):
                sku, quantity = line.sku, line.quantity
            else:
                sku, quantity = line
            if not isinstance(sku, str) or not sku.strip():
                raise ValueError("Each line needs a SKU.")
            if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= 999:
                raise ValueError("Each quantity must be a whole number from 1 to 999.")
            key = normalize_sku(sku)
            merged[key] = merged.get(key, 0) + quantity
        if not 1 <= count <= MAX_DRAFT_LINES:
            raise ValueError(f"A draft needs 1 to {MAX_DRAFT_LINES} lines.")
        if any(q > 999 for q in merged.values()):
            raise ValueError("A merged quantity exceeds 999.")
        return list(merged.items())

    def prepare_order(self, account_id: str, lines: Iterable[Any]) -> OrderDraft:
        """Validates and stores a draft. No legacy write."""
        merged = self._merge_lines(lines)
        customer = self.get_customer(account_id)
        draft_reasons: list[str] = []
        if customer.credit_hold:
            draft_reasons.append("Customer account is on credit hold.")
        products = self._catalogue_snapshot()
        stock_rows: list[RawStockRow] | None = None
        stock_error: BridgeError | None = None
        try:
            stock_rows = self._full_stock_extract()
        except BridgeError as exc:
            stock_error = exc
        draft_lines: list[DraftLine] = []
        for sku, quantity in merged:
            product = self._find_product(products, sku)
            reasons: list[str] = []
            availability: StockStatus | None = None
            available: int | None = None
            unit_price = None
            if product is None:
                reasons.append("Unknown SKU")
            else:
                unit_price = product.unit_price
                if product.discontinued:
                    reasons.append("Product is discontinued.")
                if unit_price is None:
                    reasons.append("No reliable price on file.")
                if stock_error is not None:
                    reasons.append(f"Availability cannot be established: {stock_error.message}")
                else:
                    report = assess_stock(product, stock_rows or [])
                    availability = report.status
                    available = report.available_quantity
                    if report.status == StockStatus.UNDETERMINED:
                        reasons.append(f"Availability cannot be established: {report.explanation}")
                    elif available is not None and available < quantity:
                        reasons.append(f"Insufficient stock: requested {quantity}, available {available}.")
            draft_lines.append(
                DraftLine(
                    sku=sku,
                    product_name=product.name if product else None,
                    quantity=quantity,
                    unit_price=unit_price,
                    line_total=quantize_money(unit_price * quantity) if unit_price is not None else None,
                    availability=availability,
                    available_quantity=available,
                    blocking_reasons=reasons,
                )
            )
        totals = [line.line_total for line in draft_lines]
        estimated = quantize_money(sum(totals, Decimal("0"))) if all(t is not None for t in totals) else None
        eligible = not draft_reasons and not any(line.blocking_reasons for line in draft_lines)
        with self.store.lock:
            # The id check and the insert are one step under the store lock: an existing draft (and any
            # approval already shown for it) is never replaced by a colliding candidate.
            for _ in range(DRAFT_ID_ATTEMPTS):
                draft_id = "LB-" + secrets.token_hex(4).upper()
                if draft_id not in self.store.drafts:
                    break
            else:
                raise BridgeError(
                    "DRAFT_ID_UNAVAILABLE",
                    "No unused draft id could be allocated; nothing was stored. Prepare the draft again.",
                    retryable=True,
                )
            draft = OrderDraft(
                draft_id=draft_id,
                account_id=customer.account_id,
                customer_name=customer.name,
                lines=draft_lines,
                estimated_total=estimated,
                eligible=eligible,
                blocking_reasons=draft_reasons,
                lifecycle=DraftLifecycle.READY if eligible else DraftLifecycle.BLOCKED,
                created_at=_now(),
            )
            self.store.drafts[draft_id] = draft
        return draft

    def get_draft(self, draft_id: str) -> OrderDraft:
        with self.store.lock:
            draft = self.store.drafts.get(draft_id)
        if draft is None:
            raise NotFoundError(f"No draft {draft_id}.")
        return draft

    def list_drafts(self) -> list[OrderDraft]:
        with self.store.lock:
            return sorted(self.store.drafts.values(), key=lambda d: d.created_at)

    def unresolved_drafts(self, account_id: str | None = None) -> list[OrderDraft]:
        account = normalize_account_id(account_id) if account_id is not None else None
        return [
            d
            for d in self.list_drafts()
            if d.lifecycle == DraftLifecycle.UNKNOWN and (account is None or d.account_id == account)
        ]

    def reject_draft(self, draft_id: str) -> OrderDraft:
        """READY/BLOCKED/NOT_APPLIED → REJECTED_BY_USER. The rejection itself sends nothing to HALDEN."""
        with self.store.lock:
            draft = self.store.drafts.get(draft_id)
            if draft is None:
                raise DraftStateError(f"No draft {draft_id}.")
            if draft.lifecycle not in (DraftLifecycle.READY, DraftLifecycle.BLOCKED, DraftLifecycle.NOT_APPLIED):
                raise DraftStateError(f"A draft in lifecycle {draft.lifecycle.value} cannot be rejected.")
            draft.lifecycle = DraftLifecycle.REJECTED_BY_USER
            return draft

    @staticmethod
    def rejection_message(draft: OrderDraft) -> str:
        message = REJECTED_BY_OPERATOR_MESSAGE
        if draft.attempts:
            message += (
                f" Earlier attempts: {len(draft.attempts)}, each recorded as NOT_APPLIED (see history). "
                "No additional request was sent."
            )
        return message

    # ================================================================== Workflow C — submission

    def submit_order(self, draft_id: str, approval: Approval) -> SubmissionResult:
        """Consumes the approval, then prepares, transmits and classifies one order request. Product callers: the UI approve handler only."""
        with self.store.lock:
            draft = self.store.drafts.get(draft_id)
            if draft is None:
                raise DraftStateError(f"No draft {draft_id}.")
            if draft.lifecycle not in (DraftLifecycle.READY, DraftLifecycle.NOT_APPLIED):
                raise DraftStateError(APPROVAL_USED_MESSAGE)
            if approval.draft_id != draft_id or approval.attempt_no != len(draft.attempts) + 1:
                raise DraftStateError(APPROVAL_USED_MESSAGE)
            attempt = SubmissionAttempt(
                attempt_no=approval.attempt_no, approved_at=approval.approved_at, started_at=_now()
            )
            draft.attempts.append(attempt)  # the authorization is consumed here, before any I/O
            draft.lifecycle = DraftLifecycle.SUBMITTING
            account_id = draft.account_id
            wire_lines = [(line.sku, line.quantity) for line in draft.lines]
        try:
            result = self._transmit(draft_id, attempt, account_id, wire_lines)
        except Exception as exc:  # defensive: never leave SUBMITTING without evidence
            result = self._result(
                draft_id,
                attempt,
                ResultKind.SUBMIT,
                SubmissionOutcome.UNKNOWN,
                OutcomeEvidence.RECONCILIATION_FAILED,
                f"Unexpected integration error after approval: {type(exc).__name__}: {exc}",
                "Outcome unknown — use Re-check. Do not book again.",
            )
        return self._record(draft_id, attempt, result)

    def _transmit(
        self, draft_id: str, attempt: SubmissionAttempt, account_id: str, wire_lines: list[tuple[str, int]]
    ) -> SubmissionResult:
        # Step 1 — pre-send preparation (reads only, no E8).
        try:
            customer_page = self.client.prepare_write(account_id)
            rows = [row for row in customer_page["rows"] if self._same_account(row.get("acct"), account_id)]
        except BridgeError as exc:
            return self._result(
                draft_id, attempt, ResultKind.SUBMIT, SubmissionOutcome.NOT_APPLIED, OutcomeEvidence.NOT_SENT,
                f"Pre-send preparation failed ({exc.code}): {exc.message} No order request was sent.",
                f"Not applied — {exc.message} No order request was sent. "
                f"Approve attempt {attempt.attempt_no + 1} to try again.",
            )
        if not rows:
            return self._result(
                draft_id, attempt, ResultKind.SUBMIT, SubmissionOutcome.NOT_APPLIED, OutcomeEvidence.NOT_SENT,
                f"Pre-send validation found no account {account_id}. No order request was sent.",
                f"Not applied — account {account_id} was not found. No order request was sent.",
            )
        # Step 2 — exactly one E8 handed to the transport.
        try:
            observation = self.client.post_order_once(account_id, ref=draft_id, lines=wire_lines)
        except Exception as exc:  # post_order_once never raises by contract; stay conservative
            observation = E8Observation(kind="UNCERTAIN", detail=f"Unexpected error around the order request: {exc}")
        with self.store.lock:
            attempt.e8_requests = 1
            attempt.e8_observation_kind = observation.kind
            if observation.kind == "ACCEPTED":
                attempt.acknowledged_order_no = observation.order_no
        # Step 3 — classification.
        return self._classify(draft_id, attempt, account_id, wire_lines, observation)

    def _classify(
        self,
        draft_id: str,
        attempt: SubmissionAttempt,
        account_id: str,
        wire_lines: list[tuple[str, int]],
        observation: E8Observation,
    ) -> SubmissionResult:
        n = attempt.attempt_no
        kind = observation.kind
        if kind == "NOT_DELIVERED":
            return self._result(
                draft_id, attempt, ResultKind.SUBMIT, SubmissionOutcome.NOT_APPLIED, OutcomeEvidence.NOT_SENT,
                f"The order request could not be delivered: {observation.detail}",
                f"Not applied — HALDEN ERP could not be reached, so the order request was not delivered. "
                f"Approve attempt {n + 1} to try again.",
            )
        if kind == "PRE_HANDLER_REFUSAL":
            return self._result(
                draft_id, attempt, ResultKind.SUBMIT, SubmissionOutcome.NOT_APPLIED, OutcomeEvidence.PRE_HANDLER_REJECTION,
                f"The gateway refused the order request with HTTP {observation.http_status} before any order logic ran.",
                f"Not applied — HALDEN refused the request before processing it (HTTP {observation.http_status}). "
                f"Approve attempt {n + 1} to try again.",
            )
        if kind == "REJECTED":
            return self._result(
                draft_id, attempt, ResultKind.SUBMIT, SubmissionOutcome.REJECTED, OutcomeEvidence.LEGACY_REJECTION,
                f"HALDEN answered rc {observation.rc} {observation.msg}.",
                f"Rejected by HALDEN: {observation.msg} (rc {observation.rc}). Nothing was booked.",
                legacy_code=observation.msg,
            )
        if kind == "SHORT":
            return self._result(
                draft_id, attempt, ResultKind.SUBMIT, SubmissionOutcome.REJECTED, OutcomeEvidence.LEGACY_REJECTION,
                f"HALDEN answered HTTP 409 SHORT:{observation.short_item}:{observation.short_available}.",
                f"Rejected by HALDEN: insufficient stock for {observation.short_item} "
                f"(available {observation.short_available}).",
                legacy_code="INSUFFICIENT_STOCK",
            )
        if kind == "ACCEPTED":
            return self._read_back(draft_id, attempt, account_id, wire_lines, observation.order_no or "",
                                   ResultKind.SUBMIT, parse_money(observation.total))
        return self._reconcile(draft_id, attempt, account_id, wire_lines, ResultKind.SUBMIT, observation.detail)

    @staticmethod
    def _same_account(raw_acct: Any, account_id: str) -> bool:
        try:
            return normalize_account_id(raw_acct) == account_id
        except ValueError:
            return False

    def _matches_approved(self, raw_order: Mapping[str, Any], draft_id: str, account_id: str,
                          wire_lines: list[tuple[str, int]]) -> bool:
        """Compared fields: account, reference (null is a non-match, tested before
        trimming) and the line multiset. Price, total, date and status are not compared."""
        ref = raw_order.get("Ref")
        if ref is None or not isinstance(ref, str):
            return False
        if ref.strip() != draft_id:
            return False
        if not self._same_account(raw_order.get("Acct"), account_id):
            return False
        try:
            booked = Counter(
                (normalize_sku(str(line.get("item") or "")), parse_quantity(line.get("qty")))
                for line in raw_order.get("lines") or []
            )
        except AttributeError:
            return False
        return booked == Counter(wire_lines)

    def _read_back(self, draft_id: str, attempt: SubmissionAttempt, account_id: str,
                   wire_lines: list[tuple[str, int]], order_no: str, kind: ResultKind,
                   acknowledged_total: Decimal | None = None) -> SubmissionResult:
        """Phase 2a (and the anchored re-check): E7 for the acknowledged order only."""
        try:
            body = self.client.get_order_raw(order_no)
            if body["rc"] not in (0, 404):
                raise LegacyMalformedResponse(f"HALDEN ERP answered rc {body['rc']} for order {order_no}.")
            order = build_order(body["order"]) if body["rc"] == 0 else None
        except (BridgeError, ValueError, TypeError, KeyError) as exc:
            message = getattr(exc, "message", str(exc))
            if kind == ResultKind.SUBMIT:
                return self._result(
                    draft_id, attempt, kind, SubmissionOutcome.CONFIRMED, OutcomeEvidence.ACKNOWLEDGED,
                    f"HALDEN acknowledged order {order_no} (rc 0 is sent only after commit); the read-back could "
                    f"not obtain an answer ({message}). Content not verified by read-back.",
                    f"Confirmed — HALDEN acknowledged order {order_no}. Not verified by read-back.",
                    order_no=order_no, erp_booked_total=acknowledged_total,
                )
            return self._result(
                draft_id, attempt, kind, SubmissionOutcome.UNKNOWN, OutcomeEvidence.RECONCILIATION_FAILED,
                f"Re-check of acknowledged order {order_no} failed: {message}",
                f"Outcome unknown — order {order_no} could not be read. Do not book again; use Re-check.",
                order_no=order_no,
            )
        if body["rc"] == 404 or not self._matches_approved(body["order"], draft_id, account_id, wire_lines):
            detail = (
                f"Acknowledged order {order_no} is not on file (E7 rc 404)."
                if body["rc"] == 404
                else f"Acknowledged order {order_no} differs from the approved content (account, reference or lines)."
            )
            return self._result(
                draft_id, attempt, kind, SubmissionOutcome.UNKNOWN, OutcomeEvidence.READBACK_MISMATCH, detail,
                f"Outcome unknown — {detail} Do not book again; use Re-check.",
                order_no=order_no, erp_booked_total=order.total if order else acknowledged_total,
            )
        return self._result(
            draft_id, attempt, kind, SubmissionOutcome.CONFIRMED, OutcomeEvidence.READ_BACK,
            f"Order {order_no} read back from HALDEN: account, reference {draft_id} and lines match the approval.",
            f"Confirmed — order {order_no} booked and verified by read-back.",
            order_no=order_no, erp_booked_total=order.total if order else None, order=order,
        )

    def _reconcile(self, draft_id: str, attempt: SubmissionAttempt, account_id: str,
                   wire_lines: list[tuple[str, int]], kind: ResultKind, cause: str) -> SubmissionResult:
        """Phase 2b: reads only. E6 listing for the account, then E7 for a sole candidate."""
        try:
            listing = self._order_rows(int(account_id))
        except BridgeError as exc:
            return self._result(
                draft_id, attempt, kind, SubmissionOutcome.UNKNOWN, OutcomeEvidence.RECONCILIATION_FAILED,
                f"Uncertain order request ({cause}); the order listing for account {account_id} failed: {exc.message}",
                "Outcome unknown — HALDEN could not be searched. Do not book again; use Re-check.",
            )
        candidates = [
            row for row in listing
            if row.get("Ref") is not None and isinstance(row.get("Ref"), str) and row["Ref"].strip() == draft_id
        ]
        if len(candidates) >= 2:
            numbers = ", ".join(str(row.get("OrderNo")) for row in candidates)
            return self._result(
                draft_id, attempt, kind, SubmissionOutcome.UNKNOWN, OutcomeEvidence.AMBIGUOUS_MATCH,
                f"Uncertain order request ({cause}); {len(candidates)} orders carry reference {draft_id}: {numbers}.",
                f"Outcome unknown — several orders carry reference {draft_id} ({numbers}). Do not book again.",
            )
        if not candidates:
            return self._result(
                draft_id, attempt, kind, SubmissionOutcome.UNKNOWN, OutcomeEvidence.NO_MATCH_AFTER_UNCERTAIN,
                f"Uncertain order request ({cause}); no order with reference {draft_id} is visible yet for account "
                f"{account_id}. This does not prove it was not created.",
                f"Outcome unknown — no order with reference {draft_id} is visible yet. This does not prove it was "
                "not created. Do not book again; use Re-check.",
            )
        order_no = str(candidates[0].get("OrderNo") or "").strip()
        try:
            body = self.client.get_order_raw(order_no)
            if body["rc"] not in (0, 404):
                raise LegacyMalformedResponse(f"HALDEN ERP answered rc {body['rc']} for order {order_no}.")
            order = build_order(body["order"]) if body["rc"] == 0 else None
        except (BridgeError, ValueError, TypeError, KeyError) as exc:
            return self._result(
                draft_id, attempt, kind, SubmissionOutcome.UNKNOWN, OutcomeEvidence.RECONCILIATION_FAILED,
                f"Uncertain order request ({cause}); candidate {order_no} could not be read: "
                f"{getattr(exc, 'message', str(exc))}",
                f"Outcome unknown — candidate order {order_no} could not be read. Do not book again; use Re-check.",
            )
        if body["rc"] == 404 or not self._matches_approved(body["order"], draft_id, account_id, wire_lines):
            return self._result(
                draft_id, attempt, kind, SubmissionOutcome.UNKNOWN, OutcomeEvidence.AMBIGUOUS_MATCH,
                f"Uncertain order request ({cause}); candidate {order_no} carries reference {draft_id} but its detail "
                "is missing or differs from the approved content.",
                f"Outcome unknown — order {order_no} carries the reference but does not match the approval.",
            )
        return self._result(
            draft_id, attempt, kind, SubmissionOutcome.CONFIRMED, OutcomeEvidence.RECONCILED,
            f"Uncertain order request ({cause}); reconciled to order {order_no}: account, reference {draft_id} and "
            "lines match the approval.",
            f"Confirmed by reconciliation — order {order_no}.",
            order_no=order_no, erp_booked_total=order.total if order else None, order=order,
        )

    @staticmethod
    def _result(draft_id: str, attempt: SubmissionAttempt, kind: ResultKind, outcome: SubmissionOutcome,
                evidence: OutcomeEvidence, detail: str, message: str, *, order_no: str | None = None,
                legacy_code: str | None = None, erp_booked_total: Decimal | None = None,
                order: Order | None = None) -> SubmissionResult:
        return SubmissionResult(
            draft_id=draft_id,
            attempt_no=attempt.attempt_no,
            kind=kind,
            outcome=outcome,
            evidence=evidence,
            evidence_detail=detail,
            e8_requests=attempt.e8_requests if kind == ResultKind.SUBMIT else 0,
            order_no=order_no,
            legacy_code=legacy_code,
            erp_booked_total=erp_booked_total,
            message=message,
            order=order,
            recorded_at=_now(),
        )

    def _record(self, draft_id: str, attempt: SubmissionAttempt, result: SubmissionResult) -> SubmissionResult:
        with self.store.lock:
            draft = self.store.drafts[draft_id]
            if result.kind == ResultKind.SUBMIT:
                # e8_requests is read again under the lock so the result matches the attempt fact.
                result = result.model_copy(update={"e8_requests": attempt.e8_requests})
                attempt.result = result
                draft.lifecycle = LIFECYCLE_FOR_OUTCOME[result.outcome]
            elif result.outcome == SubmissionOutcome.CONFIRMED:
                draft.lifecycle = DraftLifecycle.CONFIRMED
            draft.history.append(result)
        return result

    def recheck_submission(self, draft_id: str) -> SubmissionResult:
        """Read-only; never transmits E8; anchored to the attempt's acknowledged order.

        Re-checks of one draft are serialized by its per-draft lock, and the lifecycle is validated only
        after that lock is held. A later re-check therefore starts from the earlier one's recorded result,
        and an older observation can never be recorded after a newer confirmation."""
        with self.store.lock:
            if draft_id not in self.store.drafts:
                raise DraftStateError(f"No draft {draft_id}.")
            recheck_lock = self.store.recheck_locks.setdefault(draft_id, threading.Lock())
        with recheck_lock:
            with self.store.lock:
                draft = self.store.drafts.get(draft_id)
                if draft is None:
                    raise DraftStateError(f"No draft {draft_id}.")
                if draft.lifecycle != DraftLifecycle.UNKNOWN or not draft.attempts:
                    raise DraftStateError(f"A draft in lifecycle {draft.lifecycle.value} cannot be re-checked.")
                attempt = draft.attempts[-1]
                anchor = attempt.acknowledged_order_no
                observed = attempt.e8_observation_kind
                account_id = draft.account_id
                wire_lines = [(line.sku, line.quantity) for line in draft.lines]
            if anchor is not None:
                result = self._read_back(draft_id, attempt, account_id, wire_lines, anchor, ResultKind.RECHECK)
            elif observed == "UNCERTAIN":
                result = self._reconcile(draft_id, attempt, account_id, wire_lines, ResultKind.RECHECK,
                                         "re-check of an uncertain order request")
            else:
                raise DraftStateError("This attempt has no acknowledged or uncertain order request to re-check.")
            return self._record(draft_id, attempt, result)

    # ================================================================== demo harness (not product paths)

    def simulate_external_order(self, account_id: str, sku: str, quantity: int) -> str:
        """Guided Scenario S6 setup only (C4): the ERP books an order from another sales channel."""
        if self.sim is None:
            raise BridgeError("SIMULATION_UNAVAILABLE", "Requires simulator control.")
        observation = self.sim.external_order(normalize_account_id(account_id), normalize_sku(sku), quantity)
        if observation.kind != "ACCEPTED" or not observation.order_no:
            raise BridgeError("SIMULATION_REJECTED", f"The simulated counter sale was not booked: {observation.detail}")
        return observation.order_no

    def reset_demo(self) -> bool:
        if self.sim is None:
            return False
        ok = self.sim.reset()
        if ok:
            with self.store.lock:
                self.store.drafts.clear()
                self.store.recheck_locks.clear()
        return ok

    def arm_fault(self, method: str, path_prefix: str, mode: str, count: int = 1, delay_ms: int = 0) -> int:
        """Guided Scenario S7 setup and tests only (C2)."""
        if self.sim is None:
            raise BridgeError("SIMULATION_UNAVAILABLE", "Requires simulator control.")
        return self.sim.arm_fault(method, path_prefix, mode, count, delay_ms)
