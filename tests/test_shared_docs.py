import pytest
import json
import os
import tempfile
from causadb._shared_docs import (
    get_template,
    ensure_shared_docs,
    read_shared_doc,
    write_shared_doc,
    ALLOWED_NAMES,
    DEFAULT_TEMPLATES,
)


def test_templates_exist():
    """Plantillas por defecto existen para ambos nombres."""
    assert "AUDIT_REPORT" in DEFAULT_TEMPLATES
    assert "ACTION_PLAN" in DEFAULT_TEMPLATES
    assert ALLOWED_NAMES == frozenset({"AUDIT_REPORT", "ACTION_PLAN"})


def test_get_template_returns_copy():
    """get_template retorna copia profunda (no la misma referencia)."""
    t1 = get_template("AUDIT_REPORT")
    t2 = get_template("AUDIT_REPORT")
    assert t1 is not t2
    assert t1 == t2
    # Modificar t1 no afecta t2
    t1["resumen_ejecutivo"] = "modificado"
    assert t2["resumen_ejecutivo"] == ""


def test_get_template_rejects_invalid_name():
    """get_template rechaza nombre inválido."""
    with pytest.raises(ValueError, match="Nombre no permitido"):
        get_template("INVALID")


def test_ensure_shared_docs_creates_files(tmp_path):
    """ensure_shared_docs crea directorio y archivos si no existen."""
    ledger_path = str(tmp_path / ".causadb" / "ledger.log")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)

    ensure_shared_docs(ledger_path)

    audit_path = tmp_path / ".causadb" / "coordination" / "AUDIT_REPORT.json"
    action_path = tmp_path / ".causadb" / "coordination" / "ACTION_PLAN.json"

    assert audit_path.exists()
    assert action_path.exists()

    # Verificar contenido
    with open(audit_path) as f:
        audit = json.load(f)
    assert audit["tipo"] == "AUDIT_REPORT"
    assert audit["estado"] == "BORRADOR"
    assert "fecha_auditoria" in audit
    assert "auditor" in audit
    assert "alcance" in audit
    assert "criterio_auditoria" in audit
    assert "plan_auditado" in audit
    assert "resumen_ejecutivo" in audit
    assert "hallazgos" in audit
    assert "evidencia" in audit
    assert "conclusion" in audit
    assert "recomendaciones" in audit

    with open(action_path) as f:
        action = json.load(f)
    assert action["tipo"] == "ACTION_PLAN"
    assert action["solicitud_al_auditor"] == "APROBAR / OBJETAR"


def test_ensure_shared_docs_idempotent(tmp_path):
    """ensure_shared_docs no sobrescribe archivos existentes."""
    ledger_path = str(tmp_path / ".causadb" / "ledger.log")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)

    ensure_shared_docs(ledger_path)

    # Modificar uno
    audit_path = tmp_path / ".causadb" / "coordination" / "AUDIT_REPORT.json"
    with open(audit_path) as f:
        data = json.load(f)
    data["resumen_ejecutivo"] = "modificado por test"
    with open(audit_path, "w") as f:
        json.dump(data, f)

    # Llamar de nuevo
    ensure_shared_docs(ledger_path)

    # Debe conservar la modificación
    with open(audit_path) as f:
        data = json.load(f)
    assert data["resumen_ejecutivo"] == "modificado por test"


def test_read_shared_doc_returns_template_when_missing(tmp_path):
    """read_shared_doc retorna plantilla si archivo no existe."""
    ledger_path = str(tmp_path / ".causadb" / "ledger.log")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)

    doc = read_shared_doc(ledger_path, "AUDIT_REPORT")
    assert doc["tipo"] == "AUDIT_REPORT"
    assert doc["estado"] == "BORRADOR"


def test_read_shared_doc_reads_existing(tmp_path):
    """read_shared_doc lee archivo existente."""
    ledger_path = str(tmp_path / ".causadb" / "ledger.log")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)

    ensure_shared_docs(ledger_path)

    # Escribir algo
    write_shared_doc(ledger_path, "AUDIT_REPORT", {
        "tipo": "AUDIT_REPORT",
        "resumen_ejecutivo": "test resumen",
        "hallazgos": ["hallazgo 1"],
        "estado": "APROBADO",
    })

    doc = read_shared_doc(ledger_path, "AUDIT_REPORT")
    assert doc["resumen_ejecutivo"] == "test resumen"
    assert doc["hallazgos"] == ["hallazgo 1"]
    assert doc["estado"] == "APROBADO"


def test_read_shared_doc_rejects_invalid_name(tmp_path):
    """read_shared_doc rechaza nombre inválido."""
    ledger_path = str(tmp_path / ".causadb" / "ledger.log")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)

    with pytest.raises(ValueError, match="Nombre no permitido"):
        read_shared_doc(ledger_path, "INVALID")


def test_write_shared_doc_validates_name(tmp_path):
    """write_shared_doc valida nombre."""
    ledger_path = str(tmp_path / ".causadb" / "ledger.log")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)

    with pytest.raises(ValueError, match="Nombre no permitido"):
        write_shared_doc(ledger_path, "INVALID", {"tipo": "INVALID"})


def test_write_shared_doc_validates_tipo(tmp_path):
    """write_shared_doc valida que tipo coincida con nombre."""
    ledger_path = str(tmp_path / ".causadb" / "ledger.log")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)

    # tipo incorrecto
    with pytest.raises(ValueError, match="Campo 'tipo' obligatorio"):
        write_shared_doc(ledger_path, "AUDIT_REPORT", {"tipo": "ACTION_PLAN"})

    # tipo faltante
    with pytest.raises(ValueError, match="Campo 'tipo' obligatorio"):
        write_shared_doc(ledger_path, "AUDIT_REPORT", {"resumen": "test"})


def test_write_shared_doc_atomic_write(tmp_path):
    """write_shared_doc escribe atómicamente (fsync + replace)."""
    ledger_path = str(tmp_path / ".causadb" / "ledger.log")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)

    write_shared_doc(ledger_path, "AUDIT_REPORT", {
        "tipo": "AUDIT_REPORT",
        "resumen_ejecutivo": "test atomic",
    })

    audit_path = tmp_path / ".causadb" / "coordination" / "AUDIT_REPORT.json"
    assert audit_path.exists()

    with open(audit_path) as f:
        data = json.load(f)
    assert data["resumen_ejecutivo"] == "test atomic"
    assert "actualizado_en" in data
    assert data["actualizado_en"] != ""


def test_write_shared_doc_updates_timestamp(tmp_path):
    """write_shared_doc actualiza actualizado_en en cada escritura."""
    ledger_path = str(tmp_path / ".causadb" / "ledger.log")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)

    write_shared_doc(ledger_path, "AUDIT_REPORT", {
        "tipo": "AUDIT_REPORT",
        "resumen_ejecutivo": "primera",
    })

    import time
    time.sleep(0.01)  # asegurar timestamp diferente

    write_shared_doc(ledger_path, "AUDIT_REPORT", {
        "tipo": "AUDIT_REPORT",
        "resumen_ejecutivo": "segunda",
    })

    doc = read_shared_doc(ledger_path, "AUDIT_REPORT")
    assert doc["resumen_ejecutivo"] == "segunda"
    assert doc["actualizado_en"] != ""


def test_roundtrip_read_write(tmp_path):
    """Roundtrip completo: write -> read -> verify."""
    ledger_path = str(tmp_path / ".causadb" / "ledger.log")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)

    original = {
        "tipo": "ACTION_PLAN",
        "objetivo": "Fix bug X",
        "causa_raiz": "Race condition",
        "bit_relacionado": "BIT-CHR.XX",
        "cambios_propuestos": [{"archivo": "archivo1.py", "cambio": "fix", "justificacion": "race condition"}],
        "tests_red": ["test_race.py::test_concurrent"],
        "tests_green": [],
        "mutantes_controlados": [],
        "riesgos": [{"descripcion": "puede afectar performance", "probabilidad": "media", "impacto": "alto", "mitigacion": "benchmark"}],
        "dependencias": [],
        "solicitud_al_auditor": "APROBAR",
    }

    write_shared_doc(ledger_path, "ACTION_PLAN", original)
    read_back = read_shared_doc(ledger_path, "ACTION_PLAN")

    assert read_back["objetivo"] == "Fix bug X"
    assert read_back["causa_raiz"] == "Race condition"
    assert read_back["bit_relacionado"] == "BIT-CHR.XX"
    assert read_back["cambios_propuestos"] == [{"archivo": "archivo1.py", "cambio": "fix", "justificacion": "race condition"}]
    assert read_back["tests_red"] == ["test_race.py::test_concurrent"]
    assert read_back["solicitud_al_auditor"] == "APROBAR"
    assert "actualizado_en" in read_back


def test_notas_libres_preserved(tmp_path):
    """notas_libres se preserva en roundtrip."""
    ledger_path = str(tmp_path / ".causadb" / "ledger.log")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)

    write_shared_doc(ledger_path, "AUDIT_REPORT", {
        "tipo": "AUDIT_REPORT",
        "notas_libres": "Nota libre del auditor",
    })

    doc = read_shared_doc(ledger_path, "AUDIT_REPORT")
    assert doc["notas_libres"] == "Nota libre del auditor"