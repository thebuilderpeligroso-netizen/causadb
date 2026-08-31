"""F.10 — CLI subcommand for causadb ocb (Operational Context Buffer).

Thin delegator (Article II). Returns JSON.
"""

import json
import os

from causadb._ocb_manager import OCB
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType


def cmd_ocb(args) -> tuple:
    action = args.action
    ledger_path = args.ledger
    if action == "status":
        return _status(ledger_path)
    elif action == "close":
        summary_raw = getattr(args, "summary", None)
        summary = {}
        if summary_raw:
            try:
                summary = json.loads(summary_raw)
            except (json.JSONDecodeError, TypeError):
                return (1, json.dumps({"error": "invalid summary JSON"}))
        return _close(ledger_path, summary)
    elif action == "purge":
        keep_last = getattr(args, "keep_last", None)
        older_than_days = getattr(args, "older_than_days", None)
        return _purge(ledger_path, keep_last, older_than_days)
    elif action == "rebuild":
        return _rebuild(ledger_path)
    else:
        return (1, json.dumps({"error": f"Unknown action: {action}"}))





def _status(ledger_path: str) -> tuple:
    try:
        ocb = OCB.for_ledger(ledger_path)
        ctx = ocb.load_session_context()
        # Cosmético (Fase 0): las particiones precargadas se exponen como
        # metadata + flag ``resolved`` por línea. Los payloads ``$blob``
        # se muestran como ``{"$blob": hash, "resolved": False}`` sin leer
        # contenido; el detalle granular se resuelve a pedido con
        # ``load_partition_by_id``.
        preloaded = [
            {"id": p.get("id"), "lines": p.get("lines", [])}
            for p in ctx.get("preloaded_partitions", [])
        ]
        ctx = {**ctx, "preloaded_partitions": preloaded}
        return (0, json.dumps(ctx))
    except Exception as e:
        return (1, json.dumps({"error": str(e)}))


def _close(ledger_path: str, summary: dict) -> tuple:
    try:
        ocb = OCB.for_ledger(ledger_path)
        ocb.close_session(summary)
        return (0, json.dumps({"status": "closed", "summary": summary}))
    except Exception as e:
        return (1, json.dumps({"error": str(e)}))


def _purge(ledger_path: str, keep_last: int = None, older_than_days: int = None) -> tuple:
    try:
        ocb = OCB.for_ledger(ledger_path)
        ocb.purge(keep_last=keep_last, older_than_days=older_than_days)
        return (0, json.dumps({"status": "purged"}))
    except Exception as e:
        return (1, json.dumps({"error": str(e)}))


def _rebuild(ledger_path: str) -> tuple:
    """F0 (M1) — backfill del OCB desde el ledger. Thin wrapper (Art. II)
    que delega en ``OCB.rebuild`` y devuelve JSON ``{"rebuilt": N}``."""
    try:
        n = OCB.rebuild(ledger_path)
        return (0, json.dumps({"rebuilt": n}))
    except Exception as e:
        return (1, json.dumps({"error": str(e)}))
