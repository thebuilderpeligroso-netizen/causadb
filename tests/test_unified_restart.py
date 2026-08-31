"""Fase U1 — `causadb restart` unificado (goberna systemd cuando existe).

Cubre los 11 escenarios del auditor (design:
``docs/design_unified_process_command_2026-08-26.md`` §2 + Fase U1):

  T1  unit activo ⇒ 1 systemctl("restart") + forks cubiertos saltados
  T2  sin unit ⇒ 0 llamadas systemctl, flujo legacy idéntico al actual
  T3  --no-systemd con unit activo ⇒ 0 llamadas systemctl, legacy completo
  T4  R1 anti-duplicado: Popen JAMÁS lanza harvest/serve en modo systemd
  T5  instalado-inactivo ⇒ systemctl("start") (NO restart) + aviso reactivación
  T6  systemctl falla + puerto :7457 muerto ⇒ fallback serve fork + warning
  T7  _systemctl_cmd con TimeoutExpired ⇒ (False, "systemctl timed out")
  T8  pidfile huérfano de serve + unit activo ⇒ stop NO señala ese PID
  T9  binario systemctl ausente ⇒ legacy
  T10 orden global posicional: último kill < systemctl < primer Popen
  T11 guard estructural: _watch_stop sin subprocess.run/Popen alguno

REGLA HERMÉTICA (Artículo IX): JAMÁS se ejecuta systemctl real ni se
matan procesos reales. Todo pasa por:
  - patch ``causadb._daemon_service._systemctl_cmd`` (grabadora con
    respuestas enlatadas),
  - patch ``causadb.cli._cmd_watch.subprocess.Popen`` + ``get_daemon``,
  - parche de la hoja ``_is_serve_port_occupied``,
  - ``SYSTEMD_USER_DIR`` apuntado a un tmp_path (nunca el unit real).
"""

import argparse
import contextlib
import json
import os
import subprocess
from unittest.mock import MagicMock, patch

from causadb._workspace import WorkspaceManager
from causadb.cli import _cmd_restart
from causadb.cli import _cmd_watch

SERVICE_NAME = "causadb"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_workspace(tmp_path):
    """Crea un workspace CausaDB real en tmp_path y retorna el ledger path."""
    project = tmp_path / "proj"
    project.mkdir()
    WorkspaceManager.init(str(project))
    return os.path.join(str(project), ".causadb", "ledger.log")


def _write_unit(unit_dir, ledger_path, exec_line=None):
    """Escribe un unit file falso en ``unit_dir`` (que será SYSTEMD_USER_DIR).

    Nunca toca ``~/.config/systemd/user`` real.
    """
    os.makedirs(unit_dir, exist_ok=True)
    path = os.path.join(unit_dir, f"{SERVICE_NAME}.service")
    if exec_line is None:
        exec_line = (
            "ExecStart=/usr/local/bin/causadb serve start "
            f"--ledger {ledger_path}"
        )
    with open(path, "w") as f:
        f.write("[Unit]\nDescription=fake unit for tests\n\n[Service]\n")
        f.write(exec_line + "\n")
        f.write("Restart=on-failure\n")
    return path


def _make_systemctl_recorder(is_active=(True, "active"),
                             is_enabled=(True, "enabled"),
                             restart=(True, ""),
                             start=(True, "")):
    """Grabadora de ``_systemctl_cmd`` con respuestas enlatadas por subcomando.

    Retorna ``(calls, fake)``: ``calls`` acumula cada lista de args recibida.
    """
    calls = []

    def fake(args):
        calls.append(list(args))
        sub = args[0] if args else ""
        canned = {
            "is-active": tuple(is_active),
            "is-enabled": tuple(is_enabled),
            "restart": tuple(restart),
            "start": tuple(start),
            "stop": (True, ""),
        }
        return canned.get(sub, (True, ""))

    return calls, fake


def _make_mock_daemon(kill_events=None, serve_first_false=False):
    """Daemon mockeado: kill graba en kill_events; is_running instantáneo.

    Con ``serve_first_false=True``, la PRIMERA consulta de is_running("serve")
    retorna False (pre-spawn) y las siguientes True → el flujo legacy lanza
    el fork de serve y reporta "started".
    """
    mock = MagicMock()
    counts = {}

    def is_running(name):
        counts[name] = counts.get(name, 0) + 1
        if serve_first_false and name == "serve":
            return counts[name] > 1
        return True

    mock.is_running.side_effect = is_running

    def kill(name, timeout=5.0):
        if kill_events is not None:
            kill_events.append(("kill", name))
        return True

    mock.kill.side_effect = kill
    return mock


def _make_proc():
    proc = MagicMock()
    proc.wait = MagicMock(return_value=0)
    return proc


def _args(ledger, no_proxy=False, no_serve=False, no_systemd=False):
    return argparse.Namespace(
        ledger=ledger,
        no_proxy=no_proxy,
        no_serve=no_serve,
        no_systemd=no_systemd,
    )


@contextlib.contextmanager
def _restart_env(tmp_path, ledger, systemctl_fake, daemon,
                 port_occupied=False, popen_recorder=None):
    """Aísla el mundo: unit dir falso + systemctl grabadora + watch mockeado.

    Jamás toca el unit real ni ejecuta systemctl/Popen reales.
    """
    unit_dir = str(tmp_path / "systemd" / "user")

    def fake_popen(cmd, *a, **k):
        if popen_recorder is not None:
            popen_recorder.append(("popen", list(cmd)))
        return _make_proc()

    with patch("causadb._daemon_service.SYSTEMD_USER_DIR", unit_dir), \
         patch("causadb._daemon_service._systemctl_cmd",
               side_effect=systemctl_fake), \
         patch("causadb.cli._cmd_watch.get_daemon", return_value=daemon), \
         patch("causadb.cli._cmd_watch.subprocess.Popen",
               side_effect=fake_popen), \
         patch("causadb.cli._cmd_watch._is_serve_port_occupied",
               return_value=port_occupied), \
         patch("causadb.cli._cmd_watch._detect_and_write_resume",
               return_value=None), \
         patch("causadb.cli._cmd_watch._flush_shell_hook",
               return_value={"status": "ok"}), \
         patch("causadb.cli._cmd_watch._auto_distill",
               return_value={"status": "ok"}), \
         patch("causadb.cli._cmd_watch._auto_score",
               return_value={"status": "ok"}):
        yield


def _popen_services(popen_events):
    """Extrae los tokens de servicio de cada Popen grabado."""
    services = []
    for _, cmd in popen_events:
        # cmd = [sys.executable, "-m", "causadb.cli.main", "<svc>", ...]
        if len(cmd) >= 4:
            services.append(cmd[3])
    return services


# ---------------------------------------------------------------------------
# T1 — unit activo ⇒ exactamente 1 systemctl("restart"), cubiertos saltados
# ---------------------------------------------------------------------------


def test_t1_unit_active_restarts_unit_and_skips_covered_forks(tmp_path):
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    popen_events = []
    daemon = _make_mock_daemon()

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        code, out = _cmd_restart.cmd_restart(_args(ledger))

    assert code == 0
    payload = json.loads(out)

    # Modo systemd declarado con estado del unit
    assert payload["mode"] == "systemd"
    assert payload["unit_state"] == "active"

    # Exactamente 1 llamada systemctl("restart") (las queries no cuentan)
    restart_calls = [c for c in calls if c and c[0] == "restart"]
    assert len(restart_calls) == 1, (
        f"Esperaba exactamente 1 systemctl('restart'); obtuve {calls}"
    )

    # Forks cubiertos por el unit: skipped en stop Y start
    assert set(payload["skipped"]) == {"serve", "harvest"}
    assert payload["stop"]["serve_stopped"] == "skipped_by_unit"
    assert payload["stop"]["harvester_stopped"] == "skipped_by_unit"
    assert payload["start"]["serve"] == "skipped_by_unit"
    assert payload["start"]["harvest"] == "skipped_by_unit"

    # Complementarios reiniciados vía Popen
    launched = _popen_services(popen_events)
    for svc in ("vigilante", "mcp-proxy", "proxy-server"):
        assert svc in launched, (
            f"{svc} debe reiniciarse como fork complementario; "
            f"lanzado: {launched}"
        )


# ---------------------------------------------------------------------------
# T2 — sin unit ⇒ 0 llamadas systemctl, flujo legacy idéntico al actual
# ---------------------------------------------------------------------------


def test_t2_no_unit_legacy_flow_identical(tmp_path):
    ledger = _init_workspace(tmp_path)
    # NO se escribe unit file: SYSTEMD_USER_DIR apunta a un dir vacío.
    calls, fake = _make_systemctl_recorder()
    popen_events = []
    daemon = _make_mock_daemon(serve_first_false=True)

    with _restart_env(tmp_path, ledger, fake, daemon,
                      port_occupied=False, popen_recorder=popen_events):
        code, out = _cmd_restart.cmd_restart(_args(ledger))

    assert code == 0
    payload = json.loads(out)

    # Cero llamadas systemctl (ni siquiera queries de detección)
    assert calls == [], (
        f"Sin unit no debe consultarse systemctl; obtuve {calls}"
    )

    # Flujo legacy completo: los 5 servicios como hoy
    assert payload["mode"] == "legacy"
    assert payload["restart"] == "ok"
    launched = _popen_services(popen_events)
    for svc in ("vigilante", "mcp-proxy", "proxy-server", "harvest", "serve"):
        assert svc in launched, (
            f"Legacy debe lanzar los 5 forks como hoy; falta {svc}. "
            f"Lanzados: {launched}"
        )
    assert payload["start"]["serve"] == "started"
    assert payload["start"]["harvest"] == "started"


# ---------------------------------------------------------------------------
# T3 --no-systemd con unit activo ⇒ 0 llamadas systemctl, legacy completo
# ---------------------------------------------------------------------------


def test_t3_no_systemd_flag_forces_legacy_even_with_active_unit(tmp_path):
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    popen_events = []
    daemon = _make_mock_daemon(serve_first_false=True)

    with _restart_env(tmp_path, ledger, fake, daemon,
                      port_occupied=False, popen_recorder=popen_events):
        code, out = _cmd_restart.cmd_restart(_args(ledger, no_systemd=True))

    assert code == 0
    payload = json.loads(out)

    # El flag es real, no decorativo: cero llamadas systemctl
    assert calls == [], (
        f"--no-systemd debe saltar TODAS las llamadas systemctl; obtuve {calls}"
    )
    assert payload["mode"] == "legacy"
    assert payload["restart"] == "ok"

    # Legacy completo: los 5 forks (escape del significado viejo intacto)
    launched = _popen_services(popen_events)
    for svc in ("vigilante", "mcp-proxy", "proxy-server", "harvest", "serve"):
        assert svc in launched, (
            f"--no-systemd debe dar legacy completo; falta {svc}. "
            f"Lanzados: {launched}"
        )


# ---------------------------------------------------------------------------
# T4 — R1 anti-duplicado: nunca 2 harvesters ni 2 serves
# ---------------------------------------------------------------------------


def test_t4_never_spawns_duplicate_harvest_or_serve_forks(tmp_path):
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    popen_events = []
    daemon = _make_mock_daemon()

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        code, out = _cmd_restart.cmd_restart(_args(ledger))

    assert code == 0
    # R1: con unit activo, Popen JAMÁS invocado con harvest ni serve
    for _, cmd in popen_events:
        assert "harvest" not in cmd, (
            f"R1 VIOLADO: Popen duplicó el harvester del unit: {cmd}"
        )
        assert "serve" not in cmd, (
            f"R1 VIOLADO: Popen duplicó el serve del unit: {cmd}"
        )


# ---------------------------------------------------------------------------
# T5 — instalado-inactivo ⇒ systemctl("start"), NO restart + aviso
# ---------------------------------------------------------------------------


def test_t5_installed_inactive_starts_unit_not_restart(tmp_path):
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(
        is_active=(False, "inactive"), start=(True, "")
    )
    popen_events = []
    daemon = _make_mock_daemon()

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        code, out = _cmd_restart.cmd_restart(_args(ledger))

    assert code == 0
    payload = json.loads(out)

    assert payload["mode"] == "systemd"
    assert payload["unit_state"] == "inactive"

    # start (NO restart): producción estaba caída, se reactiva
    start_calls = [c for c in calls if c and c[0] == "start"]
    restart_calls = [c for c in calls if c and c[0] == "restart"]
    assert len(start_calls) == 1, (
        f"Instalado-inactivo debe usar systemctl start; obtuve {calls}"
    )
    assert restart_calls == [], (
        f"Instalado-inactivo NO debe usar restart; obtuve {calls}"
    )

    # Aviso de reactivación presente
    assert any("reactivada" in w for w in payload["warnings"]), (
        f"Falta aviso de reactivación; warnings: {payload['warnings']}"
    )

    # Cubiertos saltados también en este camino
    assert set(payload["skipped"]) == {"serve", "harvest"}
    assert payload["start"]["serve"] == "skipped_by_unit"


# ---------------------------------------------------------------------------
# T6 — systemctl falla + puerto muerto ⇒ fallback legacy (serve fork)
# ---------------------------------------------------------------------------


def test_t6_systemctl_fail_dead_port_falls_back_to_serve_fork(tmp_path):
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(
        is_active=(True, "active"), restart=(False, "job failed")
    )
    popen_events = []
    # Puerto muerto ⇒ no hay serve vivo: la primera consulta de
    # is_running("serve") debe decir False para que el fallback lo lance.
    daemon = _make_mock_daemon(serve_first_false=True)

    # Puerto :7457 MUERTO tras el fallo de systemctl
    with _restart_env(tmp_path, ledger, fake, daemon,
                      port_occupied=False, popen_recorder=popen_events):
        code, out = _cmd_restart.cmd_restart(_args(ledger))

    assert code == 0
    payload = json.loads(out)

    # Fallo declarado, jamás éxito silencioso
    assert payload["systemctl"]["ok"] is False
    assert payload["warnings"], (
        "Jamás éxito silencioso: fallo de systemctl debe producir warnings"
    )
    assert any("fallback" in w.lower() for w in payload["warnings"]), (
        f"Falta warning de fallback legacy; warnings: {payload['warnings']}"
    )

    # Fallback: serve fork lanzado pese al fallo de systemctl
    launched = _popen_services(popen_events)
    assert "serve" in launched, (
        f"Puerto muerto + systemctl caído ⇒ fallback debe lanzar serve fork; "
        f"lanzado: {launched}"
    )


# ---------------------------------------------------------------------------
# T7 — _systemctl_cmd con TimeoutExpired ⇒ (False, "systemctl timed out")
# ---------------------------------------------------------------------------


def test_t7_systemctl_timeout_returns_timed_out():
    from causadb._daemon_service import _systemctl_cmd

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["systemctl", "--user", "is-active"], timeout=30
        )

    with patch("causadb._daemon_service.subprocess.run", side_effect=boom):
        ok, detail = _systemctl_cmd(["is-active", SERVICE_NAME])

    assert ok is False
    assert detail == "systemctl timed out"


# ---------------------------------------------------------------------------
# T8 — pidfile huérfano de serve + unit activo ⇒ stop NO señala ese PID
# ---------------------------------------------------------------------------


def test_t8_orphan_serve_pidfile_never_signaled_in_stop(tmp_path):
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    kill_events = []  # grabadora sobre get_daemon().kill
    os_kill_calls = []  # grabadora sobre os.kill (por si algo se cuela)

    def rec_os_kill(pid, sig):
        os_kill_calls.append((pid, sig))
        return None

    daemon = _make_mock_daemon(kill_events=kill_events)

    with _restart_env(tmp_path, ledger, fake, daemon), \
         patch("causadb.cli._cmd_watch.os.kill", side_effect=rec_os_kill):
        code, out = _cmd_restart.cmd_restart(_args(ledger))

    assert code == 0
    payload = json.loads(out)

    # La fase stop NO tocó serve (ni harvest): skip también al parar
    assert payload["stop"]["serve_stopped"] == "skipped_by_unit"
    assert payload["stop"]["harvester_stopped"] == "skipped_by_unit"

    served_kills = [e for e in kill_events if e[1] == "serve"]
    assert served_kills == [], (
        f"Un pidfile huérfano de serve NO debe recibir señal cuando el "
        f"unit lo gobierna; kills: {kill_events}"
    )
    assert os_kill_calls == [], (
        f"Cero señales via os.kill durante la fase stop; obtuve "
        f"{os_kill_calls}"
    )


# ---------------------------------------------------------------------------
# T9 — binario systemctl ausente ⇒ legacy
# ---------------------------------------------------------------------------


def test_t9_missing_systemctl_binary_falls_back_to_legacy(tmp_path):
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    not_available = (
        False,
        "systemctl not available: [Errno 2] No such file or directory: 'systemctl'",
    )
    calls, fake = _make_systemctl_recorder(
        is_active=not_available, is_enabled=not_available
    )
    popen_events = []
    daemon = _make_mock_daemon(serve_first_false=True)

    with _restart_env(tmp_path, ledger, fake, daemon,
                      port_occupied=False, popen_recorder=popen_events):
        code, out = _cmd_restart.cmd_restart(_args(ledger))

    assert code == 0
    payload = json.loads(out)

    # Sin systemctl gobernable ⇒ legacy
    assert payload["mode"] == "legacy"

    # Jamás intentó gobernar: ni start ni restart
    governing = [c for c in calls if c and c[0] in ("start", "restart")]
    assert governing == [], (
        f"Con systemctl ausente no debe intentarse gobernar; obtuve {calls}"
    )

    # Legacy completo: los 5 forks
    launched = _popen_services(popen_events)
    for svc in ("vigilante", "mcp-proxy", "proxy-server", "harvest", "serve"):
        assert svc in launched, (
            f"Falta fork {svc} en fallback legacy; lanzados: {launched}"
        )


# ---------------------------------------------------------------------------
# T10 — orden global posicional: último kill < systemctl < primer Popen
# ---------------------------------------------------------------------------


def test_t10_global_order_last_kill_before_systemctl_before_first_popen(tmp_path):
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)

    events = []  # línea de tiempo global compartida
    responses = {
        "is-active": (True, "active"),
        "is-enabled": (True, "enabled"),
        "restart": (True, ""),
        "start": (True, ""),
    }

    def fake(args):
        events.append(("systemctl", args[0]))
        return responses.get(args[0], (True, ""))

    daemon = _make_mock_daemon(kill_events=events)  # kill → events

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=events):  # popen → events
        code, out = _cmd_restart.cmd_restart(_args(ledger))

    assert code == 0

    kill_idx = [i for i, e in enumerate(events) if e[0] == "kill"]
    sysctl_idx = [
        i for i, e in enumerate(events) if e[0] == "systemctl" and e[1] == "restart"
    ]
    popen_idx = [i for i, e in enumerate(events) if e[0] == "popen"]

    assert kill_idx, f"No hubo kills de forks: {events}"
    assert sysctl_idx, f"No hubo llamada systemctl restart: {events}"
    assert popen_idx, f"No hubo Popens de forks: {events}"

    assert max(kill_idx) < sysctl_idx[0], (
        f"El ÚLTIMO kill de fork debe preceder a systemctl restart; "
        f"timeline: {events}"
    )
    assert sysctl_idx[0] < min(popen_idx), (
        f"systemctl restart debe preceder al PRIMER Popen de fork; "
        f"timeline: {events}"
    )


# ---------------------------------------------------------------------------
# T11 — guard estructural: _watch_stop sin subprocess.run/Popen alguno
# ---------------------------------------------------------------------------


def test_t11_watch_stop_is_pidfiles_only_zero_subprocess(tmp_path):
    ledger = _init_workspace(tmp_path)
    daemon = _make_mock_daemon()
    popen_calls = []

    def rec_run(*args, **kwargs):
        raise AssertionError(
            "subprocess.run está PROHIBIDO en _watch_stop "
            "(invariante pidfiles-only, guard T11)"
        )

    def rec_popen(*args, **kwargs):
        popen_calls.append(args)
        raise AssertionError(
            "subprocess.Popen está PROHIBIDO en _watch_stop "
            "(invariante pidfiles-only, guard T11)"
        )

    for skip in (frozenset(), frozenset({"serve", "harvest"})):
        popen_calls.clear()
        with patch("causadb.cli._cmd_watch.get_daemon", return_value=daemon), \
             patch("causadb.cli._cmd_watch.subprocess.Popen",
                   side_effect=rec_popen), \
             patch("causadb.cli._cmd_watch.subprocess.run",
                   side_effect=rec_run), \
             patch("causadb.cli._cmd_watch._flush_shell_hook",
                   return_value={"status": "ok"}), \
             patch("causadb.cli._cmd_watch._auto_distill",
                   return_value={"status": "ok"}), \
             patch("causadb.cli._cmd_watch._auto_score",
                   return_value={"status": "ok"}):
            code, out = _cmd_watch._watch_stop(ledger, skip=skip)

        assert code == 0
        payload = json.loads(out)
        assert isinstance(payload.get("serve_stopped"), (bool, str))
        # Guard estructural: cero invocaciones a subprocess en TODO el stop
        assert popen_calls == [], (
            f"_watch_stop invocó Popen (prohibido); calls: {popen_calls}"
        )
