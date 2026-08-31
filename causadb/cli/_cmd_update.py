"""CLI handler for `causadb update` — auto-update from GitHub Releases.

Pattern A: returns `(exit_code, output_str)`. `main.py` is the single
place that calls `print()`.
"""
import json
import sys
from typing import Tuple

from causadb._updater import check_update, install_or_check


def cmd_update(args) -> Tuple[int, str]:
    """Handle `causadb update [--check]`.

    --check: only check for updates, do not download or install.
    """
    check_only = getattr(args, "check", False)

    try:
        if check_only:
            result = check_update()
            return (0, json.dumps(result, sort_keys=True))
    except Exception as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))

    # Full update flow
    try:
        status = check_update()
        if not status["needs_update"]:
            return (0, json.dumps({
                "status": "up_to_date",
                "current_version": status["current_version"],
            }))

        # Interactive: ask user
        if not _confirm(f"Update available: {status['latest_version']} "
                        f"(current: {status['current_version']}). Install? [y/N] "):
            return (0, json.dumps({"status": "cancelled"}))

        result = install_or_check(check_only=False)
        return (0, json.dumps({
            "status": "updated",
            "version": result["latest_version"],
        }))
    except Exception as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))


def _confirm(prompt: str) -> bool:
    """Ask the user for confirmation. Returns True for 'y'/'Y'."""
    try:
        answer = input(prompt).strip().lower()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False