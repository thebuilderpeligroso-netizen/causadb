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
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def _log_event(port, event_type, ctx_id="test", source="trace:test",
               payload=None, parent_event_id=None):
    event = {
        "event_type": event_type,
        "ctx_id": ctx_id,
        "source": source,
        "payload": payload or {},
    }
    if parent_event_id is not None:
        event["parent_event_id"] = parent_event_id
    return _post(port, "/api/log", event)


def test_trace_event_not_found(ledger_and_server):
    _, port, _ = ledger_and_server
    status, data = _post(port, "/api/trace", {"event_id": "nonexistent"})
    assert status == 404
    assert "not found" in data.get("error", "")


def test_trace_no_parent_no_children(ledger_and_server):
    _, port, _ = ledger_and_server
    _, log_data = _log_event(port, "FILE_MODIFIED")
    eid = log_data["event_id"]
    status, data = _post(port, "/api/trace", {"event_id": eid})
    assert status == 200
    assert data["event"]["event_id"] == eid
    assert data["parents"] == []
    assert data["children"] == []
    assert data["grandchildren"] == []


def test_trace_with_parent(ledger_and_server):
    _, port, _ = ledger_and_server
    _, parent_log = _log_event(port, "SESSION_STARTED", ctx_id="trace_parent")
    parent_id = parent_log["event_id"]
    _, child_log = _log_event(port, "FILE_MODIFIED",
                              parent_event_id=parent_id)
    child_id = child_log["event_id"]
    # Trace the child
    status, data = _post(port, "/api/trace", {"event_id": child_id})
    assert status == 200
    assert data["event"]["event_id"] == child_id
    assert len(data["parents"]) == 1
    assert data["parents"][0]["event_id"] == parent_id
    assert data["children"] == []
    assert data["grandchildren"] == []


def test_trace_with_child(ledger_and_server):
    _, port, _ = ledger_and_server
    _, parent_log = _log_event(port, "SESSION_STARTED", ctx_id="trace_child")
    parent_id = parent_log["event_id"]
    _, child_log = _log_event(port, "FILE_MODIFIED",
                              parent_event_id=parent_id)
    child_id = child_log["event_id"]
    # Trace the parent
    status, data = _post(port, "/api/trace", {"event_id": parent_id})
    assert status == 200
    assert data["event"]["event_id"] == parent_id
    assert data["parents"] == []
    assert len(data["children"]) == 1
    assert data["children"][0]["event_id"] == child_id


def test_trace_grandchild(ledger_and_server):
    _, port, _ = ledger_and_server
    _, gparent_log = _log_event(port, "SESSION_STARTED", ctx_id="trace_gc")
    gparent_id = gparent_log["event_id"]
    _, parent_log = _log_event(port, "FILE_MODIFIED",
                               parent_event_id=gparent_id)
    parent_id = parent_log["event_id"]
    _, child_log = _log_event(port, "COMMAND_RUN",
                              parent_event_id=parent_id)
    child_id = child_log["event_id"]
    # Trace the grandparent — should see child as child, grandchild as grandchild
    status, data = _post(port, "/api/trace", {"event_id": gparent_id})
    assert status == 200
    assert data["event"]["event_id"] == gparent_id
    assert data["parents"] == []
    assert len(data["children"]) == 1
    assert data["children"][0]["event_id"] == parent_id
    assert len(data["grandchildren"]) == 1
    assert data["grandchildren"][0]["event_id"] == child_id


def test_trace_no_event_id(ledger_and_server):
    _, port, _ = ledger_and_server
    status, data = _post(port, "/api/trace", {})
    assert status == 400
    assert "event_id" in data.get("error", "")


def test_trace_empty_body(ledger_and_server):
    _, port, _ = ledger_and_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/api/trace", "{}", {"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    assert resp.status == 400
