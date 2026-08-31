"""F.9 — CLI subcommand for causadb mcp-proxy.

Delegates to McpProxy core (Article II — thin wrapper).
No logic is reimplemented here. F.11.1 adds --daemon support.
"""

import asyncio
import json
import sys

from causadb._daemon import get_daemon
from causadb.mcp._proxy import McpProxy, DEFAULT_CONFIG_PATH
from causadb._workspace import resolve_ledger, NoWorkspaceError


def cmd_mcp_proxy(args) -> tuple:
    """Handler for the ``mcp-proxy`` subcommand.

    Returns (exit_code, output_str).
    """
    try:
        ledger_path = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        return (1, json.dumps({"error": str(e)}))
    action = args.action
    config_path = getattr(args, "config", None)
    daemon = getattr(args, "daemon", False)

    if action == "start":
        return _start(ledger_path, config_path, daemon)
    elif action == "stop":
        return _stop(ledger_path, config_path)
    elif action == "status":
        return _status(ledger_path, config_path)
    else:
        return (1, json.dumps({"error": f"Unknown action: {action}"}))


def _start(ledger_path: str, config_path: str = None, daemon: bool = False) -> tuple:
    """Start the MCP proxy.

    In foreground mode (daemon=False): starts, prints result, stops, returns.
    In daemon mode (daemon=True): forks, starts, writes output to log, and
    stays alive indefinitely until SIGTERM.
    """
    platform_daemon = get_daemon()
    if daemon:
        if platform_daemon.is_running("mcp_proxy"):
            return (0, json.dumps({"status": "already_running", "ledger": ledger_path}))
        platform_daemon.daemonize("mcp_proxy")

    try:
        proxy = McpProxy(
            config_path=config_path or DEFAULT_CONFIG_PATH,
            ledger_path=ledger_path,
        )
        names = asyncio.run(proxy.start())

        mode = "log-only"
        if names:
            mode = "proxy"

        output = json.dumps({
            "mode": mode,
            "ledger": ledger_path,
            "servers": names if names else [],
            "status": "started",
        })

        if daemon:
            print(output, flush=True)
            import signal
            stop_event = asyncio.Event()

            def _handle_sigterm(signum, frame):
                stop_event.set()

            signal.signal(signal.SIGTERM, _handle_sigterm)

            async def _wait_forever():
                await stop_event.wait()
                await proxy.stop()

            asyncio.run(_wait_forever())
            return (0, json.dumps({"status": "terminated", "ledger": ledger_path}))

        # Foreground mode: stop after starting
        asyncio.run(proxy.stop())
        return (0, output)
    except Exception as e:
        return (1, json.dumps({"error": str(e)}))


def _stop(ledger_path: str, config_path: str = None) -> tuple:
    """Stop the MCP proxy. Kills daemon process or returns ack."""
    platform_daemon = get_daemon()
    if platform_daemon.is_running("mcp_proxy"):
        platform_daemon.kill("mcp_proxy")
        return (0, json.dumps({"status": "stopped", "ledger": ledger_path}))
    return (0, json.dumps({"status": "stopped", "ledger": ledger_path}))


def _status(ledger_path: str, config_path: str = None) -> tuple:
    """Return proxy status, including daemon running state."""
    import os
    running = get_daemon().is_running("mcp_proxy")
    config_exists = os.path.exists(config_path or DEFAULT_CONFIG_PATH)
    return (0, json.dumps({
        "ledger": ledger_path,
        "config_exists": config_exists,
        "mode": "log-only" if not config_exists else "proxy",
        "daemon_running": running,
    }))
