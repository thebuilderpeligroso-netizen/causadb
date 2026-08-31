import pytest
import json
import inspect
from causadb._checkpoint import Checkpoint, CheckpointManager
from causadb._integrity_hasher import IntegrityHasher
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter

def test_save_checkpoint_creates_event(tmp_path, mocker):
    """Articulo IX + bloqueante #4: save_checkpoint debe pasar por
    LedgerWriter (respeta RCB / hash-chain). No open() directo."""
    ledger = str(tmp_path / "ledger.log")
    state = {"events_applied": 1, "last_hash": "h1", "timestamp": "t1"}
    
    spy = mocker.spy(LedgerWriter, "append")
    
    manager = CheckpointManager(ledger)
    manager.save_checkpoint(state)
    
    assert spy.call_count == 1, (
        "save_checkpoint debe llamar LedgerWriter.append() (no open() directo)"
    )
    
    with open(ledger, "r") as f:
        lines = f.readlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"]["event_type"] == "CHECKPOINT_CREATED"
    # Validar hash-chain (campos del Canonical entry, no solo event_type)
    assert "prev_hash" in entry
    assert "hash" in entry

def test_checkpoint_includes_replay_state(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    state = {"events_applied": 1, "last_hash": "h1", "timestamp": "t1"}
    manager = CheckpointManager(ledger)
    checkpoint = manager.save_checkpoint(state)
    
    assert checkpoint.state == state
    assert checkpoint.last_hash == "h1"
    assert checkpoint.integrity_hash == IntegrityHasher.calculate_hash(state)

def test_checkpoint_no_v2_imports():
    import causadb._checkpoint as cp
    src = inspect.getsource(cp)
    assert "ledger_v2" not in src
    assert "EXECUTION_MODE" not in src

def test_checkpoint_no_emit_failure_writes_direct():
    import causadb._checkpoint as cp
    src = inspect.getsource(cp)
    assert "open(" not in src  # Simplificado: busca uso de open directo
