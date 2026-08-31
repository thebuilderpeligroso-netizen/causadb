"""Fase 14.2 — Test del puntero al canon en el revive.

Artículo III (test-first), Artículo IX (fixture real, no mocks).

El header del markdown del revive debe incluir un puntero a la doctrina
(``docs/canon.md``) accesible vía ``causadb canon`` (CLI) o el resource MCP
``causadb://canon``, consistente con la convención de ``_cmd_init.py``.
"""

import argparse
import json
import os

from causadb._init import causadb_init
from causadb.cli._cmd_revive import cmd_revive


def _make_revive_args(ledger, fmt="markdown"):
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--format", default="markdown")
    parser.add_argument("--decisions", type=int, default=10)
    parser.add_argument("--write", default=None)
    parser.add_argument("--last", action="store_true", default=False)
    return parser.parse_args(["--ledger", ledger, "--format", fmt])


def test_revive_incluye_puntero_al_canon(tmp_path):
    """El markdown del revive contiene el puntero al canon (doctrina)."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]

    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)

    assert exit_code == 0, f"revive failed: {output}"

    # El puntero al canon debe estar presente (CLI y/o resource MCP).
    assert "causadb://canon" in output, (
        f"el revive debe referenciar el resource MCP causadb://canon, "
        f"got:\n{output[:800]}"
    )
    assert "causadb canon" in output, (
        f"el revive debe referenciar el CLI causadb canon, got:\n{output[:800]}"
    )
    # Debe apuntar a la doctrina (docs/canon.md).
    assert "docs/canon.md" in output, (
        f"el revive debe apuntar a docs/canon.md, got:\n{output[:800]}"
    )