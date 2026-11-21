"""Offline adapter tests with httpx.MockTransport."""

import httpx
import pytest

from legacybridge.bridge.client import LegacyClient, parse_stock_extract
from legacybridge.bridge.errors import LegacyAuthError, LegacyMalformedResponse, LegacyRateLimited, LegacyUnavailable

GRANT = "STATUS=GRANTED\r\nTICKET=ABCDEF0123456789ABCDEF0123456789\r\nEXPIRES_IN=900\r\n"


class Scripted:
    """MockTransport handler: a session grant plus a per-path queue of responses/exceptions."""

    def __init__(self, routes: dict[tuple[str, str], list]):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key == ("POST", "/halden/session") and key not in self.routes:
            return httpx.Response(200, text=GRANT)
        queue = self.routes[key]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    def count(self, method: str, path: str) -> int:
        return sum(1 for r in self.requests if (r.method, r.url.path) == (method, path))


def make(routes, **kwargs):
    handler = Scripted(routes)
    sleeps: list[float] = []
    client = LegacyClient("http://halden", "svc_bridge", "halden-demo-secret", sleep=sleeps.append,
                          transport=httpx.MockTransport(handler), **kwargs)
    return client, handler, sleeps


# --------------------------------------------------------------------------- parsing


def test_malformed_json_is_not_retried():
    client, handler, _ = make({("GET", "/halden/customers"): [httpx.Response(200, text="{not json")]})
    with pytest.raises(LegacyMalformedResponse):
        client.search_customers_raw(name="x")
    assert handler.count("GET", "/halden/customers") == 1


def test_malformed_xml_and_missing_pages():
    client, _, _ = make({("GET", "/halden/items"): [httpx.Response(200, text="<ITEMS page='1'>")]})
    with pytest.raises(LegacyMalformedResponse):
        client.list_items_raw(1)
    client, _, _ = make({("GET", "/halden/items"): [httpx.Response(200, text="<ITEMS page='1'></ITEMS>")]})
    with pytest.raises(LegacyMalformedResponse):
        client.list_items_raw(1)


def test_xml_absent_element_is_none_and_empty_element_is_empty_string():
    xml = ('<?xml version="1.0" encoding="UTF-8"?><ITEMS page="3" pages="3"><ITEM id="11"><ITMNO>BW-1011</ITMNO>'
           "<DESCR>Jaw Coupling 28mm</DESCR><UPRICE>23.10</UPRICE><UOM>EA</UOM><AVAIL_FLG></AVAIL_FLG>"
           "<DISC_FLG>N</DISC_FLG><UPDTS>2025-07-14</UPDTS></ITEM></ITEMS>")
    client, _, _ = make({("GET", "/halden/items"): [httpx.Response(200, content=xml.encode())]})
    items, pages = client.list_items_raw(3)
    assert pages == 3
    assert items[0]["catg"] is None and items[0]["avail"] == "" and items[0]["id"] == 11


@pytest.mark.parametrize(
    "body",
    [
        "ITEM;WH;ON_HAND;ALLOCATE\r\nBW-1001;DUS;240;0\r\n",  # wrong header
        "ITEM;WH;ON_HAND;ALLOCATED\r\nBW-1001;DUS;240;0\r\n\r\nBW-1001;MAN;60;0\r\n",  # empty middle line
        "ITEM;WH;ON_HAND;ALLOCATED\r\nBW-1001;DUS;240;0\r\nBW-1001;MAN;60\r\n",  # 3 fields
        "ITEM;WH;ON_HAND;ALLOCATED\r\nBW-1001;DUS;240;0;X\r\n",  # 5 fields
        "ITEM;WH;ON_HAND;ALLOCATED\r\n ;MAN;?;0\r\n",  # missing SKU
        "ITEM;WH;ON_HAND;ALLOCATED\r\nBW-1001;;60;0\r\n",  # missing warehouse
    ],
)
def test_f03_4_stock_extract_strictness(body):
    with pytest.raises(LegacyMalformedResponse):
        parse_stock_extract(body)
    client, _, _ = make({("GET", "/halden/stock"): [httpx.Response(200, content=body.encode())]})
    with pytest.raises(LegacyMalformedResponse):
        client.stock_raw()


def test_f03_4_valid_extract_parses_all_rows_untrimmed():
    rows = parse_stock_extract("ITEM;WH;ON_HAND;ALLOCATED\r\nBW-1006;DUS;500 ;0\r\nbw-1009 ;MAN;10;0\r\nBW-1005;MAN;?;0\r\nBW-1001;DUS;0;-2\r\n")
    assert [(r.item, r.wh, r.on_hand, r.allocated) for r in rows] == [
        ("BW-1006", "DUS", "500 ", "0"), ("bw-1009 ", "MAN", "10", "0"), ("BW-1005", "MAN", "?", "0"), ("BW-1001", "DUS", "0", "-2")]


# --------------------------------------------------------------------------- read semantics


def test_read_retries_with_backoff_on_5xx_and_transport_errors():
    client, handler, sleeps = make({("GET", "/halden/customers"): [
        httpx.Response(500, text="err"), httpx.ReadTimeout("slow"), httpx.Response(200, json={"rows": [], "next_offset": None})]})
    assert client.search_customers_raw(name="x")["rows"] == []
    assert handler.count("GET", "/halden/customers") == 3
    assert sleeps == [0.25, 0.5]


def test_read_exhaustion_raises_unavailable():
    client, handler, sleeps = make({("GET", "/halden/customers"): [httpx.Response(500, text="err")]})
    with pytest.raises(LegacyUnavailable):
        client.search_customers_raw(name="x")
    assert handler.count("GET", "/halden/customers") == 3


def test_read_relogin_once_then_auth_error():
    client, handler, _ = make({("GET", "/halden/customers"): [httpx.Response(440, json={"fault": "TICKET_EXPIRED"})]})
    with pytest.raises(LegacyAuthError):
        client.search_customers_raw(name="x")
    assert handler.count("GET", "/halden/customers") == 2
    assert handler.count("POST", "/halden/session") == 2


def test_read_429_waits_capped_and_raises_when_exhausted():
    client, _, sleeps = make({("GET", "/halden/customers"): [httpx.Response(429, text="THROTTLED", headers={"X-Halden-Wait": "9"})]})
    with pytest.raises(LegacyRateLimited):
        client.search_customers_raw(name="x")
    assert sleeps == [5, 5]


def test_login_denied_raises_auth_error():
    client, _, _ = make({("POST", "/halden/session"): [httpx.Response(200, text="STATUS=DENIED\r\nREASON=BAD_CREDENTIALS\r\n")]})
    with pytest.raises(LegacyAuthError):
        client.search_customers_raw(name="x")


# --------------------------------------------------------------------------- post_order_once kinds


@pytest.mark.parametrize(
    "reply, kind",
    [
        (httpx.ConnectError("refused"), "NOT_DELIVERED"),
        (httpx.ConnectTimeout("no route"), "NOT_DELIVERED"),
        (httpx.Response(401, text="NO TICKET"), "PRE_HANDLER_REFUSAL"),
        (httpx.Response(403, text="<html><body><h2>403 Forbidden</h2></body></html>"), "PRE_HANDLER_REFUSAL"),
        (httpx.Response(440, json={"fault": "TICKET_EXPIRED"}), "PRE_HANDLER_REFUSAL"),
        (httpx.Response(429, text="THROTTLED", headers={"X-Halden-Wait": "3"}), "PRE_HANDLER_REFUSAL"),
        (httpx.Response(200, json={"rc": 12, "msg": "ACCOUNT_ON_HOLD"}), "REJECTED"),
        (httpx.Response(409, text="SHORT:BW-1010:2"), "SHORT"),
        (httpx.Response(200, json={"rc": 0, "order_no": "SO-240111", "total": "48.50"}), "ACCEPTED"),
        (httpx.ReadTimeout("lost"), "UNCERTAIN"),
        (httpx.RemoteProtocolError("reset"), "UNCERTAIN"),
        (httpx.Response(500, text="<html><body><h1>HALDEN ERP - Internal Error</h1></body></html>"), "UNCERTAIN"),
        (httpx.Response(200, text="garbage"), "UNCERTAIN"),
        (httpx.Response(200, json={"rc": 0}), "UNCERTAIN"),
        (httpx.Response(409, text="CONFLICT"), "UNCERTAIN"),
        (httpx.Response(302, headers={"Location": "/elsewhere"}), "UNCERTAIN"),
    ],
)
def test_post_order_once_kinds_with_exactly_one_request(reply, kind):
    client, handler, sleeps = make({("POST", "/halden/orders"): [reply]})
    observation = client.post_order_once("00417", "LB-0000ABCD", [("BW-1001", 10)])
    assert observation.kind == kind
    assert handler.count("POST", "/halden/orders") == 1
    assert client.e8_request_count == 1
    assert handler.count("POST", "/halden/session") == 0  # no re-login around the E8
    assert sleeps == []  # no wait-and-resend
    if kind == "ACCEPTED":
        assert observation.order_no == "SO-240111" and observation.total == "48.50"
    if kind == "SHORT":
        assert (observation.short_item, observation.short_available) == ("BW-1010", 2)


def test_post_order_once_form_is_positional():
    client, handler, _ = make({("POST", "/halden/orders"): [httpx.Response(200, json={"rc": 0, "order_no": "SO-240111", "total": "1.00"})]})
    client.post_order_once("00417", "LB-0000ABCD", [("BW-1001", 10), ("BW-1004", 1)])
    body = handler.requests[-1].content.decode()
    assert body == "acct=00417&ref=LB-0000ABCD&item=BW-1001&item=BW-1004&qty=10&qty=1"
    assert handler.requests[-1].headers["Content-Type"].startswith("application/x-www-form-urlencoded")


@pytest.mark.parametrize("status", [401, 403, 440])
def test_post_order_once_discards_ticket_on_auth_refusal(status):
    client, handler, _ = make({
        ("POST", "/halden/orders"): [httpx.Response(status, text="refused")],
        ("GET", "/halden/customers"): [httpx.Response(200, json={"rows": [{"acct": 417}], "next_offset": None})],
    })
    client.prepare_write("00417")
    assert handler.count("POST", "/halden/session") == 1
    assert client.post_order_once("00417", "LB-0000ABCD", [("BW-1001", 1)]).kind == "PRE_HANDLER_REFUSAL"
    client.prepare_write("00417")  # the next pre-send preparation logs in again
    assert handler.count("POST", "/halden/session") == 2
