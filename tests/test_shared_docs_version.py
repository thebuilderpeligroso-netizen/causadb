"""TDD RED: versionado optimista + corrupto estricto para shared docs.

Specs auditadas:
- version:int (docs viejos default 0, se acepta 1 write para migrar)
- write(name, content, base_version) -> 409 stale si base != current
- incremento atomico bajo _atomic_write existente
- corrupto -> ValueError (NO plantilla silenciosa)
- inexistente en read -> plantilla (solo read)
"""
import json
import os

import pytest

from causadb._shared_docs import read_shared_doc, write_shared_doc


def _ledger(tmp_path):
    ledger_path = str(tmp_path / ".causadb" / "ledger.log")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
    return ledger_path


def _coord_path(tmp_path, name):
    return tmp_path / ".causadb" / "coordination" / f"{name}.json"


def test_write_write_increments_version(tmp_path):
    """write-write: segundo write con base fresca incrementa version."""
    ledger_path = _ledger(tmp_path)
    write_shared_doc(ledger_path, "AUDIT_REPORT",
                     {"tipo": "AUDIT_REPORT", "resumen_ejecutivo": "v1"},
                     base_version=0)
    doc1 = read_shared_doc(ledger_path, "AUDIT_REPORT")
    assert isinstance(doc1["version"], int)
    assert doc1["version"] == 1

    write_shared_doc(ledger_path, "AUDIT_REPORT",
                     {"tipo": "AUDIT_REPORT", "resumen_ejecutivo": "v2"},
                     base_version=1)
    doc2 = read_shared_doc(ledger_path, "AUDIT_REPORT")
    assert doc2["version"] == 2
    assert doc2["resumen_ejecutivo"] == "v2"


def test_write_stale_base_rejected_409(tmp_path):
    """base vieja rechazada: 409 stale si base != current."""
    ledger_path = _ledger(tmp_path)
    write_shared_doc(ledger_path, "AUDIT_REPORT",
                     {"tipo": "AUDIT_REPORT", "resumen_ejecutivo": "v1"},
                     base_version=0)
    with pytest.raises(ValueError, match="409|stale"):
        write_shared_doc(ledger_path, "AUDIT_REPORT",
                         {"tipo": "AUDIT_REPORT", "resumen_ejecutivo": "stale"},
                         base_version=0)
    # el documento no se modifico
    doc = read_shared_doc(ledger_path, "AUDIT_REPORT")
    assert doc["resumen_ejecutivo"] == "v1"
    assert doc["version"] == 1


def test_read_corrupt_raises(tmp_path):
    """corrupto levanta ValueError (NO plantilla silenciosa)."""
    ledger_path = _ledger(tmp_path)
    write_shared_doc(ledger_path, "AUDIT_REPORT",
                     {"tipo": "AUDIT_REPORT"},
                     base_version=0)
    path = _coord_path(tmp_path, "AUDIT_REPORT")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{json corrupto!!!")
    with pytest.raises(ValueError):
        read_shared_doc(ledger_path, "AUDIT_REPORT")


def test_read_missing_returns_template(tmp_path):
    """missing retorna plantilla (solo read)."""
    ledger_path = _ledger(tmp_path)
    doc = read_shared_doc(ledger_path, "AUDIT_REPORT")
    assert doc["tipo"] == "AUDIT_REPORT"
    assert doc["estado"] == "BORRADOR"


def test_old_doc_without_version_migrates_once(tmp_path):
    """docs viejos default 0: se acepta 1 write para migrar."""
    ledger_path = _ledger(tmp_path)
    # simular doc viejo sin campo version
    coord = tmp_path / ".causadb" / "coordination"
    coord.mkdir(parents=True, exist_ok=True)
    with open(coord / "AUDIT_REPORT.json", "w", encoding="utf-8") as f:
        json.dump({"tipo": "AUDIT_REPORT", "resumen_ejecutivo": "viejo"},
                  f)
    doc = read_shared_doc(ledger_path, "AUDIT_REPORT")
    assert doc.get("version", 0) == 0
    write_shared_doc(ledger_path, "AUDIT_REPORT",
                     {"tipo": "AUDIT_REPORT", "resumen_ejecutivo": "migrado"},
                     base_version=0)
    doc2 = read_shared_doc(ledger_path, "AUDIT_REPORT")
    assert doc2["version"] == 1
