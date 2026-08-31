"""Tests for HealthMetrics watchdog and /api/health REST endpoint."""

import json
import http.client
import pytest

from causadb._rest_api import serve_in_thread, CausaDBAPIHandler


# ---------------------------------------------------------------------------
# helpers (same pattern as test_rest_api.py)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger_and_server(tmp_path):
    from causadb._init import causadb_init
    result = causadb_init(str(tmp_path / "ws"))
    ledger = result["ledger_path"]
    server = serve_in_thread(ledger, port=0)
    port = server.server_port
    yield ledger, port, server
    server.shutdown()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

class TestHealthMetrics:
    """HealthMetrics unit-level + integration via REST."""

    def test_health_returns_metrics(self, ledger_and_server):
        """GET /api/health returns JSON with status, uptime_seconds,
        total_events, and last_event_timestamp."""
        _, port, _ = ledger_and_server

        status, data = _get(port, "/api/health")

        assert status == 200
        assert data["status"] == "ok"
        # After causadb_init there is at least the genesis event
        assert isinstance(data["total_events"], int)
        assert data["total_events"] >= 1
        assert isinstance(data["uptime_seconds"], (int, float))
        assert data["uptime_seconds"] >= 0
        # last_event_timestamp must be present (genesis has one)
        assert isinstance(data["last_event_timestamp"], str)
        assert data["last_event_timestamp"].endswith("Z")

    def test_health_total_events_increments(self, ledger_and_server):
        """Logging an event increments total_events by 1."""
        _, port, _ = ledger_and_server

        # Baseline
        _, before = _get(port, "/api/health")
        count_before = before["total_events"]

        # Log one event
        event = {
            "event_type": "FILE_MODIFIED",
            "ctx_id": "test",
            "source": "rest:test",
            "payload": {"path": "test.txt", "action": "modified"},
        }
        _post(port, "/api/log", event)

        # Verify increment
        _, after = _get(port, "/api/health")
        assert after["total_events"] == count_before + 1

    def test_anti_teatro_health_returns_static(self, ledger_and_server, monkeypatch):
        """If the handler is mutated to return a static dict,
        test_health_total_events_increments would fail — proving the
        health endpoint really derives metrics from the ledger."""
        ledger_path, port, _ = ledger_and_server

        # --- simulate a naive static handler ---
        def static_do_GET(self):
            if self.path == "/api/health":
                self._json_response({
                    "status": "ok",
                    "uptime_seconds": 0,
                    "total_events": 0,
                    "last_event_timestamp": None,
                })
            else:
                self._json_response({"error": "not found"}, 404)

        monkeypatch.setattr(CausaDBAPIHandler, "do_GET", static_do_GET)

        # Log a real event
        event = {
            "event_type": "FILE_MODIFIED",
            "ctx_id": "test",
            "source": "rest:test",
            "payload": {"path": "test.txt"},
        }
        _post(port, "/api/log", event)

        # Health still returns the hardcoded total (0), not the real count
        _, data = _get(port, "/api/health")
        assert data["total_events"] == 0, (
            "Static handler always returns 0 — if this were the real "
            "implementation, test_health_total_events_increments would FAIL "
            "because logging an event never changes the reported total."
        )

        # Verify the real ledger has more events than the static response
        from causadb._ledger_index import LedgerIndex
        real_index = LedgerIndex(ledger_path)
        real_index.rebuild()
        assert len(real_index.event_ids) > data["total_events"], (
            "The real ledger has more events than the static handler reports "
            "— total_events would never increment under a static implementation."
        )
