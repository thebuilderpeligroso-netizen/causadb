import pytest
import json
import http.client
from causadb._rest_api import serve_in_thread


@pytest.fixture
def ledger_and_server(tmp_path):
    from causadb._init import causadb_init
    result = causadb_init(str(tmp_path / "ws"))
    ledger = result["ledger_path"]
    server = serve_in_thread(ledger, port=0)
    port = server.server_port
    yield ledger, port, server
    server.shutdown()


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def _post(port, path, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def test_current_version_exists():
    from causadb._updater import get_current_version
    version = get_current_version()
    assert version == "0.1.0"


def test_check_update_no_github(monkeypatch):
    from causadb._updater import check_update, get_current_version

    def mock_get_latest():
        return {"tag_name": "v0.2.0", "assets": []}

    monkeypatch.setattr("causadb._updater.get_latest_release", mock_get_latest)
    monkeypatch.setattr("causadb._updater.get_current_version", lambda: "0.1.0")

    result = check_update()
    assert result["needs_update"] is True
    assert result["latest_version"] == "v0.2.0"
    assert result["current_version"] == "0.1.0"


def test_check_update_api_endpoint(ledger_and_server):
    _, port, _ = ledger_and_server
    status, data = _get(port, "/api/check-update")
    assert status == 200
    assert "needs_update" in data
    assert "latest_version" in data
    assert "current_version" in data
    assert isinstance(data["needs_update"], bool)
    assert isinstance(data["latest_version"], str)
    assert isinstance(data["current_version"], str)


def test_anti_teatro_update_endpoint_never_returns_true_when_offline(monkeypatch, ledger_and_server):
    """Artículo IX: test que verifica que el endpoint no miente.
    Si forzamos que check_update siempre devuelva False (offline),
    el endpoint debe devolver needs_update=False."""
    _, port, _ = ledger_and_server

    def mock_check_update():
        return {"needs_update": False, "latest_version": "v0.1.0", "current_version": "0.1.0"}

    monkeypatch.setattr("causadb._updater.check_update", mock_check_update)

    status, data = _get(port, "/api/check-update")
    assert status == 200
    assert data["needs_update"] is False


def test_post_update_endpoint(monkeypatch, ledger_and_server):
    """POST /api/update triggers install_or_check and returns status: ok."""
    _, port, _ = ledger_and_server

    def mock_install_or_check(check_only: bool = False):
        return {"needs_update": False, "latest_version": "v0.1.0", "current_version": "0.1.0"}

    monkeypatch.setattr("causadb._updater.install_or_check", mock_install_or_check)

    status, data = _post(port, "/api/update", {})
    assert status == 200
    assert data["status"] == "ok"