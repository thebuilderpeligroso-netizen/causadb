import pytest
from causadb._replay_engine import ReplayEngine
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType

@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")

def test_replay_deterministic(ledger_path):
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"))
    engine = ReplayEngine(ledger_path)
    state1 = engine.reconstruct_state()
    state2 = engine.reconstruct_state()
    assert state1 == state2

def test_replay_after_append(ledger_path):
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"))
    engine = ReplayEngine(ledger_path)
    state1 = engine.reconstruct_state()
    
    writer.append(CanonicalEvent(event_type=EventType.COMMAND_RUN, ctx_id="ctx", source="opencode:agent", payload={"command": "ls"}))
    state2 = engine.reconstruct_state()
    assert state2["events_applied"] == 2
    assert len(state2["commands_run"]) == 1

def test_replay_100_events_deterministic(ledger_path):
    writer = LedgerWriter(ledger_path)
    for i in range(100):
        writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent", payload={"path": str(i)}))
    
    engine = ReplayEngine(ledger_path)
    state1 = engine.reconstruct_state()
    state2 = engine.reconstruct_state()
    assert state1 == state2
