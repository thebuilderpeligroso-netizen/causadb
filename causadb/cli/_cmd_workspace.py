"""I.1 — ``causadb workspace`` subcommand — multi-workspace management.

Artículo II: thin wrapper over ``WorkspaceManager``. No logic reimplemented.

Usage::

    causadb workspace create <name>
    causadb workspace list
    causadb workspace delete <name>
    causadb workspace switch <name>
    causadb workspace current
"""

import json
from typing import Tuple

from causadb._workspace_manager import WorkspaceManager


def cmd_workspace(args) -> Tuple[int, str]:
    """Route ``causadb workspace create|list|delete|switch|current``.

    Returns ``(exit_code, output_str)``.
    """
    action = args.action
    root_dir = getattr(args, "root_dir", None)
    wm = WorkspaceManager(root_dir)

    if action == "create":
        name = args.name
        if not name:
            return (1, json.dumps({"error": "Workspace name is required"}))
        try:
            wm.create(name)
            return (0, json.dumps({"status": "created", "name": name}))
        except FileExistsError as e:
            return (1, json.dumps({"error": str(e)}))

    elif action == "list":
        workspaces = wm.list()
        return (0, json.dumps({"workspaces": workspaces}))

    elif action == "delete":
        name = args.name
        if not name:
            return (1, json.dumps({"error": "Workspace name is required"}))
        try:
            wm.delete(name)
            return (0, json.dumps({"status": "deleted", "name": name}))
        except FileNotFoundError as e:
            return (1, json.dumps({"error": str(e)}))

    elif action == "switch":
        name = args.name
        if not name:
            return (1, json.dumps({"error": "Workspace name is required"}))
        try:
            wm.switch(name)
            return (0, json.dumps({"status": "switched", "name": name}))
        except FileNotFoundError as e:
            return (1, json.dumps({"error": str(e)}))

    elif action == "current":
        current = wm.current()
        return (0, json.dumps({"current": current}))

    else:
        return (1, json.dumps({"error": f"Unknown action: {action}"}))
