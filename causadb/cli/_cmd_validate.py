"""`causadb validate` subcommand — thin wrapper around `LedgerValidator`."""
import json
from typing import Tuple

from causadb._ledger_validator import LedgerValidator
from causadb._workspace import resolve_ledger, NoWorkspaceError


def cmd_validate(args) -> Tuple[int, str]:
    """Validate the ledger hash chain. Auto-discovers workspace if no --ledger."""
    try:
        ledger = resolve_ledger(args.ledger)
        vr = LedgerValidator(ledger).validate_chain()
        return (0, json.dumps({
            "is_valid": vr.is_valid,
            "failure_type": vr.failure_type,
            "position": vr.position,
            "description": vr.description,
        }, sort_keys=True))
    except NoWorkspaceError:
        return (1, json.dumps({"error": "No CausaDB workspace found. Run `causadb init <path>` or provide --ledger."}))
    except Exception as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))
