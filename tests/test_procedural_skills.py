"""Tests for procedural skills registration (TDD — RED phase).

These tests MUST fail before implementation. They validate:
1. Procedural skills register with correct fields
2. Dedup works (upsert by skill_name)
3. Setup integration calls register_skill for each SKILL.md
4. --no-skills flag skips registration
5. Error handling doesn't block setup
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Test 1: register_skill works for procedural type
# ---------------------------------------------------------------------------

def test_procedural_skills_register_correctly(tmp_path):
    """Las 2 procedural skills se registran con campos correctos."""
    ledger = tmp_path / "ledger.log"
    ledger.touch()
    from causadb._skill_registry import register_skill, load_skills

    # Registrar skill 1
    skill1_id = register_skill(str(ledger), {
        "skill_type": "procedural",
        "skill_name": "state-reconstruction",
        "content": "# State Reconstruction\n\nPatrones P1-P9...",
        "token_count": 100,
        "confidence": 1.0,
        "source_session": "setup",
    })
    assert skill1_id, "skill1_id must not be empty"

    # Registrar skill 2
    skill2_id = register_skill(str(ledger), {
        "skill_type": "procedural",
        "skill_name": "shared-workspace",
        "content": "# Shared Workspace\n\nCoordinación multi-agente...",
        "token_count": 50,
        "confidence": 1.0,
        "source_session": "setup",
    })
    assert skill2_id, "skill2_id must not be empty"

    # Verificar que se listan
    skills = load_skills(str(ledger), types=["procedural"])
    assert len(skills) == 2, f"Expected 2 procedural skills, got {len(skills)}"
    names = [s["skill_name"] for s in skills]
    assert "state-reconstruction" in names
    assert "shared-workspace" in names


# ---------------------------------------------------------------------------
# Test 2: dedup (upsert by skill_name)
# ---------------------------------------------------------------------------

def test_procedural_skills_dedup(tmp_path):
    """Registrar la misma skill 2 veces no duplica."""
    ledger = tmp_path / "ledger.log"
    ledger.touch()
    from causadb._skill_registry import register_skill, load_skills

    skill_dict = {
        "skill_type": "procedural",
        "skill_name": "state-reconstruction",
        "content": "# State Reconstruction",
        "token_count": 100,
        "confidence": 1.0,
        "source_session": "setup",
    }

    register_skill(str(ledger), skill_dict)
    register_skill(str(ledger), skill_dict)  # duplicate

    skills = load_skills(str(ledger), types=["procedural"])
    assert len(skills) == 1, f"Expected 1 skill after dedup, got {len(skills)}"


# ---------------------------------------------------------------------------
# Test 3: setup integration — registers procedural skills
# ---------------------------------------------------------------------------

def test_setup_registers_procedural_skills(tmp_path, monkeypatch):
    """causadb setup registra las procedural skills al pasar por Step 7."""
    # Create a fake workspace
    ws_dir = tmp_path / "project"
    ws_dir.mkdir()
    causadb_dir = ws_dir / ".causadb"
    causadb_dir.mkdir()
    ledger_file = causadb_dir / "ledger.log"
    ledger_file.touch()
    config_file = causadb_dir / "config.json"
    config_file.write_text(json.dumps({
        "ledger_path": str(ledger_file),
        "workspace_dir": str(ws_dir),
    }))

    # Create skills directory with one SKILL.md
    skills_dir = ws_dir / "causadb" / "skills" / "state-reconstruction"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("# State Reconstruction\n\nTest content.")

    # Mock WorkspaceManager.discover to return our config
    monkeypatch.setattr(
        "causadb._workspace.WorkspaceManager.discover",
        lambda project_dir: str(config_file),
    )
    monkeypatch.setattr(
        "causadb._workspace.WorkspaceManager.load",
        lambda config_path: SimpleNamespace(
            ledger_path=str(ledger_file),
            workspace_dir=str(ws_dir),
        ),
    )

    # Mock register_skill to track calls
    registered_skills = []
    original_register = None

    def mock_register(ledger_path, skill_dict, config=None):
        registered_skills.append(skill_dict)
        return "mock-skill-id"

    monkeypatch.setattr(
        "causadb._skill_registry.register_skill",
        mock_register,
    )

    # Mock other setup steps to avoid side effects
    monkeypatch.setattr(
        "causadb._shell_hook.install",
        lambda **kwargs: True,
    )
    monkeypatch.setattr(
        "causadb._git_hook.install_post_commit_hook",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "causadb._git_hook.git_dir_from_workspace",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "causadb._telemetry.set_enabled",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "causadb._daemon_service.install_service",
        lambda *args: (True, "ok"),
    )
    monkeypatch.setattr(
        "causadb._daemon_service.start_service",
        lambda: (True, "ok"),
    )

    # Import and run setup
    from causadb.cli._cmd_setup import cmd_setup
    args = SimpleNamespace(
        project_dir=str(ws_dir),
        no_hook=True,
        no_git=True,
        no_watch=True,
        no_daemon=True,
        integrations=None,
    )

    exit_code, output = cmd_setup(args)
    results = json.loads(output)

    # Verify setup succeeded
    assert exit_code == 0, f"Setup failed: {output}"

    # Verify procedural_skills step exists and has status "ok"
    assert "procedural_skills" in results["steps"], \
        f"Missing procedural_skills step. Steps: {list(results['steps'].keys())}"
    assert results["steps"]["procedural_skills"]["status"] == "ok", \
        f"procedural_skills status: {results['steps']['procedural_skills']}"

    # Verify register_skill was called (at least once for state-reconstruction)
    assert len(registered_skills) >= 1, "register_skill was not called"
    skill_names = [s["skill_name"] for s in registered_skills]
    assert "state-reconstruction" in skill_names, \
        f"state-reconstruction not registered. Got: {skill_names}"


# ---------------------------------------------------------------------------
# Test 4: --no-skills flag skips registration
# ---------------------------------------------------------------------------

def test_setup_no_skills_flag_skips(tmp_path, monkeypatch):
    """Flag --no-skills skippa el registro de procedural skills."""
    ws_dir = tmp_path / "project"
    ws_dir.mkdir()
    causadb_dir = ws_dir / ".causadb"
    causadb_dir.mkdir()
    ledger_file = causadb_dir / "ledger.log"
    ledger_file.touch()
    config_file = causadb_dir / "config.json"
    config_file.write_text(json.dumps({
        "ledger_path": str(ledger_file),
        "workspace_dir": str(ws_dir),
    }))

    monkeypatch.setattr(
        "causadb._workspace.WorkspaceManager.discover",
        lambda project_dir: str(config_file),
    )
    monkeypatch.setattr(
        "causadb._workspace.WorkspaceManager.load",
        lambda config_path: SimpleNamespace(
            ledger_path=str(ledger_file),
            workspace_dir=str(ws_dir),
        ),
    )
    monkeypatch.setattr("causadb._shell_hook.install", lambda **kwargs: True)
    monkeypatch.setattr("causadb._git_hook.install_post_commit_hook", lambda *a, **k: True)
    monkeypatch.setattr("causadb._git_hook.git_dir_from_workspace", lambda *a: None)
    monkeypatch.setattr("causadb._telemetry.set_enabled", lambda *a: None)
    monkeypatch.setattr("causadb._daemon_service.install_service", lambda *a: (True, "ok"))
    monkeypatch.setattr("causadb._daemon_service.start_service", lambda: (True, "ok"))

    from causadb.cli._cmd_setup import cmd_setup
    args = SimpleNamespace(
        project_dir=str(ws_dir),
        no_hook=True,
        no_git=True,
        no_watch=True,
        no_daemon=True,
        no_skills=True,  # <-- KEY FLAG
        integrations=None,
    )

    exit_code, output = cmd_setup(args)
    results = json.loads(output)

    # Verify procedural_skills step exists with status "skipped"
    assert "procedural_skills" in results["steps"]
    assert results["steps"]["procedural_skills"]["status"] == "skipped"
    assert "no-skills" in results["steps"]["procedural_skills"]["detail"].lower()


# ---------------------------------------------------------------------------
# Test 5: error in procedural skills doesn't block setup
# ---------------------------------------------------------------------------

def test_setup_procedural_skills_error_doesnt_block(tmp_path, monkeypatch):
    """Si register_skill falla, setup igual completa (degradación suave)."""
    ws_dir = tmp_path / "project"
    ws_dir.mkdir()
    causadb_dir = ws_dir / ".causadb"
    causadb_dir.mkdir()
    ledger_file = causadb_dir / "ledger.log"
    ledger_file.touch()
    config_file = causadb_dir / "config.json"
    config_file.write_text(json.dumps({
        "ledger_path": str(ledger_file),
        "workspace_dir": str(ws_dir),
    }))

    # Create skills dir that will cause an error
    skills_dir = ws_dir / "causadb" / "skills"
    skills_dir.mkdir(parents=True)

    monkeypatch.setattr(
        "causadb._workspace.WorkspaceManager.discover",
        lambda project_dir: str(config_file),
    )
    monkeypatch.setattr(
        "causadb._workspace.WorkspaceManager.load",
        lambda config_path: SimpleNamespace(
            ledger_path=str(ledger_file),
            workspace_dir=str(ws_dir),
        ),
    )
    monkeypatch.setattr("causadb._shell_hook.install", lambda **kwargs: True)
    monkeypatch.setattr("causadb._git_hook.install_post_commit_hook", lambda *a, **k: True)
    monkeypatch.setattr("causadb._git_hook.git_dir_from_workspace", lambda *a: None)
    monkeypatch.setattr("causadb._telemetry.set_enabled", lambda *a: None)
    monkeypatch.setattr("causadb._daemon_service.install_service", lambda *a: (True, "ok"))
    monkeypatch.setattr("causadb._daemon_service.start_service", lambda: (True, "ok"))

    # Make register_skill raise
    def failing_register(*args, **kwargs):
        raise RuntimeError("Simulated register failure")

    monkeypatch.setattr(
        "causadb._skill_registry.register_skill",
        failing_register,
    )

    from causadb.cli._cmd_setup import cmd_setup
    args = SimpleNamespace(
        project_dir=str(ws_dir),
        no_hook=True,
        no_git=True,
        no_watch=True,
        no_daemon=True,
        integrations=None,
    )

    exit_code, output = cmd_setup(args)
    results = json.loads(output)

    # Setup should still succeed overall
    assert exit_code == 0, f"Setup failed when it shouldn't: {output}"

    # procedural_skills step should report error, not crash
    assert "procedural_skills" in results["steps"]
    assert results["steps"]["procedural_skills"]["status"] == "error"
    assert "Simulated register failure" in results["steps"]["procedural_skills"]["detail"]
