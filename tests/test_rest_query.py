import json
import http.client
import pytest

from causadb._rest_api import serve_in_thread
from causadb._event_types import EventType


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
    conn.request(
        "POST", path, json.dumps(body), {"Content-Type": "application/json"}
    )
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


def _populate_events(port):
    """Populate the ledger with 3 test events for query testing.

    Event layout:
      t0 — FILE_MODIFIED, ctx-alpha, payload={"path": "/rna/results.txt", ...}
      t1 — COMMAND_RUN,  ctx-alpha, payload={"command": "blastn", ...}
      t2 — FILE_MODIFIED, ctx-beta,  payload={"path": "/dna/report.txt", ...}
    """
    events = [
        {
            "event_type": EventType.FILE_MODIFIED.value,
            "ctx_id": "ctx-alpha",
            "source": "rest:query_test",
            "timestamp": "2026-06-01T00:00:00Z",
            "payload": {"path": "/rna/results.txt", "action": "create"},
        },
        {
            "event_type": EventType.COMMAND_RUN.value,
            "ctx_id": "ctx-alpha",
            "source": "rest:query_test",
            "timestamp": "2026-06-15T00:00:00Z",
            "payload": {"command": "blastn", "exit_code": 0},
        },
        {
            "event_type": EventType.FILE_MODIFIED.value,
            "ctx_id": "ctx-beta",
            "source": "rest:query_test",
            "timestamp": "2026-07-01T00:00:00Z",
            "payload": {"path": "/dna/report.txt", "action": "modify"},
        },
    ]
    ids = []
    for ev in events:
        status, data = _post(port, "/api/log", ev)
        assert status == 200, f"Failed to log event: {data}"
        ids.append(data["event_id"])
    return ids


class TestRestQuery:
    """GET /api/query?… — REST query endpoints (E.2)."""

    def test_rest_query_by_type(self, ledger_and_server):
        """GET /api/query?type=FILE_MODIFIED → solo eventos FILE_MODIFIED."""
        _, port, _ = ledger_and_server
        _populate_events(port)

        status, data = _get(port, "/api/query?type=FILE_MODIFIED")

        assert status == 200
        assert isinstance(data, list)
        assert len(data) == 2
        for event in data:
            assert event["event_type"] == "FILE_MODIFIED"

    def test_rest_query_by_time(self, ledger_and_server):
        """GET /api/query?from=...&to=... → eventos en rango de timestamps."""
        _, port, _ = ledger_and_server
        _populate_events(port)

        status, data = _get(
            port,
            "/api/query?from=2026-06-10T00:00:00Z&to=2026-07-10T00:00:00Z",
        )

        assert status == 200
        assert isinstance(data, list)
        # Esperado: evento t1 (2026-06-15) y t2 (2026-07-01)
        assert len(data) == 2
        timestamps = [e["timestamp"] for e in data]
        assert "2026-06-15T00:00:00Z" in timestamps
        assert "2026-07-01T00:00:00Z" in timestamps

    def test_rest_query_by_text(self, ledger_and_server):
        """GET /api/query?q=RNA → eventos con "RNA" en payload."""
        _, port, _ = ledger_and_server
        _populate_events(port)

        status, data = _get(port, "/api/query?q=RNA")

        assert status == 200
        assert isinstance(data, list)
        assert len(data) == 1
        # Solo t0 (FILE_MODIFIED de ctx-alpha) tiene "RNA" en el path
        assert data[0]["payload"]["path"] == "/rna/results.txt"

    def test_rest_query_combined(self, ledger_and_server):
        """GET /api/query?type=FILE_MODIFIED&q=RNA&from=... → AND combinado."""
        _, port, _ = ledger_and_server
        _populate_events(port)

        status, data = _get(
            port,
            "/api/query?type=FILE_MODIFIED&q=RNA&from=2026-05-01T00:00:00Z",
        )

        assert status == 200
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["event_type"] == "FILE_MODIFIED"
        assert data[0]["payload"]["path"] == "/rna/results.txt"

    def test_rest_query_empty_array(self, ledger_and_server):
        """GET /api/query?type=NONEXISTENT → [] (no error)."""
        _, port, _ = ledger_and_server
        _populate_events(port)

        status, data = _get(port, "/api/query?type=NONEXISTENT")

        assert status == 200
        assert data == []
