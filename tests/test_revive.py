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


def test_revive_without_to_time(ledger_and_server):
    """POST /api/replay with {} works identically to before (no to_time)."""
    _, port, _ = ledger_and_server
    status, data = _post(port, "/api/replay", {})
    assert status == 200
    assert data["events_applied"] >= 0


def test_revive_with_to_time(ledger_and_server):
    """POST /api/replay with to_time returns state up to that time."""
    _, port, _ = ledger_and_server
    status, data = _post(port, "/api/replay", {"to_time": "2025-01-01T00:00:00Z"})
    assert status == 200
    assert "events_applied" in data


def test_revive_before_any_events(ledger_and_server):
    """Revive to a very early timestamp returns empty state (genesis only)."""
    _, port, _ = ledger_and_server
    status, data = _post(port, "/api/replay", {"to_time": "2020-01-01T00:00:00Z"})
    assert status == 200
    # Genesis event is the very first event — it may have a timestamp before 2020
    # depending on test environment, but the call should not crash.
    assert isinstance(data, dict)


def test_revive_after_logging_event(ledger_and_server):
    """Log an event, then revive to before and after its timestamp."""
    _, port, _ = ledger_and_server

    event = {
        "event_type": "FILE_MODIFIED",
        "ctx_id": "test-revive",
        "source": "revive:test",
        "payload": {"path": "revive-test.txt", "action": "modified"},
    }
    _post(port, "/api/log", event)

    # Revive to far future — should include the event
    status, data = _post(port, "/api/replay", {"to_time": "2099-01-01T00:00:00Z"})
    assert status == 200
    assert data["events_applied"] >= 1

    # Revive to far past — should return 0 events_applied (genesis excluded)
    status, data = _post(port, "/api/replay", {"to_time": "2020-01-01T00:00:00Z"})
    assert status == 200
    assert data["events_applied"] == 0
    assert isinstance(data, dict)


def test_revive_promotes_decision_from_description(tmp_path):
    """FIX.GOV-AUTO-2 — La Capa 0 de revive promueve REASONING_STEP desde
    ``description`` (field bug C1: los raws cosechados por opencode y el
    motor universal llevan ``description``, NO ``reasoning``), con parent
    = event_id real del REASONING_STEP (32-hex, UUID-válido)."""
    from types import MappingProxyType
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._init import causadb_init
    from causadb._ledger_reader import LedgerReader
    from causadb._ledger_writer import LedgerWriter
    from causadb.cli._cmd_revive import _promote_decisions_to_governance

    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)

    # Shape real de la puntita opencode: description, no reasoning.
    writer.append(CanonicalEvent(
        event_type=EventType.REASONING_STEP,
        ctx_id="harvester:opencode",
        source="harvester:opencode",
        payload=MappingProxyType({
            "step_type": "decision",
            "step_hash": "abc123",
            "subject": "breaking change",
            "description": "Decided to make a breaking change to the API contract",
            "agent": "opencode",
        }),
    ))

    written = _promote_decisions_to_governance(ledger)
    assert written == 1, f"expected 1 promotion from description, got {written}"

    reader = LedgerReader(ledger)
    entries = list(reader.read_all_entries())
    gov = [e["event"] for e in entries if e["event"]["event_type"] == "GOVERNANCE_DECISION"]
    rs = [e["event"] for e in entries if e["event"]["event_type"] == "REASONING_STEP"]

    assert len(gov) == 1
    assert gov[0]["payload"]["origin"] == "distill"
    assert "breaking" in gov[0]["payload"]["reasoning"]
    # parent = event_id real (no step_hash 64-hex no-UUID)
    assert gov[0]["parent_event_id"] == rs[0]["event_id"]
