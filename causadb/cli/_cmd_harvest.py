"""`causadb harvest` subcommand — lifecycle del harvester de agentes.

Daemonizable (patrón de ``_cmd_vigilante.py``, F.11.4): ``start --daemon``
forkea con PID file (``~/.causadb/pids/harvest.pid``) y el proceso queda
vivo mientras el timer de harvest corre. ``watch start`` lo invoca como un
subproceso más (BIT-CHR.41; ver docs/design_index.md).

Artículo II: thin wrapper — toda la lógica vive en ``HarvesterDaemon``.
"""

import json
import time
from typing import Tuple

from causadb._daemon import get_daemon
from causadb._daemon_service import (
    HarvesterDaemon,
    install_signal_handlers,
    set_current_harvester_daemon,
)
from causadb._workspace import resolve_ledger, NoWorkspaceError


def cmd_harvest(args) -> Tuple[int, str]:
    """Route ``causadb harvest start|stop`` a su handler."""
    try:
        ledger = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        if args.action == "stop":
            return (0, json.dumps({"status": "stopped"}))
        return (1, json.dumps({"error": str(e)}))

    action = args.action
    if action == "start":
        return _start(ledger, args)
    elif action == "stop":
        return _stop(ledger)
    else:
        return (1, json.dumps({"error": f"Unknown action: {action}"}))


def _start(ledger: str, args) -> Tuple[int, str]:
    """Arranca HarvesterDaemon (en thread timer) o como fork si --daemon.

    Flow (patrón vigilante): check is_running → daemonize (si --daemon) →
    instanciar daemon → handlers → start. El check ANTES del fork evita
    dobles arranques cuando el daemon ya corre.
    """
    platform_daemon = get_daemon()
    if platform_daemon.is_running("harvest"):
        return (0, json.dumps({"status": "already_running", "ledger": ledger}))

    if getattr(args, "daemon", False):
        platform_daemon.daemonize("harvest")

    daemon = HarvesterDaemon(ledger_path=ledger)
    set_current_harvester_daemon(daemon)
    install_signal_handlers(ledger)
    daemon.start()  # primer tick en background (auditoría F)

    # Con --daemon: mantener vivo el proceso forkeado mientras el timer corre.
    if getattr(args, "daemon", False):
        try:
            while daemon.timer is not None and daemon.timer.is_alive():
                time.sleep(1.0)
        except KeyboardInterrupt:
            daemon.stop()

    return (0, json.dumps({"status": "started", "ledger": ledger}))


def _stop(ledger: str) -> Tuple[int, str]:
    """Detiene el daemon forkeado (kill por PID file) si está corriendo."""
    platform_daemon = get_daemon()
    if platform_daemon.is_running("harvest"):
        platform_daemon.kill("harvest")
        return (0, json.dumps({"status": "stopped", "ledger": ledger}))
    return (0, json.dumps({"status": "not_running", "ledger": ledger}))
