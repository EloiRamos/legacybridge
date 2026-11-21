"""The six LLM-accessible tools. All are read-only or create a local draft only.

Every tool returns a JSON string envelope and never raises:
``{"ok": true, "data": …}`` or ``{"ok": false, "error": {"code", "message", "retryable"}}``.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, ValidationError

from ..bridge.errors import BridgeError
from ..bridge.models import OrderDraft
from ..bridge.normalize import CANONICAL_CATEGORIES, UNCATEGORIZED
from ..service import BridgeService

Category = Literal["Bearings", "Hydraulics", "Valves", "PPE", "Power Tools", "Hand Tools", "Lubricants", "Uncategorized"]
assert set(Category.__args__) == set(CANONICAL_CATEGORIES) | {UNCATEGORIZED}  # type: ignore[attr-defined]

MAX_PRODUCTS = 25


class FindCustomersInput(BaseModel):
    query: str = Field(min_length=1, max_length=80, description="Company-name fragment or 5-digit account number.")


class CustomerOverviewInput(BaseModel):
    account_id: str = Field(pattern=r"^\d{1,5}$", description="Account number, 1-5 digits.")


class GetOrderInput(BaseModel):
    order_no: str = Field(pattern=r"^[Ss][Oo]-\d{6}$", description="Order number such as SO-240102.")


class SearchProductsInput(BaseModel):
    query: str | None = Field(default=None, description="Text in the SKU or description.")
    category: Category | None = Field(default=None, description="Canonical category.")


class CheckAvailabilityInput(BaseModel):
    sku: str = Field(min_length=1, max_length=40, description="Product SKU such as BW-1001.")


class DraftLineInput(BaseModel):
    sku: str = Field(min_length=1, max_length=40)
    quantity: int = Field(ge=1, le=999)


class PrepareOrderDraftInput(BaseModel):
    account_id: str = Field(pattern=r"^\d{1,5}$")
    lines: list[DraftLineInput] = Field(min_length=1, max_length=10)


ERROR_RETRYABLE = {
    "AUTH_FAILED": False,
    "LEGACY_UNAVAILABLE": True,
    "RATE_LIMITED": True,
    "MALFORMED_RESPONSE": False,
    "NOT_FOUND": False,
    "INVALID_INPUT": False,
    "INTERNAL_ERROR": False,
}


def ok(data: Any) -> str:
    return json.dumps({"ok": True, "data": data}, ensure_ascii=False)


def fail(code: str, message: str, retryable: bool | None = None) -> str:
    if retryable is None:
        retryable = ERROR_RETRYABLE.get(code, False)
    return json.dumps({"ok": False, "error": {"code": code, "message": message, "retryable": retryable}}, ensure_ascii=False)


def _error_envelope(exc: BaseException) -> str:
    if isinstance(exc, BridgeError):
        code = exc.code if exc.code in ERROR_RETRYABLE else "INTERNAL_ERROR"
        return fail(code, exc.message, exc.retryable if code != "INTERNAL_ERROR" else False)
    if isinstance(exc, (ValidationError, ValueError)):
        return fail("INVALID_INPUT", str(exc))
    return fail("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")


def _validation_error(exc: ValidationError) -> str:
    return fail("INVALID_INPUT", f"Invalid input: {exc.errors(include_url=False)}")


def _guard(schema: type[BaseModel], body: Callable[[BaseModel], str]) -> Callable[..., str]:
    def run(**kwargs: Any) -> str:
        try:
            args = schema.model_validate(kwargs)
        except ValidationError as exc:
            return _validation_error(exc)
        try:
            return body(args)
        except Exception as exc:  # no exception escapes a tool
            return _error_envelope(exc)

    return run


def draft_tool_view(draft: OrderDraft) -> dict[str, Any]:
    """Public draft view returned by the tools: exactly these keys, in this order."""
    return {
        "draft_id": draft.draft_id,
        "account_id": draft.account_id,
        "customer_name": draft.customer_name,
        "lines": [
            {
                "sku": line.sku,
                "product_name": line.product_name,
                "quantity": line.quantity,
                "unit_price": None if line.unit_price is None else str(line.unit_price),
                "line_total": None if line.line_total is None else str(line.line_total),
                "availability": None if line.availability is None else line.availability.value,
                "available_quantity": line.available_quantity,
                "blocking_reasons": list(line.blocking_reasons),
            }
            for line in draft.lines
        ],
        "estimated_total": None if draft.estimated_total is None else str(draft.estimated_total),
        "status": draft.status,
        "blocking_reasons": list(draft.blocking_reasons),
        "created_at": draft.created_at.isoformat(),
        "submission": None,
        "history": [],
        "lifecycle": draft.lifecycle.value,
        "eligible": draft.eligible,
    }


def make_tools(service: BridgeService) -> list[BaseTool]:
    def find_customers(args: FindCustomersInput) -> str:
        matches = service.search_customers(args.query)
        if not matches:
            return fail("NOT_FOUND", f"No customer matches {args.query!r}.")
        return ok({"matches": [m.model_dump(mode="json") for m in matches], "ambiguous": len(matches) > 1})

    def get_customer_overview(args: CustomerOverviewInput) -> str:
        return ok(service.get_customer_overview(args.account_id).model_dump(mode="json"))

    def get_order(args: GetOrderInput) -> str:
        return ok(service.get_order(args.order_no.upper()).model_dump(mode="json"))

    def search_products(args: SearchProductsInput) -> str:
        products = service.search_products(args.query, args.category)[:MAX_PRODUCTS]
        return ok({"products": [p.model_dump(mode="json") for p in products]})

    def check_availability(args: CheckAvailabilityInput) -> str:
        return ok(service.check_availability(args.sku).model_dump(mode="json"))

    def prepare_order_draft(args: PrepareOrderDraftInput) -> str:
        draft = service.prepare_order(args.account_id, [(line.sku, line.quantity) for line in args.lines])
        return ok(draft_tool_view(draft))

    specs = [
        ("find_customers",
         "Find customers by company-name fragment or 5-digit account number. May return several matches; "
         "never guess between them.", FindCustomersInput, find_customers),
        ("get_customer_overview",
         "Customer details plus all orders and the outstanding (undelivered) subset.",
         CustomerOverviewInput, get_customer_overview),
        ("get_order", "Full order with lines and data-quality issues.", GetOrderInput, get_order),
        ("search_products", "Search products by text and/or canonical category.", SearchProductsInput, search_products),
        ("check_availability",
         "Authoritative stock verdict for one SKU; the legacy availability flag is unreliable.",
         CheckAvailabilityInput, check_availability),
        ("prepare_order_draft",
         "Validate and prepare an order draft for human review. Does NOT place the order; a human must "
         "approve it in the console.", PrepareOrderDraftInput, prepare_order_draft),
    ]
    return [
        StructuredTool.from_function(
            func=_guard(schema, body),
            name=name,
            description=description,
            args_schema=schema,
            handle_validation_error=_validation_error,
        )
        for name, description, schema, body in specs
    ]
