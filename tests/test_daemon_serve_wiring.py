"""Tests Fase 4 — Wiring del daemon de harvest (BIT-CHR.41; docs/design_index.md).

Cierra el gap de producción (auditoría F/I): el orden correcto es instanciar
``HarvesterDaemon`` + registrar fuentes de agente + ``set_current_*`` +
``install_signal_handlers`` ANTES de ``serve()`` (que bloquea); el PRIMER
tick va en background (no congela el arranque con el backfill); ``watch``
arranca el harvester como subcomando daemonizable ``causadb harvest start
--daemon`` (patrón vigilante).

Artículo III: test-first. Artículo IX: anti-teatro (fork real + SIGTERM real).

Cobertura:
  1. ``_serve_start``: daemon + fuentes + handlers ANTES de serve() (bloqueante)
  2. primer tick en background (Timer(0), NO bloquea)
  3. el tick cosecha y re-agenda con el intervalo
  4. ``harvest start --daemon``: daemonize + is_running + kill (patrón vigilante)
  5. SIGTERM detiene el daemon registrado (real fork; sin handler, no se detiene)
"""

import json
import os
import signal
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from causadb._daemon_service import (
    HarvesterDaemon,
    install_signal_handlers,
    set_current_harvester_daemon,
)
from causadb.cli._cmd_serve import _serve_start
from causadb.cli._cmd_harvest import cmd_harvest, _start as _harvest_start


# ---------------------------------------------------------------------------
# 1. serve start: wiring completo ANTES de serve() (que bloquea)
# ---------------------------------------------------------------------------

def test_serve_start_wires_daemon_and_handlers_before_serve(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    args = SimpleNamespace(ledger=ledger, host=None, port=None)

    call_log = []

    def fake_serve(ledger_path, host="127.0.0.1", port=7457, on_server_created=None):
        call_log.append(("serve", ledger_path, host, port))
        assert on_server_created is not None, "serve debe recibir el callback"

    def fake_install(ledger_path):
        call_log.append(("install_signal_handlers", ledger_path))

    daemon_instance = MagicMock(spec=HarvesterDaemon)

    with patch("causadb.cli._cmd_serve.serve", side_effect=fake_serve), \
         patch("causadb.cli._cmd_serve.HarvesterDaemon", return_value=daemon_instance) as d_cls, \
         patch("causadb.cli._cmd_serve.install_signal_handlers", side_effect=fake_install) as inst, \
         patch("causadb.cli._cmd_serve.set_current_harvester_daemon") as set_d, \
         patch("causadb.cli._cmd_serve.set_current_server") as set_s, \
         patch("causadb.cli._cmd_serve._is_port_in_use", return_value=False):
        rc, out = _serve_start(args)

    # Orden: handlers ANTES de serve (que bloquea con serve_forever)
    order = [c[0] for c in call_log]
    assert "install_signal_handlers" in order
    assert order.index("install_signal_handlers") < order.index("serve")
    assert rc == 0
    d_cls.assert_called_once_with(ledger_path=ledger)
    set_d.assert_called_once_with(daemon_instance)
    inst.assert_called_once_with(ledger)
    set_s.assert_not_called()  # se llama recién cuando el server existe


def test_serve_on_server_created_starts_daemon_and_registers_server(tmp_path):
    """El callback que llega a serve() registra el server y arranca el daemon
    (tick en background) — todo antes de que serve_forever bloquee."""
    ledger = str(tmp_path / "ledger.log")
    args = SimpleNamespace(ledger=ledger, host="127.0.0.1", port=7457)
    captured = {}

    def fake_serve(ledger_path, host="127.0.0.1", port=7457, on_server_created=None):
        captured["callback"] = on_server_created

    daemon_instance = MagicMock(spec=HarvesterDaemon)
    with patch("causadb.cli._cmd_serve.serve", side_effect=fake_serve), \
         patch("causadb.cli._cmd_serve.HarvesterDaemon", return_value=daemon_instance), \
         patch("causadb.cli._cmd_serve.install_signal_handlers"), \
         patch("causadb.cli._cmd_serve.set_current_harvester_daemon"), \
         patch("causadb.cli._cmd_serve.set_current_server") as set_s, \
         patch("causadb.cli._cmd_serve._is_port_in_use", return_value=False):
        _serve_start(args)

        cb = captured["callback"]
        assert cb is not None
        server = MagicMock()
        cb(server)  # dentro del with: los patches siguen activos
        set_s.assert_called_once_with(server)
        daemon_instance.start.assert_called_once()  # tick inicial en background


# ---------------------------------------------------------------------------
# 2. primer tick en background (auditoría F — el backfill NO congela el start)
# ---------------------------------------------------------------------------

def test_first_tick_in_background_does_not_block(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    with patch("causadb._daemon_service.threading.Timer") as MockTimer:
        daemon = HarvesterDaemon(ledger_path=ledger)
        daemon.harvester.harvest_all = MagicMock(return_value={})
        daemon.start()
        # NO se cosecha síncronamente: el tick va agendado con Timer(0)
        assert not daemon.harvester.harvest_all.called, (
            "El primer tick debe ir en background, no bloquear el arranque"
        )
        assert MockTimer.called
        args, kwargs = MockTimer.call_args
        assert args[0] == 0.0, f"Tick inicial agendado con Timer(0), obtuvo {args[0]}"
        assert args[1] == daemon._harvest_tick
        daemon.stop()


def test_tick_harvests_and_reschedules_with_interval(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    daemon = HarvesterDaemon(ledger_path=ledger)
    daemon.harvester.harvest_all = MagicMock(return_value={"shell": 3})
    with patch("causadb._daemon_service.threading.Timer") as MockTimer:
        daemon._harvest_tick()
    # El tick pasa stop_event para abortar el backfill cooperativamente en
    # stop() (H-OPS.1): sin él, el shutdown esperaría el backfill completo.
    daemon.harvester.harvest_all.assert_called_once_with(
        dry_run=False, stop_event=daemon._shutdown_event
    )
    MockTimer.assert_called_once_with(daemon.interval, daemon._harvest_tick)


def test_harvest_all_aborts_cooperatively_on_stop_event(tmp_path):
    """Anti-teatro (H-OPS.1): con stop_event seteado, harvest_all NO cosecha
    las fuentes restantes — el backfill aborta entre fuentes. Sin esto, el
    shutdown esperaría el backfill completo (watch stop ~40s)."""
    ledger = str(tmp_path / "ledger.log")
    daemon = HarvesterDaemon(ledger_path=ledger)
    harvests = []

    def fake_harvest_one(source, cursors, dry_run=False):
        harvests.append(source.source_type())
        return 1

    daemon.harvester._harvest_one = fake_harvest_one
    daemon._shutdown_event.set()
    result = daemon.harvester.harvest_all(dry_run=False, stop_event=daemon._shutdown_event)
    assert harvests == [], (
        "con stop_event ya seteado, no debe cosechar ninguna fuente, "
        f"pero cosechó {harvests}"
    )
    assert result == {}

    daemon._shutdown_event.clear()
    result = daemon.harvester.harvest_all(dry_run=False, stop_event=daemon._shutdown_event)
    assert len(harvests) == len(daemon.harvester._sources), (
        "sin stop_event, debe cosechar todas las fuentes"
    )


# ---------------------------------------------------------------------------
# 4. causadb harvest start --daemon (patrón vigilante, F.11.4)
# ---------------------------------------------------------------------------

def _harvest_args(ledger, daemon_flag=True, action="start"):
    return SimpleNamespace(ledger=ledger, daemon=daemon_flag, action=action)


def test_harvest_cmd_daemon_lifecycle(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    daemon_fake = MagicMock()
    daemon_fake.timer = MagicMock()
    daemon_fake.timer.is_alive.return_value = False  # no bloquea el keep-alive
    daemon_fake.harvester = MagicMock()

    platform = MagicMock()
    platform.is_running.return_value = False

    with patch("causadb.cli._cmd_harvest.HarvesterDaemon", return_value=daemon_fake) as d_cls, \
         patch("causadb.cli._cmd_harvest.get_daemon", return_value=platform), \
         patch("causadb.cli._cmd_harvest.install_signal_handlers") as inst:
        rc, out = cmd_harvest(_harvest_args(ledger))

    assert rc == 0
    status = json.loads(out)["status"]
    assert status == "started"
    platform.daemonize.assert_called_once_with("harvest")
    d_cls.assert_called_once_with(ledger_path=ledger)
    inst.assert_called_once_with(ledger)
    daemon_fake.start.assert_called_once()

    # stop → kill del daemon por PID file
    platform.is_running.return_value = True
    with patch("causadb.cli._cmd_harvest.get_daemon", return_value=platform):
        rc, out = cmd_harvest(_harvest_args(ledger, action="stop"))
    assert rc == 0
    assert json.loads(out)["status"] == "stopped"
    platform.kill.assert_called_once_with("harvest")


def test_harvest_cmd_skips_start_when_already_running(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    platform = MagicMock()
    platform.is_running.return_value = True  # ya está corriendo

    with patch("causadb.cli._cmd_harvest.get_daemon", return_value=platform), \
         patch("causadb.cli._cmd_harvest.HarvesterDaemon") as d_cls:
        rc, out = cmd_harvest(_harvest_args(ledger))

    assert rc == 0
    assert json.loads(out)["status"] == "already_running"
    d_cls.assert_not_called()  # no doble fork (anti-teatro, patrón vigilante)


# ---------------------------------------------------------------------------
# 5. SIGTERM detiene el daemon registrado (real fork — anti-teatro)
# ---------------------------------------------------------------------------

def test_sigterm_stops_registered_harvest_daemon(tmp_path):
    """Daemon registrado via set_current_harvester_daemon + handlers reales:
    SIGTERM → shutdown event + stop() del daemon + exit 0."""
    ledger_path = str(tmp_path / "ledger.log")

    pid = os.fork()
    if pid == 0:
        # ---- Child ----
        daemon = HarvesterDaemon(ledger_path=ledger_path)
        set_current_harvester_daemon(daemon)
        install_signal_handlers(ledger_path)
        # NO arrancamos el timer: el test solo verifica el wiring SIGTERM→stop
        assert daemon.harvester is not None
        while True:
            time.sleep(1)
    else:
        # ---- Parent ----
        try:
            time.sleep(1.0)
            os.kill(pid, signal.SIGTERM)
            _, status = os.waitpid(pid, 0)
            assert os.WIFEXITED(status), (
                f"Child debe salir limpio tras SIGTERM, obtuvo {status}"
            )
            assert os.WEXITSTATUS(status) == 0

            with open(ledger_path) as f:
                lines = [ln for ln in f if ln.strip()]
            assert lines, "El ledger debe tener el shutdown event"
            last = json.loads(lines[-1])["event"]
            assert last["event_type"] == "SYSTEM_BOOT"
            assert last["payload"] == {"action": "shutdown"}
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass


def test_anti_teatro_sigterm_without_handler_kills_daemon(tmp_path):
    """Anti-teatro: SIN install_signal_handlers, el SIGTERM mata el proceso
    (exit por señal, sin shutdown event) — el test anterior tiene poder
    discriminante real, no es teatro."""
    ledger_path = str(tmp_path / "ledger.log")

    pid = os.fork()
    if pid == 0:
        # ---- Child SIN handlers (mutante) ----
        # Defensa: restaurar SIGTERM al default aunque el pytest padre tenga un
        # handler global instalado por otro test (p.ej. test_serve_start_daemon_
        # port_in_use_emits_hint parcheando _daemon_service pero no _cmd_serve).
        # Sin esto, el hijo hereda el handler y SIGTERM sale con exit 0
        # (KEEPS anti-teatro power: si alguien rompe el threading contract del
        # daemon con un handler real nuevo, este restore no lo protege).
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        daemon = HarvesterDaemon(ledger_path=ledger_path)
        set_current_harvester_daemon(daemon)
        while True:
            time.sleep(1)
    else:
        try:
            time.sleep(1.0)
            os.kill(pid, signal.SIGTERM)
            _, status = os.waitpid(pid, 0)
            assert not os.WIFEXITED(status), (
                "Sin handler el proceso debe morir por la señal, no con exit 0"
            )
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass
