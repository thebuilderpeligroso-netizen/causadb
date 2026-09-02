"""Tests for MCP shared_document_read / shared_document_write tools (F.14).

Test-First discipline (Article III): these tests were written BEFORE the
implementation. They verify that the tools work correctly.
"""
import anyio
import pytest
import json
import tempfile
import os

from causadb.mcp.server import create_server
from causadb._workspace import WorkspaceManager
from tests.helpers._mcp_call import _call_tool, _error_message


def _create_test_workspace(tmp_path):
    """Crea un workspace real para testing."""
    project_dir = str(tmp_path / "test_project")
    result = WorkspaceManager.init(project_dir)
    return result["ledger_path"]


def _text(content_blocks):
    return "".join(getattr(b, "text", str(b)) for b in content_blocks)


def test_shared_document_read_returns_template_when_missing(tmp_path):
    """shared_document_read retorna plantilla si documento no existe."""
    ledger_path = _create_test_workspace(tmp_path)
    server = create_server(config_ledger_path=ledger_path)

    content_blocks, _ = _call_tool(server, "shared_document_read", {"name": "AUDIT_REPORT"})
    doc = json.loads(_text(content_blocks))

    assert doc["tipo"] == "AUDIT_REPORT"
    assert doc["estado"] == "BORRADOR"
    assert doc["version"] == 1


def test_shared_document_read_reads_existing(tmp_path):
    """shared_document_read lee documento existente."""
    ledger_path = _create_test_workspace(tmp_path)
    server = create_server(config_ledger_path=ledger_path)

    # Escribir primero
    content = json.dumps({
        "tipo": "AUDIT_REPORT",
        "resumen": "Test resumen",
        "hallazgos": ["hallazgo 1", "hallazgo 2"],
        "estado": "APROBADO",
    })
    _call_tool(server, "shared_document_write", {"name": "AUDIT_REPORT", "content": content})

    # Leer
    content_blocks, _ = _call_tool(server, "shared_document_read", {"name": "AUDIT_REPORT"})
    doc = json.loads(_text(content_blocks))

    assert doc["resumen"] == "Test resumen"
    assert doc["hallazgos"] == ["hallazgo 1", "hallazgo 2"]
    assert doc["estado"] == "APROBADO"
    assert "actualizado_en" in doc


def test_shared_document_write_creates_document(tmp_path):
    """shared_document_write crea documento."""
    ledger_path = _create_test_workspace(tmp_path)
    server = create_server(config_ledger_path=ledger_path)

    content = json.dumps({
        "tipo": "ACTION_PLAN",
        "objetivo": "Fix bug",
        "causa_identificada": "Race condition",
        "cambios_propuestos": ["file1.py"],
        "tests_red": ["test_race.py"],
        "tests_green": [],
        "riesgos": [],
        "solicitud_al_auditor": "APROBAR",
    })

    content_blocks, _ = _call_tool(server, "shared_document_write", {"name": "ACTION_PLAN", "content": content})
    response = json.loads(_text(content_blocks))

    assert response["status"] == "ok"
    assert response["name"] == "ACTION_PLAN"

    # Verificar que se puede leer
    content_blocks, _ = _call_tool(server, "shared_document_read", {"name": "ACTION_PLAN"})
    doc = json.loads(_text(content_blocks))
    assert doc["objetivo"] == "Fix bug"
    assert doc["causa_identificada"] == "Race condition"


def test_shared_document_write_rejects_invalid_name(tmp_path):
    """shared_document_write rechaza nombre inválido."""
    ledger_path = _create_test_workspace(tmp_path)
    server = create_server(config_ledger_path=ledger_path)

    with pytest.raises(Exception) as exc_info:
        _call_tool(server, "shared_document_write", {"name": "INVALID", "content": "{}"})

    assert "Nombre no permitido" in _error_message(exc_info.value)


def test_shared_document_read_rejects_invalid_name(tmp_path):
    """shared_document_read rechaza nombre inválido."""
    ledger_path = _create_test_workspace(tmp_path)
    server = create_server(config_ledger_path=ledger_path)

    with pytest.raises(Exception) as exc_info:
        _call_tool(server, "shared_document_read", {"name": "INVALID"})

    assert "Nombre no permitido" in _error_message(exc_info.value)


def test_shared_document_write_rejects_invalid_json(tmp_path):
    """shared_document_write rechaza JSON inválido."""
    ledger_path = _create_test_workspace(tmp_path)
    server = create_server(config_ledger_path=ledger_path)

    with pytest.raises(Exception) as exc_info:
        _call_tool(server, "shared_document_write", {"name": "AUDIT_REPORT", "content": "not json"})

    assert "JSON inválido" in _error_message(exc_info.value)


def test_shared_document_write_validates_tipo(tmp_path):
    """shared_document_write valida que tipo coincida con nombre."""
    ledger_path = _create_test_workspace(tmp_path)
    server = create_server(config_ledger_path=ledger_path)

    # tipo incorrecto
    with pytest.raises(Exception) as exc_info:
        _call_tool(server, "shared_document_write", {"name": "AUDIT_REPORT",
                   "content": json.dumps({"tipo": "ACTION_PLAN"})})

    assert "Campo 'tipo' obligatorio" in _error_message(exc_info.value)

    # tipo faltante
    with pytest.raises(Exception) as exc_info:
        _call_tool(server, "shared_document_write", {"name": "AUDIT_REPORT",
                   "content": json.dumps({"resumen": "test"})})

    assert "Campo 'tipo' obligatorio" in _error_message(exc_info.value)


def test_shared_document_roundtrip(tmp_path):
    """Roundtrip completo: write -> read -> verify."""
    ledger_path = _create_test_workspace(tmp_path)
    server = create_server(config_ledger_path=ledger_path)

    original = {
        "tipo": "ACTION_PLAN",
        "objetivo": "Implement feature X",
        "causa_identificada": "Missing functionality",
        "cambios_propuestos": ["feature_x.py", "test_feature_x.py"],
        "tests_red": ["test_feature_x.py::test_new"],
        "tests_green": [],
        "riesgos": ["breaking change"],
        "solicitud_al_auditor": "APROBAR",
        "notas_libres": "Nota del coder",
    }

    _call_tool(server, "shared_document_write", {"name": "ACTION_PLAN", "content": json.dumps(original)})
    content_blocks, _ = _call_tool(server, "shared_document_read", {"name": "ACTION_PLAN"})
    read_back = json.loads(_text(content_blocks))

    assert read_back["objetivo"] == "Implement feature X"
    assert read_back["causa_identificada"] == "Missing functionality"
    assert read_back["cambios_propuestos"] == ["feature_x.py", "test_feature_x.py"]
    assert read_back["tests_red"] == ["test_feature_x.py::test_new"]
    assert read_back["solicitud_al_auditor"] == "APROBAR"
    assert read_back["notas_libres"] == "Nota del coder"
    assert "actualizado_en" in read_back


def test_shared_document_overwrite(tmp_path):
    """shared_document_write sobrescribe documento existente."""
    ledger_path = _create_test_workspace(tmp_path)
    server = create_server(config_ledger_path=ledger_path)

    # Primera escritura
    _call_tool(server, "shared_document_write", {"name": "AUDIT_REPORT",
               "content": json.dumps({"tipo": "AUDIT_REPORT", "resumen": "v1"})})

    # Segunda escritura
    _call_tool(server, "shared_document_write", {"name": "AUDIT_REPORT",
               "content": json.dumps({"tipo": "AUDIT_REPORT", "resumen": "v2"})})

    # Leer - debe tener v2
    content_blocks, _ = _call_tool(server, "shared_document_read", {"name": "AUDIT_REPORT"})
    doc = json.loads(_text(content_blocks))
    assert doc["resumen"] == "v2"


def test_both_documents_independent(tmp_path):
    """AUDIT_REPORT y ACTION_PLAN son independientes."""
    ledger_path = _create_test_workspace(tmp_path)
    server = create_server(config_ledger_path=ledger_path)

    _call_tool(server, "shared_document_write", {"name": "AUDIT_REPORT",
               "content": json.dumps({"tipo": "AUDIT_REPORT", "resumen": "audit summary"})})
    _call_tool(server, "shared_document_write", {"name": "ACTION_PLAN",
               "content": json.dumps({"tipo": "ACTION_PLAN", "objetivo": "plan objective"})})

    content_blocks, _ = _call_tool(server, "shared_document_read", {"name": "AUDIT_REPORT"})
    audit = json.loads(_text(content_blocks))
    content_blocks, _ = _call_tool(server, "shared_document_read", {"name": "ACTION_PLAN"})
    action = json.loads(_text(content_blocks))

    assert audit["resumen"] == "audit summary"
    assert action["objetivo"] == "plan objective"
    assert audit["tipo"] == "AUDIT_REPORT"
    assert action["tipo"] == "ACTION_PLAN"