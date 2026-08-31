"""Tests de integración — advance_cursor via _harvest_one().

Verifica que 2 ciclos consecutivos de harvest no producen duplicados
cuando el cursor se preserva correctamente via advance_cursor.

Estos tests pasan via _harvest_one() (no source.harvest() directo) para
detectar el bug donde el default advance_cursor pisa cursores no-secuenciales.
"""

import os
import tempfile
import pytest
from causadb._harvester import Harvester
from causadb._harvest_source_shell import ShellHistorySource
from causadb._harvest_source_git import GitReflogSource
from causadb._harvest_source_obsidian import ObsidianSource


# ── ShellHistorySource ──────────────────────────────────────────────

def test_shell_cursor_no_duplicates_via_harvest_one(tmp_path):
    """2 ciclos sin cambios → 0 eventos en el segundo ciclo."""
    history = tmp_path / ".bash_history"
    history.write_text("ls -la\ngit status\necho hello\n")
    ledger = tmp_path / "ledger.log"

    source = ShellHistorySource(
        source_path=str(history),
        ledger_path=str(ledger),
    )
    harvester = Harvester(ledger_path=str(ledger))

    cursors = {}
    count1 = harvester._harvest_one(source, cursors)
    assert count1 == 3

    count2 = harvester._harvest_one(source, cursors)
    assert count2 == 0


def test_shell_cursor_new_lines_only():
    """Shell: agregar nueva línea → solo el nuevo comando cosechado."""
    history = tempfile.NamedTemporaryFile(mode="w", suffix=".bash_history", delete=False)
    ledger = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
    try:
        history.write("ls -launch\ngit status\n\n")
        history.flush()
        ledger_path = ledger.name
        ledger.close()

        source = ShellHistorySource(
            source_path=history.name,
            ledger_path=ledger_path,
        )
        harvester = Harvester(ledger_path=ledger_path)

        cursors = {}
        count1 = harvester._harvest_one(source, cursors)
        assert count1 == 2

        history.write("echo new\n")
        history.flush()
        count2 = harvester._harvest_one(source, cursors)
        assert count2 == 1

        # Cursor preservado: {"shell_history": {"line": 3}}
        cursor = cursors[source.cursor_key()]
        assert cursor["line"] == 3
    finally:
        history.close()
        if os.path.exists(ledger.name):
            os.unlink(ledger.name)


# ── GitReflogSource ────────────────────────────────────────────────

def test_git_cursor_no_duplicates_via_harvest_one(tmp_path):
    """Git reflog sin cambios → segundo ciclo 0 eventos."""
    repo = tmp_path / "repo"
    repo.mkdir()
    os.system(f"git -C {repo} init --quiet 2>/dev/null")
    os.system(f"git -C {repo} config user.email test@test.com")
    os.system(f"git -C {repo} config user.name Test")
    (repo / "f.txt").write_text("v1")
    os.system(f"git -C {repo} add f.txt 2>/dev/null")
    os.system(f"git -C {repo} commit -m c1 --quiet 2>/dev/null")
    (repo / "f.txt").write_text("v2")
    os.system(f"git -C {repo} commit -am c2 --quiet 2>/dev/null")

    ledger = tmp_path / "ledger.log"

    source = GitReflogSource(
        source_path=str(repo),
        ledger_path=str(ledger),
    )
    harvester = Harvester(ledger_path=str(ledger))

    cursors = {}
    count1 = harvester._harvest_one(source, cursors)
    assert count1 >= 2

    count2 = harvester._harvest_one(source, cursors)
    assert count2 == 0


# ── ObsidianSource ─────────────────────────────────────────────────

def test_obsidian_cursor_no_duplicates_via_harvest_one(tmp_path):
    """Obsidian vault sin cambios → 0 eventos en segundo ciclo."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "nota1.md").write_text("# Nota 1\ncontenido")
    (vault / "nota2.md").write_text("# Nota 2\ncontenido")

    ledger = tmp_path / "ledger.log"

    source = ObsidianSource(
        vault_path=str(vault),
        ledger_path=str(ledger),
    )
    harvester = Harvester(ledger_path=str(ledger))

    cursors = {}
    count1 = harvester._harvest_one(source, cursors)
    assert count1 == 2

    count2 = harvester._harvest_one(source, cursors)
    assert count2 == 0


def test_obsidian_cursor_new_note():
    """Nueva nota después de primer harvest → solo la nota nueva."""
    vault = tempfile.TemporaryDirectory()
    ledger = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
    try:
        vault_path = vault.name
        open(os.path.join(vault_path, "n1.md"), "w").write("a")
        open(os.path.join(vault_path, "n2.md"), "w").write("b")
        ledger_path = ledger.name
        ledger.close()

        source = ObsidianSource(
            vault_path=vault_path,
            ledger_path=ledger_path,
        )
        harvester = Harvester(ledger_path=ledger_path)

        cursors = {}
        count1 = harvester._harvest_one(source, cursors)
        assert count1 == 2

        open(os.path.join(vault_path, "n3.md"), "w").write("c")
        count2 = harvester._harvest_one(source, cursors)
        assert count2 == 1
    finally:
        vault.cleanup()
        if os.path.exists(ledger.name):
            os.unlink(ledger.name)


def test_anti_teatro_obsidian_always_new():
    """Mutar advance_cursor para que siempre retorne cursor vacío →
    cada ciclo detecta todos los archivos como nuevos."""
    vault = tempfile.TemporaryDirectory()
    try:
        vault_path = vault.name
        open(os.path.join(vault_path, "a.md"), "w").write("a")
        open(os.path.join(vault_path, "b.md"), "w").write("b")

        ledger = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
        ledger_path = ledger.name
        ledger.close()

        source = ObsidianSource(
            vault_path=vault_path,
            ledger_path=ledger_path,
        )
        harvester = Harvester(ledger_path=ledger_path)

        cursors = {}
        count1 = harvester._harvest_one(source, cursors)
        assert count1 == 2

        # Corromper cursor manualmente (simula el bug original)
        cursors[source.cursor_key()] = {}

        # Con cursor vacío, cada archivo parece nuevo → 2 eventos
        count2 = harvester._harvest_one(source, cursors)
        assert count2 == 2  # NO es 0 porque cursor vacío corrompe la deduplicación
    finally:
        vault.cleanup()
        if os.path.exists(ledger_path):
            os.unlink(ledger_path)