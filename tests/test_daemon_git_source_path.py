"""Tests follow-up BIT-CHR.97 — GitReflogSource source_path en daemon.

Causa raíz: al daemonizar, ``_daemon.py`` ejecuta ``os.chdir("/")`` ANTES de
instanciar ``HarvesterDaemon``. ``register_sources()`` instanciaba
``GitReflogSource(ledger_path=...)`` SIN ``source_path`` → ``_repo_dir()``
caía a ``os.getcwd() == "/"`` en el daemon → ``detect()`` False → fuente git
muerta (nunca cosecha, cursor ``git_reflog`` congelado).

Fix: pasar ``source_path=_resolve_project_root(self.ledger_path)`` al
``GitReflogSource`` (mismo helper ya usado para ``FilesystemSource`` en
BIT-CHR.97). No se toca ``_harvest_source_git.py``: su firma ya acepta
``source_path`` y ``_repo_dir()`` ya lo respeta; el bug estaba en el caller.

Cobertura (Art. III, IX — RED real antes del fix):
  1. test_git_source_uses_resolve_project_root — el daemon pasa el
     watch_dir del config como ``source_path`` (no queda ``None``).
  2. test_git_source_detect_true_and_harvest_when_workspace_is_repo —
     anti-teatro end-to-end: repo git REAL, ``detect()`` True y
     ``harvest()`` emite ``COMMIT_MADE`` con ``commit_hash`` no vacío.
  3. test_git_source_detect_false_when_workspace_not_repo — pin
     anti-regresión: el fix no "inventa" un repo. GREEN desde el inicio.
"""

import json
import subprocess

import pytest

from causadb._daemon_service import HarvesterDaemon


def _write_config(ws, watch_dirs):
    """Escribe ``<ws>/.causadb/config.json`` con los watch_dirs dados."""
    config_dir = ws / ".causadb"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps({"watch_dirs": watch_dirs})
    )


def _init_git_repo(repo_dir):
    """Inicializa un repo git REAL con un commit (patrón test_harvest_sources)."""
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(repo_dir), capture_output=True)
    (repo_dir / "a.txt").write_text("hello")
    subprocess.run(["git", "add", "a.txt"], cwd=str(repo_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=str(repo_dir), capture_output=True)


def test_git_source_uses_resolve_project_root(tmp_path, monkeypatch):
    """El daemon pasa el watch_dir del config como ``source_path`` del
    GitReflogSource (no queda ``None`` → no cae a ``os.getcwd()``).

    RED con código actual: ``_source_path`` queda ``None``.
    """
    monkeypatch.delenv("CAUSADB_PROJECT_ROOT", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, [str(ws)])
    ledger = str(ws / ".causadb" / "ledger.log")  # no existe (GENESIS)

    daemon = HarvesterDaemon(ledger_path=ledger)
    git_source = daemon.harvester._sources["git"]
    assert git_source._source_path == str(ws), (
        "GitReflogSource._source_path debe venir del watch_dir del config "
        "(via _resolve_project_root), no quedar None y caer a os.getcwd()"
    )


def test_git_source_detect_true_and_harvest_when_workspace_is_repo(tmp_path, monkeypatch):
    """Anti-teatro (Art. IX): repo git REAL + chdir determinístico.

    Reproduce el follow-up end-to-end:
      - workspace ES un repo git (con commit real)
      - ``detect()`` True
      - ``harvest()`` emite al menos un ``COMMIT_MADE`` con ``commit_hash``
        no vacío.

    El ``monkeypatch.chdir`` al workspace garantiza RED determinístico sin
    importar el cwd del runner: con código actual ``_source_path`` queda
    ``None`` → ``_repo_dir()`` = ``os.getcwd()`` = ``tmp_path/ws`` (que SÍ
    es repo) → ``detect()`` True por accidente, PERO ``_source_path != ws``
    falla determinísticamente. Tras fix, GREEN total.
    """
    monkeypatch.delenv("CAUSADB_PROJECT_ROOT", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    _init_git_repo(ws)
    _write_config(ws, [str(ws)])
    ledger = str(ws / ".causadb" / "ledger.log")  # no existe (GENESIS)

    monkeypatch.chdir(str(ws))
    daemon = HarvesterDaemon(ledger_path=ledger)
    git_source = daemon.harvester._sources["git"]

    # 1. source_path viene del config (no de cwd)
    assert git_source._source_path == str(ws)

    # 2. detect() True (el workspace ES un repo git real)
    assert git_source.detect() is True

    # 3. harvest() emite al menos un COMMIT_MADE con commit_hash no vacío
    events = git_source.harvest()
    assert len(events) >= 1, "harvest() debe emitir el commit del repo real"
    commit_events = [e for e in events if e["type"] == "COMMIT_MADE"]
    assert len(commit_events) >= 1
    assert commit_events[0]["commit_hash"], "commit_hash no debe ser vacío"


def test_git_source_detect_false_when_workspace_not_repo(tmp_path, monkeypatch):
    """Pin anti-regresión: el fix no "inventa" un repo.

    Si el workspace NO es repo git (sin ``git init``), ``detect()`` debe ser
    False. GREEN desde el inicio (nunca RED): protege contra un fix que
    forzara ``detect()`` True por defecto.
    """
    monkeypatch.delenv("CAUSADB_PROJECT_ROOT", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, [str(ws)])
    ledger = str(ws / ".causadb" / "ledger.log")

    daemon = HarvesterDaemon(ledger_path=ledger)
    git_source = daemon.harvester._sources["git"]
    # El fix hace que _source_path apunte al workspace (no None); pero el
    # pin anti-regresión central es detect() is False: el fix NO inventa
    # un repo donde no lo hay. Este assert pasa tanto ANTES como DESPUÉS
    # del fix (con None → _repo_dir()=getcwd=ws no-repo → False; con
    # source_path=ws → git log -1 en ws no-repo → False).
    assert git_source.detect() is False
