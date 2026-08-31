"""F.13.1.1 — Tests del schema del DAG cache persistente.

Test-first (Artículo III): estos tests se escriben ANTES de la
implementación de ``causadb._dag_schema``. Deben fallar con
``ImportError`` hasta que el módulo exista, luego pasar.

Anti-teatro (Artículo IX): el test 10 muta ``validate_dag`` para
asegurar que la validación es real (no un ``return True``).

Schema v3 (BIT-CHR.117): agrega ``last_offset``/``last_hash``/
``covered_archives`` (para el tail-read incremental y el guard de
truncamiento), ``event_reads``/``event_meta``/``event_ids`` (para que
``trace_upstream``/``trace_downstream`` operen SIN materializar el
ledger), y ELIMINA ``event_parents`` (peso muerto — nadie lo consume
en producción).
"""
import copy
import json

import pytest

from causadb._dag_schema import (
    DAG_SCHEMA_VERSION,
    dag_from_dict,
    dag_to_dict,
    make_empty_dag,
    validate_dag,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _synthetic_dag() -> dict:
    """Construye un DAG con datos sintéticos no triviales para roundtrip."""
    return {
        "schema_version": DAG_SCHEMA_VERSION,
        "last_event_id": "evt-aaa-111",
        "last_seq": 42,
        "last_offset": 4096,
        "last_hash": "abc123def456",
        "covered_archives": ["epoch_1.log.gz", "epoch_2.log.gz"],
        "writer_history": {
            "src/main.py": [
                ["evt-aaa-111", 0],
                ["evt-bbb-222", 5],
                ["evt-ccc-333", 9],
            ],
            "src/util.py": [
                ["evt-bbb-222", 5],
            ],
        },
        "history_positions": {
            "src/main.py": [0, 5, 9],
            "src/util.py": [5],
        },
        "event_writes": {
            "evt-aaa-111": ["src/main.py"],
            "evt-bbb-222": ["src/main.py", "src/util.py"],
            "evt-ccc-333": ["src/main.py"],
        },
        "event_reads": {
            "evt-bbb-222": ["src/main.py"],
            "evt-ccc-333": ["src/util.py"],
        },
        "event_meta": {
            "evt-bbb-222": {"event_type": "FILE_MODIFIED", "timestamp": "2026-07-24T12:00:01Z"},
            "evt-ccc-333": {"event_type": "FILE_MODIFIED", "timestamp": "2026-07-24T12:00:02Z"},
        },
        "event_ids": ["evt-aaa-111", "evt-bbb-222", "evt-ccc-333"],
        "built_at": "2026-07-24T12:00:00Z",
    }


# ---------------------------------------------------------------------------
# 1-3: Empty DAG
# ---------------------------------------------------------------------------

def test_empty_dag_is_valid():
    """``make_empty_dag()`` retorna un DAG que pasa ``validate_dag()``."""
    dag = make_empty_dag()
    assert validate_dag(dag) is True


def test_empty_dag_has_correct_schema_version():
    """``schema_version`` del DAG vacío == ``DAG_SCHEMA_VERSION`` == 3.

    Version 3 (BIT-CHR.117): agrega ``last_offset``/``last_hash``/
    ``covered_archives``/``event_reads``/``event_meta``/``event_ids`` y
    elimina ``event_parents``. El bump invalida caches v2 (``read_dag``
    retorna None → rebuild).
    """
    dag = make_empty_dag()
    assert dag["schema_version"] == DAG_SCHEMA_VERSION
    assert DAG_SCHEMA_VERSION == 3


def test_empty_dag_has_zero_last_seq():
    """Un DAG vacío no tiene eventos cacheados → ``last_seq == 0``."""
    dag = make_empty_dag()
    assert dag["last_seq"] == 0


def test_empty_dag_has_defaults_for_new_fields():
    """Los campos nuevos del schema v3 tienen defaults válidos en el DAG vacío."""
    dag = make_empty_dag()
    assert dag["last_offset"] == 0
    assert dag["last_hash"] == ""
    assert dag["covered_archives"] == []
    assert dag["event_reads"] == {}
    assert dag["event_meta"] == {}
    assert dag["event_ids"] == []


# ---------------------------------------------------------------------------
# 4: Roundtrip
# ---------------------------------------------------------------------------

def test_dag_roundtrip_preserves_all_fields():
    """Serializar → deserializar preserva todos los campos y valores."""
    original = _synthetic_dag()
    serialized = dag_to_dict(original)
    # Debe ser JSON-serializable (es el punto del cache).
    json_str = json.dumps(serialized)
    restored = dag_from_dict(json.loads(json_str))
    assert restored == original


# ---------------------------------------------------------------------------
# 5: Version detection
# ---------------------------------------------------------------------------

def test_dag_from_dict_rejects_wrong_schema_version():
    """``dag_from_dict`` levanta ``ValueError`` si ``schema_version`` != 3."""
    dag = _synthetic_dag()
    dag["schema_version"] = 999
    with pytest.raises(ValueError):
        dag_from_dict(dag)


# ---------------------------------------------------------------------------
# 6: Missing fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("missing_field", [
    "schema_version",
    "last_event_id",
    "last_seq",
    "last_offset",
    "last_hash",
    "covered_archives",
    "writer_history",
    "history_positions",
    "event_writes",
    "event_reads",
    "event_meta",
    "event_ids",
    "built_at",
])
def test_validate_dag_rejects_missing_field(missing_field):
    """``validate_dag`` retorna False si falta cualquier campo requerido."""
    dag = _synthetic_dag()
    del dag[missing_field]
    assert validate_dag(dag) is False


# ---------------------------------------------------------------------------
# 7: Wrong types
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field, bad_value", [
    ("schema_version", "1"),              # str en vez de int
    ("last_event_id", 123),              # int en vez de str
    ("last_seq", "0"),                    # str en vez de int
    ("last_offset", "0"),                 # str en vez de int
    ("last_offset", True),                # bool en vez de int (bool es subtipo)
    ("last_hash", 123),                   # int en vez de str
    ("covered_archives", "epoch_1.log.gz"),  # str en vez de list
    ("writer_history", []),               # list en vez de dict
    ("history_positions", []),           # list en vez de dict
    ("event_writes", []),                 # list en vez de dict
    ("event_reads", []),                  # list en vez de dict
    ("event_meta", []),                   # list en vez de dict
    ("event_ids", {}),                    # dict en vez de list
    ("built_at", 12345),                  # int en vez de str
])
def test_validate_dag_rejects_wrong_types(field, bad_value):
    """``validate_dag`` retorna False si un campo tiene tipo incorrecto."""
    dag = _synthetic_dag()
    dag[field] = bad_value
    assert validate_dag(dag) is False


# ---------------------------------------------------------------------------
# 8: writer_history format
# ---------------------------------------------------------------------------

def test_dag_writer_history_format():
    """``writer_history`` values son listas de ``[event_id, chain_position]``.

    - Cada value es una ``list``.
    - Cada entry es una ``list`` de 2 elementos.
    - ``entry[0]`` (event_id) es ``str``.
    - ``entry[1]`` (chain_position) es ``int``.
    """
    dag = _synthetic_dag()
    assert validate_dag(dag) is True
    for file_path, entries in dag["writer_history"].items():
        assert isinstance(file_path, str)
        assert isinstance(entries, list)
        for entry in entries:
            assert isinstance(entry, list)
            assert len(entry) == 2
            assert isinstance(entry[0], str)
            assert isinstance(entry[1], int)


def test_validate_dag_rejects_bad_writer_history_entry():
    """``validate_dag`` retorna False si un entry de writer_history es inválido."""
    dag = _synthetic_dag()
    # chain_position como string (no int)
    dag["writer_history"]["src/main.py"][0][1] = "0"
    assert validate_dag(dag) is False


# ---------------------------------------------------------------------------
# 9: event_writes / event_reads / event_meta / event_ids format
# ---------------------------------------------------------------------------

def test_dag_event_writes_format():
    """``event_writes`` values son listas de strings (file paths)."""
    dag = _synthetic_dag()
    assert validate_dag(dag) is True
    for event_id, writes in dag["event_writes"].items():
        assert isinstance(event_id, str)
        assert isinstance(writes, list)
        for w in writes:
            assert isinstance(w, str)


def test_validate_dag_rejects_bad_event_writes_entry():
    """``validate_dag`` retorna False si un file path en event_writes no es str."""
    dag = _synthetic_dag()
    dag["event_writes"]["evt-aaa-111"].append(123)
    assert validate_dag(dag) is False


def test_dag_event_reads_format():
    """``event_reads`` values son listas de strings (file paths)."""
    dag = _synthetic_dag()
    assert validate_dag(dag) is True
    for event_id, reads in dag["event_reads"].items():
        assert isinstance(event_id, str)
        assert isinstance(reads, list)
        for r in reads:
            assert isinstance(r, str)


def test_validate_dag_rejects_bad_event_reads_entry():
    """``validate_dag`` retorna False si un file path en event_reads no es str."""
    dag = _synthetic_dag()
    dag["event_reads"]["evt-bbb-222"].append(123)
    assert validate_dag(dag) is False


def test_dag_event_meta_format():
    """``event_meta`` values son dicts ``{"event_type": str, "timestamp": str}``."""
    dag = _synthetic_dag()
    assert validate_dag(dag) is True
    for event_id, meta in dag["event_meta"].items():
        assert isinstance(event_id, str)
        assert isinstance(meta, dict)
        assert isinstance(meta.get("event_type"), str)
        assert isinstance(meta.get("timestamp"), str)


def test_validate_dag_rejects_bad_event_meta_entry():
    """``validate_dag`` retorna False si un meta tiene tipo incorrecto."""
    dag = _synthetic_dag()
    dag["event_meta"]["evt-bbb-222"]["event_type"] = 123
    assert validate_dag(dag) is False


def test_dag_event_ids_format():
    """``event_ids`` es una lista de strings (orden de ledger)."""
    dag = _synthetic_dag()
    assert validate_dag(dag) is True
    for eid in dag["event_ids"]:
        assert isinstance(eid, str)


def test_validate_dag_rejects_bad_event_ids_entry():
    """``validate_dag`` retorna False si un event_id en event_ids no es str."""
    dag = _synthetic_dag()
    dag["event_ids"].append(123)
    assert validate_dag(dag) is False


def test_validate_dag_rejects_bad_covered_archives_entry():
    """``validate_dag`` retorna False si un nombre de archive no es str."""
    dag = _synthetic_dag()
    dag["covered_archives"].append(123)
    assert validate_dag(dag) is False


# ---------------------------------------------------------------------------
# 10: Anti-teatro — validate_dag no es un return True
# ---------------------------------------------------------------------------

def test_anti_teatro_validate_dag_accepts_corrupt(monkeypatch):
    """Si ``validate_dag`` siempre retorna True, datos corruptos pasarían.

    Este test construye un DAG con geometría corrupta (tipos mezclados,
    campos con valores imposibles) que un ``validate_dag`` real debe
    rechazar. Si mutamos ``validate_dag`` a ``return True`` (teatro), el
    test falla — garantizando que la validación es sustantiva.

    Procedimiento:
    1. Construir un DAG corrupto que ``validate_dag`` real rechaza.
    2. Verificar que ``validate_dag(corrupto) is False`` (validación real).
    3. Mutar ``validate_dag`` a ``return True`` (teatro).
    4. Verificar que ahora el corrupto pasa — esto es lo que NO queremos.
       El test documenta que la versión real debe rechazarlo.
    """
    corrupt = _synthetic_dag()
    # Geometría rara: writer_history entry con 3 elementos (no 2).
    corrupt["writer_history"]["src/main.py"].append(
        ["evt-ddd-444", 12, "extra_field"]
    )
    # last_seq como float (no int — bool es subtipo de int, float no).
    corrupt["last_seq"] = 42.5

    # 1. La validación real debe rechazar el corrupto.
    assert validate_dag(corrupt) is False, (
        "validate_dag aceptó un DAG corrupto — la validación es teatro "
        "(Artículo IX). El DAG tiene writer_history entry con 3 elementos "
        "y last_seq como float."
    )

    # 2. Mutar validate_dag a return True (simular teatro).
    import causadb._dag_schema as schema_mod
    monkeypatch.setattr(schema_mod, "validate_dag", lambda dag: True)

    # 3. Con la mutación, el corrupto pasa — esto demuestra que sin la
    #    validación real, el teatro no detectaría la corrupción.
    assert schema_mod.validate_dag(corrupt) is True, (
        "La mutación a return True no tuvo efecto — el test anti-teatro "
        "no está ejerciendo la validación correctamente."
    )

    # 4. Verificación adicional: roundtrip de un DAG corrupto NO debería
    #    ser aceptado por la validación real (restauramos la original).
    #    Usamos una copia profunda para evitar efectos del monkeypatch.
    real_validate = validate_dag.__wrapped__ if hasattr(validate_dag, "__wrapped__") else None
    # Si validate_dag no está wrapped, reimportamos la versión real.
    # (monkeypatch solo afecta schema_mod.validate_dag, no la importación
    # original en este módulo — pytest la cachea al inicio.)
    assert validate_dag(corrupt) is False, (
        "La validación real (importada al inicio del test) aceptó el "
        "corrupto — la validación no está verificando la geometría."
    )