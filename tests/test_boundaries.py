"""Static architecture checks."""

import inspect
import re
from pathlib import Path

from legacybridge import service as service_module
from legacybridge.bridge.client import LegacyClient

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "legacybridge"


def _py_files():
    return sorted(SRC.rglob("*.py"))


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_no_shared_database_access():
    for path in _py_files():
        text = _text(path)
        assert "sqlite3" not in text, path
        # The literal token ``jdbc`` (as in ``jdbc:sqlite:``) must not appear. The console sidebar label
        # "… JDBC/SQLite" describes the legacy stack and is not a connection string.
        assert "jdbc" not in text, path
        assert "jdbc:" not in text.lower(), path
        assert not re.search(r"\.db\b", text), path


def test_httpx_only_in_client():
    importers = {p.relative_to(SRC).as_posix() for p in _py_files() if re.search(r"^\s*(import httpx|from httpx)", _text(p), re.M)}
    assert importers == {"bridge/client.py"}


def test_submit_order_only_defined_in_service_and_used_in_ui():
    for path in _py_files():
        rel = path.relative_to(SRC).as_posix()
        if "submit_order(" in _text(path):
            assert rel == "service.py" or rel.startswith("ui/"), rel
    assert len(re.findall(r"submit_order\(", _text(SRC / "service.py"))) == 1  # the definition only


def test_approval_constructed_only_in_ui_components():
    for path in _py_files():
        rel = path.relative_to(SRC).as_posix()
        if "Approval(" in _text(path):
            assert rel in {"ui/components.py", "bridge/models.py"}, rel


def test_post_order_once_only_in_client_and_service():
    for path in _py_files():
        rel = path.relative_to(SRC).as_posix()
        if "post_order_once(" in _text(path):
            assert rel in {"bridge/client.py", "service.py"}, rel


def test_submission_and_recheck_never_reference_the_simulator():
    names = ["submit_order", "recheck_submission", "_transmit", "_classify", "_read_back", "_reconcile",
             "_matches_approved", "_same_account", "_result", "_record", "_order_rows"]
    for name in names:
        source = inspect.getsource(getattr(service_module.BridgeService, name))
        assert "sim" not in source.lower(), name
        assert "SimControlClient" not in source and "_sim/" not in source, name


def test_list_items_raw_has_no_query_or_category():
    assert list(inspect.signature(LegacyClient.list_items_raw).parameters) == ["self", "page"]


def test_no_integration_call_passes_item_to_stock_raw():
    for path in _py_files():
        for match in re.finditer(r"stock_raw\(([^)]*)\)", _text(path)):
            assert "item" not in match.group(1) or match.group(0).startswith("stock_raw(self"), (path, match.group(0))
    calls = re.findall(r"\.stock_raw\(([^)]*)\)", "".join(_text(p) for p in _py_files()))
    assert calls and all(args.strip() == "" for args in calls)


def test_integration_never_reads_fault_state_on_product_paths():
    for rel in ("service.py", "assistant/tools.py", "assistant/agent.py", "bridge/normalize.py"):
        assert "list_faults" not in _text(SRC / rel), rel
    assert "/_sim/" not in inspect.getsource(LegacyClient)


def test_compose_has_no_volumes_and_loopback_ports():
    compose = _text(ROOT / "docker-compose.yml")
    assert "volumes:" not in compose
    ports = re.findall(r'ports:\s*\["([^"]+)"\]', compose)
    assert ports == ["127.0.0.1:8090:8090", "127.0.0.1:8501:8501"]
    assert '"${HALDEN_SIM_CONTROL:-true}"' in compose


def test_language_separation():
    assert not list((ROOT / "legacy-erp").rglob("*.py"))
    assert not list((ROOT / "src").rglob("*.java"))


def test_transport_settings_are_explicit():
    source = _text(SRC / "bridge" / "client.py")
    assert "follow_redirects=False" in source
    assert "HTTPTransport(retries=0)" in source
    assert "event_hooks" not in source
