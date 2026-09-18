import contextlib
import logging
import os
import gzip
import json
from typing import Generator, Dict, Any, Optional
from causadb._blob_store import BlobStore, resolve_payload
from causadb._event_schema import CanonicalEvent
from causadb._ledger_validator import ReplayIntegrityError
from causadb._file_lock import lock_sh, unlock

logger = logging.getLogger(__name__)

class LedgerReader:
    def __init__(self, ledger_path: str):
        if not ledger_path:
            raise ValueError("ledger_path is required")
        self.ledger_path = ledger_path
        self.archive_dir = os.path.join(os.path.dirname(ledger_path), "archive")
        self._blob_store_path = os.path.join(os.path.dirname(ledger_path), "blobs")
        self._blob_store: Optional[BlobStore] = None
        # MISMO .lock que el writer (LedgerWriter usa ledger_path + ".lock"
        # con lock_ex). El lector usa lock_sh para no leer a medias.
        self._lock_path = ledger_path + ".lock"
        # Contador de líneas saltadas con tolerant=True (diagnóstico).
        self.skipped_count = 0

    def _get_blob_store(self) -> BlobStore:
        if self._blob_store is None:
            self._blob_store = BlobStore(self._blob_store_path)
        return self._blob_store

    def read_all(self) -> Generator[CanonicalEvent, None, None]:
        for entry in self.read_all_entries():
            yield CanonicalEvent.from_dict(entry["event"])

    def _acquire_shared(self):
        """Adquiere lock_sh sobre el MISMO `.lock` del writer (best-effort).

        Retorna el file object bloqueado, o None si el lock no se pudo
        adquirir (permisos, etc. — degradación suave estilo
        `_dag_cache.py:335-342`). Solo cubre la ADQUISICIÓN: los errores
        del cuerpo de lectura (p. ej. blob faltante) NO se tragan aquí.
        El llamador debe soltar con `_release_shared` en un finally.
        """
        try:
            if not os.path.exists(self._lock_path):
                try:
                    open(self._lock_path, "a+b").close()
                except OSError:
                    return None
            lock_file = open(self._lock_path, "a+b")
        except (OSError, IOError):
            return None
        try:
            lock_sh(lock_file.fileno())
        except (OSError, IOError):
            try:
                lock_file.close()
            except OSError:
                pass
            return None
        return lock_file

    def _release_shared(self, lock_file) -> None:
        """Suelta lock_sh + cierra el fd (no-op si es None)."""
        if lock_file is None:
            return
        try:
            unlock(lock_file.fileno())
        finally:
            lock_file.close()

    @contextlib.contextmanager
    def _shared_locked(self):
        """MISMO `.lock` del writer, en modo compartido (`lock_sh`).

        Bloqueante sin timeout (flock): el lector espera a que el writer
        suelte `lock_ex` y así jamás lee una línea a medias. Degradación
        suave estilo `_dag_cache.py:335-342`: si el lock falla (permisos,
        etc.) se sigue sin lock (best-effort).
        NOTA NFS: flock en NFS es best-effort; el respaldo es
        `tolerant=True` (saltear la línea rota + contador/log).

        NOTA throw-safety: el `except` cubre SOLO la adquisición. El
        `yield` vive fuera del try/except para que un `throw()` del
        consumidor (p. ej. fail-fast ante blob faltante dentro de un
        generador lector) se propague sin `RuntimeError: generator
        didn't stop after throw()` ni deglución de FileNotFoundError
        (subclase de OSError).
        Los generadores lectores (`read_all_entries`, `read_until`,
        `read_until_entries`) NO usan este contextmanager: adquieren
        con `_acquire_shared` / sueltan con `_release_shared` en
        try/finally manual para que el throw no pase por aquí.
        """
        lock_file = self._acquire_shared()
        if lock_file is None:
            yield None
            return
        try:
            yield lock_file
        finally:
            self._release_shared(lock_file)

    def _note_skipped(self, line: str) -> None:
        self.skipped_count += 1
        logger.warning(
            "ledger línea saltada (tolerant=True) #%d: %r",
            self.skipped_count, line.strip()[:120],
        )

    def read_all_entries(self, resolve_blobs: bool = True, tolerant: bool = False) -> Generator[Dict[str, Any], None, None]:
        store = self._get_blob_store() if resolve_blobs else None
        if os.path.exists(self.archive_dir):
            archives = sorted([f for f in os.listdir(self.archive_dir) if f.endswith(".gz")])
            for archive in archives:
                with gzip.open(os.path.join(self.archive_dir, archive), "rt") as f:
                    for line in f:
                        entry = self._parse_line(line, tolerant)
                        if entry is None:
                            self._note_skipped(line)
                            continue
                        if resolve_blobs:
                            entry["event"]["payload"] = resolve_payload(
                                entry["event"].get("payload", {}), store
                            )
                        yield entry

        if os.path.exists(self.ledger_path):
            # try/finally manual (sin @contextmanager): el throw() del
            # consumidor ante blob faltante no pasa por contextmanager y
            # el FileNotFoundError se propaga (fail-fast).
            lock_file = self._acquire_shared()
            try:
                with open(self.ledger_path, "r") as f:
                    for line in f:
                        entry = self._parse_line(line, tolerant)
                        if entry is None:
                            self._note_skipped(line)
                            continue
                        if resolve_blobs:
                            entry["event"]["payload"] = resolve_payload(
                                entry["event"].get("payload", {}), store
                            )
                        yield entry
            finally:
                self._release_shared(lock_file)

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
            lock_file = self._acquire_shared()
            try:
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
                            found = True
                            break
            finally:
                self._release_shared(lock_file)
        if not found:
            raise ReplayIntegrityError(f"event_id {event_id!r} no encontrado en archives+ledger")

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
            lock_file = self._acquire_shared()
            try:
                with open(self.ledger_path, "r") as f:
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
            finally:
                self._release_shared(lock_file)
        if not found:
            raise ReplayIntegrityError(f"event_id {event_id!r} no encontrado en archives+ledger")
