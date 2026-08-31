"""`causadb shell-hook` subcommand — install/remove/status/flush.

Artículo II: thin wrapper delegating to ``_shell_hook`` module.
"""
import json
from typing import Tuple

from causadb._shell_hook import install, remove, status, flush
from causadb._workspace import resolve_ledger, NoWorkspaceError


def cmd_shell_hook(args) -> Tuple[int, str]:
    """Route ``causadb shell-hook install|remove|status|flush``."""
    try:
        if args.action == "install":
            result = install(ctx_id=args.ctx_id)
            return (0, json.dumps({"installed": result}, sort_keys=True))
        elif args.action == "remove":
            result = remove()
            return (0, json.dumps({"removed": result}, sort_keys=True))
        elif args.action == "status":
            result = status()
            return (0, json.dumps(result, sort_keys=True))
        elif args.action == "flush":
            ledger = resolve_ledger(args.ledger)
            result = flush(ledger)
            return (0, json.dumps(result, sort_keys=True))
        else:
            return (1, json.dumps({"error": f"Unknown action: {args.action}"}))
    except NoWorkspaceError:
        return (1, json.dumps({"error": "No CausaDB workspace found. Run `causadb init <path>` or provide --ledger."}))
    except ValueError as e:
        return (1, json.dumps({"error": str(e), "error_type": "ValueError"}))
    except Exception as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))
