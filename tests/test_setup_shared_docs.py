"""Tests for setup shared_docs step (F.14).

This test file is separate to avoid interference from the autouse fixture
in test_setup.py which mocks WorkspaceManager methods.
"""
import pytest
import json
import os
from unittest.mock import MagicMock

from causadb.cli._cmd_setup import cmd_setup
from causadb._workspace import WorkspaceManager


@pytest.mark.unit
@pytest.mark.timeout(30)
def test_setup_shared_docs_step(tmp_path):
    """Test que el step shared_docs se ejecuta y reporta ok."""
    # Usar un directorio temporal real para que ensure_shared_docs cree archivos
    test_dir = str(tmp_path / "test_project")
    os.makedirs(test_dir, exist_ok=True)

    mock_args = MagicMock()
    mock_args.project_dir = test_dir
    mock_args.no_hook = False
    mock_args.no_git = False
    mock_args.no_watch = False
    mock_args.integrations = None
    mock_args.no_daemon = True

    # No mockear nada - usar implementación real
    code, output = cmd_setup(mock_args)
    data = json.loads(output)

    assert code == 0
    assert "shared_docs" in data["steps"]
    assert data["steps"]["shared_docs"]["status"] == "ok"
    assert "AUDIT_REPORT" in data["steps"]["shared_docs"]["detail"]
    assert "ACTION_PLAN" in data["steps"]["shared_docs"]["detail"]

    # Verificar que los archivos se crearon
    audit_path = os.path.join(test_dir, ".causadb", "coordination", "AUDIT_REPORT.json")
    action_path = os.path.join(test_dir, ".causadb", "coordination", "ACTION_PLAN.json")
    assert os.path.exists(audit_path)
    assert os.path.exists(action_path)

    # Verificar contenido
    with open(audit_path) as f:
        audit = json.load(f)
    assert audit["tipo"] == "AUDIT_REPORT"
    assert audit["estado"] == "BORRADOR"

    with open(action_path) as f:
        action = json.load(f)
    assert action["tipo"] == "ACTION_PLAN"
    assert action["solicitud_al_auditor"] == "APROBAR / OBJETAR"