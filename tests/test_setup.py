import pytest
import json
import os
from causadb.cli._cmd_setup import cmd_setup
from causadb._workspace import WorkspaceManager
from unittest.mock import MagicMock

@pytest.fixture
def mock_args():
    args = MagicMock()
    args.project_dir = None
    args.no_hook = False
    args.no_git = False
    args.no_watch = False
    args.integrations = None
    return args

@pytest.fixture(autouse=True)
def patch_all(monkeypatch):
    monkeypatch.setattr("causadb._workspace.WorkspaceManager.init", lambda p: {"ledger_path": "/tmp/.causadb/ledger.log"})
    monkeypatch.setattr("causadb._workspace.WorkspaceManager.discover", lambda p: None)
    monkeypatch.setattr("causadb._workspace.WorkspaceManager.load", lambda p: MagicMock(ledger_path="/tmp/.causadb/ledger.log"))
    monkeypatch.setattr("causadb._shell_hook.install", lambda ctx_id: True)
    monkeypatch.setattr("causadb._git_hook.install_post_commit_hook", lambda *a, **kw: True)
    monkeypatch.setattr("causadb._git_hook.git_dir_from_workspace", lambda p: "/tmp/.git")
    monkeypatch.setattr("causadb.cli._cmd_watch.cmd_watch", lambda a: (0, '{"vigilante": "started"}'))

def test_setup_init_called(mock_args, monkeypatch):
    init_called = []
    monkeypatch.setattr("causadb._workspace.WorkspaceManager.init", lambda p: init_called.append(p) or {"ledger_path": ""})
    monkeypatch.setattr("causadb._workspace.WorkspaceManager.discover", lambda p: None)
    code, output = cmd_setup(mock_args)
    assert code == 0
    assert len(init_called) == 1

def test_setup_idempotent(mock_args, monkeypatch):
    init_called = []
    monkeypatch.setattr("causadb._workspace.WorkspaceManager.init", lambda p: init_called.append(p) or {"ledger_path": ""})
    monkeypatch.setattr("causadb._workspace.WorkspaceManager.discover", lambda p: None)
    cmd_setup(mock_args)
    cmd_setup(mock_args)
    assert len(init_called) == 2

def test_setup_no_hook_flag(mock_args, monkeypatch):
    mock_args.no_hook = True
    hook_called = []
    monkeypatch.setattr("causadb._shell_hook.install", lambda ctx_id: hook_called.append(ctx_id) or True)
    cmd_setup(mock_args)
    assert len(hook_called) == 0

def test_setup_no_git_flag(mock_args, monkeypatch):
    mock_args.no_git = True
    git_called = []
    monkeypatch.setattr("causadb._git_hook.install_post_commit_hook", lambda *a, **kw: git_called.append(True) or True)
    cmd_setup(mock_args)
    assert len(git_called) == 0

def test_setup_no_watch_flag(mock_args, monkeypatch):
    mock_args.no_watch = True
    watch_called = []
    monkeypatch.setattr("causadb.cli._cmd_watch.cmd_watch", lambda a: watch_called.append(True) or (0, "{}"))
    cmd_setup(mock_args)
    assert len(watch_called) == 0

def test_setup_degradacion_suave(mock_args, monkeypatch):
    monkeypatch.setattr("causadb._workspace.WorkspaceManager.init", lambda p: (_ for _ in ()).throw(Exception("Init failed")))
    hook_called = []
    monkeypatch.setattr("causadb._shell_hook.install", lambda ctx_id: hook_called.append(True) or True)
    code, output = cmd_setup(mock_args)
    data = json.loads(output)
    assert data["steps"]["init"]["status"] == "error"
    assert len(hook_called) == 1

def test_setup_integrations(mock_args, monkeypatch):
    mock_args.integrations = "opencode,cursor"
    config_calls = []
    monkeypatch.setattr("causadb.cli._cmd_config.cmd_config", lambda a: config_calls.append(a.tool) or (0, json.dumps({"status": "ok"})))
    monkeypatch.setattr("causadb._workspace.WorkspaceManager.discover", lambda p: "/tmp/.causadb/config.json")
    code, output = cmd_setup(mock_args)
    assert config_calls == ["opencode", "cursor"]

def test_setup_output_json(mock_args, monkeypatch):
    monkeypatch.setattr("causadb._workspace.WorkspaceManager.discover", lambda p: "/tmp/.causadb/config.json")
    code, output = cmd_setup(mock_args)
    data = json.loads(output)
    assert "project_dir" in data
    assert "steps" in data
    assert "init" in data["steps"]
