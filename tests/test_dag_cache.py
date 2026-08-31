"""F.13.1.2 + F.13.1.3 + F.13.1.4 — Tests del DAG Writer/Reader/Incremental.

Test-first (Artículo III): estos tests se escriben ANTES de la
implementación de ``causadb._dag_cache``. Deben fallar con
``ImportError`` hasta que el módulo exista, luego pasar.

Anti-teatro (Artículo IX):
- Test 12 (F.13.1.2) muta ``build_dag`` para no llenar ``event_writes``
  → el test #4 (event_writes matches payload) debe fallar.
- Test 13 (F.13.1.2) muta ``write_dag`` para no usar ``fcntl.flock``
  → el test #8 (flock es llamado) debe fallar.
- Test 12 (F.13.1.3) muta ``read_dag`` para no validar hash → el test
  #4 (hash mismatch → None) debe fallar (devolvería el DAG corrupto).
- Test 8 (F.13.1.4) muta ``update_dag`` para saltarse un evento → el
  test #7 (base 500 + new 500 == build 1000) debe fallar (999 vs 1000).
"""
import json
import os
from unittest import mock

import pytest

from causadb._ledger_writer import LedgerWriter
from causadb._config import CausaDBConfig
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_reader import LedgerReader
from causadb._dag_cache import (
    build_dag,
    write_dag,
    read_dag,
    compute_dag_hash,
    is_dag_stale,
    get_last_ledger_seq,
    update_dag,
)
from causadb._dag_schema import make_empty_dag, validate_dag
from causadb._causal_cone import _writer_history


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ledger(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = CausaDBConfig(ledger_path=ledger)
    writer = LedgerWriter(ledger, config=config)
    return ledger, writer


def _log_event(writer, writes=None, reads=None, parent_event_id=None):
    payload = {}
    if writes is not None:
        payload["writes"] = list(writes)
    if reads is not None:
        payload["reads"] = list(reads)
    event = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="causadb:test",
        payload=payload, parent_event_id=parent_event_id,
    )
    writer.append(event)
    return event.event_id


def _get_events(ledger_path):
    reader = LedgerReader(ledger_path)
    return [entry["event"] for entry in reader.read_all_entries()]


def _log_chain(writer, n_events, writes_per_event=None):
    """Log n_events chained via parent_event_id. Returns list of event_ids."""
    if writes_per_event is None:
        writes_per_event = [["main.py"] for _ in range(n_events)]
    ids = []
    parent = None
    for i in range(n_events):
        eid = _log_event(
            writer, writes=writes_per_event[i], parent_event_id=parent,
        )
        ids.append(eid)
        parent = eid
    return ids


# ---------------------------------------------------------------------------
# 1: build_dag from empty events
# ---------------------------------------------------------------------------

def test_build_dag_from_empty_events():
    """``build_dag([])`` retorna un DAG vacío válido (pasa validate_dag)."""
    dag = build_dag([])
    assert validate_dag(dag) is True, (
        "build_dag([]) debe retornar un DAG que pasa validate_dag"
    )
    assert dag["last_event_id"] == ""
    assert dag["last_seq"] == 0
    assert dag["writer_history"] == {}
    assert dag["event_writes"] == {}


# ---------------------------------------------------------------------------
# 2: build_dag from 10 events has correct last_event_id
# ---------------------------------------------------------------------------

def test_build_dag_from_10_events_has_correct_last_event_id(tmp_path):
    """10 eventos sintéticos → ``last_event_id`` == event_id del último."""
    ledger, writer = _make_ledger(tmp_path)
    ids = _log_chain(writer, 10)
    events = _get_events(ledger)

    dag = build_dag(events)

    assert dag["last_event_id"] == ids[-1], (
        f"last_event_id debe ser el último evento ({ids[-1]}), "
        f"got {dag['last_event_id']}"
    )


# ---------------------------------------------------------------------------
# 3: writer_history uses lists not tuples
# ---------------------------------------------------------------------------

def test_build_dag_writer_history_uses_lists_not_tuples(tmp_path):
    """``writer_history`` values son ``list[list[str, int]]`` no tuplas.

    JSON no tiene tuplas — si el DAG usara tuplas, el roundtrip JSON
    las convertiría a listas y el cache en disco diferiría del cache
    en memoria. El contrato (F.13.1.1) exige listas desde el writer.
    """
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 5, writes_per_event=[["main.py"]] * 5)
    events = _get_events(ledger)

    dag = build_dag(events)

    for file_path, entries in dag["writer_history"].items():
        for entry in entries:
            assert isinstance(entry, list), (
                f"writer_history entry debe ser list, got {type(entry).__name__}"
            )
            assert len(entry) == 2
            # Confirmar que NO es tupla: isinstance(entry, tuple) == False.
            assert not isinstance(entry, tuple), (
                "writer_history entry NO debe ser tuple — JSON no las preserva"
            )


# ---------------------------------------------------------------------------
# 4: event_writes matches payload.writes
# ---------------------------------------------------------------------------

def test_build_dag_event_writes_matches_payload_writes(tmp_path):
    """``event_writes[event_id]`` contiene los ``payload.writes`` del evento."""
    ledger, writer = _make_ledger(tmp_path)
    a_id = _log_event(writer, writes=["main.py", "util.py"])
    b_id = _log_event(writer, writes=["other.py"], parent_event_id=a_id)
    events = _get_events(ledger)

    dag = build_dag(events)

    assert set(dag["event_writes"][a_id]) == {"main.py", "util.py"}
    assert set(dag["event_writes"][b_id]) == {"other.py"}


# ---------------------------------------------------------------------------
# 5: build_dag fills event_ids / event_reads / event_meta (schema v3)
# ---------------------------------------------------------------------------

def test_build_dag_fills_event_ids(tmp_path):
    """``event_ids`` contiene los event_ids en orden de ledger."""
    ledger, writer = _make_ledger(tmp_path)
    a_id = _log_event(writer, writes=["main.py"])
    b_id = _log_event(writer, reads=["main.py"], parent_event_id=a_id)
    events = _get_events(ledger)

    dag = build_dag(events)

    assert dag["event_ids"] == [a_id, b_id], (
        f"event_ids debe listar los event_ids en orden de ledger, "
        f"got {dag['event_ids']}"
    )


def test_build_dag_fills_event_reads(tmp_path):
    """``event_reads[event_id]`` contiene los ``payload.reads`` del evento.

    Solo eventos con reads NO vacíos aparecen en ``event_reads`` (los
    únicos taintables en trace_downstream).
    """
    ledger, writer = _make_ledger(tmp_path)
    a_id = _log_event(writer, writes=["main.py"])
    b_id = _log_event(writer, reads=["main.py", "util.py"], parent_event_id=a_id)
    c_id = _log_event(writer, writes=["other.py"], parent_event_id=b_id)
    events = _get_events(ledger)

    dag = build_dag(events)

    assert set(dag["event_reads"][b_id]) == {"main.py", "util.py"}
    # a_id y c_id no tienen reads → no deben estar en event_reads.
    assert a_id not in dag["event_reads"]
    assert c_id not in dag["event_reads"]


def test_build_dag_fills_event_meta(tmp_path):
    """``event_meta[event_id]`` = ``{"event_type", "timestamp"}`` SOLO para
    eventos con reads no vacíos."""
    ledger, writer = _make_ledger(tmp_path)
    a_id = _log_event(writer, writes=["main.py"])
    b_id = _log_event(writer, reads=["main.py"], parent_event_id=a_id)
    events = _get_events(ledger)

    dag = build_dag(events)

    assert b_id in dag["event_meta"], (
        "event_meta debe incluir eventos con reads"
    )
    meta = dag["event_meta"][b_id]
    assert isinstance(meta.get("event_type"), str)
    assert isinstance(meta.get("timestamp"), str)
    # a_id no tiene reads → no debe estar en event_meta.
    assert a_id not in dag["event_meta"]


# ---------------------------------------------------------------------------
# 6: writer_history matches _causal_cone._writer_history
# ---------------------------------------------------------------------------

def test_build_dag_writer_history_matches_causal_cone(tmp_path):
    """``writer_history`` del DAG == ``_causal_cone._writer_history`` (tupla→lista)."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 8, writes_per_event=[
        ["main.py"], ["main.py", "util.py"], ["util.py"],
        ["main.py"], ["other.py"], ["main.py", "other.py"],
        ["util.py"], ["main.py"],
    ])
    events = _get_events(ledger)

    # Referencia: _causal_cone._writer_history (tuplas).
    cone_history = _writer_history(events)

    dag = build_dag(events)

    # Mismas keys (mismos archivos escritos).
    assert set(dag["writer_history"].keys()) == set(cone_history.keys()), (
        f"writer_history keys difieren: DAG={set(dag['writer_history'].keys())} "
        f"cone={set(cone_history.keys())}"
    )

    # Mismas entries (convirtiendo tupla→lista).
    for file_path in cone_history:
        cone_entries = cone_history[file_path]
        dag_entries = dag["writer_history"][file_path]
        # Convertir tuplas a listas para comparar.
        cone_as_lists = [[eid, pos] for eid, pos in cone_entries]
        assert dag_entries == cone_as_lists, (
            f"writer_history[{file_path}] difiere:\n"
            f"  DAG   = {dag_entries}\n"
            f"  cone  = {cone_as_lists}"
        )

    # history_positions debe ser paralelo a writer_history.
    for file_path in cone_history:
        cone_positions = [pos for _, pos in cone_history[file_path]]
        assert dag["history_positions"][file_path] == cone_positions, (
            f"history_positions[{file_path}] difiere:\n"
            f"  DAG   = {dag['history_positions'][file_path]}\n"
            f"  cone  = {cone_positions}"
        )


# ---------------------------------------------------------------------------
# 7: write_dag creates file
# ---------------------------------------------------------------------------

def test_write_dag_creates_file(tmp_path):
    """``write_dag(dag, path)`` crea el archivo y es JSON válido."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 3)
    events = _get_events(ledger)
    dag = build_dag(events)

    dag_path = str(tmp_path / "dag.json")
    write_dag(dag, dag_path)

    assert os.path.exists(dag_path), "write_dag debe crear el archivo"
    with open(dag_path, "r") as f:
        loaded = json.load(f)
    assert isinstance(loaded, dict)
    # El archivo debe contener todos los campos del DAG.
    assert validate_dag(loaded) is True


# ---------------------------------------------------------------------------
# 8: write_dag uses flock
# ---------------------------------------------------------------------------

def test_write_dag_uses_flock(tmp_path):
    """``write_dag`` llama ``fcntl.flock`` con ``LOCK_EX`` (file locking)."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 2)
    events = _get_events(ledger)
    dag = build_dag(events)

    dag_path = str(tmp_path / "dag.json")
    with mock.patch("causadb._dag_cache.fcntl.flock") as mock_flock:
        write_dag(dag, dag_path)

    assert mock_flock.called, "fcntl.flock debe ser llamado por write_dag"
    # Verificar que se llamó con LOCK_EX (primer call).
    call_args = mock_flock.call_args_list[0]
    assert call_args[0][1] == mock.ANY  # segundo arg es el lock operation
    # Importar fcntl para comparar el valor exacto.
    import fcntl as real_fcntl
    lock_ops = [c[0][1] for c in mock_flock.call_args_list]
    assert real_fcntl.LOCK_EX in lock_ops, (
        f"fcntl.flock debe llamarse con LOCK_EX, got calls: {lock_ops}"
    )


# ---------------------------------------------------------------------------
# 9: compute_dag_hash is stable
# ---------------------------------------------------------------------------

def test_compute_dag_hash_is_stable(tmp_path):
    """Mismo DAG → mismo hash (estabilidad)."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 5)
    events = _get_events(ledger)
    dag = build_dag(events)

    hash1 = compute_dag_hash(dag)
    hash2 = compute_dag_hash(dag)

    assert hash1 == hash2, (
        "compute_dag_hash debe ser estable: mismo DAG → mismo hash"
    )
    assert isinstance(hash1, str) and len(hash1) == 64, (
        "SHA-256 hex digest tiene 64 caracteres"
    )


# ---------------------------------------------------------------------------
# 10: compute_dag_hash changes on modification
# ---------------------------------------------------------------------------

def test_compute_dag_hash_changes_on_modification(tmp_path):
    """Modificar un event_id → hash distinto (sensibilidad)."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 3)
    events = _get_events(ledger)
    dag = build_dag(events)

    original_hash = compute_dag_hash(dag)

    # Mutar un event_id en writer_history y event_writes.
    # Tomar el primer event_id encontrado.
    first_eid = next(iter(dag["event_writes"]))
    mutated_eid = first_eid + "_MUTATED"

    # Mutar writer_history: reemplazar el event_id en todas las entries.
    for file_path, entries in dag["writer_history"].items():
        for entry in entries:
            if entry[0] == first_eid:
                entry[0] = mutated_eid
    # Mutar event_writes: mover la key.
    dag["event_writes"][mutated_eid] = dag["event_writes"].pop(first_eid)
    # Mutar last_event_id si era ese.
    if dag["last_event_id"] == first_eid:
        dag["last_event_id"] = mutated_eid

    mutated_hash = compute_dag_hash(dag)

    assert mutated_hash != original_hash, (
        "Modificar un event_id debe cambiar el hash — el hash no es sensible"
    )


# ---------------------------------------------------------------------------
# 11: write_dag includes hash
# ---------------------------------------------------------------------------

def test_write_dag_includes_hash(tmp_path):
    """El JSON escrito contiene ``dag_hash`` como campo top-level."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 4)
    events = _get_events(ledger)
    dag = build_dag(events)

    dag_path = str(tmp_path / "dag.json")
    write_dag(dag, dag_path)

    with open(dag_path, "r") as f:
        loaded = json.load(f)

    assert "dag_hash" in loaded, (
        "El JSON escrito debe incluir 'dag_hash' como campo top-level"
    )
    assert isinstance(loaded["dag_hash"], str)
    assert len(loaded["dag_hash"]) == 64, "dag_hash debe ser SHA-256 (64 hex chars)"

    # El dag_hash escrito debe matchear compute_dag_hash del DAG original
    # (sin el campo dag_hash, que es lo que se hashea).
    expected = compute_dag_hash(dag)
    assert loaded["dag_hash"] == expected, (
        f"dag_hash escrito ({loaded['dag_hash']}) != compute_dag_hash(dag) ({expected})"
    )


# ---------------------------------------------------------------------------
# 12: Anti-teatro — build_dag skips event_writes
# ---------------------------------------------------------------------------

def test_anti_teatro_build_dag_skips_event_writes(tmp_path, monkeypatch):
    """Si ``build_dag`` no llena ``event_writes``, el test #4 debe fallar.

    Este test muta ``build_dag`` para que devuelva un DAG con
    ``event_writes`` vacío. Si la implementación real fuera teatro
    (no populando event_writes), el test #4 pasaría trivialmente —
    este test garantiza que el test #4 ejerce la populación real.
    """
    ledger, writer = _make_ledger(tmp_path)
    a_id = _log_event(writer, writes=["main.py", "util.py"])
    b_id = _log_event(writer, writes=["other.py"], parent_event_id=a_id)
    events = _get_events(ledger)

    # Capturar el DAG real primero (implementación real debe popular event_writes).
    real_dag = build_dag(events)
    assert set(real_dag["event_writes"][a_id]) == {"main.py", "util.py"}, (
        "La implementación real debe popular event_writes — si esto falla, "
        "build_dag es teatro (Artículo IX)."
    )

    # Mutar build_dag para vaciar event_writes (simular teatro).
    # Guardar la referencia a la build_dag real ANTES de mutar.
    import causadb._dag_cache as dag_cache_mod
    real_build_dag = dag_cache_mod.build_dag

    def _teatro_build_dag(events):
        real = real_build_dag(events)
        real["event_writes"] = {}
        return real

    monkeypatch.setattr(dag_cache_mod, "build_dag", _teatro_build_dag)

    # Con la mutación, event_writes está vacío → el test #4 fallaría.
    teatro_dag = dag_cache_mod.build_dag(events)
    assert teatro_dag["event_writes"] == {}, (
        "La mutación a teatro no tuvo efecto — el test anti-teatro no ejerce "
        "correctamente la populación de event_writes."
    )
    # Verificar que el test #4 efectivamente fallaría con la mutación.
    with pytest.raises(AssertionError):
        assert set(teatro_dag["event_writes"].get(a_id, [])) == {"main.py", "util.py"}


# ---------------------------------------------------------------------------
# 13: Anti-teatro — write_dag no flock
# ---------------------------------------------------------------------------

def test_anti_teatro_write_dag_no_flock(tmp_path, monkeypatch):
    """Si ``write_dag`` no usa ``fcntl.flock``, el test #8 debe fallar.

    Este test muta ``write_dag`` para que no llame ``fcntl.flock``.
    Si la implementación real fuera teatro (sin flock), el test #8
    pasaría trivialmente — este test garantiza que el test #8 ejerce
    el file locking real.
    """
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 2)
    events = _get_events(ledger)
    dag = build_dag(events)

    # Primero verificar que la implementación real SÍ usa flock.
    dag_path_real = str(tmp_path / "dag_real.json")
    with mock.patch("causadb._dag_cache.fcntl.flock") as mock_flock_real:
        write_dag(dag, dag_path_real)
    assert mock_flock_real.called, (
        "La implementación real de write_dag debe usar fcntl.flock — "
        "si esto falla, write_dag es teatro (Artículo IX)."
    )

    # Mutar write_dag para no usar flock (simular teatro).
    import causadb._dag_cache as dag_cache_mod
    import fcntl as real_fcntl

    def _teatro_write_dag(dag, path):
        # Igual que write_dag pero SIN flock — teatro.
        dag_with_hash = dict(dag)
        dag_with_hash["dag_hash"] = compute_dag_hash(dag)
        payload = json.dumps(dag_with_hash, sort_keys=True)
        with open(path, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

    monkeypatch.setattr(dag_cache_mod, "write_dag", _teatro_write_dag)

    # Con la mutación, flock NO se llama → el test #8 fallaría.
    dag_path_teatro = str(tmp_path / "dag_teatro.json")
    with mock.patch("causadb._dag_cache.fcntl.flock") as mock_flock_teatro:
        dag_cache_mod.write_dag(dag, dag_path_teatro)

    assert not mock_flock_teatro.called, (
        "La mutación a teatro no tuvo efecto — el test anti-teatro no ejerce "
        "correctamente el file locking."
    )
    # Verificar que el test #8 efectivamente fallaría con la mutación.
    with pytest.raises(AssertionError):
        assert mock_flock_teatro.called


# ===========================================================================
# F.13.1.3 — read_dag / staleness detection
# ===========================================================================

# Helpers para tests de read_dag (según spec F.13.1.3).

def _write_corrupt_dag(path, corrupt_content):
    """Escribe contenido arbitrario (corrupto) al path del DAG."""
    with open(path, "w") as f:
        f.write(corrupt_content)


def _dag_with_wrong_hash(path):
    """Escribe un DAG con schema válido pero ``dag_hash`` incorrecto."""
    dag = make_empty_dag()
    dag["dag_hash"] = "wrong_hash_value"
    with open(path, "w") as f:
        json.dump(dag, f)


# ---------------------------------------------------------------------------
# 1: read_dag returns None when file missing
# ---------------------------------------------------------------------------

def test_read_dag_returns_none_when_file_missing(tmp_path):
    """``read_dag`` en un path inexistente → ``None`` (cache frío)."""
    missing_path = str(tmp_path / "no_existe.json")
    result = read_dag(missing_path)
    assert result is None, (
        "read_dag en archivo inexistente debe retornar None (cache frío)"
    )


# ---------------------------------------------------------------------------
# 2: read_dag returns DAG when valid
# ---------------------------------------------------------------------------

def test_read_dag_returns_dag_when_valid(tmp_path):
    """``read_dag`` en un DAG válido escrito por ``write_dag`` → el DAG original."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 5)
    events = _get_events(ledger)
    original_dag = build_dag(events)

    dag_path = str(tmp_path / "dag.json")
    write_dag(original_dag, dag_path)

    loaded = read_dag(dag_path)

    assert loaded is not None, (
        "read_dag en un DAG válido debe retornar el dict, no None"
    )
    # El DAG cargado debe pasar validate_dag.
    assert validate_dag(loaded) is True, (
        "El DAG cargado por read_dag debe pasar validate_dag"
    )
    # El DAG cargado NO debe tener el campo dag_hash (es metadata de
    # integridad, read_dag lo remueve antes de retornar).
    assert "dag_hash" not in loaded, (
        "read_dag debe remover dag_hash antes de retornar (es metadata)"
    )
    # Comparar campos semánticos con el original.
    assert loaded["last_event_id"] == original_dag["last_event_id"]
    assert loaded["last_seq"] == original_dag["last_seq"]
    assert loaded["writer_history"] == original_dag["writer_history"]
    assert loaded["event_writes"] == original_dag["event_writes"]
    assert loaded["event_reads"] == original_dag["event_reads"]
    assert loaded["event_meta"] == original_dag["event_meta"]
    assert loaded["event_ids"] == original_dag["event_ids"]


# ---------------------------------------------------------------------------
# 3: read_dag returns None on corrupt JSON
# ---------------------------------------------------------------------------

def test_read_dag_returns_none_on_corrupt_json(tmp_path):
    """JSON truncado/inválido → ``None`` (degradación suave)."""
    dag_path = str(tmp_path / "dag.json")
    # JSON truncado (incompleto).
    _write_corrupt_dag(dag_path, '{"schema_version": 1, "last_event_id": "abc"')

    result = read_dag(dag_path)
    assert result is None, (
        "read_dag en JSON corrupto (truncado) debe retornar None "
        "(degradación suave)"
    )


# ---------------------------------------------------------------------------
# 4: read_dag returns None on hash mismatch
# ---------------------------------------------------------------------------

def test_read_dag_returns_none_on_hash_mismatch(tmp_path):
    """``dag_hash`` alterado → ``None`` (corrupción detectada)."""
    dag_path = str(tmp_path / "dag.json")
    _dag_with_wrong_hash(dag_path)

    result = read_dag(dag_path)
    assert result is None, (
        "read_dag en DAG con dag_hash incorrecto debe retornar None "
        "(corrupción detectada por hash mismatch)"
    )


# ---------------------------------------------------------------------------
# 5: read_dag returns None on invalid schema
# ---------------------------------------------------------------------------

def test_read_dag_returns_none_on_invalid_schema(tmp_path):
    """DAG sin campos requeridos (schema inválido) → ``None``."""
    dag_path = str(tmp_path / "dag.json")
    # Construir un DAG con hash válido pero schema incompleto
    # (faltan campos requeridos). Necesitamos que el dag_hash matchee
    # para aislar el fallo a validate_dag, no al hash check.
    incomplete_dag = {
        "schema_version": 1,
        "last_event_id": "",
        # Faltan: last_seq, writer_history, history_positions,
        # event_parents, event_writes, built_at.
    }
    # Computar el hash correcto para el DAG incompleto.
    incomplete_dag["dag_hash"] = compute_dag_hash(incomplete_dag)
    with open(dag_path, "w") as f:
        json.dump(incomplete_dag, f)

    result = read_dag(dag_path)
    assert result is None, (
        "read_dag en DAG con schema inválido (campos faltantes) debe "
        "retornar None (validate_dag falla)"
    )


# ---------------------------------------------------------------------------
# 6: read_dag uses flock shared (LOCK_SH, not LOCK_EX)
# ---------------------------------------------------------------------------

def test_read_dag_uses_flock_shared(tmp_path):
    """``read_dag`` llama ``fcntl.flock`` con ``LOCK_SH`` (no ``LOCK_EX``).

    El reader usa un lock compartido para permitir lecturas
    concurrentes — múltiples readers pueden leer al mismo tiempo.
    Si usara ``LOCK_EX``, serializaría innecesariamente las lecturas.
    """
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 3)
    events = _get_events(ledger)
    dag = build_dag(events)

    dag_path = str(tmp_path / "dag.json")
    write_dag(dag, dag_path)

    with mock.patch("causadb._dag_cache.fcntl.flock") as mock_flock:
        result = read_dag(dag_path)

    assert result is not None, "read_dag debe retornar el DAG en el caso válido"
    assert mock_flock.called, (
        "read_dag debe llamar fcntl.flock para file locking de lectura"
    )

    # Verificar que se llamó con LOCK_SH (no LOCK_EX).
    import fcntl as real_fcntl
    lock_ops = [c[0][1] for c in mock_flock.call_args_list]
    assert real_fcntl.LOCK_SH in lock_ops, (
        f"read_dag debe usar LOCK_SH (lectura compartida), got calls: {lock_ops}"
    )
    # Confirmar que NO usó LOCK_EX (escritura exclusiva).
    assert real_fcntl.LOCK_EX not in lock_ops, (
        f"read_dag NO debe usar LOCK_EX (escritura exclusiva), got calls: {lock_ops}"
    )


# ---------------------------------------------------------------------------
# 7: get_last_ledger_seq empty ledger
# ---------------------------------------------------------------------------

def test_get_last_ledger_seq_empty_ledger(tmp_path):
    """Ledger vacío (sin eventos) → ``0``."""
    ledger, _ = _make_ledger(tmp_path)
    # No loguear ningún evento — ledger está vacío (o no existe).

    seq = get_last_ledger_seq(ledger)
    assert seq == 0, (
        f"get_last_ledger_seq en ledger vacío debe retornar 0, got {seq}"
    )


# ---------------------------------------------------------------------------
# 8: get_last_ledger_seq returns last sequence_number
# ---------------------------------------------------------------------------

def test_get_last_ledger_seq_returns_last_sequence_number(tmp_path):
    """3 eventos logueados → seq del último evento (el más alto)."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 3)
    events = _get_events(ledger)

    # El último evento debe tener el sequence_number más alto.
    expected_seq = events[-1].get("sequence_number")
    assert expected_seq is not None, (
        "El último evento debe tener sequence_number (lo asigna LedgerWriter)"
    )

    seq = get_last_ledger_seq(ledger)
    assert seq == expected_seq, (
        f"get_last_ledger_seq debe retornar el sequence_number del último "
        f"evento ({expected_seq}), got {seq}"
    )
    # Confirmar que es el más alto de los 3.
    all_seqs = [e.get("sequence_number") for e in events]
    assert seq == max(all_seqs), (
        f"get_last_ledger_seq debe ser el seq más alto ({max(all_seqs)}), "
        f"got {seq}"
    )


# ---------------------------------------------------------------------------
# 9: is_dag_stale returns False when fresh
# ---------------------------------------------------------------------------

def test_is_dag_stale_returns_false_when_fresh(tmp_path):
    """DAG con ``last_seq`` == último seq del ledger → ``False`` (fresh)."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 5)
    events = _get_events(ledger)
    dag = build_dag(events)

    # El DAG se construyó con todos los eventos del ledger → last_seq
    # debe matchear el último sequence_number del ledger.
    assert dag["last_seq"] == get_last_ledger_seq(ledger), (
        "Precondición: build_dag debe setear last_seq == último seq del ledger"
    )

    assert is_dag_stale(dag, ledger) is False, (
        "DAG con last_seq == último seq del ledger debe ser fresh (False)"
    )


# ---------------------------------------------------------------------------
# 10: is_dag_stale returns True when ledger has more events
# ---------------------------------------------------------------------------

def test_is_dag_stale_returns_true_when_ledger_has_more_events(tmp_path):
    """DAG con ``last_seq=5``, ledger tiene evento con seq=10 → ``True``."""
    ledger, writer = _make_ledger(tmp_path)
    # Loguear 5 eventos → DAG se construye con last_seq=4 (0-indexed seqs).
    _log_chain(writer, 5)
    events = _get_events(ledger)
    dag = build_dag(events)
    original_last_seq = dag["last_seq"]

    # Loguear 5 eventos más → ledger ahora tiene seqs hasta 9.
    _log_chain(writer, 5, writes_per_event=[["more.py"]] * 5)

    # El DAG sigue teniendo last_seq=4, pero el ledger tiene seqs hasta 9.
    assert dag["last_seq"] == original_last_seq, (
        "Precondición: el DAG no debe mutarse al loguear más eventos"
    )
    assert get_last_ledger_seq(ledger) > original_last_seq, (
        "Precondición: el ledger debe tener seqs más altos que el DAG"
    )

    assert is_dag_stale(dag, ledger) is True, (
        "DAG con last_seq < último seq del ledger debe ser stale (True)"
    )


# ---------------------------------------------------------------------------
# 11: is_dag_stale returns True for empty DAG
# ---------------------------------------------------------------------------

def test_is_dag_stale_returns_true_for_empty_dag(tmp_path):
    """DAG vacío (``last_seq=0``), ledger no vacío → ``True`` (stale)."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 3)

    empty_dag = make_empty_dag()
    assert empty_dag["last_seq"] == 0, (
        "Precondición: make_empty_dag debe setear last_seq=0"
    )

    assert is_dag_stale(empty_dag, ledger) is True, (
        "DAG vacío (last_seq=0) contra ledger no vacío debe ser stale (True)"
    )


# ---------------------------------------------------------------------------
# 12: Anti-teatro — read_dag skips hash validation
# ---------------------------------------------------------------------------

def test_anti_teatro_read_dag_returns_corrupt(tmp_path, monkeypatch):
    """Si ``read_dag`` no valida hash, el test #4 debe fallar.

    Este test muta ``read_dag`` para que NO verifique el ``dag_hash``
    (simular teatro). Si la implementación real fuera teatro (sin
    verificación de integridad), el test #4 (hash mismatch → None)
    pasaría trivialmente — este test garantiza que el test #4 ejerce
    la verificación de hash real.

    La mutación: omitir el paso de comparar ``recomputed_hash`` con
    ``stored_hash``. Esto hace que ``read_dag`` retorne el DAG corrupto
    en vez de ``None``.
    """
    dag_path = str(tmp_path / "dag.json")
    _dag_with_wrong_hash(dag_path)

    # Primero verificar que la implementación real SÍ valida el hash.
    real_result = read_dag(dag_path)
    assert real_result is None, (
        "La implementación real de read_dag debe validar dag_hash y "
        "retornar None ante mismatch — si esto falla, read_dag es "
        "teatro (Artículo IX)."
    )

    # Mutar read_dag para no validar hash (simular teatro).
    import causadb._dag_cache as dag_cache_mod

    def _teatro_read_dag(path):
        # Igual que read_dag pero SIN verificar dag_hash — teatro.
        if not os.path.exists(path):
            return None
        lock_path = path + ".lock"
        if not os.path.exists(lock_path):
            try:
                open(lock_path, "a").close()
            except OSError:
                return None
        try:
            with open(lock_path, "a") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
                try:
                    with open(path, "r") as f:
                        raw = f.read()
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except (OSError, IOError):
            return None
        try:
            loaded = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(loaded, dict):
            return None
        # TEATRO: NO verificar dag_hash. Removerlo y retornar directo.
        dag_for_hash = {k: v for k, v in loaded.items() if k != "dag_hash"}
        if not validate_dag(dag_for_hash):
            return None
        return dag_for_hash

    monkeypatch.setattr(dag_cache_mod, "read_dag", _teatro_read_dag)

    # Importar fcntl aquí para que la mutación tenga acceso.
    import fcntl

    # Con la mutación, read_dag NO valida hash → retorna el DAG corrupto.
    teatro_result = dag_cache_mod.read_dag(dag_path)
    assert teatro_result is not None, (
        "La mutación a teatro no tuvo efecto — el test anti-teatro no "
        "ejerce correctamente la validación de hash."
    )
    # Verificar que el test #4 efectivamente fallaría con la mutación:
    # el DAG corrupto se retorna en vez de None.
    with pytest.raises(AssertionError):
        assert teatro_result is None


# ===========================================================================
# F.13.1.4 — update_dag (incremental update)
# ===========================================================================

# ---------------------------------------------------------------------------
# 1: update_dag with no new events returns identical DAG
# ---------------------------------------------------------------------------

def test_update_dag_with_no_new_events(tmp_path):
    """``update_dag(dag, [])`` retorna un DAG idéntico (excepto ``built_at``).

    Sin eventos nuevos, el DAG no debe cambiar en ningún campo semántico.
    Solo ``built_at`` puede diferir (se actualiza al timestamp actual).
    """
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 5)
    events = _get_events(ledger)
    dag = build_dag(events)

    updated = update_dag(dag, [])

    # Debe pasar validate_dag.
    assert validate_dag(updated) is True, (
        "update_dag con [] debe retornar un DAG válido"
    )
    # Campos semánticos idénticos.
    assert updated["last_event_id"] == dag["last_event_id"]
    assert updated["last_seq"] == dag["last_seq"]
    assert updated["writer_history"] == dag["writer_history"]
    assert updated["history_positions"] == dag["history_positions"]
    assert updated["event_writes"] == dag["event_writes"]
    assert updated["event_reads"] == dag["event_reads"]
    assert updated["event_meta"] == dag["event_meta"]
    assert updated["event_ids"] == dag["event_ids"]
    assert updated["schema_version"] == dag["schema_version"]
    # last_offset/last_hash/covered_archives NO los toca update_dag
    # (los setea el caller — _load_or_build_dag).
    assert updated["last_offset"] == dag["last_offset"]
    assert updated["last_hash"] == dag["last_hash"]
    assert updated["covered_archives"] == dag["covered_archives"]
    # built_at puede diferir (se actualiza), pero debe ser str.
    assert isinstance(updated["built_at"], str)


# ---------------------------------------------------------------------------
# 2: update_dag with new events extends last_seq
# ---------------------------------------------------------------------------

def test_update_dag_with_new_events_extends_last_seq(tmp_path):
    """DAG con ``last_seq=4``, agregar 3 eventos con seq 5,6,7 → ``last_seq=7``."""
    ledger, writer = _make_ledger(tmp_path)
    # Loguear 5 eventos → seqs 0-4 (asumiendo 0-indexed).
    _log_chain(writer, 5)
    events = _get_events(ledger)
    dag = build_dag(events)
    original_last_seq = dag["last_seq"]

    # Loguear 3 eventos más → seqs 5, 6, 7.
    _log_chain(writer, 3, writes_per_event=[["new.py"]] * 3)
    all_events = _get_events(ledger)
    new_events = all_events[len(events):]  # los 3 nuevos.

    updated = update_dag(dag, new_events)

    expected_last_seq = all_events[-1].get("sequence_number")
    assert updated["last_seq"] == expected_last_seq, (
        f"update_dag debe extender last_seq a {expected_last_seq} "
        f"(último evento), got {updated['last_seq']}"
    )
    assert updated["last_seq"] > original_last_seq, (
        f"last_seq debe aumentar: original={original_last_seq}, "
        f"updated={updated['last_seq']}"
    )


# ---------------------------------------------------------------------------
# 3: update_dag with new events extends writer_history
# ---------------------------------------------------------------------------

def test_update_dag_with_new_events_extends_writer_history(tmp_path):
    """Agregar evento con writes=["new.py"] → ``writer_history["new.py"]`` contiene el nuevo event_id."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 3, writes_per_event=[["main.py"]] * 3)
    events = _get_events(ledger)
    dag = build_dag(events)

    # Loguear 1 evento que escribe "new.py".
    new_eid = _log_event(writer, writes=["new.py"], parent_event_id=events[-1]["event_id"])
    all_events = _get_events(ledger)
    new_events = all_events[len(events):]

    updated = update_dag(dag, new_events)

    assert "new.py" in updated["writer_history"], (
        "writer_history debe incluir 'new.py' después del update"
    )
    # El event_id del nuevo evento debe aparecer en writer_history["new.py"].
    new_entries = updated["writer_history"]["new.py"]
    new_eids = [entry[0] for entry in new_entries]
    assert new_eid in new_eids, (
        f"writer_history['new.py'] debe contener el nuevo event_id "
        f"{new_eid}, got entries={new_entries}"
    )


# ---------------------------------------------------------------------------
# 4: update_dag with new events extends event_ids / event_reads / event_meta
# ---------------------------------------------------------------------------

def test_update_dag_with_new_events_extends_event_ids(tmp_path):
    """Agregar eventos nuevos → ``event_ids`` se extiende en orden."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 2)
    events = _get_events(ledger)
    dag = build_dag(events)

    new_eid = _log_event(writer, writes=["child.py"], parent_event_id=events[-1]["event_id"])
    all_events = _get_events(ledger)
    new_events = all_events[len(events):]

    updated = update_dag(dag, new_events)

    assert updated["event_ids"] == dag["event_ids"] + [new_eid], (
        f"event_ids debe extenderse con el nuevo event_id, "
        f"got {updated['event_ids']}"
    )


def test_update_dag_with_new_events_extends_event_reads_and_meta(tmp_path):
    """Agregar evento con reads → ``event_reads`` y ``event_meta`` se extienden."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 2)
    events = _get_events(ledger)
    dag = build_dag(events)

    new_eid = _log_event(
        writer, reads=["main.py"], writes=["child.py"],
        parent_event_id=events[-1]["event_id"],
    )
    all_events = _get_events(ledger)
    new_events = all_events[len(events):]

    updated = update_dag(dag, new_events)

    assert set(updated["event_reads"][new_eid]) == {"main.py"}, (
        f"event_reads debe incluir el nuevo evento con sus reads, "
        f"got {updated['event_reads'].get(new_eid)}"
    )
    assert new_eid in updated["event_meta"], (
        "event_meta debe incluir el nuevo evento con reads"
    )
    meta = updated["event_meta"][new_eid]
    assert isinstance(meta.get("event_type"), str)
    assert isinstance(meta.get("timestamp"), str)


# ---------------------------------------------------------------------------
# 5: update_dag with new events extends event_writes
# ---------------------------------------------------------------------------

def test_update_dag_with_new_events_extends_event_writes(tmp_path):
    """Agregar evento con writes=["a.py", "b.py"] → ``event_writes[new_id] = ["a.py", "b.py"]``."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 2)
    events = _get_events(ledger)
    dag = build_dag(events)

    new_eid = _log_event(
        writer, writes=["a.py", "b.py"], parent_event_id=events[-1]["event_id"]
    )
    all_events = _get_events(ledger)
    new_events = all_events[len(events):]

    updated = update_dag(dag, new_events)

    assert new_eid in updated["event_writes"], (
        f"event_writes debe incluir el nuevo event_id {new_eid}"
    )
    assert list(updated["event_writes"][new_eid]) == ["a.py", "b.py"], (
        f"event_writes[{new_eid}] debe ser ['a.py', 'b.py'], "
        f"got {updated['event_writes'][new_eid]}"
    )


# ---------------------------------------------------------------------------
# 6: update_dag chain_positions are sequential
# ---------------------------------------------------------------------------

def test_update_dag_chain_positions_are_sequential(tmp_path):
    """DAG con ``last_seq=2``, agregar 3 eventos → chain_positions son 3, 4, 5 (secuenciales).

    Los nuevos eventos extienden la cadena lineal, así que sus
    chain_positions deben ser secuenciales a partir de last_seq + 1.
    """
    ledger, writer = _make_ledger(tmp_path)
    # 3 eventos → seqs 0, 1, 2 → last_seq=2.
    _log_chain(writer, 3, writes_per_event=[["main.py"]] * 3)
    events = _get_events(ledger)
    dag = build_dag(events)
    assert dag["last_seq"] == 2, (
        f"Precondición: DAG base debe tener last_seq=2, got {dag['last_seq']}"
    )

    # Loguear 3 eventos más que extienden la cadena.
    _log_chain(writer, 3, writes_per_event=[["main.py"]] * 3)
    all_events = _get_events(ledger)
    new_events = all_events[len(events):]

    updated = update_dag(dag, new_events)

    # Los nuevos event_ids deben tener chain_positions 3, 4, 5 en
    # writer_history["main.py"].
    new_eids = [ev["event_id"] for ev in new_events]
    main_history = updated["writer_history"]["main.py"]

    # Extraer las entries de los nuevos event_ids.
    new_entries = [entry for entry in main_history if entry[0] in new_eids]
    new_positions = [entry[1] for entry in new_entries]

    assert new_positions == [3, 4, 5], (
        f"chain_positions de los nuevos eventos deben ser [3, 4, 5] "
        f"(secuenciales), got {new_positions}"
    )


# ---------------------------------------------------------------------------
# 7: CRÍTICO — base 500 + new 500 == build_dag(1000)
# ---------------------------------------------------------------------------

def test_update_dag_base_500_plus_500_equals_build_dag_1000(tmp_path):
    """**CRÍTICO**: ``update_dag(build_dag(500), events_500_1000) == build_dag(1000)``.

    Construir un DAG con los primeros 500 eventos, hacer update_dag
    con los 500 eventos restantes, y comparar con build_dag(1000).
    Los campos writer_history, event_parents, event_writes,
    last_event_id, last_seq deben ser IDENTICAL. built_at puede diferir.

    Este test verifica que el incremental update produce exactamente
    el mismo resultado que un full rebuild — la propiedad fundamental
    del cache DAG (artículo VIII: no crear abstracciones que no
    preserven invariantes).
    """
    ledger, writer = _make_ledger(tmp_path)
    # Loguear 1000 eventos en una cadena lineal.
    _log_chain(writer, 1000, writes_per_event=[["main.py"]] * 1000)
    all_events = _get_events(ledger)
    assert len(all_events) == 1000, (
        f"Precondición: deben haber 1000 eventos, got {len(all_events)}"
    )

    # Split: primeros 500 para build_dag, últimos 500 para update_dag.
    base_events = all_events[:500]
    new_events = all_events[500:]

    # Construir DAG base con los primeros 500.
    base_dag = build_dag(base_events)
    assert base_dag["last_seq"] == 499, (
        f"Precondición: base_dag debe tener last_seq=499, "
        f"got {base_dag['last_seq']}"
    )

    # Update incremental con los 500 restantes.
    updated_dag = update_dag(base_dag, new_events)

    # Full rebuild con los 1000 eventos.
    full_dag = build_dag(all_events)

    # Comparar campos semánticos — deben ser IDENTICAL.
    assert updated_dag["last_event_id"] == full_dag["last_event_id"], (
        f"last_event_id difiere:\n  updated={updated_dag['last_event_id']}\n"
        f"  full={full_dag['last_event_id']}"
    )
    assert updated_dag["last_seq"] == full_dag["last_seq"], (
        f"last_seq difiere: updated={updated_dag['last_seq']}, "
        f"full={full_dag['last_seq']}"
    )
    assert updated_dag["writer_history"] == full_dag["writer_history"], (
        "writer_history difiere entre update_dag y build_dag(1000)"
    )
    assert updated_dag["history_positions"] == full_dag["history_positions"], (
        "history_positions difiere entre update_dag y build_dag(1000)"
    )
    assert updated_dag["event_writes"] == full_dag["event_writes"], (
        "event_writes difiere entre update_dag y build_dag(1000)"
    )
    assert updated_dag["event_reads"] == full_dag["event_reads"], (
        "event_reads difiere entre update_dag y build_dag(1000)"
    )
    assert updated_dag["event_meta"] == full_dag["event_meta"], (
        "event_meta difiere entre update_dag y build_dag(1000)"
    )
    assert updated_dag["event_ids"] == full_dag["event_ids"], (
        "event_ids difiere entre update_dag y build_dag(1000)"
    )
    assert updated_dag["schema_version"] == full_dag["schema_version"], (
        "schema_version difiere (no debe cambiar en incremental)"
    )
    # built_at puede diferir — no se compara.


# ---------------------------------------------------------------------------
# 8: Anti-teatro — update_dag omits event
# ---------------------------------------------------------------------------

def test_anti_teatro_update_dag_omits_event(tmp_path, monkeypatch):
    """Si ``update_dag`` saltea un evento, el test #7 (base+new==build_all) debe fallar.

    Este test muta ``update_dag`` para procesar solo ``new_events[:-1]``
    (omitir el último evento). Si la implementación real fuera teatro
    (no procesando todos los eventos), el test #7 pasaría trivialmente
    — este test garantiza que el test #7 ejerce el procesamiento real
    de todos los eventos.

    La mutación: procesar ``new_events[:-1]`` en vez de ``new_events``.
    Esto produce un DAG con 999 eventos en vez de 1000, que NO matchea
    ``build_dag(1000)``.
    """
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 1000, writes_per_event=[["main.py"]] * 1000)
    all_events = _get_events(ledger)
    base_events = all_events[:500]
    new_events = all_events[500:]

    base_dag = build_dag(base_events)

    # Primero verificar que la implementación real SÍ procesa todos los
    # eventos (el test #7 debe pasar con la implementación real).
    real_updated = update_dag(base_dag, new_events)
    full_dag = build_dag(all_events)
    assert real_updated["writer_history"] == full_dag["writer_history"], (
        "La implementación real de update_dag debe procesar todos los "
        "eventos — si esto falla, update_dag es teatro (Artículo IX)."
    )

    # Mutar update_dag para omitir el último evento (simular teatro).
    import causadb._dag_cache as dag_cache_mod
    real_update_dag = dag_cache_mod.update_dag

    def _teatro_update_dag(dag, new_events):
        # TEATRO: procesar solo new_events[:-1] (omitir el último).
        return real_update_dag(dag, new_events[:-1])

    monkeypatch.setattr(dag_cache_mod, "update_dag", _teatro_update_dag)

    # Con la mutación, update_dag omite el último evento → el resultado
    # tiene 999 eventos, no 1000 → NO matchea build_dag(1000).
    teatro_updated = dag_cache_mod.update_dag(base_dag, new_events)

    # Verificar que el test #7 efectivamente fallaría con la mutación:
    # writer_history no debe matchear (999 vs 1000 eventos).
    with pytest.raises(AssertionError):
        assert teatro_updated["writer_history"] == full_dag["writer_history"], (
            "Esta aserción debe FALLAR con la mutación — si pasa, el "
            "test anti-teatro no ejerce correctamente el procesamiento "
            "de todos los eventos."
        )


# ===========================================================================
# BIT-CHR.117 — Normalización de paths en el DAG cache
# ===========================================================================
# La normalización ``causadb/causadb/X`` → ``causadb/X`` (BIT-CHR.110
# TD-#3d) debe aplicarse en build_dag y update_dag para que el cono
# causal conecte eventos que escriben/leen el mismo archivo con estilos
# mixtos.

def test_build_dag_normalizes_event_writes(tmp_path):
    """``payload.writes`` con ``causadb/causadb/X`` → clave normalizada ``causadb/X``."""
    ledger, writer = _make_ledger(tmp_path)
    a_id = _log_event(writer, writes=["causadb/causadb/main.py"])
    events = _get_events(ledger)

    dag = build_dag(events)

    assert "causadb/main.py" in dag["event_writes"][a_id], (
        f"event_writes debe normalizar causadb/causadb/main.py → "
        f"causadb/main.py, got {dag['event_writes'][a_id]}"
    )
    assert "causadb/causadb/main.py" not in dag["event_writes"][a_id]
    # writer_history también normalizado (vía _effective_writes).
    assert "causadb/main.py" in dag["writer_history"], (
        f"writer_history debe tener la clave normalizada, "
        f"got {list(dag['writer_history'].keys())}"
    )


def test_build_dag_normalizes_event_reads(tmp_path):
    """``payload.reads`` con ``causadb/causadb/X`` → clave normalizada ``causadb/X``."""
    ledger, writer = _make_ledger(tmp_path)
    b_id = _log_event(writer, reads=["causadb/causadb/main.py"])
    events = _get_events(ledger)

    dag = build_dag(events)

    assert "causadb/main.py" in dag["event_reads"][b_id], (
        f"event_reads debe normalizar causadb/causadb/main.py → "
        f"causadb/main.py, got {dag['event_reads'][b_id]}"
    )
    assert "causadb/causadb/main.py" not in dag["event_reads"][b_id]


def test_update_dag_normalizes_writes_and_reads(tmp_path):
    """``update_dag`` normaliza writes/reads de los eventos nuevos."""
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 2)
    events = _get_events(ledger)
    dag = build_dag(events)

    new_eid = _log_event(
        writer,
        writes=["causadb/causadb/new.py"],
        reads=["causadb/causadb/main.py"],
        parent_event_id=events[-1]["event_id"],
    )
    all_events = _get_events(ledger)
    new_events = all_events[len(events):]

    updated = update_dag(dag, new_events)

    assert "causadb/new.py" in updated["event_writes"][new_eid], (
        f"update_dag debe normalizar writes, got {updated['event_writes'][new_eid]}"
    )
    assert "causadb/main.py" in updated["event_reads"][new_eid], (
        f"update_dag debe normalizar reads, got {updated['event_reads'][new_eid]}"
    )
    assert "causadb/new.py" in updated["writer_history"], (
        f"update_dag debe normalizar writer_history keys, "
        f"got {list(updated['writer_history'].keys())}"
    )


# ===========================================================================
# BIT-CHR.117 — get_last_ledger_seq por última línea (sin leer el ledger completo)
# ===========================================================================

def test_get_last_ledger_seq_ignores_truncated_last_line(tmp_path):
    """Última línea cortada (crash) → seq de la línea anterior válida.

    El writer reescribe la línea final truncada, así que
    ``get_last_ledger_seq`` debe ignorarla y devolver el seq de la
    última línea JSON válida.
    """
    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 3)
    events = _get_events(ledger)
    expected_seq = events[-2].get("sequence_number")

    # Truncar la última línea (simular crash a mitad de escritura).
    with open(ledger, "rb") as f:
        data = f.read()
    # Cortar la última línea a la mitad: mantener todo hasta la primera
    # mitad de la última línea (JSON incompleto que no parsea). NOTA: un
    # crash a mitad de escritura deja una línea FINAL INCOMPLETA — no
    # appendea basura después de una línea completa. El ledger termina
    # con "\n", así que la última línea real arranca tras el newline
    # ANTERIOR al final.
    last_nl = data.rfind(b"\n")
    prev_nl = data.rfind(b"\n", 0, last_nl)
    line_start = prev_nl + 1
    line_len = last_nl - line_start
    half_last_line = line_start + line_len // 2
    truncated = data[:half_last_line]
    with open(ledger, "wb") as f:
        f.write(truncated)

    seq = get_last_ledger_seq(ledger)
    assert seq == expected_seq, (
        f"get_last_ledger_seq debe ignorar la línea truncada y devolver "
        f"el seq de la anterior ({expected_seq}), got {seq}"
    )


def test_get_last_ledger_seq_empty_ledger_without_archive_returns_zero(tmp_path):
    """Ledger vacío (0 bytes) SIN archive/ → 0 (sin scan completo)."""
    ledger, _ = _make_ledger(tmp_path)
    # Crear el ledger vacío (0 bytes).
    open(ledger, "w").close()

    seq = get_last_ledger_seq(ledger)
    assert seq == 0, (
        f"ledger vacío sin archives debe retornar 0, got {seq}"
    )


def test_get_last_ledger_seq_does_not_read_full_ledger(tmp_path, monkeypatch):
    """``get_last_ledger_seq`` NO lee el ledger completo (anti-teatro).

    Si la implementación usara ``LedgerReader.read_all_entries`` (lectura
    completa), este test fallaría: monkeypatcheamos
    ``read_all_entries`` para que levante una excepción, y la lectura de
    la última línea debe funcionar igual.
    """
    import causadb._ledger_reader as reader_mod

    ledger, writer = _make_ledger(tmp_path)
    _log_chain(writer, 5)
    events = _get_events(ledger)
    expected_seq = events[-1].get("sequence_number")

    def _boom(*args, **kwargs):
        raise AssertionError("read_all_entries NO debe llamarse — get_last_ledger_seq debe leer solo la última línea")

    monkeypatch.setattr(reader_mod.LedgerReader, "read_all_entries", _boom)

    seq = get_last_ledger_seq(ledger)
    assert seq == expected_seq, (
        f"get_last_ledger_seq debe leer la última línea sin tocar "
        f"read_all_entries, got {seq}, expected {expected_seq}"
    )
