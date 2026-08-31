"""Tests for F.11.2 — Config persistente + workspace discovery (_workspace.py).

Artículo III: Test-first. Artículo IX: Anti-teatro.
"""

import json
import os
import tempfile

import pytest

from causadb._workspace import CausaDBWorkspace, WorkspaceManager, resolve_ledger, NoWorkspaceError
from causadb.cli.main import main


# ---------------------------------------------------------------------------
# WorkspaceManager.discover
# ---------------------------------------------------------------------------


def test_workspace_discover_from_child_dir():
    root = tempfile.mkdtemp()
    causadb_dir = os.path.join(root, ".causadb")
    os.makedirs(causadb_dir)
    config = {"ledger_path": os.path.join(causadb_dir, "ledger.log")}
    with open(os.path.join(causadb_dir, "config.json"), "w") as f:
        json.dump(config, f)
    child = os.path.join(root, "a", "b", "c")
    os.makedirs(child)
    found = WorkspaceManager.discover(child)
    assert found == os.path.join(causadb_dir, "config.json")


def test_workspace_discover_not_found():
    root = tempfile.mkdtemp()
    child = os.path.join(root, "a", "b")
    os.makedirs(child)
    found = WorkspaceManager.discover(child)
    assert found is None


def test_workspace_discover_exact_match():
    root = tempfile.mkdtemp()
    causadb_dir = os.path.join(root, ".causadb")
    os.makedirs(causadb_dir)
    config = {"ledger_path": os.path.join(causadb_dir, "ledger.log")}
    with open(os.path.join(causadb_dir, "config.json"), "w") as f:
        json.dump(config, f)
    found = WorkspaceManager.discover(root)
    assert found == os.path.join(causadb_dir, "config.json")


def test_workspace_discover_skips_config_without_ledger_path():
    """Anti-teatro (Art. IX) + G5.B: un `.causadb/config.json` sin
    `ledger_path` (p.ej. el config global de telemetría `~/.causadb/`)
    NO es un workspace válido — `discover()` debe saltearlo y seguir
    subiendo, no devolverlo (rompería `WorkspaceManager.load()` con
    `TypeError` y a los 10 callers de `discover()`).
    """
    root = tempfile.mkdtemp()
    global_dir = os.path.join(root, ".causadb")
    os.makedirs(global_dir)
    with open(os.path.join(global_dir, "config.json"), "w") as f:
        json.dump({"telemetry": {"enabled": True}}, f)

    # Workspace válido más arriba en el árbol — discover debe encontrarlo.
    project = os.path.join(root, "proyecto")
    project_causadb = os.path.join(project, ".causadb")
    os.makedirs(project_causadb)
    config = {"ledger_path": os.path.join(project_causadb, "ledger.log")}
    with open(os.path.join(project_causadb, "config.json"), "w") as f:
        json.dump(config, f)
    child = os.path.join(project, "a", "b")
    os.makedirs(child)

    found = WorkspaceManager.discover(child)
    assert found == os.path.join(project_causadb, "config.json")


def test_workspace_discover_skips_config_without_ledger_path_only_global():
    """Si el ÚNICO `.causadb/config.json` no tiene `ledger_path`, discover
    retorna None (no crashea load() con TypeError)."""
    root = tempfile.mkdtemp()
    causadb_dir = os.path.join(root, ".causadb")
    os.makedirs(causadb_dir)
    with open(os.path.join(causadb_dir, "config.json"), "w") as f:
        json.dump({"telemetry": {"enabled": True}}, f)
    child = os.path.join(root, "a", "b")
    os.makedirs(child)
    found = WorkspaceManager.discover(child)
    assert found is None


# ---------------------------------------------------------------------------
# WorkspaceManager.load / save
# ---------------------------------------------------------------------------


def test_workspace_save_load_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.json")
        ws = CausaDBWorkspace(
            ledger_path=os.path.join(tmp, "ledger.log"),
            watch_dirs=["/tmp"],
            chronicle_path=os.path.join(tmp, "chronicle.md"),
            daemon_enabled=True,
        )
        WorkspaceManager.save(ws, config_path)
        loaded = WorkspaceManager.load(config_path)
        assert loaded.ledger_path == ws.ledger_path
        assert loaded.watch_dirs == ws.watch_dirs
        assert loaded.chronicle_path == ws.chronicle_path
        assert loaded.daemon_enabled == ws.daemon_enabled


def test_workspace_load_nonexistent_raises():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "nonexistent.json")
        with pytest.raises(FileNotFoundError):
            WorkspaceManager.load(path)


def test_workspace_save_writes_fsync():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.json")
        ws = CausaDBWorkspace(
            ledger_path=os.path.join(tmp, "ledger.log"),
            watch_dirs=[],
            chronicle_path=os.path.join(tmp, "chronicle.md"),
        )
        WorkspaceManager.save(ws, config_path)
        assert os.path.exists(config_path)
        with open(config_path) as f:
            data = json.load(f)
        assert data["ledger_path"] == ws.ledger_path


# ---------------------------------------------------------------------------
# WorkspaceManager.init
# ---------------------------------------------------------------------------


def test_workspace_init_creates_config():
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "myproject")
        result = WorkspaceManager.init(project)
        config_path = os.path.join(project, ".causadb", "config.json")
        assert os.path.exists(config_path)
        assert result["config_path"] == config_path


def test_workspace_init_creates_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "myproject")
        result = WorkspaceManager.init(project)
        ledger_path = result["ledger_path"]
        assert os.path.exists(ledger_path)
        with open(ledger_path) as f:
            line = f.readline()
            assert "SYSTEM_BOOT" in line


# ---------------------------------------------------------------------------
# B.1 — alineación de blob_store_enabled (deuda #21)
# ---------------------------------------------------------------------------


def test_init_blob_store_enabled_defaults_true(tmp_path):
    """init() crea config.json con blob_store_enabled=True por default,
    consistente con CausaDBConfig (_config.py:30)."""
    project = tmp_path / "p"
    WorkspaceManager.init(project)
    config_path = os.path.join(project, ".causadb", "config.json")
    with open(config_path) as f:
        data = json.load(f)
    assert data["blob_store_enabled"] is True


def test_workspace_dataclass_blob_store_default_true(tmp_path):
    """El dataclass CausaDBWorkspace default True, alineado con el runtime."""
    ws = CausaDBWorkspace(ledger_path=str(tmp_path / "ledger.log"))
    assert ws.blob_store_enabled is True


def test_init_honors_explicit_blob_store_false(tmp_path):
    """Un blob_store_enabled=False explícito se respeta en el config.json."""
    project = tmp_path / "p"
    WorkspaceManager.init(project, blob_store_enabled=False)
    config_path = os.path.join(project, ".causadb", "config.json")
    with open(config_path) as f:
        data = json.load(f)
    assert data["blob_store_enabled"] is False


# ---------------------------------------------------------------------------
# Anti-teatro
# ---------------------------------------------------------------------------


def test_anti_teatro_discover_returns_none_on_empty_parent():
    root = tempfile.mkdtemp()
    found = WorkspaceManager.discover(root)
    assert found is None


def test_anti_teatro_init_without_ledger_no_file():
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "noproject")
        os.makedirs(project)
        found = WorkspaceManager.discover(project)
        assert found is None


# ---------------------------------------------------------------------------
# Config CLI integration
# ---------------------------------------------------------------------------


def test_config_cli_path_shows_config_path(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        # Init workspace
        rc_init = main(["init", project])
        assert rc_init == 0
        # Run config path from project dir
        cwd = os.getcwd()
        os.chdir(project)
        try:
            rc, _ = 1, ""
            rc = main(["config", "path"])
            assert rc == 0, f"config path failed: {rc}"
        finally:
            os.chdir(cwd)


def test_config_cli_get_returns_json(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        rc_init = main(["init", project])
        assert rc_init == 0
        cwd = os.getcwd()
        os.chdir(project)
        try:
            rc = main(["config", "get"])
            assert rc == 0
        finally:
            os.chdir(cwd)


def test_config_cli_set_updates_config(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        rc_init = main(["init", project])
        assert rc_init == 0
        cwd = os.getcwd()
        os.chdir(project)
        try:
            rc = main(["config", "set", "daemon_enabled", "true"])
            assert rc == 0
            ws = WorkspaceManager.load(
                os.path.join(project, ".causadb", "config.json")
            )
            assert ws.daemon_enabled is True
        finally:
            os.chdir(cwd)


def test_config_cli_init_via_config(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "cfg-proj")
        rc = main(["config", "init", "--path", project])
        assert rc == 0
        config_path = os.path.join(project, ".causadb", "config.json")
        assert os.path.exists(config_path)


# ---------------------------------------------------------------------------
# CLI without --ledger — workspace discovery
# ---------------------------------------------------------------------------


def test_vigilante_start_without_ledger_uses_discovery(capsys):
    """Verify that vigilante start works without --ledger if .causadb/ exists."""
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        rc_init = main(["init", project])
        assert rc_init == 0
        cwd = os.getcwd()
        os.chdir(project)
        try:
            # Should discover .causadb/ and start (returns quickly
            # because --daemon is not set — starts as thread).
            rc = main(["vigilante", "start"])
            assert rc == 0
            # Stop it
            rc = main(["vigilante", "stop"])
            assert rc == 0
        finally:
            os.chdir(cwd)


def test_vigilante_without_ledger_no_workspace_errors(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            rc = main(["vigilante", "start"])
            assert rc != 0
        finally:
            os.chdir(cwd)


def test_resolve_ledger_raises_no_workspace():
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with pytest.raises(NoWorkspaceError):
                resolve_ledger()
        finally:
            os.chdir(cwd)


def test_resolve_ledger_returns_explicit():
    path = resolve_ledger("/tmp/explicit/ledger.log")
    assert path == "/tmp/explicit/ledger.log"
