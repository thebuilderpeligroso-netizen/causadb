"""Tests NUEVOS (RED primero) — lector/validador con .lock compartido.

Specs auditadas:
- lector/validador abren el MISMO `.lock` del writer con `lock_sh`
  (bloqueante con timeout documentado; degradación suave estilo
  `_dag_cache.py:335-342`; nota NFS best-effort + tolerant como respaldo).
- `tolerant=True`: yield + contador/log de saltadas; `validate_chain`
  SIGUE fallando en el hueco (tolerant = diagnóstico, jamás replay válido).
- Validador cubre `sequence_number` + schema mínimo (event_id/event_type)
  SIN expandir a schema completo.
- NO tocar `_get_last_hash`.
"""
import hashlib
import json
import os
import threading
import time
from unittest import mock

import pytest

from causadb._ledger_reader import LedgerReader
from causadb._ledger_validator import LedgerValidator


@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")


def _chain_entry(event, prev_hash):
    event_json = json.dumps(event, sort_keys=True)
    h = hashlib.sha256((event_json + prev_hash).encode()).hexdigest()
    return {"event": event, "prev_hash": prev_hash, "hash": h}, h


def _write_chain(path, events):
    prev = "GENESIS"
    with open(path, "w") as f:
        for ev in events:
            entry, prev = _chain_entry(ev, prev)
            f.write(json.dumps(entry, sort_keys=True) + "\n")
    return prev


# 1. MISMO .lock con lock_sh — lector
def test_reader_uses_same_lock_with_shared_lock(ledger_path):
    """El lector abre ledger.lock y usa lock_sh (no otro path, no lock_ex)."""
    _write_chain(ledger_path, [
        {"event_id": "e0", "event_type": "FILE_MODIFIED", "sequence_number": 0},
    ])
    with mock.patch("causadb._ledger_reader.lock_sh") as m_sh, \
         mock.patch("causadb._ledger_reader.unlock") as m_un:
        reader = LedgerReader(ledger_path)
        list(reader.read_all_entries(resolve_blobs=False))
    assert m_sh.called, "read_all_entries debe adquirir lock_sh"
    assert m_un.called, "debe liberar con unlock"
    # Verifica que el fd bloqueado pertenece al MISMO .lock del writer.
    import causadb._ledger_reader as lr_mod
    assert hasattr(reader, "_lock_path") or "_lock_path" in dir(reader) or True
    # El path del lock debe ser ledger_path + ".lock" (convención del writer).
    expected = ledger_path + ".lock"
    # Si el lector expone _lock_path, debe coincidir; si no, el test exige exponerlo.
    assert getattr(reader, "_lock_path", None) == expected, \
        f"lector debe exponer _lock_path={expected!r}"


# 1b. MISMO .lock con lock_sh — validador
def test_validator_uses_same_lock_with_shared_lock(ledger_path):
    _write_chain(ledger_path, [
        {"event_id": "e0", "event_type": "FILE_MODIFIED", "sequence_number": 0},
    ])
    with mock.patch("causadb._ledger_validator.lock_sh") as m_sh, \
         mock.patch("causadb._ledger_validator.unlock") as m_un:
        v = LedgerValidator(ledger_path)
        v.validate_chain()
    assert m_sh.called, "validate_chain debe adquirir lock_sh"
    assert m_un.called, "debe liberar con unlock"
    assert getattr(v, "_lock_path", None) == ledger_path + ".lock"


# 2. Lectura concurrente con escritura sin CORRUPTION espuria
def test_concurrent_read_during_write_no_spurious_corruption(tmp_path):
    """N escrituras via LedgerWriter + lecturas concurrentes: sin CORRUPTION espuria."""
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType

    ledger = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger)
    N = 20
    errors = []
    stop = threading.Event()

    def do_reads():
        reader = LedgerReader(ledger)
        while not stop.is_set():
            try:
                list(reader.read_all_entries(resolve_blobs=False, tolerant=False))
            except Exception as e:  # noqa: BLE001 — cualquier error es fallo
                errors.append(e)

    t = threading.Thread(target=do_reads)
    t.start()
    try:
        for i in range(N):
            ev = CanonicalEvent(
                event_type=EventType.FILE_MODIFIED, ctx_id="ctx",
                source="agent", payload={"i": i},
            )
            writer.append(ev)
    finally:
        stop.set()
        t.join(timeout=10)

    assert not errors, f"lecturas concurrentes fallaron: {errors[:3]}"
    reader = LedgerReader(ledger)
    entries = list(reader.read_all_entries(resolve_blobs=False))
    assert len(entries) == N
    assert LedgerValidator(ledger).validate_chain().is_valid


# 2b. El lector bloquea bajo lock_ex (prueba determinista del lock compartido)
def test_reader_blocks_while_writer_holds_exclusive_lock(ledger_path):
    """Si el writer tiene lock_ex, el lector con lock_sh debe esperar (no leer a medias)."""
    from causadb._file_lock import lock_ex, unlock
    _write_chain(ledger_path, [
        {"event_id": "e0", "event_type": "FILE_MODIFIED", "sequence_number": 0},
    ])
    lock_path = ledger_path + ".lock"
    open(lock_path, "a+b").close()
    with open(lock_path, "a+b") as lf:
        lock_ex(lf.fileno())
        try:
            reader = LedgerReader(ledger_path)
            done = []
            def _read():
                done.append(list(reader.read_all_entries(resolve_blobs=False)))
            t = threading.Thread(target=_read)
            t.start()
            time.sleep(0.3)
            # Con lock compartido correcto, el lector sigue bloqueado.
            assert not done, "lector debe bloquearse mientras lock_ex está tomado"
        finally:
            unlock(lf.fileno())
        t.join(timeout=10)
        assert done, "lector debe completar tras liberar lock_ex"


# 3. tolerant + contador/log, y validate SIGUE fallando en el hueco
def test_tolerant_skips_corrupt_with_counter(ledger_path):
    prev = _write_chain(ledger_path, [
        {"event_id": "e0", "event_type": "FILE_MODIFIED", "sequence_number": 0},
    ])
    with open(ledger_path, "a") as f:
        f.write("ESTA LINEA ESTA ROTA{{{\n")
        entry, prev = _chain_entry(
            {"event_id": "e1", "event_type": "FILE_MODIFIED", "sequence_number": 1}, prev)
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    reader = LedgerReader(ledger_path)
    entries = list(reader.read_all_entries(resolve_blobs=False, tolerant=True))
    assert len(entries) == 2, "tolerant debe saltear la línea rota y dar las 2 válidas"
    assert getattr(reader, "skipped_count", 0) >= 1, "tolerant debe exponer contador de saltadas"


def test_tolerant_validate_still_fails_on_gap(ledger_path):
    """tolerant = diagnóstico: validate_chain(tolerant=True) SIGUE inválido en el hueco."""
    prev = _write_chain(ledger_path, [
        {"event_id": "e0", "event_type": "FILE_MODIFIED", "sequence_number": 0},
    ])
    with open(ledger_path, "a") as f:
        f.write("LINEA ROTA\n")
        entry, _ = _chain_entry(
            {"event_id": "e1", "event_type": "FILE_MODIFIED", "sequence_number": 1}, prev)
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    v = LedgerValidator(ledger_path)
    result = v.validate_chain(tolerant=True)
    assert not result.is_valid, "validate con hueco jamás es replay válido, ni con tolerant=True"


# 4. seq duplicado → validate falla
def test_duplicate_sequence_number_fails_validation(ledger_path):
    prev = "GENESIS"
    with open(ledger_path, "w") as f:
        for seq in (0, 0):  # duplicado
            ev = {"event_id": f"e{seq}-{f.tell()}", "event_type": "FILE_MODIFIED",
                  "sequence_number": seq}
            entry, prev = _chain_entry(ev, prev)
            f.write(json.dumps(entry, sort_keys=True) + "\n")
    result = LedgerValidator(ledger_path).validate_chain()
    assert not result.is_valid, "sequence_number duplicado debe invalidar la cadena"


# 5. evento sin event_id → validate falla
def test_missing_event_id_fails_validation(ledger_path):
    prev = "GENESIS"
    with open(ledger_path, "w") as f:
        ev = {"event_type": "FILE_MODIFIED", "sequence_number": 0}  # sin event_id
        entry, _ = _chain_entry(ev, prev)
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    result = LedgerValidator(ledger_path).validate_chain()
    assert not result.is_valid, "evento sin event_id debe invalidar la cadena"


# 5b. evento sin event_type → validate falla (schema mínimo)
def test_missing_event_type_fails_validation(ledger_path):
    prev = "GENESIS"
    with open(ledger_path, "w") as f:
        ev = {"event_id": "e0", "sequence_number": 0}  # sin event_type
        entry, _ = _chain_entry(ev, prev)
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    result = LedgerValidator(ledger_path).validate_chain()
    assert not result.is_valid, "evento sin event_type debe invalidar la cadena"
