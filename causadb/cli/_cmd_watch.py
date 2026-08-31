"""F.11.4 — Comando unificado `causadb watch`.

Thin orchestration wrapper (Artículo II): starts each service as an
independent subprocess with --daemon, so each service manages its own
PID lifecycle. The watch command itself is a thin coordinator that
returns immediately after launching.

Artículo VII: mínimo funcional. Artículo II: no duplica lógica.
"""

import json
import os
import site
import socket
import subprocess
import sys
import time
from typing import Tuple, Optional

from causadb._daemon import get_daemon
from causadb._shell_hook import flush as flush_shell_hook
from causadb._systemd_utils import get_unit_status
from causadb._workspace import resolve_ledger, NoWorkspaceError


def _is_source_install() -> bool:
    """Detect if causadb is running from a source checkout (not installed in site-packages).

    Returns True when causadb.__file__ is NOT inside any site-packages directory.
    This covers the case where causadb runs from a git clone / source directory
    without being installed (or installed in a way that doesn't put it in site-packages).

    Returns:
        True if running from source, False if installed via pip (regular or editable).
    """
    import causadb

    causadb_file = causadb.__file__
    if causadb_file is None:
        # Built-in or frozen - treat as not source
        return False

    # Normalize paths for comparison
    causadb_file = os.path.realpath(causadb_file)

    # Get all site-packages directories
    site_packages = set()
    for sp in site.getsitepackages():
        site_packages.add(os.path.realpath(sp))
    user_site = site.getusersitepackages()
    if user_site:
        site_packages.add(os.path.realpath(user_site))

    # Check if causadb.__file__ is inside any site-packages directory
    for sp in site_packages:
        try:
            # Use commonpath to check if causadb_file is under site-packages
            if os.path.commonpath([causadb_file, sp]) == sp:
                return False
        except ValueError:
            # Different drives on Windows, etc.
            pass

    # Not in any site-packages → source install
    return True


def cmd_watch(args) -> Tuple[int, str]:
    """Route ``causadb watch start|stop|status``."""
    try:
        ledger = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        if args.action == "stop":
            return (0, json.dumps({"status": "stopped"}))
        return (1, json.dumps({"error": str(e)}))

    no_proxy = getattr(args, "no_proxy", False)
    no_serve = getattr(args, "no_serve", False)
    format = getattr(args, "format", None)

    # Auto-detect format: text if TTY, json if piped
    if format is None:
        import sys
        format = "text" if sys.stdout.isatty() else "json"

    if args.action == "start":
        return _watch_start(ledger, no_proxy, no_serve)
    elif args.action == "stop":
        return _watch_stop(ledger)
    elif args.action == "status":
        return _watch_status(ledger, format=format)
    else:
        return (1, json.dumps({"error": f"Unknown action: {args.action}"}))


import logging

log = logging.getLogger(__name__)

# Timeout for daemon startup polling (seconds per service)
_DAEMON_STARTUP_TIMEOUT = 10.0
_DAEMON_STARTUP_POLL_INTERVAL = 0.1

# Default serve port
_SERVE_DEFAULT_PORT = 7457


def _is_serve_port_occupied() -> bool:
    """Check if the default serve port (7457) is already in use.

    Used to detect legacy/systemd serve processes that don't create PID files.
    Returns True if something is listening on 127.0.0.1:7457.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", _SERVE_DEFAULT_PORT)) == 0
    except OSError:
        return False


def _is_serve_already_running() -> bool:
    """Check if serve is already running via PID file or port occupancy.

    Returns True if either:
    - PID file exists and process is alive (get_daemon().is_running)
    - Port 7457 is occupied (legacy/systemd serve)
    """
    daemon = get_daemon()
    if daemon.is_running("serve"):
        return True
    return _is_serve_port_occupied()


def _start_daemon_service(
    ledger: str,
    service_name: str,
    results: dict,
    args: list,
) -> None:
    """Start a daemon service via subprocess and poll until it's ready.

    Replaces the buggy wait() + sleep(0.3) + single check pattern with
    active polling of is_running() until the daemon writes its PID file.

    Args:
        ledger: Ledger path to pass to the service.
        service_name: Name of the service (for results key and logging).
        results: Dict to update with the service status.
        args: Command-line args for the service (e.g., ["vigilante", "start", "--ledger", ledger, "--daemon"]).
    """
    # Prepare environment for subprocess
    env = os.environ.copy()

    # If running from source (not installed in site-packages), add package parent to PYTHONPATH
    # so the subprocess can find the causadb module.
    if _is_source_install():
        import causadb
        # causadb.__file__ is .../causadb/cli/__init__.py
        # Go up 2 levels: cli/ -> causadb/ (package) -> package parent (contains causadb/ package)
        pkg_parent = os.path.dirname(os.path.dirname(causadb.__file__))
        # Append to existing PYTHONPATH (don't overwrite)
        existing_pythonpath = env.get('PYTHONPATH', '')
        if existing_pythonpath:
            env['PYTHONPATH'] = pkg_parent + os.pathsep + existing_pythonpath
        else:
            env['PYTHONPATH'] = pkg_parent

    proc = subprocess.Popen(
        [sys.executable, "-m", "causadb.cli.main"] + args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    # Reap the parent process immediately (it exits after double-fork).
    # This prevents zombie processes.
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        # Parent didn't exit in 1s - unexpected but not fatal.
        pass

    # Poll is_running() until daemon is ready or timeout expires.
    daemon = get_daemon()
    start_time = time.time()
    while time.time() - start_time < _DAEMON_STARTUP_TIMEOUT:
        if daemon.is_running(service_name):
            results[service_name] = "started"
            return
        time.sleep(_DAEMON_STARTUP_POLL_INTERVAL)

    # Timeout expired - daemon never became ready.
    log.warning(
        "%s: timeout waiting for daemon to be ready after %.1fs",
        service_name,
        _DAEMON_STARTUP_TIMEOUT,
    )
    results[service_name] = "failed"


def _watch_start(ledger: str, no_proxy: bool = False, no_serve: bool = False,
                 skip: frozenset = frozenset()) -> Tuple[int, str]:
    """Lanzar los forks de watch.

    Fase U1: ``skip`` lista servicios gobernados por el unit systemd
    (serve/harvest cuando el ExecStart corre ``serve start``). Cada
    servicio en skip reporta ``"skipped_by_unit"`` ANTES de lanzarse —
    evita la ley doble (dos harvesters / dos serves, BIT-CHR.101).
    """
    # Ensure ledger path is absolute for subprocesses that may have different CWD
    ledger = os.path.abspath(ledger)
    results = {"ledger": ledger}

    # R.2 — Detect abrupt_close and auto-generate RESUME.md before starting services.
    # This gives the agent context about what happened in the previous session.
    resume_info = _detect_and_write_resume(ledger)
    if resume_info:
        results["resume"] = resume_info

    # Start vigilante as daemon subprocess
    if "vigilante" in skip:
        results["vigilante"] = "skipped_by_unit"
    else:
        _start_daemon_service(
            ledger,
            "vigilante",
            results,
            ["vigilante", "start", "--ledger", ledger, "--daemon"],
        )

    # Start mcp-proxy as daemon subprocess
    if "mcp_proxy" in skip:
        results["mcp_proxy"] = "skipped_by_unit"
    else:
        _start_daemon_service(
            ledger,
            "mcp_proxy",
            results,
            ["mcp-proxy", "start", "--ledger", ledger, "--daemon"],
        )

    # Start LLM capture proxy server (unless --no-proxy or covered by unit)
    if "proxy_server" in skip:
        results["proxy_server"] = "skipped_by_unit"
    elif not no_proxy:
        _start_daemon_service(
            ledger,
            "proxy_server",
            results,
            ["proxy-server", "start", "--ledger", ledger, "--daemon"],
        )
    else:
        results["proxy_server"] = "skipped"

    # Start agent-store harvester as daemon subprocess (BIT-CHR.41
    # §5.4): harvest pasivo de gemini-cli + opencode, intervalo propio.
    if "harvest" in skip:
        results["harvest"] = "skipped_by_unit"
    else:
        _start_daemon_service(
            ledger,
            "harvest",
            results,
            ["harvest", "start", "--ledger", ledger, "--daemon"],
        )

    # H-OPS.1 Fase 2 — Start serve as daemon subprocess (REST API).
    # Skip si ya corre (back-compat), si --no-serve (headless mode) o si
    # el unit systemd lo gobierna (Fase U1).
    if "serve" in skip:
        results["serve"] = "skipped_by_unit"
    elif not no_serve:
        if _is_serve_already_running():
            results["serve"] = "already_running"
        else:
            _start_daemon_service(
                ledger,
                "serve",
                results,
                ["serve", "start", "--ledger", ledger, "--daemon"],
            )
    else:
        results["serve"] = "skipped"

    return (0, json.dumps(results, sort_keys=True))


def _watch_stop(ledger: str, skip: frozenset = frozenset()) -> Tuple[int, str]:
    """H-OPS.1 Fase 2 — Orden critico (hallazgo 4).

    Matar PRIMERO los productores de eventos (vigilante, mcp_proxy,
    proxy_server, harvest) -> flush shell hook -> distill -> score ->
    AL FINAL matar serve (que escribe el shutdown event final al ledger).
    Si se matara serve primero, el shutdown event quedaria antes que
    los flush/distill/score, rompiendo la consistencia del ledger.

    Fase U1 — ``skip``: servicios gobernados por el unit systemd. Los
    servicios en skip NI SE INTENTAN matar (reportan
    ``"skipped_by_unit"``): impide enviar SIGTERM al pidfile huérfano
    de un serve/harvest que en realidad vive bajo supervisión systemd.
    INVARIANTE SAGRADO (guard T11): esta fase es pidfiles-only —
    jamás invoca subprocess/pgrep.
    """
    results = {}
    daemon = get_daemon()
    # Matar PRIMERO los productores de eventos
    if "vigilante" in skip:
        results["vigilante_stopped"] = "skipped_by_unit"
    else:
        results["vigilante_stopped"] = daemon.kill("vigilante", timeout=35.0)
    if "mcp_proxy" in skip:
        results["mcp_proxy_stopped"] = "skipped_by_unit"
    else:
        results["mcp_proxy_stopped"] = daemon.kill("mcp_proxy")
    if "proxy_server" in skip:
        results["proxy_server_stopped"] = "skipped_by_unit"
    else:
        results["proxy_server_stopped"] = daemon.kill("proxy_server", timeout=35.0)
    if "harvest" in skip:
        results["harvester_stopped"] = "skipped_by_unit"
    else:
        results["harvester_stopped"] = daemon.kill("harvest", timeout=35.0)

    # Flush shell hook queue (despues de matar daemons, antes de distill).
    # Captura comandos que corrieron durante la sesion.
    shell_flushed = _flush_shell_hook(ledger)
    results["shell_hook"] = shell_flushed

    # F.13.4.4 — Auto-distill on watch stop (despues de matar daemons,
    # antes del score que viene en F.13.3.5).
    distilled = _auto_distill(ledger)
    results["distill"] = distilled

    # F.13.3.5 — Auto-score on watch stop (DESPUES de distill, orden critico
    # del roadmap: daemon stop -> distill -> score). Computa el score de la
    # sesion y lo persiste como SCORE_RECORDED al ledger.
    scored = _auto_score(ledger)
    results["score"] = scored

    # AL FINAL matar serve con timeout extendido (para que el shutdown
    # event alcance a escribirse — hallazgo 2: harvest tick puede tardar 30s).
    if "serve" in skip:
        results["serve_stopped"] = "skipped_by_unit"
    else:
        results["serve_stopped"] = daemon.kill("serve", timeout=35.0)

    return (0, json.dumps(results, sort_keys=True))


def _auto_score(ledger: str) -> dict:
    """F.13.3.5 — Compute session score and log ``SCORE_RECORDED``.

    Degradación suave (Artículo V): si score falla por cualquier motivo,
    retorna ``{"status": "skipped", "reason": str(e)}`` y NO propaga el
    error a ``watch stop``. El usuario siempre puede hacer ``watch stop``
    sin que se rompa por un problema en el scoring.

    Orden de ejecución (roadmap F.13.3.5):
        daemon stop → distill (F.13.4.4) → score (acá).

    El score se computa sobre el estado ACTUAL del ledger (que ya incluye
    los SKILL_CREATED del distill si corrió), y se persiste como un evento
    SCORE_RECORDED via ``LedgerWriter`` (Artículo I — Ledger-first: la
    persistencia ES el evento en el ledger).

    Returns:
        Dict con uno de:
        - ``{"status": "ok", "overall_score": float, "score_event_id": str}``
        - ``{"status": "skipped", "reason": str}``
    """
    try:
        from causadb._score import compute_score
        from causadb._ledger_writer import LedgerWriter
        from causadb._event_schema import CanonicalEvent
        from causadb._event_types import EventType
        from types import MappingProxyType

        # Compute score sobre el estado actual del ledger.
        score_result = compute_score(ledger)

        # Ensamblar el payload del SCORE_RECORDED. Las keys matchean 1:1
        # lo que el handler en _replay_engine.py:290-301 lee
        # (overall_score, churn_score, waste_score, survival_score,
        # session_id, weights_used, correlation_method).
        payload = MappingProxyType({
            "overall_score": score_result.get("overall_score", 0.0),
            "churn_score": score_result.get("churn_score", 0.0),
            "waste_score": score_result.get("waste_score", 0.0),
            "survival_score": score_result.get("survival_score", 0.0),
            "session_id": "watch_stop",
            "weights_used": score_result.get("weights_used", {}),
            "correlation_method": score_result.get("correlation_method", "timestamp_proximity"),
        })
        event = CanonicalEvent(
            event_type=EventType.SCORE_RECORDED,
            ctx_id="watch_stop",
            source="causadb:watch_stop",
            source_type="agent",
            payload=payload,
        )
        writer = LedgerWriter(ledger)
        writer.append(event)

        return {
            "status": "ok",
            "overall_score": score_result.get("overall_score"),
            "score_event_id": event.event_id,
        }
    except Exception as e:
        # Degradación suave: loggeamos el reason pero no rompemos watch stop.
        return {"status": "skipped", "reason": str(e)}


def _auto_distill(ledger: str) -> dict:
    """F.13.4.4 — Run distill on the closing session and register skills.

    Degradación suave (Artículo V): si distill falla, retorna
    ``{"status": "skipped", "reason": str(e)}`` y NO propaga el error
    a ``watch stop``. El usuario siempre puede hacer ``watch stop`` sin
    que se rompa por un problema en distill.

    Umbral: ``distill_min_events`` (default 50). Por debajo no vale la
    pena distilar — el profile no tendría suficiente señal para producir
    skills significativos. Configurable via ``CausaDBConfig`` en el
    futuro (por ahora hardcodeado a 50, igual que el roadmap).

    Orden de ejecución (roadmap F.13.4.4):
        daemon stop → distill (acá) → score (F.13.3.5, Ola 6d).

    Returns:
        Dict con uno de:
        - ``{"status": "ok", "skills_produced": int, "skill_ids": list}``
        - ``{"status": "skipped", "reason": str}``
    """
    try:
        from causadb._distill import distill
        from causadb._skill_registry import register_skill
        from causadb._dag_cache import get_last_ledger_seq

        # --- Umbral: no distilar sessions chicas ---
        last_seq = get_last_ledger_seq(ledger)
        distill_min_events = 50  # configurable via CausaDBConfig en futuro
        if last_seq < distill_min_events:
            return {
                "status": "skipped",
                "reason": f"below distill_min_events ({distill_min_events})",
            }

        # --- Run distill ---
        result = distill(ledger)
        skills = result.get("skills", [])

        # --- Register each skill via register_skill ---
        # Mapear las keys del output de distill (type/name/content/...)
        # a las keys canónicas de register_skill (skill_type/skill_name/...).
        skill_ids = []
        for skill in skills:
            payload = {
                "skill_type": skill.get("type"),
                "skill_name": skill.get("name"),
                "content": skill.get("content"),
                "token_count": skill.get("token_count"),
                "confidence": skill.get("confidence"),
                "source_session": "watch_stop",
            }
            skill_id = register_skill(ledger, payload)
            skill_ids.append(skill_id)

        return {
            "status": "ok",
            "skills_produced": len(skills),
            "skill_ids": skill_ids,
        }
    except Exception as e:
        # Degradación suave: loggeamos el reason pero no rompemos watch stop.
        return {"status": "skipped", "reason": str(e)}


def _watch_status(ledger: str, format: str = "text") -> Tuple[int, str]:
    """Get status of all CausaDB services including systemd unit.

    Args:
        ledger: Ledger path.
        format: Output format - "text" (human-readable) or "json".

    Returns:
        Tuple of (exit_code, output_string).
    """
    daemon = get_daemon()
    unit_status = get_unit_status()

    # Check PID files for each service
    pid_status = {
        "vigilante": daemon.is_running("vigilante"),
        "mcp_proxy": daemon.is_running("mcp_proxy"),
        "proxy_server": daemon.is_running("proxy_server"),
        "harvest": daemon.is_running("harvest"),
        "serve": daemon.is_running("serve"),
    }

    # Determine watch_forks status
    # If systemd unit is active and covers serve/harvest, those are "skipped_by_unit"
    watch_forks = {}
    if unit_status.active and unit_status.installed:
        # Check if unit covers serve/harvest (ExecStart contains "serve start")
        covers_serve_harvest = "serve start" in unit_status.exec_start
        watch_forks = {
            "vigilante": "running" if pid_status["vigilante"] else "stopped",
            "mcp_proxy": "running" if pid_status["mcp_proxy"] else "stopped",
            "proxy_server": "running" if pid_status["proxy_server"] else "stopped",
            "harvest": "skipped_by_unit" if covers_serve_harvest else ("running" if pid_status["harvest"] else "stopped"),
            "serve": "skipped_by_unit" if covers_serve_harvest else ("running" if pid_status["serve"] else "stopped"),
        }
    else:
        watch_forks = {
            "vigilante": "running" if pid_status["vigilante"] else "stopped",
            "mcp_proxy": "running" if pid_status["mcp_proxy"] else "stopped",
            "proxy_server": "running" if pid_status["proxy_server"] else "stopped",
            "harvest": "running" if pid_status["harvest"] else "stopped",
            "serve": "running" if pid_status["serve"] else "stopped",
        }

    # Get last restart from ledger (RESTART_COMPLETED event)
    last_restart = _get_last_restart(ledger)

    result = {
        "mode": "systemd" if (unit_status.installed and unit_status.active) else "legacy",
        "systemd_unit": {
            "installed": unit_status.installed,
            "active": unit_status.active,
            "state": unit_status.state,
            "enabled": unit_status.enabled,
            "main_pid": unit_status.main_pid,
            "exec_start": unit_status.exec_start,
            "since": unit_status.since,
            "load_error": unit_status.load_error,
        },
        "watch_forks": watch_forks,
        "last_restart": last_restart,
    }

    if format == "json":
        return (0, json.dumps(result, sort_keys=True))
    else:
        # Human-readable text format
        return (0, _format_status_text(result))


def _get_last_restart(ledger: str) -> Optional[dict]:
    """Get the last RESTART_COMPLETED event from the ledger."""
    try:
        from causadb._ledger_reader import LedgerReader
        from causadb._event_types import EventType

        reader = LedgerReader(ledger)
        events = reader.read_all()
        for event in reversed(events):
            if event.event_type == EventType.RESTART_COMPLETED:
                payload = dict(event.payload)
                return {
                    "mode": payload.get("mode"),
                    "unit_state": payload.get("unit_state"),
                    "timestamp": payload.get("timestamp"),
                    "systemctl_action": payload.get("systemctl_action"),
                    "systemctl_ok": payload.get("systemctl_ok"),
                }
    except Exception:
        pass
    return None


def _format_status_text(result: dict) -> str:
    """Format status as human-readable text."""
    lines = []
    lines.append(f"Mode: {result['mode']}")
    lines.append("")

    sysd = result["systemd_unit"]
    lines.append("Systemd Unit:")
    lines.append(f"  Installed: {sysd['installed']}")
    lines.append(f"  Active: {sysd['active']}")
    lines.append(f"  State: {sysd['state']}")
    lines.append(f"  Enabled: {sysd['enabled']}")
    if sysd["main_pid"]:
        lines.append(f"  Main PID: {sysd['main_pid']}")
    if sysd["since"]:
        lines.append(f"  Since: {sysd['since']}")
    if sysd["exec_start"]:
        lines.append(f"  ExecStart: {sysd['exec_start']}")
    if sysd["load_error"]:
        lines.append(f"  Load Error: {sysd['load_error']}")
    lines.append("")

    lines.append("Watch Forks:")
    for name, status in result["watch_forks"].items():
        lines.append(f"  {name}: {status}")
    lines.append("")

    lr = result["last_restart"]
    if lr:
        lines.append("Last Restart:")
        lines.append(f"  Mode: {lr['mode']}")
        lines.append(f"  Unit State: {lr['unit_state']}")
        lines.append(f"  Systemctl Action: {lr['systemctl_action']}")
        lines.append(f"  Systemctl OK: {lr['systemctl_ok']}")
        if lr["timestamp"]:
            import time
            lines.append(f"  Timestamp: {time.ctime(lr['timestamp'])}")
    else:
        lines.append("Last Restart: (none recorded)")

    return "\n".join(lines)


def _detect_and_write_resume(ledger: str) -> dict:
    """R.2 — Detect abrupt_close and auto-write RESUME.md.

    Called at the start of ``watch start`` to give the agent context about
    what happened in the previous session. Delegates to R.1's
    ``generate_resume()`` for OCB detection + ledger replay (no duplication).

    Returns a dict with:
      - ``session_type``: first_run | abrupt_close | normal_close
      - ``resume_md_path``: path to RESUME.md (only if written)
      - ``reason``: why RESUME.md was/wasn't written

    Degradación suave: if anything fails, returns minimal dict, does NOT
    crash the watch start.
    """
    try:
        import os
        from causadb.cli._cmd_resume import generate_resume, generate_resume_markdown

        resume_data = generate_resume(ledger)
        session_type = resume_data.get("session_type", "first_run")

        if session_type == "first_run":
            return {"session_type": "first_run", "reason": "no previous session"}

        # abrupt_close or normal_close — write RESUME.md
        ocb_dir = os.path.join(os.path.dirname(ledger), "ocb")
        os.makedirs(ocb_dir, exist_ok=True)
        md_path = os.path.join(ocb_dir, "RESUME.md")
        md_content = generate_resume_markdown(resume_data)
        with open(md_path, "w") as f:
            f.write(md_content)
            f.flush()
            os.fsync(f.fileno())

        return {
            "session_type": session_type,
            "resume_md_path": md_path,
            "events_count": resume_data.get("events_count", 0),
        }
    except Exception as e:
        return {"session_type": "error", "reason": str(e)}


def _flush_shell_hook(ledger: str) -> dict:
    """Flush shell hook queue to ledger on watch stop.

    Degradación suave (Artículo V): si flush falla por cualquier motivo,
    retorna ``{"status": "skipped", "reason": str(e)}`` y NO propaga el
    error a ``watch stop``.
    """
    try:
        result = flush_shell_hook(ledger)
        return {
            "status": "ok",
            "flushed": result.get("flushed", 0),
            "errors": result.get("errors", 0),
        }
    except Exception as e:
        return {"status": "skipped", "reason": str(e)}
