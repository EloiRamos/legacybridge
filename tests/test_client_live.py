"""LegacyClient and SimControlClient against the running Java ERP."""

import time

import httpx
import pytest

from legacybridge.bridge.client import LegacyClient, SimControlClient
from legacybridge.bridge.errors import LegacyAuthError
from tests.conftest import ERP_URL, LOGIN, SECRET, CountingTransport, erp_orders

pytestmark = pytest.mark.live


def counted(**kwargs):
    transport = CountingTransport()
    return LegacyClient(ERP_URL, LOGIN, SECRET, transport=transport, **kwargs), transport


def test_ping_banner():
    client, _ = counted()
    assert client.ping() == "HALDEN-ERP 3.1 READY"


def test_lazy_login_on_first_request():
    client, transport = counted()
    assert transport.records == []
    client.search_customers_raw(name="nordkap")
    assert [(r.method, r.path) for r in transport.records] == [("POST", "/halden/session"), ("GET", "/halden/customers")]


def test_transparent_relogin_after_revocation():
    client, transport = counted()
    client.search_customers_raw(name="nordkap")
    httpx.post(f"{ERP_URL}/halden/session", data={"login": LOGIN, "secret": SECRET}, timeout=5)  # revokes the ticket
    transport.reset()
    assert client.search_customers_raw(name="nordkap")["rows"][0]["acct"] == 417
    assert [(r.method, r.path, r.status) for r in transport.records] == [
        ("GET", "/halden/customers", 403), ("POST", "/halden/session", 200), ("GET", "/halden/customers", 200)]


def test_zero_ttl_second_auth_failure_raises(sim):
    assert sim.reset(rate_limit=0, ticket_ttl_sec=0, rate_window_sec=10) is True
    client, transport = counted()
    with pytest.raises(LegacyAuthError):
        client.search_customers_raw(name="nordkap")
    assert [r.status for r in transport.records if r.path == "/halden/customers"] == [440, 440]


def test_rate_limited_read_is_waited_out(sim):
    assert sim.reset(rate_limit=2, ticket_ttl_sec=900, rate_window_sec=3) is True
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        time.sleep(seconds)

    client, transport = counted(sleep=sleep)
    for _ in range(3):
        client.search_customers_raw(name="nordkap")
    assert 429 in [r.status for r in transport.records]
    assert sleeps and all(0 < s <= 5 for s in sleeps)


def test_read_after_one_armed_error_500_succeeds_on_retry(sim):
    assert sim.arm_fault("GET", "/halden/customers", "error_500", 1) == 1
    client, transport = counted(sleep=lambda s: None)
    assert client.search_customers_raw(name="nordkap")["rows"]
    assert [r.status for r in transport.records if r.path == "/halden/customers"] == [500, 200]


def test_customers_in_two_offset_pages():
    client, _ = counted()
    first = client.search_customers_raw()
    second = client.search_customers_raw(offset=first["next_offset"])
    assert (len(first["rows"]), first["next_offset"], len(second["rows"]), second["next_offset"]) == (5, 5, 3, None)


def test_item_pages_and_raw_values():
    client, _ = counted()
    records = []
    for page in (1, 2, 3):
        items, pages = client.list_items_raw(page)
        assert pages == 3 and len(items) == 4
        records.extend(items)
    assert [r["id"] for r in records] == list(range(1, 13))
    by_id = {r["id"]: r for r in records}
    assert by_id[2]["catg"] == "bearings "
    assert by_id[11]["catg"] is None and by_id[11]["avail"] == ""
    assert by_id[4]["disc"] is None and by_id[3]["disc"] == ""
    assert by_id[12]["item_no"] == "bw-1009 "
    assert client.list_items_raw(4) == ([], 3)


def test_stock_extract_rows_untrimmed():
    client, _ = counted()
    rows = client.stock_raw()
    assert len(rows) == 17
    assert (rows[9].item, rows[9].on_hand) == ("BW-1006", "500 ")
    assert (rows[14].item, rows[14].wh) == ("bw-1009 ", "MAN")
    assert rows[8].on_hand == "?" and rows[16].on_hand == "-3"


def test_order_cursor_pages_and_missing_detail():
    client, _ = counted()
    sizes, cursor = [], None
    while True:
        body = client.list_orders_raw(cursor=cursor)
        sizes.append(len(body["data"]["orders"]))
        cursor = body["continue"]
        if not cursor:
            break
    assert sizes == [3, 3, 3, 1]
    assert client.get_order_raw("SO-999999") == {"rc": 404, "msg": "ORDER NOT ON FILE"}


@pytest.mark.parametrize(
    "acct, lines, kind",
    [("00417", [("BW-1001", 10)], "ACCEPTED"), ("10871", [("BW-1001", 1)], "REJECTED"), ("52290", [("BW-1010", 5)], "SHORT")],
)
def test_post_order_once_against_the_erp(acct, lines, kind):
    client, transport = counted()
    client.prepare_write(acct)
    observation = client.post_order_once(acct, "LB-0000TEST", lines)
    assert observation.kind == kind and transport.e8_count == 1 and client.e8_request_count == 1
    if kind == "ACCEPTED":
        assert observation.order_no == "SO-240111" and observation.total == "48.50"
    if kind == "REJECTED":
        assert (observation.rc, observation.msg) == (12, "ACCOUNT_ON_HOLD")
    if kind == "SHORT":
        assert (observation.short_item, observation.short_available) == ("BW-1010", 2)


def test_sim_external_order_and_fault_round_trip(sim):
    observation = sim.external_order("31002", "BW-1010", 2)
    assert observation.kind == "ACCEPTED" and observation.order_no == "SO-240111"
    assert [o["Ref"] for o in erp_orders("31002") if o["OrderNo"] == "SO-240111"] == ["EXT-000001"]
    assert sim.arm_fault("POST", "/halden/orders", "delay", 2, 100) == 1
    assert sim.list_faults() == [{"method": "POST", "path_prefix": "/halden/orders", "mode": "delay", "remaining": 2, "delay_ms": 100}]
    sim.clear_faults()
    assert sim.list_faults() == []


def test_sim_reset_returns_boolean_true_at_the_client(sim):
    result = SimControlClient(ERP_URL).reset(rate_limit=0, ticket_ttl_sec=900, rate_window_sec=10)
    assert result is True


def test_c1_http_body_effective_settings_on_the_wire():
    response = httpx.post(f"{ERP_URL}/_sim/reset", data={"rate_limit": "0", "ticket_ttl_sec": "900", "rate_window_sec": "10"}, timeout=10)
    assert response.status_code == 200
    assert response.json() == {"reset": True, "rate_limit": 0, "ticket_ttl_sec": 900, "rate_window_sec": 10}
