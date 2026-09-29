"""H-OPS.1 — `causadb serve` subcommand con lifecycle daemon completo.

Articulo II: thin wrapper. No logica duplicada.

Wiring del daemon — orden correcto (BIT-CHR.41; docs/design_index.md):
instanciar ``HarvesterDaemon`` + registrar fuentes (incluye gemini y
opencode) + ``set_current_harvester_daemon`` + ``set_current_server`` +
``install_signal_handlers`` ANTES de ``serve()`` (que bloquea con
``serve_forever()``). El primer tick del daemon va en background
(auditoria F) — el backfill no congela el arranque del server.

Fase 1 (H-OPS.1): ``start --daemon`` daemoniza (double-fork + PID file);
``stop`` mata via ``get_daemon().kill("serve", timeout=35.0)`` con fallback
``pgrep`` si no hay PID file (back-compat con serve legacy sin PID file).
"""

import json
import logging
import os
import subprocess
import socket
import uuid
from typing import Tuple

from causadb._workspace import resolve_ledger, NoWorkspaceError
from causadb._daemon import get_daemon, remove_pidfile
from causadb._daemon_service import (
    HarvesterDaemon,
    install_signal_handlers,
    set_current_harvester_daemon,
    set_current_server,
    set_current_daemon_name,
)
from causadb._rest_api import serve


def _is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Chequeo preventivo de puerto."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def _resolve_port(port: int, host: str = "127.0.0.1") -> Tuple[int, str]:
    """Resolve port 0 to ephemeral port and return (port, port_file_path).
    
    If port=0, binds to ephemeral port and returns (actual_port, port_file_path).
    Otherwise returns (port, None).
    """
    if port != 0:
        return port, None
    
    # Bind to ephemeral port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        actual_port = s.getsockname()[1]
    
    # Generate unique test ID and create port file
    test_uuid = uuid.uuid4().hex[:8]
    port_dir = f"/tmp/causadb-test-{actual_port}"
    os.makedirs(port_dir, exist_ok=True)
    port_file = os.path.join(port_dir, "port.txt")
    
    with open(port_file, "w") as f:
        f.write(str(actual_port))
    
    return actual_port, port_file


def cmd_serve(args) -> Tuple[int, str]:
    action = args.action
    if action == "start":
        return _serve_start(args)
    elif action == "stop":
        return _serve_stop(args)
    else:
        return (1, json.dumps({"error": f"Unknown action: {action}"}))


def _attach_user_store(auth_manager, ledger_path: str):
    """Adjunta el UserStore persistente al AuthManager del serve.

    Deriva ``config_dir`` como el directorio ``.causadb`` que contiene el
    ``ledger_path`` YA resuelto (robusto bajo systemd donde cwd=/; NO usa
    ``WorkspaceManager.discover(os.getcwd())``). Enchufa vía la API pública
    ``AuthManager.enable_with_user_store(config_dir, {master: admin})``
    (``_auth.py:158``), preservando dev-keys-primero: ``authenticate()``
    chequea ``_api_keys`` antes que el store (``_auth.py:200-208``).

    Alimenta AMBOS stores con el MISMO objeto UserStore:
      - ``auth_manager._user_store`` (vía ``enable_with_user_store``; lo usan
        ``authenticate()`` y ``_handle_auth_me``).
      - ``handler._user_store`` (el caller pasa ``user_store=
        auth_manager.user_store`` a ``serve()``; lo usa ``_handle_auth_login``).
      ``serve()`` NO propaga uno al otro (``_rest_api.py:1120-1124`` pasa ambos
      por separado al handler), por eso el caller debe pasar ambos.

    Store vacío/ausente → degrada a solo-maestra con log informativo (el serve
    de producción con cero usuarios sigue arrancando igual). Fail-closed SOLO
    si falta la llave maestra (ya implementado en ``_build_rest_auth_or_fail``,
    no se toca).
    """
    if auth_manager is None:
        return None
    try:
        config_dir = os.path.dirname(os.path.abspath(ledger_path))
        dev_keys = dict(getattr(auth_manager, "_api_keys", {}) or {})
        auth_manager.enable_with_user_store(config_dir, dev_keys or None)
        try:
            n = auth_manager.user_store.user_count()
        except Exception:
            n = None
        if not n:
            logging.info(
                "CausaDB REST: user store vacío/ausente en %s — "
                "degradado a solo-maestra",
                config_dir,
            )
        else:
            logging.info(
                "CausaDB REST: user store con %d usuario(s) en %s",
                n, config_dir,
            )
    except Exception as e:
        logging.warning(
            "CausaDB REST: no se pudo adjuntar user store (%s) — "
            "sigo solo-maestra", e,
        )
    return auth_manager


def _serve_blocking(ledger_path: str, host, port, auth_manager=None) -> None:
    """Bloquea en serve_forever() con el wiring del daemon ya hecho.

    Extraido para reusar entre el path foreground y el path daemon.
    Asume que el caller ya instancio HarvesterDaemon, registro el
    global y instalo los signal handlers.
    """
    daemon = HarvesterDaemon(ledger_path=ledger_path)
    set_current_harvester_daemon(daemon)

    def _on_server_created(server):
        set_current_server(server)
        daemon.start()

    _attach_user_store(auth_manager, ledger_path)
    user_store = getattr(auth_manager, "user_store", None) if auth_manager is not None else None
    serve(ledger_path, host=host or "127.0.0.1", port=port or 7457,
          auth_manager=auth_manager, user_store=user_store,
          on_server_created=_on_server_created)


def _build_rest_auth_or_fail():
    """Construye AuthManager desde env/archivo o retorna error fail-fast.

    Fase 2: sin auth configurada `serve` NO arranca. Nada de clave de
    fábrica. Key vive en env ``CAUSADB_API_KEY`` o archivo 0600
    (``CAUSADB_API_KEY_FILE``). Returns (auth_manager, error_json).
    """
    from causadb._auth import AuthManager, load_rest_api_key, rest_auth_error_message
    key = load_rest_api_key()
    if not key:
        return None, json.dumps({"error": rest_auth_error_message()}, sort_keys=True)
    am = AuthManager()
    am.enable({key: "admin"})
    return am, None


def _serve_start(args) -> Tuple[int, str]:
    try:
        ledger_path = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        return (1, json.dumps({"error": str(e)}))
    # Fase 2 "Puertas con traba": fail-fast sin auth (antes de puertos/forks).
    auth_manager, auth_err = _build_rest_auth_or_fail()
    if auth_err is not None:
        return (1, auth_err)
    host = getattr(args, "host", None) or "127.0.0.1"
    # Fix: use getattr with default 7457 directly, not `or 7457` (0 is falsy)
    port = getattr(args, "port", 7457)

    # Resolve port 0 to ephemeral port if needed
    actual_port, port_file = _resolve_port(port)
    
    # Chequeo preventivo (Fix de Friccion 0) - solo para puertos no-ephemeral
    if port != 0 and _is_port_in_use(actual_port):
        return (0, json.dumps({"status": "already_running"}))

    platform = get_daemon()

    if getattr(args, "daemon", False):
        # Back-compat seguro: si ya corre el serve (con PID file) no duplicar
        if platform.is_running("serve"):
            return (0, json.dumps({"status": "already_running"}))

        # ORDEN FIJE (hallazgo 7): wiring ANTES de daemonize para que el
        # handler SIGTERM se herede a traves del fork en Linux.
        install_signal_handlers(ledger_path)
        set_current_daemon_name("serve")
        platform.daemonize("serve")

        try:
            _serve_blocking(ledger_path, host, actual_port, auth_manager=auth_manager)
        except OSError as e:
            msg = str(e).lower()
            if "in use" in msg or "already in use" in msg:
                remove_pidfile("serve")
                return (1, json.dumps({
                    "error": f"port {actual_port} already in use; likely a legacy "
                             f"`causadb serve start` without PID file is still running",
                    "hint": "run `pkill -f 'causadb serve start'` and retry",
                }, sort_keys=True))
            return (1, json.dumps({"error": str(e)}))
        finally:
            remove_pidfile("serve")
        return (0, json.dumps({"status": "stopped"}))
    else:
        # Path FOREGROUND (desarrollo). SIN daemonize.
        install_signal_handlers(ledger_path)
        try:
            _serve_blocking(ledger_path, host, actual_port, auth_manager=auth_manager)
        finally:
            from causadb._daemon_service import _current_harvester_daemon
            if _current_harvester_daemon is not None:
                _current_harvester_daemon.stop()
        return (0, json.dumps({"status": "stopped"}))


def _serve_stop(args) -> Tuple[int, str]:
    platform = get_daemon()
    if not platform.is_running("serve"):
        # Back-compat con serve LEGACY sin PID file (hallazgo 3)
        try:
            result = subprocess.run(
                ["pgrep", "-f", "causadb serve start"],
                capture_output=True, text=True, timeout=2,
            )
            pids = result.stdout.strip().splitlines() if result.stdout.strip() else []
        except Exception:
            pids = []
        if pids:
            return (0, json.dumps({
                "status": "not_running",
                "warning": "no PID file found, but legacy `causadb serve start` "
                           "processes detected:",
                "pids": pids,
                "hint": "run `pkill -f 'causadb serve start'` to stop them, "
                        "then `causadb serve start --daemon`",
            }, sort_keys=True))
        return (0, json.dumps({"status": "not_running"}))
    # kill con timeout extendido (hallazgo 2: harvest tick puede tardar 30s)
    killed = platform.kill("serve", timeout=35.0)
    return (0, json.dumps({"status": "stopped" if killed else "failed"}))
