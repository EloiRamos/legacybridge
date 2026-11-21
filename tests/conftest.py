"""Shared test harness.

* ``live`` tests need the running sim-on Java ERP; an autouse fixture pings E1 and resets with
  ``rate_limit=0, ticket_ttl_sec=900, rate_window_sec=10``. Unreachable → ``pytest.fail`` (never skip).
* ``ordinary`` tests use their own fixtures and touch ``/_sim/*`` only to assert 404.
* Transports: ``CountingTransport`` records every request below all client layers;
  ``OverrideTransport`` forwards to the live ERP except for matching rules; ``GateTransport``
  holds the E8 open on an event. ``FakeHalden`` is an in-memory MockTransport ERP for offline tests.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode
from xml.sax.saxutils import escape

import httpx
import pytest

from legacybridge.bridge.client import LegacyClient, SimControlClient
from legacybridge.service import BridgeService

ERP_URL = os.environ.get("LEGACYBRIDGE_TEST_ERP_URL", "http://127.0.0.1:8090").rstrip("/")
LOGIN = "svc_bridge"
SECRET = "halden-demo-secret"
E8 = ("POST", "/halden/orders")


# ============================================================================ live fixtures


def _ping(url: str) -> str:
    return httpx.get(f"{url}/halden/ping", timeout=5).text


@pytest.fixture(scope="session")
def _live_session():
    yield
    try:
        SimControlClient(ERP_URL).reset()
    except Exception:  # pragma: no cover - best effort finalizer
        pass


@pytest.fixture(autouse=True)
def _live_reset(request):
    if request.node.get_closest_marker("live") is None:
        yield
        return
    request.getfixturevalue("_live_session")
    try:
        banner = _ping(ERP_URL)
    except httpx.HTTPError as exc:
        pytest.fail(f"HALDEN ERP is unreachable at {ERP_URL}: {exc} (start it with docker compose up -d --no-build legacy-erp)")
    if "HALDEN-ERP 3.1 READY" not in banner:
        pytest.fail(f"Unexpected ping banner from {ERP_URL}: {banner!r}")
    if not SimControlClient(ERP_URL).reset(rate_limit=0, ticket_ttl_sec=900, rate_window_sec=10):
        pytest.fail("Simulator control answered 404; live tests need a sim-on ERP.")
    yield


@pytest.fixture
def sim() -> SimControlClient:
    return SimControlClient(ERP_URL)


@pytest.fixture
def counting_transport() -> "CountingTransport":
    return CountingTransport()


@pytest.fixture
def live_client(counting_transport) -> LegacyClient:
    return LegacyClient(ERP_URL, LOGIN, SECRET, transport=counting_transport)


@pytest.fixture
def live_service(live_client, sim) -> BridgeService:
    return BridgeService(live_client, sim)


def verification_client() -> LegacyClient:
    """A separate client for checking ERP state. Logging in revokes the service client's ticket;
    reads re-login transparently, so call this between operations, not during one."""
    return LegacyClient(ERP_URL, LOGIN, SECRET)


def erp_orders(acct: str | None = None) -> list[dict]:
    client = verification_client()
    try:
        rows: list[dict] = []
        cursor = None
        while True:
            body = client.list_orders_raw(acct=int(acct) if acct else None, cursor=cursor)
            rows.extend(body["data"]["orders"])
            cursor = body["continue"]
            if not cursor:
                return rows
    finally:
        client.close()


def raw_e8(form: list[tuple[str, str]]) -> httpx.Response:
    """A raw E8 posted by a test (not by the integration), with its own session."""
    session = httpx.post(f"{ERP_URL}/halden/session", data={"login": LOGIN, "secret": SECRET}, timeout=5)
    ticket = dict(line.split("=", 1) for line in session.text.splitlines() if "=" in line)["TICKET"]
    return httpx.post(
        f"{ERP_URL}/halden/orders",
        content=urlencode(form).encode("utf-8"),
        headers={"X-Halden-Ticket": ticket, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        timeout=10,
    )


# ============================================================================ transports


@dataclass
class Record:
    method: str
    path: str
    params: list[tuple[str, str]]
    status: int | None = None


class CountingTransport(httpx.BaseTransport):
    """Records (method, path, query) for every request handed to it, then forwards."""

    def __init__(self, inner: httpx.BaseTransport | None = None) -> None:
        self.inner = inner if inner is not None else httpx.HTTPTransport(retries=0)
        self.records: list[Record] = []
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        record = Record(request.method, request.url.path, list(parse_qsl(request.url.query.decode(), keep_blank_values=True)))
        with self._lock:
            self.records.append(record)
        response = self.inner.handle_request(request)
        record.status = response.status_code
        return response

    def count(self, method: str, path: str | None = None, prefix: str | None = None) -> int:
        with self._lock:
            return sum(
                1 for r in self.records
                if r.method == method and (path is None or r.path == path) and (prefix is None or r.path.startswith(prefix))
            )

    @property
    def e8_count(self) -> int:
        return self.count(*E8)

    def paths(self) -> list[str]:
        return [r.path for r in self.records]

    def reset(self) -> None:
        with self._lock:
            self.records.clear()


Rule = tuple[str, str, Callable[[httpx.Request], httpx.Response]]


class OverrideTransport(httpx.BaseTransport):
    """Forwards to the live ERP except for matching (method, path-regex) rules. An E5 rule applies
    only to E5 requests WITHOUT ``item``; any other E5 request is forwarded unchanged."""

    def __init__(self, rules: list[Rule]) -> None:
        self.rules = rules
        self.forward = httpx.HTTPTransport(retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        for method, pattern, handler in self.rules:
            if request.method == method and re.fullmatch(pattern, request.url.path):
                if request.url.path == "/halden/stock" and "item" in dict(parse_qsl(request.url.query.decode())):
                    break
                return handler(request)
        return self.forward.handle_request(request)


def stock_response(body: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(200, content=body.encode("utf-8"), headers={"Content-Type": "text/plain;charset=UTF-8"})


def override_transport(rules: list[Rule]) -> CountingTransport:
    return CountingTransport(OverrideTransport(rules))


class GateTransport(httpx.BaseTransport):
    """Holds every E8 until ``release`` is set; ``entered`` is set when an E8 arrives."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self.inner = inner
        self.release = threading.Event()
        self.entered = threading.Event()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == E8:
            self.entered.set()
            self.release.wait(timeout=10)
        return self.inner.handle_request(request)


SEED_STOCK = (
    "ITEM;WH;ON_HAND;ALLOCATED\r\n"
    "BW-1001;DUS;240;0\r\nBW-1001;MAN;60;0\r\nBW-1002;DUS;0;0\r\nBW-1002;MAN;0;0\r\nBW-1003;DUS;3;0\r\n"
    "BW-1004;DUS;18;3\r\nBW-1004;MAN;4;0\r\nBW-1005;DUS;50;5\r\nBW-1005;MAN;?;0\r\nBW-1006;DUS;500 ;0\r\n"
    "BW-1006;MAN;1.200;0\r\nBW-1007;DUS;12;0\r\nBW-1008;DUS;7;0\r\nBW-1009;DUS;25;3\r\nbw-1009 ;MAN;10;0\r\n"
    "BW-1010;DUS;2;0\r\nBW-1011;DUS;-3;0\r\n"
)


# ============================================================================ in-memory fake ERP (offline)


def json_response(body: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})


FAKE_CUSTOMERS = [
    {"acct": 417, "nm": "NORDKAP MARINE SERVICES AS", "contact": "HOLM, ERIK", "mail": " Erik.Holm@Nordkap-Marine.example ",
     "tel": "+47 22 33 44 55", "hold": "N", "since": "2019-03-04", "city": "Bergen", "ctry": "NO"},
    {"acct": 10871, "nm": "ALDER & FINCH FABRICATION LTD", "contact": "RAMAN, PRIYA", "mail": "accounts@alderfinch.example",
     "tel": "+44 161 496 0732", "hold": "J", "since": "20200211", "city": "Manchester", "ctry": "GB"},
]
FAKE_ITEMS = [
    {"id": 1, "ITMNO": "BW-1001", "DESCR": "Deep Groove Ball Bearing 6205-2RS", "CATG": "Bearings", "UPRICE": "4.85",
     "UOM": "EA", "AVAIL_FLG": "J", "DISC_FLG": "N", "UPDTS": "2025-10-01"},
    {"id": 4, "ITMNO": "BW-1004", "DESCR": "Ball Valve DN50 Stainless Steel", "CATG": "Valves", "UPRICE": "86.20",
     "UOM": "EA", "AVAIL_FLG": "1", "UPDTS": "2025-10-15"},
]
FAKE_STOCK = "ITEM;WH;ON_HAND;ALLOCATED\r\nBW-1001;DUS;240;0\r\nBW-1001;MAN;60;0\r\nBW-1004;DUS;18;3\r\nBW-1004;MAN;4;0\r\n"


@dataclass
class FakeHalden:
    """A programmable in-memory HALDEN for MockTransport tests. Every request is recorded."""

    customers: list[dict] = field(default_factory=lambda: [dict(c) for c in FAKE_CUSTOMERS])
    items: list[dict] = field(default_factory=lambda: [dict(i) for i in FAKE_ITEMS])
    stock: str = FAKE_STOCK
    orders: dict[str, dict] = field(default_factory=lambda: {
        "SO-240101": {"OrderNo": "SO-240101", "Acct": "417", "Date": "2025-09-02", "Stat": "DL", "Total": "97.00",
                      "Ref": None, "lines": [{"ln": 1, "item": "BW-1001", "qty": "20", "price": "4.85"}]},
    })
    next_order: int = 240111
    # e8: None → book the order; otherwise a callable(request) -> Response (or raise httpx exception)
    e8: Callable[[httpx.Request], httpx.Response] | None = None
    e6_rows: Callable[[], list[dict]] | None = None
    e6_error: bool = False
    e7: dict[str, Callable[[httpx.Request], httpx.Response]] = field(default_factory=dict)
    e3_error: Exception | None = None
    records: list[Record] = field(default_factory=list)
    gate: threading.Event | None = None
    gate_entered: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # ---------------------------------------------------------------- helpers
    def count(self, method: str, path_prefix: str) -> int:
        with self.lock:
            return sum(1 for r in self.records if r.method == method and r.path.startswith(path_prefix))

    @property
    def e8_count(self) -> int:
        with self.lock:
            return sum(1 for r in self.records if (r.method, r.path) == E8)

    def io_count(self) -> int:
        with self.lock:
            return len(self.records)

    def book(self, form: list[tuple[str, str]]) -> dict:
        items = [v for k, v in form if k == "item"]
        qtys = [v for k, v in form if k == "qty"]
        acct = dict(form)["acct"]
        ref = dict(form).get("ref")
        order_no = f"SO-{self.next_order:06d}"
        self.next_order += 1
        prices = {i["ITMNO"]: i["UPRICE"] for i in self.items}
        lines = [{"ln": n, "item": item, "qty": qty, "price": prices.get(item, "0.00")} for n, (item, qty) in enumerate(zip(items, qtys), start=1)]
        total = sum(float(prices.get(i, "0")) * int(q) for i, q in zip(items, qtys))
        self.orders[order_no] = {"OrderNo": order_no, "Acct": str(int(acct)), "Date": "20.11.2026", "Stat": "OP",
                                 "Total": f"{total:.2f}", "Ref": ref, "lines": lines}
        return {"rc": 0, "order_no": order_no, "total": f"{total:.2f}"}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, **kwargs) -> LegacyClient:
        kwargs.setdefault("sleep", lambda s: None)
        return LegacyClient("http://fake-halden", LOGIN, kwargs.pop("secret", SECRET), transport=self.transport(), **kwargs)

    # ---------------------------------------------------------------- dispatcher
    def handle(self, request: httpx.Request) -> httpx.Response:
        params = list(parse_qsl(request.url.query.decode(), keep_blank_values=True))
        with self.lock:
            self.records.append(Record(request.method, request.url.path, params))
        path, method = request.url.path, request.method
        if path == "/halden/session":
            form = dict(parse_qsl(request.content.decode()))
            if form.get("secret") != SECRET:
                return httpx.Response(200, text="STATUS=DENIED\r\nREASON=BAD_CREDENTIALS\r\n")
            return httpx.Response(200, text="STATUS=GRANTED\r\nTICKET=ABCDEF0123456789ABCDEF0123456789\r\nEXPIRES_IN=900\r\n")
        if path == "/halden/customers":
            if self.e3_error is not None:
                raise self.e3_error
            q = dict(params)
            rows = self.customers
            if "acct" in q:
                rows = [r for r in rows if str(r["acct"]) == str(int(q["acct"]))]
            if q.get("name"):
                rows = [r for r in rows if q["name"].lower() in r["nm"].lower()]
            return json_response({"rows": rows, "next_offset": None})
        if path == "/halden/items":
            body = '<?xml version="1.0" encoding="UTF-8"?><ITEMS page="1" pages="1">'
            for item in self.items:
                body += f'<ITEM id="{item["id"]}">' + "".join(
                    f"<{tag}>{escape(item[tag])}</{tag}>" for tag in ("ITMNO", "DESCR", "CATG", "UPRICE", "UOM", "AVAIL_FLG", "DISC_FLG", "UPDTS") if tag in item
                ) + "</ITEM>"
            return httpx.Response(200, content=(body + "</ITEMS>").encode())
        if path == "/halden/stock":
            return httpx.Response(200, content=self.stock.encode())
        if path == "/halden/orders" and method == "GET":
            if self.e6_error:
                return httpx.Response(500, text="<html><body><h1>HALDEN ERP - Internal Error</h1></body></html>")
            acct = dict(params).get("acct")
            rows = self.e6_rows() if self.e6_rows else [
                {k: v for k, v in o.items() if k != "lines"} for o in self.orders.values()
                if acct is None or o["Acct"] == str(int(acct))
            ]
            return json_response({"data": {"orders": rows}, "continue": ""})
        if path.startswith("/halden/orders/") and method == "GET":
            order_no = path.rsplit("/", 1)[1].upper()
            if order_no in self.e7:
                return self.e7[order_no](request)
            order = self.orders.get(order_no)
            return json_response({"rc": 0, "order": order} if order else {"rc": 404, "msg": "ORDER NOT ON FILE"})
        if (method, path) == E8:
            if self.gate is not None:
                self.gate_entered.set()
                self.gate.wait(timeout=10)
            if self.e8 is not None:
                return self.e8(request)
            with self.lock:
                return json_response(self.book(list(parse_qsl(request.content.decode()))))
        return httpx.Response(404, text="not found")


@pytest.fixture
def fake() -> FakeHalden:
    return FakeHalden()


@pytest.fixture
def fake_service(fake) -> BridgeService:
    return BridgeService(fake.client(), None)
