"""FIX.3 — `causadb blobs gc` CLI subcommand.

Thin delegator (Artículo II) al ``BlobGC`` (``causadb/_gc_blobs.py``).
Retorna ``Tuple[int, str]`` (exit_code, output_json).

Uso::

    causadb blobs gc --ledger <path>            # dry-run (default, no muta)
    causadb blobs gc --ledger <path> --execute  # mueve huerfanos a .trash/

El subcomando por defecto es **dry-run** — no muta el BlobStore. Esto es
crítico para seguridad del operador (Artículo I: el GC no escribe al ledger,
solo mueve blobs; ``--execute`` requiere ledger válido).
"""

import json
import os
from typing import Tuple

from causadb._gc_blobs import BlobGC
from causadb._workspace import resolve_ledger


def cmd_blobs(args) -> Tuple[int, str]:
    """Route to the correct blobs subcommand based on ``args.blobs_action``."""
    action = getattr(args, "blobs_action", None)
    if action == "gc":
        return _gc(
            ledger_path=getattr(args, "ledger", None),
            execute=getattr(args, "execute", False),
        )
    return (2, json.dumps({
        "error": "Unknown blobs action. Use: gc",
    }))


def _gc(ledger_path: str, execute: bool) -> Tuple[int, str]:
    """Run ``BlobGC.collect(dry_run=not execute)`` and return JSON report.

    Args:
        ledger_path: Ruta al ledger (None → auto-discovery via resolve_ledger).
        execute: Si True, mueve huerfanos a ``.trash/`` (requiere ledger válido).

    Returns:
        ``(exit_code, output_json)``. El reporte incluye: ``executed``,
        ``total_blobs``, ``orphan_count``, ``by_class``, ``moved_count``.
    """
    try:
        resolved = resolve_ledger(ledger_path)
    except Exception as e:
        return (1, json.dumps({"error": f"Could not resolve ledger: {e}"}))

    try:
        gc = BlobGC(resolved)
        report = gc.collect(dry_run=not execute)
    except RuntimeError as e:
        return (1, json.dumps({"error": str(e)}))
    except Exception as e:
        return (1, json.dumps({"error": f"GC failed: {e}"}))

    return (0, json.dumps({
        "executed": report.executed,
        "total_blobs": report.total_blobs,
        "orphan_count": report.orphan_count,
        "by_class": report.by_class,
        "moved_count": len(report.moved),
    }))
