"""First-install adoption (sin extras OTel) — Art. III test-first, Art. IX anti-teatro.

Contexto: el crash OTel viene por la cadena
``causadb/otel/__init__.py:15`` → ``_mapper.py:37-39`` (importa
``opentelemetry.sdk`` al top); ``_importer.py`` es stdlib-limpio.
Si ``causadb.cli.main`` importa ``causadb.otel`` al top (via
``_cmd_import``/``_cmd_export``), un fresh ``pip install causadb``
(sin extra ``dev``) crashea hasta con ``--help``.

Anti-teatro: estos tests corren en SUBPROCESS AISLADO (nuevo proceso
python con bloqueo de ``opentelemetry*`` en ``sys.meta_path`` ANTES
de cualquier import de ``causadb``). Un test en el mismo proceso
pytest sin aislamiento sería teatro (el módulo ya estaría importado
con OTel disponible).

Doctrina ``pyproject:40``: núcleo SIN OTel como dep dura; OTel vive
en el extra ``dev`` (``pyproject.toml`` ``[project.optional-dependencies]``).
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Extra real verificado en pyproject.toml (NO inventar `causadb[otel]`).
OTEL_EXTRA_HINT = "pip install 'causadb[dev]'"

# Strings exactos de la nota MCP (C(b) — ancla README + canon).
README_NAMING_NOTE = (
    "Protocol names are `query`, `revive`, `log_decision` "
    "(see `causadb/mcp/server.py`); each MCP client prefixes the server "
    "name, so agents see them as `causadb_*`."
)
CANON_NAMING_NOTE = (
    "A nivel protocolo los nombres son `query`, `revive`, `log_decision` "
    "(ver `causadb/mcp/server.py`); cada cliente MCP antepone el nombre "
    "del servidor, así los agentes los ven como `causadb_*`."
)

# Bloqueador: simula first-install sin extra `dev`. Se inyecta ANTES de
# cualquier import de `causadb` y cubre TODA la cadena (parent package
# `causadb.otel.__init__` → `_mapper` → `opentelemetry.sdk`), porque
# cualquier `import opentelemetry[.x]` raisea ModuleNotFoundError.
_BLOCKER = """
import importlib.abc
import sys

class _BlockOTel(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ModuleNotFoundError(
                "No module named %r (blocked: first-install simulation)" % name
            )
        return None

sys.meta_path.insert(0, _BlockOTel())
for _m in [m for m in list(sys.modules)
           if m == "opentelemetry" or m.startswith("opentelemetry.")]:
    del sys.modules[_m]
"""


def _run_isolated(code: str) -> subprocess.CompletedProcess:
    """Corre `code` en un proceso python nuevo con OTel bloqueado."""
    return subprocess.run(
        [sys.executable, "-c", _BLOCKER + code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


# ---------------------------------------------------------------------------
# C(a) ANTI-TEATRO — subprocess aislado sin OTel
# ---------------------------------------------------------------------------

def test_cli_help_without_otel_exits_zero():
    """`import causadb.cli.main` + `--help` sin OTel → exit 0 (no crash)."""
    proc = _run_isolated(
        "from causadb.cli.main import main\n"
        "raise SystemExit(main(['--help']))\n"
    )
    assert proc.returncode == 0, (
        f"--help crasheó sin OTel (first-install roto).\n"
        f"returncode={proc.returncode}\n--- stderr ---\n{proc.stderr[-2000:]}"
    )
    assert "usage" in proc.stdout.lower()


def test_cli_import_without_otel_exits_1_with_hint():
    """Subcomando `import` real sin OTel → exit 1 + hint del extra."""
    proc = _run_isolated(
        "from causadb.cli.main import main\n"
        "raise SystemExit(main(['import', '--format', 'otel', "
        "'--ledger', '/tmp/first-install-test.log', "
        "'--file', '/tmp/first-install-spans.jsonl']))\n"
    )
    assert proc.returncode == 1, (
        f"`import` sin OTel debe dar exit 1 (Pattern A), no crash.\n"
        f"returncode={proc.returncode}\n--- stderr ---\n{proc.stderr[-2000:]}"
    )
    assert "hint" in proc.stdout, (
        f"output sin `hint`:\n{proc.stdout[-2000:]}"
    )
    assert OTEL_EXTRA_HINT in proc.stdout, (
        f"output sin hint del extra {OTEL_EXTRA_HINT!r}:\n{proc.stdout[-2000:]}"
    )


def test_cli_export_without_otel_exits_1_with_hint():
    """Subcomando `export` real sin OTel → exit 1 + hint del extra."""
    proc = _run_isolated(
        "from causadb.cli.main import main\n"
        "raise SystemExit(main(['export', '--format', 'otel', "
        "'--ledger', '/tmp/first-install-test.log', "
        "'--endpoint', 'http://localhost:6006/v1/traces']))\n"
    )
    assert proc.returncode == 1, (
        f"`export` sin OTel debe dar exit 1 (Pattern A), no crash.\n"
        f"returncode={proc.returncode}\n--- stderr ---\n{proc.stderr[-2000:]}"
    )
    assert "hint" in proc.stdout, (
        f"output sin `hint`:\n{proc.stdout[-2000:]}"
    )
    assert OTEL_EXTRA_HINT in proc.stdout, (
        f"output sin hint del extra {OTEL_EXTRA_HINT!r}:\n{proc.stdout[-2000:]}"
    )


# ---------------------------------------------------------------------------
# C(b) ancla — la nota existe en README + canon (nada más)
# ---------------------------------------------------------------------------

def test_readme_documents_mcp_naming():
    """La nota de naming MCP existe en la sección MCP del README."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert README_NAMING_NOTE in text


def test_canon_documents_mcp_naming():
    """La nota de naming MCP existe en la tabla de tools del canon."""
    text = (REPO_ROOT / "docs" / "canon.md").read_text(encoding="utf-8")
    assert CANON_NAMING_NOTE in text
