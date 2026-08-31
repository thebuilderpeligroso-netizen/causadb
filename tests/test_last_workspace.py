"""F.11.3 — Last-workspace registry tests (_workspace.py).

Artículo III: Test-first. Artículo IX: Anti-teatro — a registry entry that
points to a nonexistent ledger is useless and must resolve to None.
"""

import json
import os

import pytest

from causadb._workspace import (
    WorkspaceManager,
    get_last_workspace,
    record_last_workspace,
    resolve_ledger,
    NoWorkspaceError,
)
from causadb.cli._cmd_revive import _run_revive
from causadb.cli.main import main


# ---------------------------------------------------------------------------
# record / get roundtrip
# ---------------------------------------------------------------------------


def _real_ledger(project) -> str:
    """Create a real workspace at *project* and return its ledger path."""
    return WorkspaceManager.init(str(project))["ledger_path"]


def test_record_and_get_roundtrip(tmp_path):
    ledger = _real_ledger(tmp_path)
    registry = str(tmp_path / "registry.json")
    record_last_workspace(ledger, registry_path=registry)
    assert get_last_workspace(registry_path=registry) == ledger


def test_get_none_when_registry_missing(tmp_path):
    registry = str(tmp_path / "missing.json")
    assert get_last_workspace(registry_path=registry) is None


def test_get_none_when_ledger_deleted(tmp_path):
    ledger = _real_ledger(tmp_path)
    registry = str(tmp_path / "registry.json")
    record_last_workspace(ledger, registry_path=registry)
    os.remove(ledger)
    assert get_last_workspace(registry_path=registry) is None


def test_get_none_when_malformed(tmp_path):
    registry = str(tmp_path / "registry.json")
    with open(registry, "w") as f:
        f.write("not json {")
    assert get_last_workspace(registry_path=registry) is None


def test_record_ignores_nonexistent_ledger(tmp_path):
    registry = str(tmp_path / "registry.json")
    record_last_workspace(str(tmp_path / "ghost" / "ledger.log"), registry_path=registry)
    assert not os.path.exists(registry)


# ---------------------------------------------------------------------------
# resolve_ledger fallback
# ---------------------------------------------------------------------------


def test_resolve_fallback_last_uses_last_workspace(tmp_path, monkeypatch):
    ledger = _real_ledger(tmp_path / "proj")
    registry = str(tmp_path / "registry.json")
    record_last_workspace(ledger, registry_path=registry)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert resolve_ledger(None, fallback_last=True) == ledger


def test_resolve_no_fallback_raises(tmp_path, monkeypatch):
    ledger = _real_ledger(tmp_path / "proj")
    registry = str(tmp_path / "registry.json")
    record_last_workspace(ledger, registry_path=registry)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    with pytest.raises(NoWorkspaceError):
        resolve_ledger(None, fallback_last=False)


def test_resolve_prefers_discovery_over_last(tmp_path, monkeypatch):
    # Project A exists at cwd (discovery); last workspace points to project B.
    ledger_a = _real_ledger(tmp_path / "a" / "proj")
    ledger_b = _real_ledger(tmp_path / "b" / "proj")
    registry = str(tmp_path / "registry.json")
    record_last_workspace(ledger_b, registry_path=registry)
    monkeypatch.chdir(str(tmp_path / "a" / "proj"))
    assert resolve_ledger(None, fallback_last=True) == ledger_a


def test_resolve_explicit_overrides_last(tmp_path, monkeypatch):
    ledger = _real_ledger(tmp_path / "proj")
    registry = str(tmp_path / "registry.json")
    record_last_workspace(ledger, registry_path=registry)
    other = str(tmp_path / "explicit" / "ledger.log")
    assert resolve_ledger(other, fallback_last=True) == other


# ---------------------------------------------------------------------------
# revive CLI integration
# ---------------------------------------------------------------------------


def test_revive_last_flag_uses_last_workspace(tmp_path, monkeypatch, capsys):
    ledger = _real_ledger(tmp_path)
    registry = str(tmp_path / "registry.json")
    record_last_workspace(ledger, registry_path=registry)
    monkeypatch.setenv("CAUSADB_LAST_WORKSPACE", registry)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    rc = main(["revive", "--last", "--format", "json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ledger_path"] == ledger


def test_revive_last_flag_no_registry_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("CAUSADB_LAST_WORKSPACE", str(tmp_path / "no_registry.json"))
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    rc = main(["revive", "--last", "--format", "json"])
    assert rc != 0


def test_revive_falls_back_to_last_without_ledger(tmp_path, monkeypatch, capsys):
    ledger = _real_ledger(tmp_path)
    registry = str(tmp_path / "registry.json")
    record_last_workspace(ledger, registry_path=registry)
    monkeypatch.setenv("CAUSADB_LAST_WORKSPACE", registry)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    rc = main(["revive", "--format", "json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ledger_path"] == ledger


def test_revive_markdown_shows_ledger_path(tmp_path):
    ledger = _real_ledger(tmp_path)
    _, output = _run_revive(ledger, output_format="markdown")
    assert ledger in output


# ---------------------------------------------------------------------------
# Recording hooks
# ---------------------------------------------------------------------------


def test_init_records_last_workspace(tmp_path, monkeypatch):
    registry = str(tmp_path / "registry.json")
    monkeypatch.setenv("CAUSADB_LAST_WORKSPACE", registry)
    project = tmp_path / "proj"
    ledger = WorkspaceManager.init(str(project))["ledger_path"]
    assert get_last_workspace() == ledger


def test_mcp_resolve_falls_back_to_last(tmp_path, monkeypatch):
    from causadb.mcp.server import _resolve_ledger
    ledger = _real_ledger(tmp_path)
    registry = str(tmp_path / "registry.json")
    record_last_workspace(ledger, registry_path=registry)
    monkeypatch.setenv("CAUSADB_LAST_WORKSPACE", registry)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert _resolve_ledger() == ledger
