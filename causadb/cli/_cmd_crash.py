"""CLI handler for ``causadb crash``.

Thin wrapper (Article II) — delegates all logic to ``causadb._crash_reporter``.
Pattern A: returns ``(exit_code, output_str)``.

Usage::

    causadb crash list                    # list crashes
    causadb crash delete [--crash-id ID]  # delete all (or specific)
    causadb crash export                  # output crashes as JSON to stdout
"""
import json
from typing import Tuple

from causadb._crash_reporter import list_crashes, delete_crash, delete_all_crashes, crashes_to_export_file


def cmd_crash(args) -> Tuple[int, str]:
    """Handle ``causadb crash`` subcommands."""
    action = getattr(args, "action", None)

    if action == "list":
        crashes = list_crashes()
        output = []
        for c in crashes:
            output.append({
                "crash_id": c["crash_id"],
                "timestamp": c["timestamp"],
                "exception_type": c["exception_type"],
                "exception_msg": c["exception_msg"],
                "os": c["os"],
                "version": c["version"],
                "occurrences": c["occurrences"],
            })
        return (0, json.dumps(output, indent=2, sort_keys=True))

    elif action == "delete":
        crash_id = getattr(args, "crash_id", None)
        if crash_id:
            if delete_crash(crash_id):
                return (0, json.dumps({"status": "deleted", "crash_id": crash_id}))
            else:
                return (1, json.dumps({"error": f"crash '{crash_id}' not found"}))
        else:
            count = delete_all_crashes()
            return (0, json.dumps({"status": "deleted", "count": count}))

    elif action == "export":
        try:
            path = crashes_to_export_file()
            with open(path) as f:
                data = json.load(f)
            return (0, json.dumps(data, indent=2, sort_keys=True))
        except Exception as e:
            return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))

    else:
        return (1, json.dumps({"error": f"unknown action: {action}"}))
