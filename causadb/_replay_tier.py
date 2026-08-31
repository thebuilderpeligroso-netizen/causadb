from enum import Enum
import os
import gzip
import json
from typing import Optional
from causadb._config import CausaDBConfig
from causadb._ledger_reader import LedgerReader

class ReplayTier(Enum):
    HOT = "HOT"
    WARM = "WARM"
    ARCHIVAL = "ARCHIVAL"

class ReplayStratification:
    def __init__(self, config: CausaDBConfig):
        self.config = config

    def get_tier(self, event_id: str, ledger_path: str) -> ReplayTier:
        reader = LedgerReader(ledger_path)
        
        # 1. Archival check
        if os.path.exists(reader.archive_dir):
            for archive in sorted(os.listdir(reader.archive_dir)):
                if archive.endswith(".gz"):
                    with gzip.open(os.path.join(reader.archive_dir, archive), "rt") as f:
                        for line in f:
                            entry = json.loads(line.strip())
                            if entry["event"]["event_id"] == event_id:
                                return ReplayTier.ARCHIVAL
        
        # 2. Hot/Warm check (últimos N)
        entries = list(reader.read_all_entries())
        hot_entries = entries[-self.config.hot_tier_size:]
        for entry in hot_entries:
            if entry["event"]["event_id"] == event_id:
                return ReplayTier.HOT
        
        # 3. Warm check
        for entry in entries:
            if entry["event"]["event_id"] == event_id:
                return ReplayTier.WARM
        
        # 4. Fall-Closed: si no existe
        raise ValueError(
            f"Event {event_id} not found in any tier (active ledger or archives)"
        )
