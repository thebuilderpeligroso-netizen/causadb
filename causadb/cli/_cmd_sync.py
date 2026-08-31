"""``causadb sync`` subcommand — ledger federation (push/pull/config/status).

Artículo II: thin wrapper over ``causadb._sync.SyncEngine``.
Artículo III: test-first.
"""

import json
import os
from typing import Tuple

from causadb._sync import SyncEngine, SyncError


def cmd_sync(args) -> Tuple[int, str]:
    """Route ``causadb sync status|push|pull|full|config``.

    Returns (exit_code, output_json).
    """
    # Resolve ledger path: --ledger arg > workspace discovery > error
    ledger_from_args = getattr(args, "ledger", None)
    if ledger_from_args:
        ledger_path = os.path.abspath(ledger_from_args)
        config_dir = os.path.dirname(ledger_path)
    else:
        try:
            from causadb._workspace import resolve_ledger, NoWorkspaceError
            try:
                ledger_path = resolve_ledger(None)
                config_dir = os.path.dirname(ledger_path)
            except (NoWorkspaceError, FileNotFoundError):
                return (
                    1,
                    json.dumps({
                        "error": (
                            "No ledger found. Use --ledger or run from a "
                            "CausaDB workspace."
                        )
                    }),
                )
        except Exception:
            return (
                1,
                json.dumps({"error": "No ledger found and --ledger not provided."}),
            )

    engine = SyncEngine(ledger_path, config_dir)
    action = args.sync_action

    try:
        if action == "config":
            hub_url = getattr(args, "hub_url", None) or ""
            api_key = getattr(args, "api_key", None) or ""
            interval = getattr(args, "interval", 60)

            if hub_url and api_key:
                engine.configure(hub_url, api_key, interval)
                return 0, json.dumps({"status": "configured", "hub_url": hub_url})
            else:
                cfg = engine.get_config()
                return 0, json.dumps(cfg)

        elif action == "status":
            cfg = engine.get_config()
            state = engine._load_state()
            is_configured = bool(state.get("hub_url"))
            return 0, json.dumps({
                "configured": is_configured,
                "hub_url": cfg["hub_url"] if is_configured else "(not configured)",
                "last_synced_seq": cfg["last_synced_seq"],
                "sync_interval_minutes": cfg["interval_minutes"],
            })

        elif action == "push":
            result = engine.push()
            return 0, json.dumps(result)

        elif action == "pull":
            result = engine.pull()
            return 0, json.dumps(result)

        elif action == "full":
            result = engine.full_sync()
            return 0, json.dumps(result)

        return 1, json.dumps({"error": f"Unknown sync action: {action}"})

    except SyncError as e:
        return 1, json.dumps({"error": str(e)})
