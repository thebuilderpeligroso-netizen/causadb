"""``causadb explain`` subcommand — explain a governance decision.

Article II: thin wrapper over ``causadb._explain.explain_decision``.
"""

import json
from typing import Tuple

from causadb._explain import explain_decision


def cmd_explain(args) -> Tuple[int, str]:
    """Explain a governance decision by its event ID.

    Returns (exit_code, output_json).
    """
    event_id = getattr(args, "event_id", None)
    if event_id is None:
        return (1, json.dumps({"error": "Missing event_id argument"}))

    try:
        from causadb._workspace import resolve_ledger, NoWorkspaceError
        ledger = resolve_ledger(getattr(args, "ledger", None))
        result = explain_decision(ledger, event_id)
        return (0, json.dumps(result, sort_keys=True, default=str))
    except ValueError as e:
        return (1, json.dumps({"error": str(e), "error_type": "ValueError"}))
    except Exception as e:
        return (
            1,
            json.dumps({"error": str(e), "error_type": type(e).__name__}),
        )