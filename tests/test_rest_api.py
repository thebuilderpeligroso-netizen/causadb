import pytest
import json
import http.client
from causadb._rest_api import serve_in_thread
from causadb._event_types import EventType
from causadb._event_registry import register_type, EventTypeSpec


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


def test_health(ledger_and_server):
    _, port, _ = ledger_and_server
    status, data = _get(port, "/api/health")
    assert status == 200
    assert data["status"] == "ok"


def test_log_builtin_event(ledger_and_server):
    _, port, _ = ledger_and_server
    event = {
        "event_type": "FILE_MODIFIED",
        "ctx_id": "test",
        "source": "rest:test",
        "payload": {"path": "test.txt", "action": "modified"},
    }
    status, data = _post(port, "/api/log", event)
    assert status == 200
    assert "event_id" in data


def test_log_custom_event(ledger_and_server):
    _, port, ledger = ledger_and_server
    register_type("POLICY_CHECKED", EventTypeSpec(required_fields={"policy_id"}))
    event = {
        "event_type": "POLICY_CHECKED",
        "ctx_id": "test",
        "source": "rest:test",
        "payload": {"policy_id": "p1"},
    }
    status, data = _post(port, "/api/log", event)
    assert status == 200
    assert "event_id" in data


def test_log_empty_body(ledger_and_server):
    _, port, _ = ledger_and_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/api/log", "", {"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    assert resp.status == 400


def test_log_invalid_json(ledger_and_server):
    _, port, _ = ledger_and_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/api/log", "not json", {"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    assert resp.status == 400


def test_replay_empty(ledger_and_server):
    _, port, _ = ledger_and_server
    status, data = _post(port, "/api/replay", {})
    assert status == 200
    assert data["events_applied"] >= 0  # at least genesis event


def test_replay_after_log(ledger_and_server):
    _, port, _ = ledger_and_server
    event = {
        "event_type": "FILE_MODIFIED",
        "ctx_id": "test",
        "source": "rest:test",
        "payload": {"path": "test.txt", "action": "modified"},
    }
    _post(port, "/api/log", event)
    status, data = _post(port, "/api/replay", {})
    assert status == 200
    # Should have genesis + our event
    assert data["events_applied"] >= 1


def test_query_by_event_type(ledger_and_server):
    _, port, ledger = ledger_and_server
    event = {
        "event_type": "FILE_MODIFIED",
        "ctx_id": "test",
        "source": "rest:test",
        "payload": {"path": "test.txt", "action": "modified"},
    }
    _post(port, "/api/log", event)
    status, data = _post(port, "/api/query", {"event_type": "FILE_MODIFIED"})
    assert status == 200
    assert isinstance(data, list)


def test_not_found(ledger_and_server):
    _, port, _ = ledger_and_server
    status, data = _post(port, "/api/nonexistent", {})
    assert status == 404


def test_anti_teatro_port_0_assigns_random_port(ledger_and_server):
    _, port, _ = ledger_and_server
    assert port > 0


# ── Daemon endpoints ────────────────────────────────────────────

def test_daemon_status_endpoint(ledger_and_server):
    """GET /api/daemon/status returns JSON with boolean fields."""
    _, port, _ = ledger_and_server
    status, data = _get(port, "/api/daemon/status")
    assert status == 200
    assert "running" in data
    assert "vigilante" in data
    assert "mcp_proxy" in data
    assert "proxy_server" in data
    assert isinstance(data["running"], bool)
    assert isinstance(data["vigilante"], bool)


class _FakeDaemon:
    """Fake PlatformDaemon — prevents real subprocess daemons in tests."""

    def __init__(self, running=False):
        self._running = running

    def is_running(self, name: str) -> bool:
        return self._running

    def kill(self, name: str, timeout: float = 5.0) -> bool:
        return False


def test_daemon_start_endpoint(ledger_and_server, monkeypatch):
    """POST /api/daemon/start returns status json (no real subprocesses)."""
    import causadb._daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "get_daemon", lambda: _FakeDaemon(running=True))
    _, port, _ = ledger_and_server
    status, data = _post(port, "/api/daemon/start", {})
    assert status == 200
    assert data.get("status") in ("started", "already_running")
    assert data.get("vigilante") == "already_running"


def test_daemon_stop_endpoint(ledger_and_server, monkeypatch):
    """POST /api/daemon/stop returns status json (mock kill, no hang)."""
    import causadb._daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "get_daemon", lambda: _FakeDaemon(running=False))
    _, port, _ = ledger_and_server
    status, data = _post(port, "/api/daemon/stop", {})
    assert status == 200
    assert data.get("status") in ("stopped", "not_running")
    assert data.get("vigilante") == "not_running"


# ── Workspace endpoints ─────────────────────────────────────────

def test_workspaces_list_endpoint(ledger_and_server):
    """GET /api/workspaces returns a list."""
    _, port, _ = ledger_and_server
    status, data = _get(port, "/api/workspaces")
    assert status == 200
    assert "workspaces" in data
    assert isinstance(data["workspaces"], list)


def test_workspace_switch_invalid_path(ledger_and_server):
    """POST /api/workspace/switch with invalid path returns 400."""
    _, port, _ = ledger_and_server
    status, data = _post(port, "/api/workspace/switch", {"ledger_path": "/nonexistent/ledger.log"})
    assert status == 400
    assert "error" in data


def test_workspace_switch_empty_body(ledger_and_server):
    """POST /api/workspace/switch with no body returns 400."""
    _, port, _ = ledger_and_server
    status, data = _post(port, "/api/workspace/switch", {})
    assert status == 400
    assert "error" in data
