import json
from causadb._ledger_index import LedgerIndex


def cmd_stream(args) -> tuple:
    try:
        from causadb._workspace import resolve_ledger, NoWorkspaceError
        ledger = resolve_ledger(args.ledger)
        index = LedgerIndex(ledger)
        results = index.query(event_type="STREAM_INTERRUPTED")
        return (0, json.dumps(results, default=str, sort_keys=True))
    except Exception as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))