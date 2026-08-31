"""Tests GAP-01 — mecanismo compartido de discovery de stores de agentes.

Ver docs/plan_gaps_01_02.md (GAP-01). Artículo III (test-first): estos tests
definen el contrato ANTES de la implementación. Artículo IX (anti-teatro):
assertions sobre comportamiento real (archivos en disco), no mocks.

Contrato del módulo ``causadb/_store_discovery.py``:

- ``discover_chats_dirs(projects_json=None)`` → lista de dirs de chats de
  gemini-cli (``~/.gemini/tmp/<slug>/chats``) para TODOS los proyectos del
  ``projects.json`` real. Fail-open: archivo ausente/corrupto → [] (no crash).
- ``normalize_store_path(env_var, config_file, default)`` → prioridad
  env override > config del agente (key ``data``) > default. Fail-open.
- ``coverage_gaps(chats_dirs, ledger_path, cursors_path=None)`` → dirs con
  sesiones sin cosechar. Fail-open: sin cursores → todos son gaps.
"""

import json
import os

import pytest

from causadb._store_discovery import (
    discover_chats_dirs,
    normalize_store_path,
    coverage_gaps,
)


def _write_projects_json(home, projects):
    gdir = os.path.join(str(home), ".gemini")
    os.makedirs(gdir, exist_ok=True)
    with open(os.path.join(gdir, "projects.json"), "w") as f:
        json.dump({"projects": projects}, f)


def _make_chats(home, slug, filenames):
    chats = os.path.join(str(home), ".gemini", "tmp", slug, "chats")
    os.makedirs(chats, exist_ok=True)
    for name in filenames:
        with open(os.path.join(chats, name), "w") as f:
            f.write("{}\n")
    return chats


# ---------------------------------------------------------------------------
# t1 — discover_chats_dirs: todos los proyectos del projects.json real
# ---------------------------------------------------------------------------

def test_discover_chats_dirs_all_projects(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    # formato REAL del projects.json: {projects: {<abs_path>: <slug>}}
    _write_projects_json(home, {str(tmp_path / "pa"): "proj-a", str(tmp_path / "pb"): "proj-b"})
    _make_chats(home, "proj-a", ["session-a.jsonl"])
    _make_chats(home, "proj-b", ["session-b.jsonl"])
    monkeypatch.setenv("HOME", str(home))

    dirs = discover_chats_dirs()
    assert len(dirs) == 2
    assert str(home / ".gemini" / "tmp" / "proj-a" / "chats") in dirs
    assert str(home / ".gemini" / "tmp" / "proj-b" / "chats") in dirs


def test_discover_chats_dirs_skips_slugs_without_chats(tmp_path, monkeypatch):
    """Proyecto sin dir de chats (nunca usado) → no aparece (detect False)."""
    home = tmp_path / "home"
    home.mkdir()
    _write_projects_json(home, {str(tmp_path / "u"): "usado", str(tmp_path / "n"): "nunca"})
    _make_chats(home, "usado", ["session-a.jsonl"])
    monkeypatch.setenv("HOME", str(home))

    dirs = discover_chats_dirs()
    assert dirs == [str(home / ".gemini" / "tmp" / "usado" / "chats")]


def test_discover_chats_dirs_missing_or_corrupt_projects_json(tmp_path, monkeypatch):
    """projects.json ausente o corrupto → fail-open: lista vacía (no crash)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert discover_chats_dirs() == []

    gdir = home / ".gemini"
    gdir.mkdir()
    (gdir / "projects.json").write_text("{ not json")
    assert discover_chats_dirs() == []


# ---------------------------------------------------------------------------
# t2 — normalize_store_path: env > config > default
# ---------------------------------------------------------------------------

def test_normalize_store_path_env_wins_over_config(tmp_path, monkeypatch):
    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({"data": "/tmp/from-config.db"}))
    default = "/tmp/default.db"

    assert normalize_store_path("CAUSADB_OPENCODE_DB_PATH", str(cfg), default) == "/tmp/from-config.db"
    monkeypatch.setenv("CAUSADB_OPENCODE_DB_PATH", "/tmp/from-env.db")
    assert normalize_store_path("CAUSADB_OPENCODE_DB_PATH", str(cfg), default) == "/tmp/from-env.db"


def test_normalize_store_path_missing_or_corrupt_config(tmp_path):
    default = "/tmp/default.db"
    assert normalize_store_path("CAUSADB_OPENCODE_DB_PATH", "/no/existe.json", default) == default
    bad = tmp_path / "bad.json"
    bad.write_text("{ invalid")
    assert normalize_store_path("CAUSADB_OPENCODE_DB_PATH", str(bad), default) == default


# ---------------------------------------------------------------------------
# t3 — coverage_gaps: dirs con sesiones sin cosechar (fail-open)
# ---------------------------------------------------------------------------

def test_coverage_gaps_returns_unharvested_dirs(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    cursors = str(tmp_path / ".harvester_cursors.json")
    d1 = _make_chats(tmp_path / "home", "a", ["session-a.jsonl"])
    d2 = _make_chats(tmp_path / "home", "b", ["session-b.jsonl"])
    # d1 cosechado (cursor con su archivo, clave multi-store), d2 no
    with open(cursors, "w") as f:
        json.dump({"agent:gemini": {"files": {"a/session-a.jsonl": {"offset": 10, "mtime": 0}}}}, f)

    gaps = coverage_gaps([d1, d2], ledger, cursors)
    assert gaps == [d2]


def test_coverage_gaps_fail_open_without_cursors(tmp_path):
    """Sin archivo de cursores (o corrupto) → TODOS los dirs son gaps."""
    ledger = str(tmp_path / "ledger.log")
    d1 = _make_chats(tmp_path / "home", "a", ["session-a.jsonl"])
    d2 = _make_chats(tmp_path / "home", "b", ["session-b.jsonl"])
    gaps = coverage_gaps([d1, d2], ledger)
    assert set(gaps) == {d1, d2}
