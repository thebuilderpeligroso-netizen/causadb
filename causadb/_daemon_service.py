"""Systemd user service lifecycle for CausaDB daemon (D.1).

Provides :func:`install_service`, :func:`start_service`, :func:`stop_service`,
and :func:`status_service` for managing a ``systemctl --user`` unit that runs
the CausaDB daemon as a systemd-managed process (alternative to the
PID-file-based daemonization in ``_daemon.py``).

D.3 (Graceful shutdown)
------------------------
:func:`install_signal_handlers` registers a SIGTERM handler that:
1. Opens the ledger via :class:`~causadb._ledger_writer.LedgerWriter`
2. Writes a ``SYSTEM_BOOT`` event with ``{"action": "shutdown"}``
3. Calls ``server.shutdown()`` on the globally registered REST API server
4. Exits with code ``0``

The shutdown event is protected by ``fcntl.flock`` + ``os.fsync`` to prevent
truncation — the last event survives even under SIGTERM pressure.

F.2 (MCP auto-discovery)
-------------------------
:func:`register_mcp_discovery` writes (or updates) the CausaDB MCP server entry
in OpenCode's ``~/.config/opencode/mcp.json`` with the ``causadb-mcp`` command
and the environment variable ``CAUSADB_LEDGER_PATH`` pointing to the ledger.
Degradation is graceful: if the file/directory is not writable the function
returns ``False`` without crashing the caller.

Artículo VIII: este módulo NO introduce una abstracción nueva con una sola
implementación. ``install_service`` es la única función de escritura; no hay
clase ``ServiceManager`` porque no hay plataformas alternativas a systemd
(systemd es específico de Linux — en macOS/Windows no aplica).
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import logging
from typing import Tuple

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._workspace import CAUSADB_DIR, CONFIG_FILE
from causadb._harvester import Harvester
from causadb._harvest_source_shell import ShellHistorySource
from causadb._harvest_source_git import GitReflogSource
from causadb._harvest_source_browser import BrowserHistorySource
from causadb._harvest_source_aw import ActivityWatchSource
from causadb._harvest_source_mt5 import MT5HarvestSource
from causadb._harvest_source_jupyter import JupyterHarvestSource
from causadb._harvest_source_obsidian import ObsidianSource
from causadb._harvest_source_zotero import ZoteroSource
from causadb._harvest_source_filesystem import FilesystemSource
from causadb._config import CausaDBConfig
from causadb._harvest_source_gemini import GeminiHarvestSource
from causadb._harvest_source_opencode import OpenCodeHarvestSource
from causadb._harvest_source_hermes import HermesHarvestSource
from causadb._harvest_source_openjarvis import OpenJarvisHarvestSource
from causadb._harvest_source_claude import ClaudeHarvestSource
from causadb._harvest_source_grok import GrokHarvestSource
from causadb._harvest_source_codex import CodexHarvestSource
from causadb._harvest_source_cursor import CursorHarvestSource
from causadb._harvest_source_windsurf import WindsurfHarvestSource
from causadb._harvest_source_n8n import N8nHarvestSource
from causadb._harvest_source_freqtrade import FreqtradeHarvestSource

SERVICE_NAME = "causadb"
"""Name of the systemd user service unit (without the ``.service`` suffix)."""


def _resolve_project_root(ledger_path: str) -> str:
    """Resolve the filesystem harvest root for the daemon (BIT-CHR.97).

    Al daemonizar, ``_daemon.py`` ejecuta ``os.chdir("/")`` antes de
    instanciar ``HarvesterDaemon``; ``os.getcwd()`` en el daemon es ``/`` y
    el harvester filesystem dejaba de cosechar (os.walk("/") no matchea los
    paths relativos del cursor). Precedencia:

    1. ``CAUSADB_PROJECT_ROOT`` env var — override explícito, gana siempre.
    2. Primer ``watch_dirs`` del ``config.json`` del workspace (dirname del
       ledger) que resuelva a un directorio existente en disco. Los paths
       relativos se normalizan contra el workspace root, NO contra cwd.
    3. ``os.getcwd()`` — back-compat: daemon sin config.
    """
    env_root = os.environ.get("CAUSADB_PROJECT_ROOT")
    if env_root:
        return env_root

    config_path = os.path.join(os.path.dirname(ledger_path), CONFIG_FILE)
    try:
        with open(config_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    watch_dirs = data.get("watch_dirs") or []
    workspace_root = os.path.dirname(os.path.dirname(config_path))
    for d in watch_dirs:
        if not os.path.isabs(d):
            d = os.path.abspath(os.path.join(workspace_root, d))
        if os.path.isdir(d):
            return d

    return os.getcwd()

SYSTEMD_USER_DIR = os.path.expanduser("~/.config/systemd/user")
"""Target directory for systemd --user unit files."""

SERVICE_TEMPLATE = """\
[Unit]
Description=CausaDB causal ledger daemon
Documentation=https://github.com/causadb/causadb
After=network.target

[Service]
Type=simple
ExecStart={executable} serve start --ledger {ledger_path}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
"""Systemd unit file template.

``{executable}`` and ``{ledger_path}`` are substituted at install time via
:meth:`str.format`.
"""

class HarvesterDaemon:
    def __init__(self, ledger_path: str):
        self.ledger_path = ledger_path
        self.harvester = Harvester(ledger_path)
        self.register_sources()
        self.interval = int(os.environ.get("CAUSADB_HARVEST_INTERVAL", 5)) * 60
        self.timer = None
        self._shutdown_event = threading.Event()

    def register_sources(self):
        self.harvester.register_source(ShellHistorySource(ledger_path=self.ledger_path))
        # Follow-up BIT-CHR.97: pasar source_path al GitReflogSource. Sin él,
        # _repo_dir() cae a os.getcwd() que tras daemonize() es "/" →
        # detect() False → fuente git muerta en modo daemon. Reusamos el
        # mismo helper que FilesystemSource (precedencia: env > watch_dirs
        # del config.json > getcwd).
        git_repo_path = _resolve_project_root(self.ledger_path)
        self.harvester.register_source(GitReflogSource(source_path=git_repo_path, ledger_path=self.ledger_path))
        self.harvester.register_source(BrowserHistorySource(ledger_path=self.ledger_path))
        self.harvester.register_source(ActivityWatchSource(ledger_path=self.ledger_path))
        self.harvester.register_source(MT5HarvestSource(ledger_path=self.ledger_path))
        self.harvester.register_source(N8nHarvestSource(ledger_path=self.ledger_path))
        self.harvester.register_source(FreqtradeHarvestSource(ledger_path=self.ledger_path))
        self.harvester.register_source(JupyterHarvestSource(ledger_path=self.ledger_path))
        self.harvester.register_source(ObsidianSource(vault_path=os.environ.get("OBSIDIAN_VAULT_PATH", "/tmp/obsidian"), ledger_path=self.ledger_path))
        self.harvester.register_source(ZoteroSource(api_base=os.environ.get("ZOTERO_API_BASE", "http://127.0.0.1:23123/api"), ledger_path=self.ledger_path))
        
        project_root = _resolve_project_root(self.ledger_path)
        config = CausaDBConfig(ledger_path=self.ledger_path)
        self.harvester.register_source(
            FilesystemSource(
                ledger_path=self.ledger_path,
                project_root=project_root,
                config=config,
            )
        )

        # Daemon wiring — fuentes de agentes (BIT-CHR.41; docs/design_index.md)
        # los stores reales; Artículo VIII: el motor ya tiene 2 puntitas).
        # gemini-cli: ~/.gemini/tmp/<proyecto>/chats/ (env override).
        # opencode: ~/.local/share/opencode/opencode.db (env override).
        # detect() False si el store no existe → no generan eventos.
        # Controladas por CAUSADB_ENABLE_AGENT_SOURCES=1 (default off) para
        # evitar OOM en primer arranque con stores grandes.
        if os.environ.get("CAUSADB_ENABLE_AGENT_SOURCES") == "1":
            self.harvester.register_source(
                GeminiHarvestSource(ledger_path=self.ledger_path)
            )
            self.harvester.register_source(
                OpenCodeHarvestSource(ledger_path=self.ledger_path)
            )
            self.harvester.register_source(
                HermesHarvestSource(ledger_path=self.ledger_path)
            )
            self.harvester.register_source(
                OpenJarvisHarvestSource(ledger_path=self.ledger_path)
            )
            self.harvester.register_source(
                ClaudeHarvestSource(ledger_path=self.ledger_path)
            )
            self.harvester.register_source(
                GrokHarvestSource(ledger_path=self.ledger_path)
            )
            self.harvester.register_source(
                CodexHarvestSource(ledger_path=self.ledger_path)
            )
            self.harvester.register_source(
                CursorHarvestSource(ledger_path=self.ledger_path)
            )
            self.harvester.register_source(
                WindsurfHarvestSource(ledger_path=self.ledger_path)
            )

    def _harvest_tick(self):
        if self._shutdown_event.is_set():
            return

        # Rastreo del thread activo: stop() (SIGTERM) hace join sobre él para
        # garantizar que ningún append al ledger ocurra DESPUÉS del shutdown.
        self._active_thread = threading.current_thread()
        logging.info("Starting scheduled harvest.")
        try:
            results = self.harvester.harvest_all(dry_run=False, stop_event=self._shutdown_event)
            for source_type, count in results.items():
                logging.info(f"Harvested {count} events from {source_type}")
        except Exception as e:
            logging.error(f"Error during harvest: {e}")
        finally:
            self._active_thread = None

        # GAP-01 — cobertura: si hay stores de gemini-cli con sesiones sin
        # cosechar, avisarlo (no falla el tick; el gap se cierra solo en la
        # próxima corrida). Fail-open: sin cursores → todos gaps (mejor
        # avisar de más que callar cobertura real).
        try:
            from causadb._store_discovery import coverage_gaps
            gemini = self.harvester._sources.get("gemini")
            if gemini is not None:
                gaps = coverage_gaps(gemini.chats_dirs, self.ledger_path)
                if gaps:
                    logging.warning(
                        "GAP-01: %d store(s) de gemini-cli con sesiones sin "
                        "cosechar: %s", len(gaps), ", ".join(gaps)
                    )
        except Exception as e:
            logging.warning(f"Coverage gap check failed: {e}")

        if self._shutdown_event.is_set():
            return  # stop() pidió parar: no re-agendar
        self.timer = threading.Timer(self.interval, self._harvest_tick)
        self.timer.start()

    def start(self):
        logging.info(f"Starting harvest daemon. Interval: {self.interval/60} mins")
        # Auditoría F (BIT-CHR.41; ver docs/design_index.md): el PRIMER tick va en
        # background. El backfill inicial puede tardar (gemini/opencode
        # stores grandes); agendarlo con Timer(0) NO congela el arranque
        # del serve/watcher que invocó al daemon.
        self.timer = threading.Timer(0.0, self._harvest_tick)
        self.timer.start()

    def stop(self):
        self._shutdown_event.set()
        if self.timer:
            self.timer.cancel()
        # Esperar (acotado) al harvest en curso: después de stop() ningún
        # evento nuevo puede llegar al ledger (el shutdown queda último —
        # D.3 bajo presión de SIGTERM con harvest concurrente). El harvest
        # aborta cooperativamente el backfill (stop_event en harvest_all),
        # así que el join retorna en el tiempo de UNA fuente; 5s es margen
        # suficiente para que el append atómico de la fuente en curso
        # termine. Si una fuente puntual excede 5s (store enorme), SIGKILL
        # corta el append a mitad — un evento perdido, no ledger corrupto
        # (los appends son una línea atómica).
        active = getattr(self, "_active_thread", None)
        if active is not None and active is not threading.current_thread():
            active.join(timeout=5.0)
        logging.info("Harvest daemon stopped.")

def _find_causadb_executable() -> str:
    """Find the ``causadb`` CLI executable on ``PATH``.

    Returns the first match from:
    1. ``shutil.which("causadb")`` (installed via pip).
    2. ``sys.argv[0]`` (running from source / ``python -m``).
    3. Literal ``"causadb"`` as last-resort (relies on PATH at runtime).
    """
    exe = shutil.which("causadb")
    if exe is not None:
        return exe
    if sys.argv and sys.argv[0]:
        return sys.argv[0]
    return "causadb"


def install_service(ledger_path: str) -> Tuple[bool, str]:
    """Install (write) the ``causadb.service`` systemd user unit file.

    Creates ``~/.config/systemd/user/causadb.service`` with an ``ExecStart``
    that runs ``causadb serve start --ledger <ledger_path>`` under systemd
    supervision (``Type=simple``, ``Restart=on-failure``).

    Args:
        ledger_path: Absolute path to the ledger file that the daemon
            should serve.

    Returns:
        ``(True, absolute_path_to_service_file)`` on success.
        ``(False, error_message)`` on failure (e.g. permission denied).
    """
    executable = _find_causadb_executable()
    # Escape spaces for systemd ExecStart (systemd splits unquoted args at spaces)
    ledger_path_escaped = ledger_path.replace(" ", "\\x20")
    service_content = SERVICE_TEMPLATE.format(
        executable=executable,
        ledger_path=ledger_path_escaped,
    )
    try:
        os.makedirs(SYSTEMD_USER_DIR, exist_ok=True)
        service_path = os.path.join(SYSTEMD_USER_DIR, f"{SERVICE_NAME}.service")
        with open(service_path, "w") as f:
            f.write(service_content)
        return (True, service_path)
    except OSError as exc:
        return (False, str(exc))


def start_service() -> Tuple[bool, str]:
    """Start the ``causadb.service`` systemd user service.

    Runs ``systemctl --user start causadb``.

    Returns:
        ``(True, stdout)`` on success.
        ``(False, stderr_or_message)`` on failure (service not installed,
        systemctl not available, etc.).
    """
    return _systemctl_cmd(["start", SERVICE_NAME])


def stop_service() -> Tuple[bool, str]:
    """Stop the ``causadb.service`` systemd user service.

    Runs ``systemctl --user stop causadb``.

    Returns:
        ``(True, stdout)`` on success.
        ``(False, stderr_or_message)`` on failure.
    """
    return _systemctl_cmd(["stop", SERVICE_NAME])


def restart_service() -> Tuple[bool, str]:
    """Restart the ``causadb.service`` systemd user service (Fase U1).

    Runs ``systemctl --user restart causadb``. Used by ``causadb restart``
    when the unit is installed and active, so production is refreshed under
    systemd supervision instead of spawning duplicate forks (BIT-CHR.101).

    Returns:
        ``(True, stdout)`` on success.
        ``(False, stderr_or_message)`` on failure.
    """
    return _systemctl_cmd(["restart", SERVICE_NAME])


def status_service() -> Tuple[bool, str]:
    """Check whether the ``causadb.service`` systemd user service is active.

    Runs ``systemctl --user is-active causadb`` and parses the output:

    - If the exit code is 0 and the output is ``"active"`` → ``(True, "active")``.
    - Otherwise → ``(False, raw_output)``.

    Returns:
        ``(True, "active")`` if the service is running.
        ``(False, raw_message)`` if inactive, failed, or systemctl unavailable.
    """
    return _systemctl_cmd(["is-active", SERVICE_NAME])


def _systemctl_cmd(args: list) -> Tuple[bool, str]:
    """Run a ``systemctl --user`` subcommand and return a parsed result.

    Args:
        args: systemctl subcommand and arguments (e.g. ``["start", "causadb"]``).

    Returns:
        ``(True, stripped_stdout)`` when ``returncode == 0``.
        ``(False, stripped_stderr_or_stdout)`` otherwise.

    Fase U1 (design_unified_process_command): timeout duro de 30s — un
    systemctl colgado (dbus muerto, etc.) no debe bloquear ``causadb
    restart`` indefinidamente (CRITICAL auditor NR1).
    """
    full_cmd = ["systemctl", "--user"] + args
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return (False, "systemctl timed out")
    except (OSError, FileNotFoundError) as exc:
        return (False, f"systemctl not available: {exc}")

    output = (result.stdout or "").strip()
    if result.returncode == 0:
        return (True, output)
    else:
        return (False, output or (result.stderr or "").strip())


# ---------------------------------------------------------------------------
# D.3 — Graceful shutdown via signal handlers
# ---------------------------------------------------------------------------

_current_server = None
_current_harvester_daemon = None
_current_daemon_name = None

def set_current_server(server) -> None:
    global _current_server
    _current_server = server

def set_current_harvester_daemon(daemon: HarvesterDaemon) -> None:
    global _current_harvester_daemon
    _current_harvester_daemon = daemon

def set_current_daemon_name(name: str) -> None:
    """Registrar el nombre del daemon actual (para cleanup condicional del PID file).

    H-OPS.1 hallazgo 1: el _sigterm_handler hace os._exit(0) que salta el
    finally del caller. Para 'serve' (path daemon), el handler debe limpiar
    el PID file antes de salir. Otros daemons (vigilante/harvest) no usan
    este handler en path daemon (tienen su propio finally), asi que el
    cleanup es condicional al nombre registrado.
    """
    global _current_daemon_name
    _current_daemon_name = name


def install_signal_handlers(ledger_path: str) -> None:
    """Register a SIGTERM handler that flushes the ledger and shuts down gracefully.
    """
    def _sigterm_handler(signum, frame):
        # 1. Detener el harvest daemon PRIMERO (join del harvest en curso,
        #    acotado a 30s): ningún append nuevo puede llegar al ledger
        #    después del shutdown event (D.3 bajo SIGTERM).
        global _current_server, _current_harvester_daemon
        if _current_harvester_daemon is not None:
            try:
                _current_harvester_daemon.stop()
            except Exception:
                pass

        # 2. Write shutdown event to ledger (ahora queda como ÚLTIMO)
        try:
            writer = LedgerWriter(ledger_path)
            event = CanonicalEvent(
                event_type=EventType.SYSTEM_BOOT,
                ctx_id="system",
                source="causadb:daemon",
                source_type="agent",
                payload={"action": "shutdown"},
            )
            writer.append(event)
        except Exception:
            pass 

        # 3. Graceful shutdown of the REST API server (if set)
        if _current_server is not None:
            try:
                _current_server.shutdown()
            except Exception:
                pass

        # 4. Limpiar PID file si el daemon actual es 'serve' (hallazgo 1).
        # El handler hace os._exit(0) que salta el finally del caller; sin
        # este cleanup el PID file queda huerfano. Condicionado al nombre
        # registrado via set_current_daemon_name para no tocar PID files
        # de otros daemons (vigilante/harvest limpian en su propio finally).
        if _current_daemon_name == "serve":
            try:
                from causadb._daemon import remove_pidfile
                remove_pidfile("serve")
            except Exception:
                pass

        # 5. Exit cleanly
        os._exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)


# ---------------------------------------------------------------------------
# F.2 — MCP auto-discovery for OpenCode
# ---------------------------------------------------------------------------


def register_mcp_discovery(ledger_path: str) -> bool:
    mcp_dir = os.path.join(os.path.expanduser("~"), ".config", "opencode")
    mcp_path = os.path.join(mcp_dir, "mcp.json")

    try:
        os.makedirs(mcp_dir, exist_ok=True)
        config = {}
        if os.path.exists(mcp_path):
            try:
                with open(mcp_path, "r") as f:
                    config = json.load(f)
            except (json.JSONDecodeError, OSError):
                config = {}

        if "mcpServers" not in config:
            config["mcpServers"] = {}

        config["mcpServers"]["causadb"] = {
            "command": "causadb-mcp",
            "args": [],
            "env": {
                "CAUSADB_LEDGER_PATH": ledger_path,
            },
        }

        with open(mcp_path, "w") as f:
            json.dump(config, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        return True
    except (OSError, PermissionError):
        return False


# ---------------------------------------------------------------------------
# Fase U2 — RESTART_COMPLETED event logging
# ---------------------------------------------------------------------------


def record_restart_completed(
    ledger_path: str,
    mode: str,
    unit_state: str,
    systemctl_action: str,
    systemctl_ok: bool,
) -> bool:
    """Append a RESTART_COMPLETED event to the ledger.

    This event serves as the source of truth for the last restart time
    (replacing any orphan field). Called after a successful restart
    (both systemd and legacy modes).

    Args:
        ledger_path: Absolute path to the ledger file.
        mode: "systemd" or "legacy".
        unit_state: "active", "inactive", "none", etc.
        systemctl_action: "restart", "start", or "none" (legacy).
        systemctl_ok: Whether the systemctl command succeeded.

    Returns:
        True if event was written, False on any error (degraded gracefully).
    """
    try:
        from causadb._event_schema import CanonicalEvent
        from causadb._event_types import EventType
        from causadb._ledger_writer import LedgerWriter
        from types import MappingProxyType
        import time

        payload = MappingProxyType({
            "mode": mode,
            "unit_state": unit_state,
            "timestamp": time.time(),
            "systemctl_action": systemctl_action,
            "systemctl_ok": systemctl_ok,
        })
        event = CanonicalEvent(
            event_type=EventType.RESTART_COMPLETED,
            ctx_id="restart",
            source="causadb:restart",
            source_type="agent",
            payload=payload,
        )
        writer = LedgerWriter(ledger_path)
        writer.append(event)
        return True
    except Exception:
        # Degradación suave: no romper el restart por un fallo de logging
        return False
