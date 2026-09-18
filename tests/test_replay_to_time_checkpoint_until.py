"""TDD RED — specs auditadas: read_until* fail-closed, to_time continue, checkpoint whitelist."""
import pytest
from causadb._ledger_reader import LedgerReader
from causadb._replay_engine import ReplayEngine
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_validator import ReplayIntegrityError


def _append(ledger_path, event_type, payload, timestamp=None):
    w = LedgerWriter(ledger_path)
    kw = dict(event_type=EventType(event_type), ctx_id="t", source="t", payload=payload)
    if timestamp:
        kw["timestamp"] = timestamp
    return w.append(CanonicalEvent(**kw))


def test_read_until_missing_raises(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    _append(ledger, "FILE_MODIFIED", {"path": "/a", "action": "create"})
    reader = LedgerReader(ledger)
    with pytest.raises(ReplayIntegrityError):
        list(reader.read_until("no-existe-xyz"))


def test_read_until_entries_missing_raises(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    _append(ledger, "FILE_MODIFIED", {"path": "/a", "action": "create"})
    reader = LedgerReader(ledger)
    with pytest.raises(ReplayIntegrityError):
        list(reader.read_until_entries("no-existe-xyz"))


def test_reconstruct_until_missing_propagates(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    _append(ledger, "FILE_MODIFIED", {"path": "/a", "action": "create"})
    with pytest.raises(ReplayIntegrityError):
        ReplayEngine(ledger).reconstruct_state(until_event_id="no-existe-xyz")


def test_to_time_includes_equal_skips_future_continue(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    _append(ledger, "FILE_MODIFIED", {"path": "/t1", "action": "create"},
            timestamp="2026-01-01T00:00:00Z")
    _append(ledger, "FILE_MODIFIED", {"path": "/futuro", "action": "create"},
            timestamp="2026-12-31T00:00:00Z")
    _append(ledger, "FILE_MODIFIED", {"path": "/t2", "action": "create"},
            timestamp="2026-06-01T00:00:00Z")
    state = ReplayEngine(ledger).reconstruct_state(to_time="2026-06-01T00:00:00Z")
    paths = [f["path"] for f in state["files_modified"]]
    assert paths == ["/t1", "/t2"], f"to_time debe incluir t2 y saltear futuro con continue, got {paths}"
    assert state["events_applied"] == 2


def test_checkpoint_extra_key_goes_to_bag(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    _append(ledger, "FILE_MODIFIED", {"path": "/a", "action": "create"})
    _append(ledger, "CHECKPOINT_CREATED",
            {"checkpoint_id": "c1",
             "snapshot": {"context": {"restored": True}, "clave_extra_xyz": 123}})
    state = ReplayEngine(ledger).reconstruct_state()
    # whitelist a raíz
    assert state["context"].get("restored") is True
    # extra NO pisa raíz y queda en bolsa
    assert "clave_extra_xyz" not in state
    assert state["checkpoint_data"].get("clave_extra_xyz") == 123
