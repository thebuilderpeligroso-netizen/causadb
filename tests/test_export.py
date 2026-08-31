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


def _post(port, path, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data, resp.getheader("Content-Type")


def _log_event(port, event_type, ctx_id="test-export"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps({
        "event_type": event_type,
        "ctx_id": ctx_id,
        "source": "export:test",
        "payload": {"test": True},
    })
    conn.request("POST", "/api/log", body, {"Content-Type": "application/json"})
    resp = conn.getresponse()
    resp.read()
    conn.close()


def test_export_json_default(ledger_and_server):
    """POST /api/export without format returns JSON."""
    _, port, _ = ledger_and_server
    _log_event(port, "FILE_MODIFIED")
    status, data, ctype = _post(port, "/api/export", {})
    assert status == 200
    assert "application/json" in ctype
    results = json.loads(data)
    assert isinstance(results, list)


def test_export_json_explicit(ledger_and_server):
    """POST /api/export?format=json returns JSON."""
    _, port, _ = ledger_and_server
    _log_event(port, "COMMAND_RUN")
    status, data, ctype = _post(port, "/api/export", {"format": "json"})
    assert status == 200
    assert "application/json" in ctype
    results = json.loads(data)
    assert isinstance(results, list)


def test_export_csv(ledger_and_server):
    """POST /api/export?format=csv returns CSV."""
    _, port, _ = ledger_and_server
    _log_event(port, "FILE_MODIFIED")
    status, data, ctype = _post(port, "/api/export", {"format": "csv"})
    assert status == 200
    assert "text/csv" in ctype
    body = data.decode()
    assert "event_type" in body
    assert "FILE_MODIFIED" in body


def test_export_csv_with_event_type_filter(ledger_and_server):
    """POST /api/export with event_type filter works."""
    _, port, _ = ledger_and_server
    _log_event(port, "FILE_MODIFIED")
    _log_event(port, "COMMAND_RUN")

    status, data, ctype = _post(port, "/api/export", {
        "format": "csv",
        "event_type": "FILE_MODIFIED",
    })
    assert status == 200
    body = data.decode()
    assert "FILE_MODIFIED" in body
    assert "COMMAND_RUN" not in body


def test_export_csv_empty_ledger(ledger_and_server):
    """Export CSV from empty ledger returns CSV with header only."""
    _, port, _ = ledger_and_server
    status, data, ctype = _post(port, "/api/export", {"format": "csv"})
    assert status == 200
    body = data.decode()
    # At minimum a header row (genesis events may exist)
    assert "event_type" in body or body.strip() == ""
