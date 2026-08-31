import json
import sys
from typing import List, Dict, Any
from causadb._ledger_index import LedgerIndex


def cmd_query(args) -> tuple:
    """`causadb query` — busqueda sobre ledger index.

    BIT-CHR.99 Gap #1 — propaga ``args.limit`` a ``index.query`` y, si
    el cap oculta eventos recientes, emite un hint a stderr (stdout
    sigue siendo el array JSON intacto para no romper parsers).
    """
    try:
        from causadb._workspace import resolve_ledger, NoWorkspaceError
        ledger = resolve_ledger(args.ledger)
        index = LedgerIndex(ledger)
        results: List[Dict[str, Any]] = index.query(
            event_type=args.event_type,
            ctx_id=args.ctx_id,
            parent_event_id=args.parent_event_id,
            source=args.source,
            text=args.text,
            from_time=args.from_time,
            to_time=args.to_time,
            limit=getattr(args, "limit", None),
        )
        # BIT-CHR.99 Gap #1 — hint a stderr cuando el cap oculta recientes.
        hint = getattr(index, "last_query_hint", None)
        if hint and results:
            last_ts = (
                results[-1].get("event", {}).get("timestamp", "")
                if results else ""
            )
            sys.stderr.write(
                f"[hint] {hint['hint']} — el ledger tiene eventos más "
                f"recientes (seq <{hint['max_seq_in_ledger']}) no mostrados "
                f"por el cap (default 1000). Usá "
                f"--from-time={last_ts} o --limit=<N> para verlos.\n"
            )
        return (0, json.dumps(results, default=str, sort_keys=True))
    except Exception as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))
