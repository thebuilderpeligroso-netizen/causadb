import os
import gzip
import json
from typing import Generator, Dict, Any, Optional
from causadb._blob_store import BlobStore, resolve_payload
from causadb._event_schema import CanonicalEvent

class LedgerReader:
    def __init__(self, ledger_path: str):
        if not ledger_path:
            raise ValueError("ledger_path is required")
        self.ledger_path = ledger_path
        self.archive_dir = os.path.join(os.path.dirname(ledger_path), "archive")
        self._blob_store_path = os.path.join(os.path.dirname(ledger_path), "blobs")
        self._blob_store: Optional[BlobStore] = None

    def _get_blob_store(self) -> BlobStore:
        if self._blob_store is None:
            self._blob_store = BlobStore(self._blob_store_path)
        return self._blob_store

    def read_all(self) -> Generator[CanonicalEvent, None, None]:
        for entry in self.read_all_entries():
            yield CanonicalEvent.from_dict(entry["event"])

    def read_all_entries(self, resolve_blobs: bool = True, tolerant: bool = False) -> Generator[Dict[str, Any], None, None]:
        store = self._get_blob_store() if resolve_blobs else None
        if os.path.exists(self.archive_dir):
            archives = sorted([f for f in os.listdir(self.archive_dir) if f.endswith(".gz")])
            for archive in archives:
                with gzip.open(os.path.join(self.archive_dir, archive), "rt") as f:
                    for line in f:
                        entry = self._parse_line(line, tolerant)
                        if entry is None:
                            continue
                        if resolve_blobs:
                            entry["event"]["payload"] = resolve_payload(
                                entry["event"].get("payload", {}), store
                            )
                        yield entry
        
        if os.path.exists(self.ledger_path):
            with open(self.ledger_path, "r") as f:
                for line in f:
                    entry = self._parse_line(line, tolerant)
                    if entry is None:
                        continue
                    if resolve_blobs:
                        entry["event"]["payload"] = resolve_payload(
                            entry["event"].get("payload", {}), store
                        )
                    yield entry

    @staticmethod
    def _parse_line(line: str, tolerant: bool) -> Optional[Dict[str, Any]]:
        """Parsea una línea JSON del ledger.

        Con ``tolerant=True`` una línea corrupta/truncada (crash a mitad
        de escritura) se saltea en vez de propagar la excepción — el
        writer la reescribe (semántica de crash-recovery usada por el
        rebuild del DAG cache, BIT-CHR.117). Con ``tolerant=False``
        (default) propaga el error como antes (fail-fast).
        """
        try:
            return json.loads(line.strip())
        except (json.JSONDecodeError, ValueError):
            if tolerant:
                return None
            raise

    def read_until(self, event_id: str, resolve_blobs: bool = True) -> Generator[CanonicalEvent, None, None]:
        store = self._get_blob_store() if resolve_blobs else None
        found = False
        if os.path.exists(self.archive_dir):
            archives = sorted([f for f in os.listdir(self.archive_dir) if f.endswith(".gz")])
            for archive in archives:
                with gzip.open(os.path.join(self.archive_dir, archive), "rt") as f:
                    for line in f:
                        entry = json.loads(line.strip())
                        if resolve_blobs:
                            entry["event"]["payload"] = resolve_payload(
                                entry["event"].get("payload", {}), store
                            )
                        event = CanonicalEvent.from_dict(entry["event"])
                        yield event
                        if event.event_id == event_id:
                            found = True
                            break
                if found:
                    return
        
        if os.path.exists(self.ledger_path) and not found:
            with open(self.ledger_path, "r") as f:
                for line in f:
                    entry = json.loads(line.strip())
                    if resolve_blobs:
                        entry["event"]["payload"] = resolve_payload(
                            entry["event"].get("payload", {}), store
                        )
                    event = CanonicalEvent.from_dict(entry["event"])
                    yield event
                    if event.event_id == event_id:
                        break

    def read_until_entries(self, event_id: str, resolve_blobs: bool = True) -> Generator[Dict[str, Any], None, None]:
        """Entradas completas (event + hash + prev_hash) hasta event_id inclusive.

        GAP-02: la frontera de ``reconstruct`` necesita el orden de APPEND
        (hash chain) y las entradas completas para hacer el replay parcial
        — ``read_until`` devuelve solo CanonicalEvent (sin hash). Respeta el
        mismo orden de lectura que ``read_all_entries`` (archive/ → ledger).
        """
        store = self._get_blob_store() if resolve_blobs else None
        found = False
        if os.path.exists(self.archive_dir):
            archives = sorted([f for f in os.listdir(self.archive_dir) if f.endswith(".gz")])
            for archive in archives:
                with gzip.open(os.path.join(self.archive_dir, archive), "rt") as f:
                    for line in f:
                        entry = json.loads(line.strip())
                        if resolve_blobs:
                            entry["event"]["payload"] = resolve_payload(
                                entry["event"].get("payload", {}), store
                            )
                        yield entry
                        if entry["event"].get("event_id") == event_id:
                            found = True
                            break
                if found:
                    return

        if os.path.exists(self.ledger_path) and not found:
            with open(self.ledger_path, "r") as f:
                for line in f:
                    entry = json.loads(line.strip())
                    if resolve_blobs:
                        entry["event"]["payload"] = resolve_payload(
                            entry["event"].get("payload", {}), store
                        )
                    yield entry
                    if entry["event"].get("event_id") == event_id:
                        break
