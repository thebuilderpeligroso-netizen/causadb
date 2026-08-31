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


def test_score_returns_json(ledger_and_server):
    _, port, _ = ledger_and_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/score")
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    assert resp.status == 200
    assert "overall_score" in data
    assert "churn_score" in data
    assert "waste_score" in data
    assert "survival_score" in data
    assert "weights_used" in data
    assert "per_session" in data
    assert 0 <= data["overall_score"] <= 100


def test_score_empty_ledger(ledger_and_server):
    _, port, _ = ledger_and_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/score")
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    assert resp.status == 200
    assert "overall_score" in data


def test_score_after_log_event(ledger_and_server):
    _, port, _ = ledger_and_server
    # Log a file event
    event = {
        "event_type": "FILE_MODIFIED",
        "ctx_id": "score-test",
        "source": "score:test",
        "payload": {"path": "score-test.py", "action": "modified"},
    }
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/api/log", json.dumps(event), {"Content-Type": "application/json"})
    conn.getresponse().read()
    conn.close()
    # Now get score
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/score")
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    assert resp.status == 200
    assert "overall_score" in data