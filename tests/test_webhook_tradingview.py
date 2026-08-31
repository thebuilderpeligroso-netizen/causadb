import pytest
import json
import http.client
from causadb._rest_api import serve_in_thread


@pytest.fixture
def ledger_and_server(tmp_path):
    from causadb._init import causadb_init
    result = causadb_init(str(tmp_path / "ws"))
    ledger = result["ledger_path"]
    server = serve_in_thread(ledger, port=0)  # port 0 = OS-assigned
    port = server.server_port
    yield ledger, port, server
    server.shutdown()


def _post(port, path, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def test_webhook_tradingview_logs_trade(ledger_and_server):
    """POST /api/webhook/tradingview with a valid trade payload."""
    _, port, _ = ledger_and_server
    payload = {"symbol": "BTCUSD", "side": "buy", "qty": 0.01, "price": 95000.0}
    status, data = _post(port, "/api/webhook/tradingview", payload)
    assert status == 200
    assert "event_id" in data
    assert "timestamp" in data


def test_webhook_tradingview_event_appears_in_query(ledger_and_server):
    """The TRADE_EXECUTED event written by the webhook is queryable."""
    _, port, _ = ledger_and_server
    payload = {"symbol": "BTCUSD", "side": "buy", "qty": 0.01, "price": 95000.0}
    status, data = _post(port, "/api/webhook/tradingview", payload)
    assert status == 200
    event_id = data["event_id"]

    # Query for TRADE_EXECUTED events
    status, events = _post(port, "/api/query", {"event_type": "TRADE_EXECUTED"})
    assert status == 200
    assert isinstance(events, list)
    assert len(events) >= 1

    # Find our event — query returns entries with nested "event" key
    found = [e for e in events if e.get("event", {}).get("event_id") == event_id]
    assert len(found) == 1
    ev = found[0]["event"]
    assert ev["event_type"] == "TRADE_EXECUTED"
    assert ev["ctx_id"] == "tradingview"
    assert ev["source"] == "tradingview:webhook"


def test_webhook_tradingview_empty_body(ledger_and_server):
    """POST /api/webhook/tradingview with empty body → still 200 (fall-closed)."""
    _, port, _ = ledger_and_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/api/webhook/tradingview", "", {"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    assert resp.status == 200
    assert "event_id" in data


def test_webhook_tradingview_no_auth_required(ledger_and_server):
    """TradingView webhook is public — no API key needed."""
    _, port, _ = ledger_and_server
    payload = {"symbol": "ETHUSD", "side": "sell", "qty": 2.5, "price": 3200.0}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    # No X-API-Key header
    conn.request("POST", "/api/webhook/tradingview", json.dumps(payload),
                 {"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    assert resp.status == 200
    assert "event_id" in data