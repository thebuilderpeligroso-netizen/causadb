"""`causadb vigilante` subcommand — lifecycle for Modo Vigilante.

Maintains a module-level registry of running watcher threads keyed by
ledger path so that ``start`` / ``stop`` can be called across separate
invocations of the CLI.

Supports ``--daemon`` (F.11.1): fork + PID file for background operation.
"""
import json
import os
import threading
from typing import Dict, Tuple

from causadb._daemon import get_daemon
from causadb._vigilante import VigilanteWatcher
from causadb._workspace import resolve_ledger, NoWorkspaceError

# Module-level registry — keyed by ledger path so a started watcher can
# be stopped later, possibly from a different CLI invocation.
_vigilante_threads: Dict[str, threading.Thread] = {}
_vigilante_watchers: Dict[str, VigilanteWatcher] = {}


def cmd_vigilante(args) -> Tuple[int, str]:
    """Route ``causadb vigilante start|stop`` to the corresponding handler."""
    try:
        ledger = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        return (1, json.dumps({"error": str(e)}))
    action = args.action
    if action == "start":
        return _start(ledger, getattr(args, "watch", None), args)
    elif action == "stop":
        return _stop(ledger)
    else:
        return (1, json.dumps({"error": f"Unknown action: {action}"}))


def _start(ledger: str, watch_dir: str, args) -> Tuple[int, str]:
    """Start a VigilanteWatcher in a daemon thread (or fork if --daemon).

    Flow: check is_running → daemonize (if --daemon) → check thread → start.
    The is_running check BEFORE daemonize prevents double-fork races
    when the daemon is already running.
    """
    platform_daemon = get_daemon()
    if platform_daemon.is_running("vigilante"):
        return (0, json.dumps({"status": "already_running", "ledger": ledger}))

    if getattr(args, "daemon", False):
        platform_daemon.daemonize("vigilante")

    if ledger in _vigilante_threads and _vigilante_threads[ledger].is_alive():
        return (0, json.dumps({"status": "already_running", "ledger": ledger}))

    if not watch_dir:
        watch_dir = os.path.dirname(os.path.dirname(ledger))

    watcher = VigilanteWatcher(
        ledger_path=ledger,
        watch_dir=watch_dir,
        skip_baseline=getattr(args, "skip_baseline", False),
    )
    t = threading.Thread(target=watcher.start, daemon=False)
    t.start()

    _vigilante_watchers[ledger] = watcher
    _vigilante_threads[ledger] = t

    # When --daemon: keep process alive while watcher thread runs
    if getattr(args, "daemon", False):
        try:
            while t.is_alive():
                t.join(timeout=1.0)
        except KeyboardInterrupt:
            watcher.stop()
            t.join(timeout=5.0)

    return (0, json.dumps({
        "status": "started",
        "ledger": ledger,
        "watch": watch_dir,
    }))


def _stop(ledger: str) -> Tuple[int, str]:
    """Stop a running VigilanteWatcher (or kill daemon if running as fork)."""
    platform_daemon = get_daemon()
    if platform_daemon.is_running("vigilante"):
        platform_daemon.kill("vigilante")
        return (0, json.dumps({"status": "stopped", "ledger": ledger}))

    watcher = _vigilante_watchers.pop(ledger, None)
    thread = _vigilante_threads.pop(ledger, None)

    if watcher is None:
        return (0, json.dumps({"status": "not_running", "ledger": ledger}))

    watcher.stop()
    if thread is not None:
        thread.join(timeout=5.0)

    return (0, json.dumps({"status": "stopped", "ledger": ledger}))
