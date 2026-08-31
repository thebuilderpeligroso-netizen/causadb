"""Tests J.3 + J.4 — Harvest sources.

J.3 (4 tests): shell, git, browser, activitywatch
J.4 (4 tests): mt5, jupyter, obsidian, zotero

Cobertura:
  1. test_harvest_shell_history — ShellHistorySource → COMMAND_RUN
  2. test_harvest_git_reflog — GitReflogSource → COMMIT_MADE
  3. test_harvest_chrome_history — BrowserHistorySource → OBSERVATION
  4. test_harvest_activitywatch — ActivityWatchSource → TOOL_CALLED
  5. test_harvest_mt5_logs — MT5HarvestSource → TRADE_EXECUTED
  6. test_harvest_jupyter_notebooks — JupyterHarvestSource → COMMAND_RUN
  7. test_harvest_obsidian_vault — ObsidianSource → FILE_MODIFIED
  8. test_harvest_zotero_api — ZoteroSource → CONTEXT_UPDATED
"""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from causadb._harvest_source_shell import ShellHistorySource
from causadb._harvest_source_git import GitReflogSource
from causadb._harvest_source_browser import BrowserHistorySource
from causadb._harvest_source_aw import ActivityWatchSource
from causadb._harvest_source_mt5 import MT5HarvestSource
from causadb._harvest_source_jupyter import JupyterHarvestSource
from causadb._harvest_source_obsidian import ObsidianSource
from causadb._harvest_source_zotero import ZoteroSource


# ===================================================================
# J.3 — MVP 4 fuentes
# ===================================================================

def test_harvest_shell_history(tmp_path):
    """Crea un .bash_history mock con 3 comandos, ShellHistorySource
    debe retornar 3 eventos COMMAND_RUN."""
    history_file = tmp_path / ".bash_history"
    history_file.write_text("ls -la\ncd /tmp\necho hello\n")

    source = ShellHistorySource(source_path=str(history_file))
    assert source.detect()
    events = source.harvest()
    assert len(events) == 3
    for ev in events:
        assert ev["type"] == "COMMAND_RUN"
    assert events[0]["command"] == "ls -la"
    assert events[1]["command"] == "cd /tmp"
    assert events[2]["command"] == "echo hello"


def test_harvest_git_reflog(tmp_path):
    """Crea un repo git con 2 commits, GitReflogSource debe retornar
    2 eventos COMMIT_MADE con commit_hash y message."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(repo_dir), capture_output=True)

    # First commit
    (repo_dir / "a.txt").write_text("hello")
    subprocess.run(["git", "add", "a.txt"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "first commit"],
                   cwd=str(repo_dir), capture_output=True)

    # Second commit
    (repo_dir / "b.txt").write_text("world")
    subprocess.run(["git", "add", "b.txt"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "second commit"],
                   cwd=str(repo_dir), capture_output=True)

    source = GitReflogSource(source_path=str(repo_dir))
    assert source.detect()
    events = source.harvest()
    assert len(events) == 2
    assert events[0]["type"] == "COMMIT_MADE"
    assert events[1]["type"] == "COMMIT_MADE"
    assert len(events[0]["commit_hash"]) == 7  # SHA abreviado
    # git reflog show lista el más reciente primero
    assert "second commit" in events[0]["message"]
    assert "first commit" in events[1]["message"]


def test_harvest_chrome_history(tmp_path):
    """Crea una base SQLite con schema de Chrome History,
    BrowserHistorySource debe retornar eventos OBSERVATION."""
    db_path = tmp_path / "History"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER, last_visit_time INTEGER)")
    conn.execute("INSERT INTO urls (url, title, last_visit_time) VALUES (?, ?, ?)",
                 ("https://example.com", "Example", 13150000000000000))
    conn.execute("INSERT INTO urls (url, title, last_visit_time) VALUES (?, ?, ?)",
                 ("https://test.org", "Test", 13150000000000001))
    conn.commit()
    conn.close()

    source = BrowserHistorySource(browser_paths=[str(db_path)])
    assert source.detect()
    events = source.harvest()
    assert len(events) == 2
    assert events[0]["type"] == "OBSERVATION"
    assert events[0]["url"] == "https://example.com"
    assert events[1]["url"] == "https://test.org"


def test_harvest_activitywatch():
    """Mockea la respuesta REST de ActivityWatch, verifica que se
    generen eventos TOOL_CALLED con app y title."""
    source = ActivityWatchSource(api_base="http://localhost:5600/api/0")

    mock_buckets = {
        "aw-watcher-window-abc": {"type": "window", "id": "aw-watcher-window-abc"},
        "aw-watcher-afk-def": {"type": "afk", "id": "aw-watcher-afk-def"},
    }
    mock_events = [
        {"timestamp": "2024-01-01T10:00:00Z", "data": {"app": "firefox", "title": "Mozilla Firefox"}},
        {"timestamp": "2024-01-01T10:01:00Z", "data": {"app": "code", "title": "test.py - VS Code"}},
    ]

    def mock_urlopen(url, *args, **kwargs):
        resp = MagicMock()
        resp.getcode.return_value = 200
        if "/buckets" in url and "/events" not in url:
            resp.read.return_value = json.dumps(mock_buckets).encode()
        elif "/events" in url:
            resp.read.return_value = json.dumps(mock_events).encode()
        else:
            resp.read.return_value = b'{"status": "ok"}'
        cm = MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = None
        return cm

    with patch("urllib.request.urlopen", mock_urlopen):
        assert source.detect()
        events = source.harvest()
        assert len(events) == 4  # 2 buckets × 2 events
        assert events[0]["type"] == "TOOL_CALLED"
        # Ambos buckets retornan los mismos mock_events, así que
        # events[0]=firefox, events[1]=code, events[2]=firefox, events[3]=code
        apps = [e["app"] for e in events]
        assert "firefox" in apps
        assert "code" in apps


# ===================================================================
# J.4 — Fuentes ampliadas
# ===================================================================

def test_harvest_mt5_logs(tmp_path):
    """Crea un .LOG de MetaTrader con 2 órdenes,
    MT5HarvestSource debe retornar 2 TRADE_EXECUTED."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "test.LOG"
    log_file.write_text(
        "2024.01.01 10:00:00 || Order #1 symbol EURUSD buy\n"
        "2024.01.01 10:01:00 || Order 2 symbol BTCUSD sell\n"
    )

    source = MT5HarvestSource(ledger_path="/fake/ledger.log", source_path=str(logs_dir))
    assert source.detect()
    events = source.harvest()
    assert len(events) == 2
    assert events[0]["type"] == "TRADE_EXECUTED"
    assert events[0]["order"] == "1"
    assert events[0]["symbol"] == "EURUSD"
    assert events[0]["side"] == "buy"
    assert events[1]["order"] == "2"
    assert events[1]["symbol"] == "BTCUSD"
    assert events[1]["side"] == "sell"


def test_harvest_jupyter_notebooks(tmp_path):
    """Crea un .ipynb con 3 celdas, JupyterHarvestSource debe retornar
    3 COMMAND_RUN."""
    nb_dir = tmp_path / "jupyter"
    nb_dir.mkdir()
    nb_file = nb_dir / "test.ipynb"
    nb_file.write_text(json.dumps({
        "cells": [
            {"cell_type": "code", "source": ["print(1)"]},
            {"cell_type": "code", "source": ["x = 2"]},
            {"cell_type": "markdown", "source": ["# Title"]},
        ]
    }))

    source = JupyterHarvestSource(ledger_path="/fake/ledger.log", source_path=str(nb_dir))
    assert source.detect()
    events = source.harvest()
    assert len(events) == 3
    assert events[0]["type"] == "COMMAND_RUN"
    assert events[0]["command"] == "print(1)"
    assert events[1]["command"] == "x = 2"
    assert events[2]["command"] == "# Title"


def test_harvest_obsidian_vault(tmp_path):
    """Crea 2 .md files en un vault mock, ObsidianSource debe retornar
    2 FILE_MODIFIED."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note1.md").write_text("# Note 1")
    (vault / "note2.md").write_text("# Note 2")

    source = ObsidianSource(vault_path=str(vault))
    assert source.detect()
    events = source.harvest()
    assert len(events) == 2
    assert events[0]["type"] == "FILE_MODIFIED"
    assert events[1]["type"] == "FILE_MODIFIED"
    # Verificar que los paths son absolutos
    assert os.path.isabs(events[0]["path"])


def test_harvest_zotero_api():
    """Mockea la respuesta REST de Zotero, verifica que se generen
    eventos CONTEXT_UPDATED."""
    source = ZoteroSource(api_base="http://127.0.0.1:23123/api")

    mock_items = json.dumps([
        {"key": "k1", "data": {"title": "Paper 1", "itemType": "journalArticle"}, "dateModified": "2024-01-01T10:00:00Z"},
        {"key": "k2", "data": {"title": "Book 1", "itemType": "book"}, "dateModified": "2024-01-01T11:00:00Z"},
    ]).encode()

    def mock_urlopen(url, *args, **kwargs):
        resp = MagicMock()
        resp.getcode.return_value = 200
        resp.read.return_value = mock_items
        cm = MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = None
        return cm

    with patch("urllib.request.urlopen", mock_urlopen):
        assert source.detect()
        events = source.harvest()
        assert len(events) == 2
        assert events[0]["type"] == "CONTEXT_UPDATED"
        assert events[0]["title"] == "Paper 1"
        assert events[0]["key"] == "k1"
        assert events[1]["title"] == "Book 1"
        assert events[1]["key"] == "k2"