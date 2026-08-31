"""Tests para _resolve_project_root — fix del bug BIT-CHR.97.

Causa raíz: al daemonizar, ``_daemon.py`` ejecuta ``os.chdir("/")`` ANTES de
instanciar ``HarvesterDaemon``. ``register_sources()`` resolvía
``project_root = os.environ.get("CAUSADB_PROJECT_ROOT", os.getcwd())`` →
``os.getcwd() == "/"`` en el daemon → ``FilesystemSource.harvest()`` hacía
``os.walk("/")`` → cero eventos, cursor congelado.

Fix: helper ``_resolve_project_root(ledger_path)`` con precedencia:
  1. ``CAUSADB_PROJECT_ROOT`` env var (override explícito, gana siempre).
  2. Primer ``watch_dirs`` del ``config.json`` del workspace (dirname del
     ledger) que resuelva a un directorio existente en disco. Los paths
     relativos se normalizan contra el workspace root (NO contra cwd, que
     en daemon es "/").
  3. ``os.getcwd()`` como fallback (back-compat: daemon sin config).

Cobertura (Art. III, IX — RED real antes del fix):
  1. test_resolve_project_root_env_override_wins — env gana sobre config.
  2. test_resolve_project_root_reads_watch_dirs_from_config — config → path.
  3. test_resolve_project_root_falls_back_to_getcwd_without_config — back-compat.
  4. test_resolve_project_root_skips_nonexistent_watch_dir — skip + fallback.
  5. test_daemon_filesystem_source_uses_config_watch_dir — anti-teatro de
     integración: reproduce BIT-CHR.97 con HarvesterDaemon real.
"""

import json
import os

import pytest

from causadb._daemon_service import HarvesterDaemon, _resolve_project_root


def _write_config(ws, watch_dirs):
    """Escribe ``<ws>/.causadb/config.json`` con los watch_dirs dados."""
    config_dir = ws / ".causadb"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps({"watch_dirs": watch_dirs})
    )


def test_resolve_project_root_env_override_wins(tmp_path, monkeypatch):
    """Precedencia 1: CAUSADB_PROJECT_ROOT gana aunque el config exista."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, [str(ws)])
    monkeypatch.setenv("CAUSADB_PROJECT_ROOT", str(tmp_path / "env_root"))
    ledger = str(ws / ".causadb" / "ledger.log")

    assert _resolve_project_root(ledger) == str(tmp_path / "env_root")


def test_resolve_project_root_reads_watch_dirs_from_config(tmp_path, monkeypatch):
    """Precedencia 2: watch_dirs del config.json del workspace (absoluto)."""
    monkeypatch.delenv("CAUSADB_PROJECT_ROOT", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, [str(ws)])
    ledger = str(ws / ".causadb" / "ledger.log")

    assert _resolve_project_root(ledger) == str(ws)


def test_resolve_project_root_falls_back_to_getcwd_without_config(tmp_path, monkeypatch):
    """Precedencia 3: sin config.json → os.getcwd() (back-compat)."""
    monkeypatch.delenv("CAUSADB_PROJECT_ROOT", raising=False)
    ledger = str(tmp_path / "sin_config" / "ledger.log")

    assert _resolve_project_root(ledger) == os.getcwd()


def test_resolve_project_root_skips_nonexistent_watch_dir(tmp_path, monkeypatch):
    """Precedencia 2: salta watch_dirs inexistentes; si todos fallan → getcwd."""
    monkeypatch.delenv("CAUSADB_PROJECT_ROOT", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, ["/no/existe", str(ws)])
    ledger = str(ws / ".causadb" / "ledger.log")

    assert _resolve_project_root(ledger) == str(ws)

    # Todos inexistentes → fallback a getcwd
    _write_config(ws, ["/no/existe/1", "/no/existe/2"])
    assert _resolve_project_root(ledger) == os.getcwd()


def test_daemon_filesystem_source_uses_config_watch_dir(tmp_path, monkeypatch):
    """Anti-teatro (Art. IX): reproduce BIT-CHR.97 con HarvesterDaemon real.

    El config del workspace declara watch_dirs=[str(tmp_path/"ws")] (path
    ABSOLUTO — CRITICAL de auditoría: un relativo jamás resolvería contra
    cwd en daemon). El ledger aún NO existe (LedgerWriter genera GENESIS).
    Verifica que la fuente filesystem use el watch_dir del config y que
    harvest() emita el FILE_MODIFIED del archivo real con relpath correcto.
    """
    monkeypatch.delenv("CAUSADB_PROJECT_ROOT", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "archivo.txt").write_text("contenido de prueba")
    _write_config(ws, [str(ws)])
    ledger = str(ws / ".causadb" / "ledger.log")  # aún no existe (GENESIS)

    daemon = HarvesterDaemon(ledger_path=ledger)
    fs_source = daemon.harvester._sources["filesystem"]
    assert fs_source.project_root == str(ws), (
        "project_root debe venir del watch_dir del config, no del cwd del test"
    )

    events = list(fs_source.harvest())
    assert len(events) == 1
    assert events[0]["type"] == "FILE_MODIFIED"
    assert events[0]["action"] == "created"
    assert events[0]["path"] == "archivo.txt"