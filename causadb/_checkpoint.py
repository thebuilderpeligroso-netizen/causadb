from dataclasses import dataclass
from typing import Optional, Dict, Any
from causadb._integrity_hasher import IntegrityHasher
from causadb._ledger_writer import LedgerWriter
from causadb._config import CausaDBConfig
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType

@dataclass
class Checkpoint:
    state: Dict[str, Any]
    last_hash: str
    timestamp: str
    integrity_hash: str

class CheckpointManager:
    def __init__(self, ledger_path: str, config: Optional[CausaDBConfig] = None):
        self.ledger_path = ledger_path
        self.config = config or CausaDBConfig(ledger_path=ledger_path)
        self.writer = LedgerWriter(ledger_path, self.config)

    def save_checkpoint(self, state: Dict[str, Any]) -> Checkpoint:
        checkpoint = Checkpoint(
            state=state,
            last_hash=state["last_hash"],
            timestamp=state["timestamp"],
            integrity_hash=IntegrityHasher.calculate_hash(state)
        )
        
        # Guardar evento de checkpoint
        event = CanonicalEvent(
            event_type=EventType.CHECKPOINT_CREATED,
            ctx_id=state.get("context", {}).get("ctx_id", "checkpoint"),
            source="causadb:checkpoint",
            payload={"checkpoint": checkpoint.__dict__},
            metadata=EventMetadata(trace_id="checkpoint", session_id="checkpoint")
        )
        self.writer.append(event)
        return checkpoint
