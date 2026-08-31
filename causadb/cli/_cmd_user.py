"""CLI handler for ``causadb user`` — RBAC user management (#10).

Usage::

    causadb user add --username <name> --password <pw> [--role <role>]
    causadb user remove --username <name>
    causadb user list

Pattern A: returns ``(exit_code, output_str)``.
"""

import json
import os
from typing import Tuple

from causadb._user_store import UserStore, UserStoreError


def _get_user_store() -> Tuple[UserStore, str]:
    """Discover workspace and return a UserStore instance.

    Returns:
        ``(user_store, config_dir)`` tuple.

    Raises:
        SystemExit (via ``(1, msg)``) if no workspace is found.
    """
    from causadb._workspace import WorkspaceManager
    config_path = WorkspaceManager.discover(os.getcwd())
    if config_path is None:
        raise RuntimeError(
            "No .causadb/ workspace found in current directory or parents. "
            "Run `causadb init` first."
        )
    config_dir = os.path.dirname(config_path)
    return UserStore(config_dir), config_dir


def cmd_user(args) -> Tuple[int, str]:
    """Route to the correct user subcommand based on ``args.user_action``."""
    action = getattr(args, "user_action", None)
    if action == "add":
        return cmd_user_add(args)
    elif action == "remove":
        return cmd_user_remove(args)
    elif action == "list":
        return cmd_user_list(args)
    else:
        return (2, json.dumps({
            "error": "Unknown user action. Use: add, remove, list"
        }))


def cmd_user_add(args) -> Tuple[int, str]:
    """``causadb user add --username NAME --password PW [--role ROLE]``"""
    try:
        store, _ = _get_user_store()
        user = store.add_user(
            username=args.username,
            password=args.password,
            role=args.role,
        )
        output = json.dumps({
            "status": "created",
            "user": user,
        })
        return (0, output)
    except (UserStoreError, RuntimeError) as e:
        return (1, json.dumps({"error": str(e)}))


def cmd_user_remove(args) -> Tuple[int, str]:
    """``causadb user remove --username NAME``"""
    try:
        store, _ = _get_user_store()
        store.remove_user(username=args.username)
        return (0, json.dumps({
            "status": "removed",
            "username": args.username,
        }))
    except (UserStoreError, RuntimeError) as e:
        return (1, json.dumps({"error": str(e)}))


def cmd_user_list(args) -> Tuple[int, str]:
    """``causadb user list``"""
    try:
        store, _ = _get_user_store()
        users = store.list_users()
        return (0, json.dumps({"users": users, "count": len(users)}))
    except RuntimeError as e:
        return (1, json.dumps({"error": str(e)}))
