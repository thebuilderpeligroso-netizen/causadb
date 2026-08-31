"""F.11.2 — `causadb config` subcommand.
    
Artículo II: thin wrapper over WorkspaceManager. No logic reimplemented.
"""

import json
import os
from typing import Tuple

from causadb._secrets import Secrets
from causadb._workspace import WorkspaceManager


# Keys that are stored in workspace config.json (WorkspaceManager).
_ALLOWED_CONFIG_KEYS = frozenset({
    "ledger_path",
    "watch_dirs",
    "chronicle_path",
    "daemon_enabled",
    "ocb_base_path",
    "blob_store_enabled",
})

# Keys that are stored in the system keyring (Secrets), not in workspace config.
_KEYSECRET_KEYS = frozenset({
    "openai_key",
    "anthropic_key",
})


def cmd_config(args) -> Tuple[int, str]:
    """Route ``causadb config get|set|path|init``.

    Special cases:
      - ``config set openai_key <value>`` → stores in system keyring
      - ``config set anthropic_key <value>`` → stores in system keyring
      - ``config delete-key openai_key`` → deletes from system keyring

    Returns (exit_code, output_str).
    """
    action = args.action

    if action == "get":
        return _config_get()
    elif action == "set":
        if args.key in _KEYSECRET_KEYS:
            return _config_set_secret(args.key, args.value)
        return _config_set(args.key, args.value)
    elif action == "delete-key":
        return _config_delete_key(args.key)
    elif action == "register-type":
        return _config_register_type(args.key, args.fields)
    elif action == "path":
        return _config_path()
    elif action == "init":
        return _config_init(args.path)
    elif action == "mcp":
        from ._cmd_config_mcp import cmd_config_mcp
        return cmd_config_mcp(args)
    else:
        return (1, json.dumps({"error": f"Unknown action: {action}"}))


def _get_config_path() -> str:
    """Discover workspace config from CWD."""
    config_path = WorkspaceManager.discover(os.getcwd())
    if config_path is None:
        raise FileNotFoundError(
            "No .causadb/config.json found in current or parent directories. "
            "Run `causadb config init <path>` first."
        )
    return config_path


def _config_get() -> Tuple[int, str]:
    try:
        from causadb._telemetry import is_enabled as _tel_enabled
        config_path = _get_config_path()
        ws = WorkspaceManager.load(config_path)
        return (0, json.dumps(
            {
                "ledger_path": ws.ledger_path,
                "watch_dirs": ws.watch_dirs,
                "chronicle_path": ws.chronicle_path,
                "daemon_enabled": ws.daemon_enabled,
                "ocb_base_path": ws.ocb_base_path,
                "blob_store_enabled": ws.blob_store_enabled,
                "telemetry_enabled": _tel_enabled(),
            },
            indent=2,
            sort_keys=True,
        ))
    except FileNotFoundError as e:
        return (1, json.dumps({"error": str(e)}))


def _config_set(key: str, value: str) -> Tuple[int, str]:
    # Special case: telemetry.enabled → user-level config
    if key == "telemetry.enabled":
        try:
            from causadb._telemetry import set_enabled
            parsed = value.lower() in ("true", "1", "yes")
            set_enabled(parsed)
            return (0, json.dumps({"status": "updated", "key": key, "value": parsed}))
        except Exception as e:
            return (1, json.dumps({"error": str(e)}))
    try:
        config_path = _get_config_path()
        ws = WorkspaceManager.load(config_path)
        if key not in _ALLOWED_CONFIG_KEYS:
            return (1, json.dumps({"error": f"Unknown config key: {key}"}))
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            parsed = value
        setattr(ws, key, parsed)
        WorkspaceManager.save(ws, config_path)
        return (0, json.dumps({"status": "updated", "key": key, "value": parsed}))
    except FileNotFoundError as e:
        return (1, json.dumps({"error": str(e)}))


def _config_register_type(name: str, fields_str: str) -> Tuple[int, str]:
    """Register a custom event type and persist to config.json."""
    try:
        config_path = _get_config_path()
        from causadb._event_registry import register_type, EventTypeSpec
        fields = set(f.strip() for f in fields_str.split(",")) if fields_str else set()
        spec = EventTypeSpec(required_fields=fields)
        register_type(name, spec)
        # Persistir
        ws = WorkspaceManager.load(config_path)
        ws.custom_event_types[name] = {"required_fields": list(fields)}
        WorkspaceManager.save(ws, config_path)
        return (0, json.dumps({"status": "ok", "registered": name, "fields": list(fields)}))
    except Exception as e:
        return (1, json.dumps({"error": str(e)}))


def _config_path() -> Tuple[int, str]:
    try:
        config_path = _get_config_path()
        return (0, json.dumps({"config_path": config_path}))
    except FileNotFoundError as e:
        return (1, json.dumps({"error": str(e)}))


def _config_init(path: str) -> Tuple[int, str]:
    try:
        result = WorkspaceManager.init(path)
        return (0, json.dumps(result, indent=2, sort_keys=True))
    except (ValueError, FileExistsError) as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))


def _config_set_secret(key: str, value: str) -> Tuple[int, str]:
    """Store an API key in the system keyring.

    Args:
        key: Secret key name (e.g. ``"openai_key"``).
        value: The secret value to store.

    Returns:
        Tuple of (exit_code, output_json).
    """
    try:
        Secrets.set(key, value)
        return (0, json.dumps({
            "status": "stored",
            "key": key,
            "backend": "keyring",
        }))
    except RuntimeError as e:
        return (1, json.dumps({"error": str(e)}))


def _config_delete_key(key: str) -> Tuple[int, str]:
    """Delete an API key from the system keyring.

    Args:
        key: Secret key name (e.g. ``"openai_key"``).

    Returns:
        Tuple of (exit_code, output_json).
    """
    try:
        Secrets.delete(key)
        return (0, json.dumps({
            "status": "deleted",
            "key": key,
        }))
    except (KeyError, RuntimeError) as e:
        return (1, json.dumps({"error": str(e)}))
