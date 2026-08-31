"""`causadb replay` subcommand — thin wrapper around `ReplayEngine`."""
import json
from typing import Tuple

from causadb._replay_engine import ReplayEngine
from causadb._workspace import resolve_ledger


def cmd_replay(args) -> Tuple[int, str]:
    """Delegate to `ReplayEngine(ledger).reconstruct_state()`.

    If --chronicle is truthy, filter output to only show chronicle_entries.
    """
    try:
        ledger = resolve_ledger(args.ledger)
        state = ReplayEngine(ledger).reconstruct_state()

        # 7.3b — If --chronicle flag is set, filter output to chronicle_entries only
        chronicle_flag = getattr(args, "chronicle", None)
        if chronicle_flag:
            return (0, json.dumps(
                {"chronicle_entries": state.get("chronicle_entries", [])},
                sort_keys=True,
                default=str,
            ))

        return (0, json.dumps(state, sort_keys=True, default=str))
    except Exception as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))