"""H-OPS.1 Fase 3 + Fase U1 + Fase U2 — `causadb restart` unificado.

Cuando existe el unit systemd del daemon
(``~/.config/systemd/user/causadb.service``), este comando GOBIERNA ese
mundo: reinicia el unit vía ``systemctl --user`` y solo refresca los
forks complementarios (vigilante/mcp_proxy/proxy_server). Esto elimina
la ley doble que originó BIT-CHR.101 (dos harvesters / dos serves sobre
el mismo ledger).

Sin unit (o con ``--no-systemd``), conserva el comportamiento legacy
exacto: ``_watch_stop`` + ``_watch_start`` de los 5 forks.

Máquina de estados del unit (design §2):
    NO-INSTALADO       → legacy forks silencioso.
    INSTALADO-INACTIVO → ``systemctl --user start`` (NO restart) + aviso
                          de reactivación + forks complementarios.
    ACTIVO             → ``systemctl --user restart`` + forks complementarios.

Verificación del unit, no confianza: se lee el ``ExecStart=`` del
archivo instalado; solo si corre ``serve start`` se considera que cubre
{serve, harvest} (el harvester vive DENTRO de serve — BIT-CHR.41).

Política de fallo de systemctl (jamás éxito silencioso):
    - falla Y puerto :7457 muerto → fallback legacy (serve fork) + warning.
    - falla pero puerto vivo      → continuar + warning.

Fase U2 — Dry-run mode:
    ``--dry-run`` simula el restart sin ejecutar nada. Retorna JSON con
    las acciones que se habrían ejecutado: systemctl action, forks a
    detener/iniciar, warnings, y modo (systemd/legacy).

Diseño: ``docs/design_unified_process_command_2026-08-26.md`` §2.
"""
import json
import os
from typing import Tuple

from causadb import _daemon_service
from causadb._systemd_utils import get_unit_status
# Namespace, no from-import: los tests parchean la hoja
# ``causadb.cli._cmd_watch._is_serve_port_occupied`` y un from-import
# congelaría la referencia (misma razón que _daemon_service abajo).
from causadb.cli import _cmd_watch as _watch_mod
from causadb._workspace import resolve_ledger, NoWorkspaceError

# Servicios que el unit cubre cuando su ExecStart corre `serve start`
# (el harvester de agentes vive DENTRO de serve — BIT-CHR.41).
_COVERED_BY_SERVE_UNIT = frozenset({"serve", "harvest"})


def _read_unit_execstart(unit_path: str) -> str:
    """Extrae la línea ``ExecStart=`` del unit instalado ('' si no hay)."""
    try:
        with open(unit_path, "r") as f:
            for line in f:
                if line.strip().startswith("ExecStart="):
                    return line.strip()
    except OSError:
        pass
    return ""


def _detect_unit_state() -> Tuple[str, dict]:
    """Máquina de 3 estados del unit (design §2).

    Returns:
        ``(state, info)`` con ``state`` en ``"none" | "inactive" |
        "active"``. ``info`` lleva ``unit_path``, ``enabled_state``,
        ``masked_or_disabled`` y/o ``systemctl_error`` según corresponda.

    Notas:
        - ``status_service()`` no distingue no-instalado de inactivo ⇒
          la detección es existe-archivo AND is-active.
        - ``is-enabled`` = masked/disabled ⇒ se respeta la intención
          humana (flag en info; el caller cae a legacy con aviso).
        - systemctl roto (binario ausente / timeout) ⇒ ``"none"``:
          no hay nada gobernable → legacy.
    """
    unit_path = os.path.join(
        _daemon_service.SYSTEMD_USER_DIR,
        f"{_daemon_service.SERVICE_NAME}.service",
    )
    if not os.path.exists(unit_path):
        return ("none", {})

    info = {"unit_path": unit_path}

    active_ok, active_detail = _daemon_service.status_service()
    if not active_ok and (
        "not available" in (active_detail or "")
        or "timed out" in (active_detail or "")
    ):
        # systemctl roto: no podemos gobernar → legacy.
        info["systemctl_error"] = active_detail
        return ("none", info)

    enabled_ok, enabled_out = _daemon_service._systemctl_cmd(
        ["is-enabled", _daemon_service.SERVICE_NAME]
    )
    enabled_state = (enabled_out or "").strip().lower()
    info["enabled_state"] = enabled_state
    if enabled_state in ("masked", "disabled"):
        info["masked_or_disabled"] = True

    state = (
        "active"
        if (active_ok and (active_detail or "").strip() == "active")
        else "inactive"
    )
    return (state, info)


def _verify_unit_coverage(ledger: str, unit_info: dict) -> Tuple[frozenset, list]:
    """Verificar el unit, no confiar (design §2 "Verificación del unit").

    Lee el ``ExecStart=`` del archivo instalado:
        - contiene ``serve start`` → covered={serve, harvest}.
        - no matchea               → covered vacío + warning (legacy).
    Bonus del parse: avisar si el ledger del ExecStart difiere del
    ledger resuelto por el CLI.
    """
    warnings = []
    exec_line = _read_unit_execstart(unit_info.get("unit_path", ""))
    if not exec_line or "serve start" not in exec_line:
        warnings.append(
            "unit instalado pero su ExecStart no corre 'serve start'; "
            "no gobierna serve/harvest — usando modo legacy"
        )
        return (frozenset(), warnings)

    if "--ledger" in exec_line:
        after = exec_line.split("--ledger", 1)[1].strip()
        parts = after.split()
        unit_ledger = parts[0] if parts else ""
        # Unescape \x20 → space (install_service encodes spaces for systemd)
        unit_ledger = unit_ledger.replace("\\x20", " ") if unit_ledger else ""
        if unit_ledger and os.path.abspath(unit_ledger) != os.path.abspath(ledger):
            warnings.append(
                f"ledger del unit ({unit_ledger}) difiere del ledger "
                f"resuelto por el CLI ({ledger})"
            )
    return (_COVERED_BY_SERVE_UNIT, warnings)


def _restart_via_systemd(ledger, state, covered, systemctl_block, warnings,
                         no_proxy, no_serve) -> Tuple[int, str]:
    """Rama systemd: stop forks complementarios → systemctl → start complementarios.

    Orden global (T10): último kill de fork < llamada systemctl <
    primer Popen de fork.
    """
    if state == "inactive":
        warnings.append(
            "producción estaba caída; reactivada bajo supervisión "
            "(systemd causadb.service)"
        )
    if no_serve:
        warnings.append(
            "--no-serve ignorado en modo systemd: serve está gobernado "
            "por el unit (el restart del unit es atómico)"
        )
    skipped = sorted(covered)

    # 1. Stop SOLO de forks complementarios (los cubiertos ni se
    #    intentan matar — impide SIGTERM a pidfiles huérfanos).
    _, stop_out = _watch_mod._watch_stop(ledger, skip=covered)

    # 2. systemctl: restart si estaba activo, start si estaba caído.
    if state == "active":
        action = "restart"
        ok, detail = _daemon_service.restart_service()
    else:
        action = "start"
        ok, detail = _daemon_service.start_service()
    systemctl_block.update({"action": action, "ok": ok, "detail": detail})

    # 3. Política de fallo (jamás éxito silencioso).
    if not ok:
        if _watch_mod._is_serve_port_occupied():
            warnings.append(
                f"systemctl {action} falló ({detail}) pero el puerto "
                f"{_watch_mod._SERVE_DEFAULT_PORT} sigue vivo; continuando con "
                f"forks complementarios"
            )
            _, start_out = _watch_mod._watch_start(ledger, no_proxy, True, skip=covered)
        else:
            warnings.append(
                f"systemctl {action} falló ({detail}) y el puerto "
                f"{_watch_mod._SERVE_DEFAULT_PORT} está muerto; fallback legacy "
                f"(serve fork)"
            )
            # Fallback: lanzar serve fork (harvest sigue cubierto por el
            # unit — no duplicar el cosechador).
            _, start_out = _watch_mod._watch_start(
                ledger, no_proxy, False, skip=covered - {"serve"}
            )
    else:
        # 4. Start de forks complementarios.
        _, start_out = _watch_mod._watch_start(ledger, no_proxy, True, skip=covered)

        # Record RESTART_COMPLETED event for observability (last restart time)
        try:
            _daemon_service.record_restart_completed(
                ledger_path=ledger,
                mode="systemd",
                unit_state=state,
                systemctl_action=action,
                systemctl_ok=ok,
            )
        except Exception:
            # Degraded gracefully - don't fail restart if event logging fails
            pass

    return (0, json.dumps({
        "mode": "systemd",
        "unit_state": state,
        "systemctl": systemctl_block,
        "skipped": skipped,
        "warnings": warnings,
        "stop": json.loads(stop_out),
        "start": json.loads(start_out),
    }, sort_keys=True))


def _dry_run_systemd(ledger: str, state: str, covered: frozenset,
                     unit_info: dict, no_proxy: bool, no_serve: bool,
                     no_systemd: bool, fmt: str = "json") -> Tuple[int, str]:
    """Simulate systemd restart without executing anything.

    Returns JSON with would_execute, warnings, mode, etc.
    """
    warnings = []
    skipped = sorted(covered)

    # Determine systemctl action
    if state == "active":
        systemctl_action = "restart"
    elif state == "inactive":
        systemctl_action = "start"
        warnings.append(
            "producción estaba caída; sería reactivada bajo supervisión "
            "(systemd causadb.service)"
        )
    else:
        systemctl_action = "none"

    if no_serve:
        warnings.append(
            "--no-serve ignorado en modo systemd: serve está gobernado "
            "por el unit (el restart del unit es atómico)"
        )

    # Determine forks to stop/start
    forks_to_stop = [s for s in ("vigilante", "mcp_proxy", "proxy_server") if s not in covered]
    forks_to_start = list(forks_to_stop)

    # Covered services are skipped
    for svc in covered:
        if svc in forks_to_stop:
            forks_to_stop.remove(svc)
        if svc in forks_to_start:
            forks_to_start.remove(svc)

    # Simulate systemctl failure policy
    would_fallback = False
    if systemctl_action != "none":
        # We can't know if systemctl would succeed without running it
        # In dry-run, we assume it would succeed but note the uncertainty
        warnings.append(
            f"DRY-RUN: systemctl {systemctl_action} no ejecutado; "
            f"se asume éxito. Si fallara y puerto 7457 muerto → fallback legacy."
        )

    result = {
        "mode": "systemd",
        "unit_state": state,
        "dry_run": True,
        "would_execute": {
            "systemctl_action": systemctl_action,
            "forks_to_stop": forks_to_stop,
            "forks_to_start": forks_to_start,
            "skipped": skipped,
        },
        "warnings": warnings,
        "systemctl": {"action": systemctl_action, "ok": None, "detail": "dry-run"},
    }

    if fmt == "json":
        return (0, json.dumps(result, sort_keys=True))
    else:
        # Human-readable text format
        lines = []
        lines.append(f"Mode: {result['mode']}")
        lines.append(f"Unit State: {result['unit_state']}")
        lines.append(f"Dry-Run: True")
        lines.append("")
        lines.append("Would Execute:")
        lines.append(f"  systemctl: {result['would_execute']['systemctl_action']}")
        lines.append(f"  forks to stop: {', '.join(result['would_execute']['forks_to_stop']) or '(none)'}")
        lines.append(f"  forks to start: {', '.join(result['would_execute']['forks_to_start']) or '(none)'}")
        lines.append(f"  skipped: {', '.join(result['would_execute']['skipped']) or '(none)'}")
        lines.append("")
        if result['warnings']:
            lines.append("Warnings:")
            for w in result['warnings']:
                lines.append(f"  - {w}")
        return (0, "\n".join(lines))


def _dry_run_legacy(ledger: str, no_proxy: bool, no_serve: bool, fmt: str = "json") -> Tuple[int, str]:
    """Simulate legacy restart without executing anything."""
    # In legacy mode, all 5 services would be stopped and started
    # (unless skipped by flags)
    forks_to_stop = ["vigilante", "mcp_proxy", "proxy_server", "harvest", "serve"]
    forks_to_start = list(forks_to_stop)

    if no_proxy:
        forks_to_stop = [s for s in forks_to_stop if s != "proxy_server"]
        forks_to_start = [s for s in forks_to_start if s != "proxy_server"]
    if no_serve:
        forks_to_stop = [s for s in forks_to_stop if s != "serve"]
        forks_to_start = [s for s in forks_to_start if s != "serve"]

    result = {
        "mode": "legacy",
        "unit_state": "none",
        "dry_run": True,
        "would_execute": {
            "systemctl_action": "none",
            "forks_to_stop": forks_to_stop,
            "forks_to_start": forks_to_start,
            "skipped": [],
        },
        "warnings": ["DRY-RUN: legacy restart no ejecutado"],
        "systemctl": {"action": "none", "ok": None, "detail": "dry-run"},
    }

    if fmt == "json":
        return (0, json.dumps(result, sort_keys=True))
    else:
        # Human-readable text format
        lines = []
        lines.append(f"Mode: {result['mode']}")
        lines.append(f"Unit State: {result['unit_state']}")
        lines.append(f"Dry-Run: True")
        lines.append("")
        lines.append("Would Execute:")
        lines.append(f"  systemctl: {result['would_execute']['systemctl_action']}")
        lines.append(f"  forks to stop: {', '.join(result['would_execute']['forks_to_stop']) or '(none)'}")
        lines.append(f"  forks to start: {', '.join(result['would_execute']['forks_to_start']) or '(none)'}")
        lines.append(f"  skipped: {', '.join(result['would_execute']['skipped']) or '(none)'}")
        lines.append("")
        if result['warnings']:
            lines.append("Warnings:")
            for w in result['warnings']:
                lines.append(f"  - {w}")
        return (0, "\n".join(lines))


def cmd_restart(args) -> Tuple[int, str]:
    try:
        ledger = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        return (1, json.dumps({"error": str(e)}))
    no_proxy = getattr(args, "no_proxy", False)
    no_serve = getattr(args, "no_serve", False)
    no_systemd = getattr(args, "no_systemd", False)
    dry_run = getattr(args, "dry_run", False)
    fmt = getattr(args, "format", "json")

    warnings = []
    systemctl_block = {"action": None, "ok": None, "detail": None}

    # --no-systemd es real, no decorativo: salta TODA interacción con
    # systemctl (ni siquiera queries de detección).
    if no_systemd:
        state, unit_info = "none", {}
    else:
        state, unit_info = _detect_unit_state()

    use_systemd = False
    covered = frozenset()

    if not no_systemd and state != "none":
        if unit_info.get("masked_or_disabled"):
            warnings.append(
                f"unit {_daemon_service.SERVICE_NAME}.service está "
                f"'{unit_info.get('enabled_state')}'; se respeta la "
                f"intención humana — usando modo legacy"
            )
        else:
            covered, exec_warnings = _verify_unit_coverage(ledger, unit_info)
            warnings.extend(exec_warnings)
            if covered:
                use_systemd = True

    # Dry-run mode: simulate without executing
    if dry_run:
        if use_systemd:
            return _dry_run_systemd(ledger, state, covered, unit_info,
                                    no_proxy, no_serve, no_systemd, fmt)
        else:
            return _dry_run_legacy(ledger, no_proxy, no_serve, fmt)

    if use_systemd:
        return _restart_via_systemd(
            ledger, state, covered, systemctl_block, warnings,
            no_proxy, no_serve,
        )

    # ---- Legacy: comportamiento actual intacto (compat otras máquinas) ----
    _, stop_out = _watch_mod._watch_stop(ledger)
    _, start_out = _watch_mod._watch_start(ledger, no_proxy, no_serve)

    # Record RESTART_COMPLETED event for observability (last restart time)
    try:
        _daemon_service.record_restart_completed(
            ledger_path=ledger,
            mode="legacy",
            unit_state=state,
            systemctl_action="none",
            systemctl_ok=True,
        )
    except Exception:
        # Degraded gracefully - don't fail restart if event logging fails
        pass

    return (0, json.dumps({
        "mode": "legacy",
        "unit_state": state,
        "systemctl": systemctl_block,
        "skipped": [],
        "warnings": warnings,
        "restart": "ok",
        "stop": json.loads(stop_out),
        "start": json.loads(start_out),
    }, sort_keys=True))
