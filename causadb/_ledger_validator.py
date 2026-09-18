import contextlib
import hashlib
import json
import logging
import os
import gzip
from typing import Optional
from causadb._validation_result import ValidationResult
from causadb._file_lock import lock_sh, unlock

logger = logging.getLogger(__name__)

class ReplayIntegrityError(Exception):
    pass

class LedgerValidator:
    def __init__(self, ledger_path: str):
        if not ledger_path:
            raise ValueError("ledger_path is required")
        self.ledger_path = ledger_path
        self.archive_dir = os.path.join(os.path.dirname(ledger_path), "archive")
        # MISMO .lock que el writer (ledger_path + ".lock"), en lock_sh.
        self._lock_path = ledger_path + ".lock"
        # Contador diagnóstico de huecos vistos con tolerant=True.
        self.skipped_count = 0

    @contextlib.contextmanager
    def _shared_locked(self):
        """MISMO `.lock` del writer, en modo compartido (`lock_sh`).

        Bloqueante sin timeout (flock): el validador espera a que el
        writer suelte `lock_ex` y así no reporta CORRUPTION espuria por
        leer una línea a medias. Degradación suave estilo
        `_dag_cache.py:335-342`: ante OSError se sigue sin lock.
        NOTA NFS: flock en NFS es best-effort; el respaldo es
        `tolerant=True` (diagnóstico: cuenta el hueco pero SIGUE
        fallando — jamás replay válido).
        """
        try:
            if not os.path.exists(self._lock_path):
                try:
                    open(self._lock_path, "a+b").close()
                except OSError:
                    yield None
                    return
            with open(self._lock_path, "a+b") as lock_file:
                lock_sh(lock_file.fileno())
                try:
                    yield lock_file
                finally:
                    unlock(lock_file.fileno())
        except (OSError, IOError):
            yield None

    def _compute_hash(self, event_data: str, prev_hash: str) -> str:
        return hashlib.sha256((event_data + prev_hash).encode()).hexdigest()

    def _validate_from_lines(self, lines, start_index: int, expected_prev: str,
                             expected_seq: Optional[int] = None,
                             tolerant: bool = False):
        entry_index = start_index - 1
        last_hash = expected_prev
        for line in lines:
            entry_index += 1
            try:
                entry = json.loads(line.strip())
            except json.JSONDecodeError:
                if tolerant:
                    self.skipped_count += 1
                    logger.warning(
                        "validate hueco en pos %d (tolerant=True, diagnóstico): %r",
                        entry_index, line.strip()[:120],
                    )
                return ValidationResult(is_valid=False, failure_type="CORRUPTION",
                                        position=entry_index, description="Invalid JSON format")

            if entry.get("prev_hash") != last_hash:
                return ValidationResult(is_valid=False, failure_type="CONTINUITY_BREAK",
                                        position=entry_index,
                                        description=f"Expected {last_hash}")

            event_json = json.dumps(entry.get("event", {}), sort_keys=True)
            computed = self._compute_hash(event_json, entry.get("prev_hash", ""))
            if entry.get("hash") != computed:
                return ValidationResult(is_valid=False, failure_type="HASH_MISMATCH",
                                        position=entry_index)
            # Schema mínimo + sequence_number, SOLO para eventos formato
            # nuevo (con sequence_number). Legacy sin seq se tolera
            # (compat con benchmarks históricos y tests sedimentados).
            # SIN expandir a schema completo: solo event_id/event_type.
            ev = entry.get("event", {})
            if isinstance(ev, dict) and "sequence_number" in ev:
                eid = ev.get("event_id")
                etype = ev.get("event_type")
                if not isinstance(eid, str) or not eid:
                    return ValidationResult(
                        is_valid=False, failure_type="SCHEMA_VIOLATION",
                        position=entry_index,
                        description="event sin event_id")
                if not isinstance(etype, str) or not etype:
                    return ValidationResult(
                        is_valid=False, failure_type="SCHEMA_VIOLATION",
                        position=entry_index,
                        description="event sin event_type")
                seq = ev.get("sequence_number")
                if not isinstance(seq, int) or isinstance(seq, bool):
                    return ValidationResult(
                        is_valid=False, failure_type="SEQUENCE_BREAK",
                        position=entry_index,
                        description=f"sequence_number inválido: {seq!r}")
                if expected_seq is not None and seq != expected_seq:
                    return ValidationResult(
                        is_valid=False, failure_type="SEQUENCE_BREAK",
                        position=entry_index,
                        description=f"Expected seq {expected_seq}, got {seq}")
                expected_seq = seq + 1
            last_hash = entry.get("hash")
        result = ValidationResult(is_valid=True, _last_hash=last_hash)
        # Propagar el próximo seq esperado sin romper la firma pública.
        result._next_seq = expected_seq  # type: ignore[attr-defined]
        return result

    def validate_chain(self, tolerant: bool = False) -> ValidationResult:
        """Valida hash-chain + `sequence_number` + schema mínimo.

        `tolerant=True` es SOLO diagnóstico: cuenta/loguea el hueco
        (`skipped_count`) pero SIGUE retornando inválido — jamás un
        replay válido sobre un hueco.
        """
        expected_prev = "GENESIS"
        expected_seq: Optional[int] = None
        offset = 1

        if os.path.exists(self.archive_dir):
            archives = sorted([f for f in os.listdir(self.archive_dir) if f.endswith(".gz")])
            for archive in archives:
                with gzip.open(os.path.join(self.archive_dir, archive), "rt") as f:
                    lines = f.readlines()
                    result = self._validate_from_lines(
                        lines, offset, expected_prev, expected_seq, tolerant)
                    if not result.is_valid:
                        return result
                    expected_prev = result._last_hash
                    expected_seq = getattr(result, "_next_seq", expected_seq)
                    offset += len(lines)

        if os.path.exists(self.ledger_path) and os.path.getsize(self.ledger_path) > 0:
            with self._shared_locked():
                with open(self.ledger_path, "r") as f:
                    lines = f.readlines()
                    return self._validate_from_lines(
                        lines, offset, expected_prev, expected_seq, tolerant)

        return ValidationResult(is_valid=True, _last_hash=expected_prev)

    def validate_or_raise(self):
        result = self.validate_chain()
        if not result.is_valid:
            raise ReplayIntegrityError(f"Ledger corruption: {result.failure_type} at position {result.position}"
                                       + (f" - {result.description}" if result.description else ""))
        return result
