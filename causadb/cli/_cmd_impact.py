"""CLI handler for `causadb impact <event_id>` (F.12.4).

Returns the downstream causal cone of an event — the list of events
that depend on it transitively (the "blast radius" of reverting it).

Pattern A: returns `(exit_code, output_str)`. `main.py` is the single
place that calls `print()`. Output is a JSON string.
"""
import json
from typing import Tuple

from causadb._causal_cone import trace_downstream


def cmd_impact(args) -> Tuple[int, str]:
    event_id = args.event_id
    from causadb._workspace import resolve_ledger, NoWorkspaceError
    try:
        ledger = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        return (1, json.dumps({
            "error": str(e),
            "error_type": "NoWorkspaceError",
        }))

    try:
        result = trace_downstream(event_id, ledger)
    except ValueError as e:
        return (1, json.dumps({
            "error": str(e),
            "error_type": "ValueError",
        }))
    except Exception as e:
        return (
            1,
            json.dumps({"error": str(e), "error_type": type(e).__name__}),
        )

    return (0, json.dumps({
        "source_event_id": event_id,
        "tainted_count": len(result),
        "tainted_events": result,
    }, sort_keys=True))
