"""GB-ORD: the ordinary product path against a fresh, control-disabled ERP (HALDEN_SIM_CONTROL=false).

Run only with ``uv run pytest -m ordinary tests/test_ordinary_path.py``. ``/_sim/*`` is touched only to
assert that it answers 404.
"""

import httpx
import pytest

from legacybridge.bridge.client import LegacyClient
from legacybridge.bridge.models import DraftLifecycle, OutcomeEvidence, StockStatus, SubmissionOutcome
from legacybridge.service import BridgeService
from legacybridge.ui import components
from tests.conftest import ERP_URL, LOGIN, SECRET, CountingTransport

pytestmark = pytest.mark.ordinary


@pytest.fixture
def ordinary_erp():
    try:
        banner = httpx.get(f"{ERP_URL}/halden/ping", timeout=5).text
    except httpx.HTTPError as exc:
        pytest.fail(f"HALDEN ERP is unreachable at {ERP_URL}: {exc}")
    assert banner.strip() == "HALDEN-ERP 3.1 READY"


@pytest.mark.parametrize(
    "method, path",
    [("POST", "/_sim/reset"), ("POST", "/_sim/faults"), ("GET", "/_sim/faults"), ("DELETE", "/_sim/faults"), ("POST", "/_sim/external-order")],
)
def test_simulation_control_is_absent(ordinary_erp, method, path):
    response = httpx.request(method, f"{ERP_URL}{path}", timeout=5)
    assert response.status_code == 404


def test_gb_ord_ordinary_booking(ordinary_erp):
    transport = CountingTransport()
    service = BridgeService(LegacyClient(ERP_URL, LOGIN, SECRET, transport=transport), sim=None)
    assert [c.account_id for c in service.search_customers("nordkap")] == ["00417"]
    report = service.check_availability("BW-1001")
    assert (report.status, report.available_quantity) == (StockStatus.IN_STOCK, 300)
    draft = service.prepare_order("00417", [("BW-1001", 10)])
    assert draft.lifecycle == DraftLifecycle.READY
    result = components.approve_draft(service, draft.draft_id, 1)
    assert (result.outcome, result.evidence, result.order_no) == (SubmissionOutcome.CONFIRMED, OutcomeEvidence.READ_BACK, "SO-240111")
    assert all(path.startswith("/halden/") for path in transport.paths())
    assert transport.e8_count == 1
