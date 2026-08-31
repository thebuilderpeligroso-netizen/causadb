import os
import gzip
import json
import shutil
import time
from typing import Optional
from causadb._config import CausaDBConfig
from causadb._ledger_validator import LedgerValidator

class ArchiveManager:
    def __init__(self, ledger_path: str, archive_dir: str, config: Optional[CausaDBConfig] = None):
        self.ledger_path = ledger_path
        self.archive_dir = archive_dir
        self.config = config or CausaDBConfig(ledger_path=ledger_path)
        self.last_hash_path = ledger_path + ".last_hash.json"

    def archive_current_epoch(self):
        if not os.path.exists(self.ledger_path) or os.path.getsize(self.ledger_path) == 0:
            return

        # 1. Validar integridad antes de archivar
        validator = LedgerValidator(self.ledger_path)
        validator.validate_or_raise()

        # 2. Copiar a .gz
        epoch_path = os.path.join(self.archive_dir, f"epoch_{int(time.time())}.log.gz")
        with open(self.ledger_path, "rb") as f_in:
            with gzip.open(epoch_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        
        # Validar integridad del archivo gz creado
        with gzip.open(epoch_path, "rt") as f:
            lines = f.readlines()
            last_entry = json.loads(lines[-1])
            last_hash = last_entry["hash"]
            
        # 3. Guardar hash para el próximo genesis
        with open(self.last_hash_path, "w") as f:
            json.dump({"last_hash": last_hash}, f)
            f.flush()
            os.fsync(f.fileno())

        # 4. Limpiar ledger activo
        with open(self.ledger_path, "w") as f:
            f.flush()
            os.fsync(f.fileno())
