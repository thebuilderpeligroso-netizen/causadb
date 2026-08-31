"""``causadb daemon`` subcommand — systemd user service lifecycle (D.1).

Artículo II: thin wrapper. Artículo III: test-first.

Actions
-------
install
    Write the systemd user service unit file (requires ``--ledger``).
start
    ``systemctl --user start causadb``
stop
    ``systemctl --user stop causadb``
status
    ``systemctl --user is-active causadb`` (parse output)
"""
import json
from typing import Tuple

from causadb._daemon_service import (
    install_service,
    start_service,
    stop_service,
    status_service,
)


def cmd_daemon(args) -> Tuple[int, str]:
    """Handle ``causadb daemon <action> [--ledger <path>]``.

    Returns:
        ``(exit_code, json_string)`` — standard CausaDB CLI contract.
    """
    action = args.action

    if action == "install":
        if not args.ledger:
            return (1, json.dumps({
                "error": "--ledger is required for install action",
                "error_type": "ValueError",
            }, sort_keys=True))
        success, msg = install_service(args.ledger)
        if success:
            return (0, json.dumps({
                "status": "installed",
                "service_path": msg,
            }, sort_keys=True))
        return (1, json.dumps({
            "error": msg,
            "error_type": "OSError",
        }, sort_keys=True))

    elif action == "start":
        success, msg = start_service()
        if success:
            return (0, json.dumps({"status": "started"}, sort_keys=True))
        return (1, json.dumps({
            "error": msg,
            "error_type": "ServiceError",
        }, sort_keys=True))

    elif action == "stop":
        success, msg = stop_service()
        if success:
            return (0, json.dumps({"status": "stopped"}, sort_keys=True))
        return (1, json.dumps({
            "error": msg,
            "error_type": "ServiceError",
        }, sort_keys=True))

    elif action == "status":
        success, msg = status_service()
        if success:
            return (0, json.dumps({
                "status": "active",
                "message": msg,
            }, sort_keys=True))
        # "inactive" / "failed" are NOT errors — exit 0 with status field.
        return (0, json.dumps({
            "status": "inactive",
            "message": msg,
        }, sort_keys=True))

    # Not reached (argparse enforces choices), but Fall-Closed.
    return (1, json.dumps({
        "error": f"Unknown action: {action}",
        "error_type": "ValueError",
    }, sort_keys=True))
