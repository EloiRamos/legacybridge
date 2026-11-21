"""Every normalization test vector."""

from datetime import date
from decimal import Decimal

import pytest

from legacybridge.bridge.models import IssueCode, OrderStatus, RawStockRow, StockStatus
from legacybridge.bridge.normalize import (
    assess_availability,
    assess_stock,
    build_customer,
    build_order,
    build_products,
    map_order_status,
    normalize_account_id,
    normalize_category,
    normalize_company_name,
    normalize_email,
    normalize_person_name,
    normalize_sku,
    parse_date,
    parse_flag,
    parse_money,
    parse_quantity,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("4.85", "4.85"), ("€12,40", "12.40"), ("1.249,00", "1249.00"), ("EUR 9.90", "9.90"), ("6,75", "6.75"),
        ("172,40", "172.40"), (" 42.00 ", "42.00"), ("1,234.50", "1234.50"), ("118", "118.00"),
        ("N/A", None), ("12,4", None), ("abc", None), (None, None), ("", None), ("n/a", None),
    ],
)
def test_parse_money(raw, expected):
    assert parse_money(raw) == (Decimal(expected) if expected else None)


@pytest.mark.parametrize("raw, expected", [("240", 240), ("500 ", 500), ("1.200", 1200), ("-3", -3), ("?", None), ("", None), (None, None)])
def test_parse_quantity(raw, expected):
    assert parse_quantity(raw) == expected


@pytest.mark.parametrize("raw, expected", [("J", True), ("nein", False), ("", None), ("maybe", None), (None, None), ("true", True), ("0", False), ("Y", True)])
def test_parse_flag(raw, expected):
    assert parse_flag(raw) is expected


@pytest.mark.parametrize(
    "raw, expected",
    [("2025-09-02", date(2025, 9, 2)), ("14.10.2025", date(2025, 10, 14)), ("20251103", date(2025, 11, 3)), ("2025/13/45", None), ("2025-13-45", None)],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_normalize_account_id():
    assert normalize_account_id(417) == "00417"
    assert normalize_account_id("04450") == "04450"
    with pytest.raises(ValueError):
        normalize_account_id("ABC")


def test_normalize_sku():
    assert normalize_sku("bw-1009 ") == "BW-1009"


def test_company_and_person_names():
    assert normalize_company_name("NORDKAP MARINE SERVICES AS") == "Nordkap Marine Services AS"
    assert normalize_company_name("ALDER & FINCH FABRICATION LTD") == "Alder & Finch Fabrication Ltd"
    assert normalize_company_name("Castell Water Utilities GmbH") == "Castell Water Utilities GmbH"
    assert normalize_person_name("HOLM, ERIK") == "Erik Holm"
    assert normalize_person_name("RAMAN, PRIYA") == "Priya Raman"
    assert normalize_person_name("  ") is None


def test_email():
    assert normalize_email(" Erik.Holm@Nordkap-Marine.example ") == "erik.holm@nordkap-marine.example"
    assert normalize_email("n/a") is None
    customer = build_customer({"acct": 20514, "nm": "Castell Water Utilities GmbH", "mail": "n/a", "hold": "0", "since": "2018-11-23", "city": "Düsseldorf"})
    assert customer.email is None and [i.code for i in customer.issues] == [IssueCode.INVALID_EMAIL]
    assert customer.city == "Düsseldorf"


@pytest.mark.parametrize(
    "raw, expected",
    [("bearings ", "Bearings"), ("power-tools", "Power Tools"), ("Hand tools", "Hand Tools"), (None, "Uncategorized"), ("ppe", "PPE"), ("HYDRAULICS", "Hydraulics")],
)
def test_category(raw, expected):
    assert normalize_category(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [("sh", OrderStatus.SHIPPED), ("Shipped", OrderStatus.SHIPPED), ("ZZ", OrderStatus.UNKNOWN), ("DLV", OrderStatus.DELIVERED),
     ("CX", OrderStatus.CANCELLED), ("HD", OrderStatus.ON_HOLD), ("AL", OrderStatus.ALLOCATED), ("OP", OrderStatus.OPEN)],
)
def test_order_status(raw, expected):
    assert map_order_status(raw) == expected


def _item(i, no, price, avail="Y", disc="N", catg="Bearings", descr=None):
    return {"id": i, "item_no": no, "descr": descr or f"Item {i}", "catg": catg, "price": price, "uom": "EA", "avail": avail, "disc": disc, "upd": "2025-10-01"}


def test_build_products_duplicate_and_conflicting_price():
    products = build_products([_item(9, "BW-1009", "42.00", catg="Hydraulics"), _item(12, "bw-1009 ", "39.50", avail="N", catg="Hydraulics"), _item(8, "BW-1008", "N/A")])
    by_sku = {p.sku: p for p in products}
    dup = by_sku["BW-1009"]
    assert dup.record_ids == [9, 12] and dup.unit_price is None
    assert {i.code for i in dup.issues} == {IssueCode.DUPLICATE_SKU, IssueCode.CONFLICTING_PRICE}
    assert [i.code for i in by_sku["BW-1008"].issues] == [IssueCode.MISSING_PRICE]


def test_assess_availability_rules():
    def product(no, avail="Y"):
        return build_products([_item(1, no, "1.00", avail=avail)])[0]

    rows = [RawStockRow(item="BW-1005", wh="DUS", on_hand="50", allocated="5"), RawStockRow(item="BW-1005", wh="MAN", on_hand="?", allocated="0")]
    report = assess_availability(product("BW-1005"), rows)
    assert report.status == StockStatus.UNDETERMINED and report.known_minimum == 45 and report.available_quantity is None
    assert [i.code for i in report.issues] == [IssueCode.UNKNOWN_QUANTITY]
    negative = assess_availability(product("BW-1011", avail=""), [RawStockRow(item="BW-1011", wh="DUS", on_hand="-3", allocated="0")])
    assert negative.status == StockStatus.UNDETERMINED and [i.code for i in negative.issues] == [IssueCode.NEGATIVE_QUANTITY]
    none = assess_availability(product("BW-2000", avail="N"), [])
    assert none.status == StockStatus.OUT_OF_STOCK and none.available_quantity == 0 and none.issues == []
    mismatch = assess_availability(product("BW-1002", avail="Y"), [RawStockRow(item="BW-1002", wh="DUS", on_hand="0", allocated="0")])
    assert mismatch.status == StockStatus.OUT_OF_STOCK and [i.code for i in mismatch.issues] == [IssueCode.FLAG_MISMATCH]
    pooled = assess_availability(product("BW-1006", avail="J"), [RawStockRow(item="BW-1006", wh="DUS", on_hand="500 ", allocated="0"), RawStockRow(item="BW-1006", wh="MAN", on_hand="1.200", allocated="0")])
    assert pooled.status == StockStatus.IN_STOCK and pooled.available_quantity == 1700
    dup = build_products([_item(9, "BW-1009", "42.00"), _item(12, "bw-1009 ", "39.50")])[0]
    report = assess_availability(dup, [RawStockRow(item="BW-1009", wh="DUS", on_hand="25", allocated="3")])
    assert report.status == StockStatus.UNDETERMINED
    assert report.explanation == ("Stock cannot be determined: SKU BW-1009 exists as 2 item records, so warehouse quantities "
                                  "cannot be attributed reliably.")


def test_negative_allocated_is_undetermined_before_assessment():
    product = build_products([_item(1, "BW-1001", "4.85", avail="J")])[0]
    report = assess_stock(product, [RawStockRow(item="BW-1001", wh="DUS", on_hand="0", allocated="-2")])
    assert report.status == StockStatus.UNDETERMINED and report.available_quantity is None and report.known_minimum is None
    assert [(i.code, i.field) for i in report.issues] == [(IssueCode.NEGATIVE_QUANTITY, "allocated")]
    assert report.explanation == "Stock cannot be determined: warehouse DUS reports a negative allocated quantity (-2) for BW-1001."


def test_build_order_total_mismatch():
    order = build_order({"OrderNo": "SO-240109", "Acct": "52290", "Date": "2025-11-10", "Stat": "OP", "Total": "100.00", "Ref": "RRM-118",
                         "lines": [{"ln": 1, "item": "BW-1011", "qty": "4", "price": "23.10"}]})
    assert order.total == Decimal("100.00") and order.lines_total == Decimal("92.40")
    assert [i.code for i in order.issues] == [IssueCode.TOTAL_MISMATCH]
