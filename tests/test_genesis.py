"""Tests de la feature Génesis (onboarding para proyectos ya comenzados).

Cubre:
- F1.1: motor Génesis CLI (ingesta one-shot con provenance="genesis").
- F1.3: CODEBASE_ARCHITECTURE_SNAPSHOT (ast + codebase-memory optativo).
- F3:   integración UX en `causadb init` (prompt + --no-genesis).

Artículo III: test-first. Artículo IX: anti-teatro (ledgers temporales en
tmp_path, nunca el ledger real de Master).
"""

import json
import os
import subprocess

import pytest

from causadb._init import causadb_init
from causadb._ledger_reader import LedgerReader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_git_repo(path):
    """Crea un repo git temporal con 2 commits."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "first"], cwd=path, check=True)
    (path / "b.py").write_text("y = 2\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=path, check=True)


# ---------------------------------------------------------------------------
# F1.1 — motor Génesis CLI (ingesta one-shot)
# ---------------------------------------------------------------------------


def test_genesis_import_git_writes_provenance(tmp_path):
    """F1.1: genesis import --source git escribe COMMIT_MADE con
    payload.provenance == 'genesis'."""
    repo = tmp_path / "repo"
    _make_git_repo(repo)
    ledger = causadb_init(str(tmp_path / "ws"))["ledger_path"]

    from causadb.cli._cmd_genesis import run_genesis_import
    result = run_genesis_import(ledger_path=ledger, source="git", source_path=str(repo))

    events = list(LedgerReader(ledger).read_all())
    commits = [e for e in events if e.event_type.value == "COMMIT_MADE"]
    assert len(commits) >= 1, "debe cosechar al menos un COMMIT_MADE"
    assert all(e.payload.get("provenance") == "genesis" for e in commits), (
        "todo COMMIT_MADE debe llevar provenance='genesis'"
    )
    assert result["sources"].get("git", 0) >= 1


def test_genesis_import_isolates_cursors(tmp_path):
    """F1.1: el génesis usa .genesis_cursors.json y NO toca el archivo de
    cursores del daemon live (.harvester_cursors.json)."""
    repo = tmp_path / "repo"
    _make_git_repo(repo)
    ledger = causadb_init(str(tmp_path / "ws"))["ledger_path"]
    ledger_dir = os.path.dirname(ledger)

    from causadb.cli._cmd_genesis import run_genesis_import
    run_genesis_import(ledger_path=ledger, source="git", source_path=str(repo))

    default_cursors = os.path.join(ledger_dir, ".harvester_cursors.json")
    genesis_cursors = os.path.join(ledger_dir, ".genesis_cursors.json")
    assert os.path.exists(genesis_cursors), "el génesis debe persistir sus cursores"
    assert not os.path.exists(default_cursors), (
        "el génesis NO debe crear/modificar el archivo de cursores del daemon live"
    )


def test_genesis_import_no_sources_degrades_gracefully(tmp_path):
    """F1.1: sin fuentes detectadas no bloquea (exit 0, mensaje)."""
    ledger = causadb_init(str(tmp_path / "ws"))["ledger_path"]
    empty = tmp_path / "empty"
    empty.mkdir()

    from causadb.cli._cmd_genesis import run_genesis_import
    result = run_genesis_import(ledger_path=ledger, source="git", source_path=str(empty))
    assert result["sources"].get("git", 0) == 0
    # el ledger sigue escribible (snapshot + summary se emiten igual)
    events = list(LedgerReader(ledger).read_all())
    assert any(e.event_type.value == "GENESIS_SUMMARY" for e in events)


# ---------------------------------------------------------------------------
# F1.3 — CODEBASE_ARCHITECTURE_SNAPSHOT
# ---------------------------------------------------------------------------


def test_genesis_codebase_snapshot_ast_detects_structure(tmp_path):
    """F1.3: el snapshot detecta archivos, imports y resuelve imports
    intra-paquete."""
    proj = tmp_path / "proj"
    (proj / "pkg").mkdir(parents=True)
    (proj / "pkg" / "__init__.py").write_text("")
    (proj / "pkg" / "mod_a.py").write_text(
        "import os\nfrom pkg import mod_b\n\ndef f():\n    return mod_b.g()\n"
    )
    (proj / "pkg" / "mod_b.py").write_text("def g():\n    return 1\n")

    from causadb._genesis_codebase import generate_codebase_snapshot
    snap = generate_codebase_snapshot(str(proj), project_id="pid")

    assert snap["generator"] in ("ast", "codebase-memory")
    assert len(snap["nodes"]) >= 2, "debe detectar al menos 2 archivos .py"
    # import intra-paquete resuelto a un edge
    edges = snap["edges"]
    assert any(
        e["source"] == "pkg/mod_a.py" and e["target"] == "pkg/mod_b.py"
        for e in edges
    ), f"import intra-paquete no resuelto: {edges}"


def test_genesis_codebase_snapshot_event_written_with_blob(tmp_path):
    """F1.3: el snapshot se emite como evento CODEBASE_ARCHITECTURE_SNAPSHOT
    con $blob en BlobStore (resoluble al leer el ledger)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("import os\n")
    ledger = causadb_init(str(tmp_path / "ws"))["ledger_path"]

    from causadb.cli._cmd_genesis import run_genesis_import
    run_genesis_import(ledger_path=ledger, source="git", source_path=str(proj))

    events = list(LedgerReader(ledger).read_all())
    snaps = [e for e in events if e.event_type.value == "CODEBASE_ARCHITECTURE_SNAPSHOT"]
    assert len(snaps) == 1, "debe emitirse exactamente un snapshot"
    # LedgerReader resuelve el $blob → el payload trae nodes/edges
    assert "nodes" in snaps[0].payload
    assert "edges" in snaps[0].payload
    assert snaps[0].payload["generator"] in ("ast", "codebase-memory")


def test_genesis_codebase_snapshot_degrades_on_failure(tmp_path):
    """F1.3: cualquier fallo degrada a snapshot mínimo sin romper."""
    from causadb._genesis_codebase import generate_codebase_snapshot
    # path inexistente → no debe crashear
    snap = generate_codebase_snapshot(str(tmp_path / "noexiste"), project_id="pid")
    assert snap["generator"] in ("ast", "codebase-memory")
    assert "nodes" in snap and "edges" in snap


# ---------------------------------------------------------------------------
# F3 — integración UX en `causadb init`
# ---------------------------------------------------------------------------


def test_cmd_init_no_genesis_flag_skips_prompt(tmp_path, monkeypatch, capsys):
    """F3: con --no-genesis, init NO pregunta y NO bloquea."""
    from causadb.cli.main import main
    monkeypatch.chdir(tmp_path)
    rc = main(["init", "--no-genesis"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "proyecto ya comenzado" not in out


def test_cmd_init_non_interactive_does_not_block(tmp_path, monkeypatch, capsys):
    """F3: con stdin no-interactivo (isatty False), init NO pregunta y NO
    bloquea (patrón existente if sys.stdin.isatty())."""
    from causadb.cli.main import main
    monkeypatch.chdir(tmp_path)
    rc = main(["init"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "proyecto ya comenzado" not in out