import os
import json
import uuid
from datetime import datetime
from typing import Optional
from causadb._config import CausaDBConfig
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType

def causadb_init(workspace_path: str, config: Optional[CausaDBConfig] = None):
    if not os.path.isabs(workspace_path):
        raise ValueError("workspace_path must be absolute")
    
    if os.path.exists(workspace_path):
        raise FileExistsError(f"Workspace already exists: {workspace_path}")
        
    os.makedirs(workspace_path)
    ledger_path = os.path.join(workspace_path, "ledger.log")
    
    # Crear chronicle
    with open(os.path.join(workspace_path, "CAUSADB_CHRONICLE.md"), "w") as f:
        f.write("# CAUSADB_CHRONICLE.md\n")
        
    # Inicializar Ledger
    writer = LedgerWriter(ledger_path, config)
    genesis = CanonicalEvent(
        event_type=EventType.SYSTEM_BOOT,
        ctx_id="genesis",
        source="causadb:init",
        source_type="human",
        payload={"action": "init"},
        metadata=EventMetadata(trace_id="init", session_id="init")
    )
    writer.append(genesis)
    
    return {
        "ledger_path": ledger_path,
        "chronicle_path": os.path.join(workspace_path, "CAUSADB_CHRONICLE.md"),
        "genesis_event_id": genesis.event_id
    }
