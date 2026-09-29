"""Supervisión del harvester: heartbeat + unidad causadb-harvest + health.

TDD RED→GREEN. No toca systemd real, sin timeouts largos, sin eventos
de heartbeat en el ledger (el heartbeat es un archivo plano, jamás un
evento — Artículo I).
"""
import json
import os
import time
from unittest.mock import MagicMock, patch

import causadb._daemon_service as ds


def _ledger_in(tmp_path, name="ledger.log"):
    d = tmp_path / ".causadb"
    d.mkdir(parents=True, exist_ok=True)
    ledger = str(d / name)
    open(ledger, "a").close()
    return ledger


def _heartbeat_for(ledger):
    return os.path.join(os.path.dirname(ledger), "harvest.heartbeat")


# (a) tick actualiza heartbeat (epoch fresco) -------------------------------


def test_tick_writes_fresh_heartbeat(tmp_path):
    ledger = _ledger_in(tmp_path)
    with patch("threading.Timer") as MockTimer:
        MockTimer.return_value = MagicMock()
        daemon = ds.HarvesterDaemon(ledger_path=ledger)
        daemon.harvester.harvest_all = MagicMock(return_value={"shell": 1})
        daemon._harvest_tick()
        daemon.stop()
    hb = _heartbeat_for(ledger)
    assert os.path.isfile(hb), "el tick debe escribir harvest.heartbeat"
    epoch = float(open(hb).read().strip())
    assert abs(time.time() - epoch) < 60, "epoch debe ser fresco"
    # El heartbeat NO es un evento del ledger: el ledger sigue vacío.
    assert os.path.getsize(ledger) == 0


# (b) STALE mutante ----------------------------------------------------------

def test_health_fresh_is_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("CAUSADB_HARVEST_INTERVAL", "1")  # 60s → umbral 180s
    ledger = _ledger_in(tmp_path)
    with open(_heartbeat_for(ledger), "w") as f:
        f.write(str(time.time()))
    h = ds.get_harvest_health(ledger)
    assert h["stale"] is False
    assert h["age_s"] is not None and h["age_s"] < 180


def test_health_old_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("CAUSADB_HARVEST_INTERVAL", "1")
    ledger = _ledger_in(tmp_path)
    with open(_heartbeat_for(ledger), "w") as f:
        f.write(str(time.time() - 1000))  # > 3×60s
    h = ds.get_harvest_health(ledger)
    assert h["stale"] is True
    assert h["age_s"] > 180


def test_health_missing_is_stale_never(tmp_path, monkeypatch):
    monkeypatch.setenv("CAUSADB_HARVEST_INTERVAL", "1")
    ledger = _ledger_in(tmp_path)
    h = ds.get_harvest_health(ledger)  # sin archivo
    assert h["stale"] is True
    assert h["last_tick"] is None


def test_health_corrupt_is_stale_without_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("CAUSADB_HARVEST_INTERVAL", "1")
    ledger = _ledger_in(tmp_path)
    with open(_heartbeat_for(ledger), "w") as f:
        f.write("no-es-un-epoch{{{")
    h = ds.get_harvest_health(ledger)  # jamás excepción
    assert h["stale"] is True


def test_health_invalid_env_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("CAUSADB_HARVEST_INTERVAL", "basura")
    ledger = _ledger_in(tmp_path)
    with open(_heartbeat_for(ledger), "w") as f:
        f.write(str(time.time() - 100))  # fresco bajo fallback 900s
    h = ds.get_harvest_health(ledger)
    assert h["stale"] is False


# (c) installer en tmp HOME ---------------------------------------------------

def test_install_harvest_service_unit_content(tmp_path, monkeypatch):
    tmp_units = str(tmp_path / "units")
    monkeypatch.setattr(ds, "SYSTEMD_USER_DIR", tmp_units)
    monkeypatch.setattr(ds, "_find_causadb_executable",
                        lambda: "/usr/bin/causadb")
    with patch("subprocess.run") as sp:
        ok, path = ds.install_harvest_service("/tmp/proj/.causadb/ledger.log")
        sp.assert_not_called()  # solo ESCRIBE, no enable/reload
    assert ok is True
    assert path.startswith(tmp_units)
    assert path.endswith("causadb-harvest.service")
    content = open(path).read()
    assert "Restart=always" in content
    assert "RestartSec=5" in content
    assert "/tmp/proj/.causadb/ledger.log" in content
    assert "harvest start" in content
    # El installer existente NO fue tocado (precedente intacto).
    assert "Restart=on-failure" in ds.SERVICE_TEMPLATE


def test_install_harvest_service_escapes_spaces(tmp_path, monkeypatch):
    tmp_units = str(tmp_path / "units")
    monkeypatch.setattr(ds, "SYSTEMD_USER_DIR", tmp_units)
    monkeypatch.setattr(ds, "_find_causadb_executable",
                        lambda: "/usr/bin/causadb")
    ledger = "/tmp/mi proyecto/.causadb/ledger.log"
    ok, path = ds.install_harvest_service(ledger)
    assert ok is True
    content = open(path).read()
    assert "\\x20" in content, "espacios deben escaparse como \\x20"
    assert "mi proyecto" not in content.replace("\\x20", "")


def test_install_harvest_service_docstring_documents_manual_enable():
    doc = (ds.install_harvest_service.__doc__ or "").lower()
    assert "systemctl" in doc and "enable" in doc


# (d) exclusión del vigilante -------------------------------------------------

def test_vigilante_excludes_heartbeat(tmp_path):
    from causadb._vigilante import VigilanteWatcher
    watch_dir = str(tmp_path / "watch")
    os.makedirs(watch_dir)
    ledger = os.path.join(watch_dir, ".causadb", "ledger.log")
    os.makedirs(os.path.dirname(ledger), exist_ok=True)
    open(ledger, "a").close()
    hb = os.path.join(os.path.dirname(ledger), "harvest.heartbeat")
    open(hb, "w").write(str(time.time()))
    w = VigilanteWatcher(ledger_path=ledger, watch_dir=watch_dir,
                         skip_baseline=True)
    assert w._is_excluded(hb) is True  # bajo .causadb/ oculto
    normal = os.path.join(watch_dir, "nota.txt")
    open(normal, "w").write("x")
    assert w._is_excluded(normal) is False  # poder discriminatorio


# (e) degradación prolija -----------------------------------------------------

def test_health_without_workspace_is_stale(tmp_path):
    h = ds.get_harvest_health(str(tmp_path / "no-existe" / "ledger.log"))
    assert h["stale"] is True
    assert h["last_tick"] is None


def test_installer_failure_returns_error_tuple(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "SYSTEMD_USER_DIR",
                        str(tmp_path / "units"))
    monkeypatch.setattr(ds, "_find_causadb_executable",
                        lambda: "/usr/bin/causadb")
    with patch("os.makedirs", side_effect=OSError("permiso denegado")):
        ok, msg = ds.install_harvest_service("/tmp/x/ledger.log")
    assert ok is False
    assert isinstance(msg, str) and len(msg) > 0


# (f) fallback legacy del fork harvest en `watch status` ----------------------
# Regresión T1 (intento 1): sin unidad `causadb-harvest` instalada, el fork
# harvest debe seguir la heurística legacy (paridad con serve): si el unit
# `causadb` cubre serve/harvest → `skipped_by_unit`, si no por PID. La
# gobernanza por unidad propia rige SOLO cuando está instalada.

import argparse  # noqa: E402
import threading  # noqa: E402

from causadb.cli import _cmd_watch  # noqa: E402


def _watch_fixture_units(tmp_path, ledger, with_harvest_unit=False):
    """Escribe unit files falsos en un SYSTEMD_USER_DIR temporal."""
    from causadb._workspace import WorkspaceManager  # noqa
    unit_dir = str(tmp_path / "systemd" / "user")
    os.makedirs(unit_dir, exist_ok=True)
    with open(os.path.join(unit_dir, "causadb.service"), "w") as f:
        f.write("[Unit]\nDescription=fake\n\n[Service]\n")
        f.write(f"ExecStart=/usr/bin/causadb serve start --ledger {ledger}\n")
        f.write("Restart=on-failure\n")
    if with_harvest_unit:
        with open(os.path.join(unit_dir, "causadb-harvest.service"), "w") as f:
            f.write("[Unit]\nDescription=fake harvest\n\n[Service]\n")
            f.write(f"ExecStart=/usr/bin/causadb harvest start --ledger {ledger} --foreground\n")
            f.write("Restart=always\n")
    return unit_dir


def _fake_systemctl_run(active_out="active"):
    class _R:
        def __init__(self, rc, out):
            self.returncode = rc
            self.stdout = out
            self.stderr = ""

    def fake(cmd, **kw):
        s = str(cmd)
        if "is-active" in s:
            return _R(0, active_out + "\n")
        if "is-enabled" in s:
            return _R(0, "enabled\n")
        if "show" in s:
            return _R(0, "MainPID=1234\nActiveEnterTimestamp=Mon 2024-01-01\n")
        return _R(0, "")

    return fake


def _mock_daemon_for_watch(running_map):
    m = MagicMock()
    m.is_running.side_effect = lambda name: running_map.get(name, False)
    return m


def _watch_status_json(ledger, daemon):
    args = argparse.Namespace(ledger=ledger, action="status", format="json")
    code, out = _cmd_watch.cmd_watch(args)
    assert code == 0
    return json.loads(out)


def test_watch_harvest_fallback_no_unit_serve_covered(tmp_path):
    """(i) Sin unidad harvest + serve cubierto → harvest `skipped_by_unit`."""
    from causadb._workspace import WorkspaceManager
    proj = tmp_path / "proj"
    proj.mkdir()
    WorkspaceManager.init(str(proj))
    ledger = os.path.join(str(proj), ".causadb", "ledger.log")
    unit_dir = _watch_fixture_units(tmp_path, ledger, with_harvest_unit=False)
    daemon = _mock_daemon_for_watch({"vigilante": True, "mcp_proxy": True,
                                     "proxy_server": True, "harvest": True,
                                     "serve": True})
    with patch("causadb._daemon_service.SYSTEMD_USER_DIR", unit_dir), \
         patch("causadb._systemd_utils.SYSTEMD_USER_DIR", unit_dir), \
         patch("causadb._systemd_utils.subprocess.run",
               side_effect=_fake_systemctl_run("active")), \
         patch("causadb.cli._cmd_watch.get_daemon", return_value=daemon):
        payload = _watch_status_json(ledger, daemon)
    assert payload["mode"] == "systemd"
    assert payload["harvest_unit"]["installed"] is False
    assert payload["watch_forks"]["serve"] == "skipped_by_unit"
    # Paridad legacy: el ExecStart del serve cubre al harvest.
    assert payload["watch_forks"]["harvest"] == "skipped_by_unit"


def test_watch_harvest_unit_installed_active(tmp_path):
    """(ii) Con unidad harvest instalada+activa → `skipped_by_unit`."""
    from causadb._workspace import WorkspaceManager
    proj = tmp_path / "proj"
    proj.mkdir()
    WorkspaceManager.init(str(proj))
    ledger = os.path.join(str(proj), ".causadb", "ledger.log")
    unit_dir = _watch_fixture_units(tmp_path, ledger, with_harvest_unit=True)
    daemon = _mock_daemon_for_watch({"vigilante": True, "mcp_proxy": True,
                                     "proxy_server": True, "harvest": True,
                                     "serve": True})
    with patch("causadb._daemon_service.SYSTEMD_USER_DIR", unit_dir), \
         patch("causadb._systemd_utils.SYSTEMD_USER_DIR", unit_dir), \
         patch("causadb._systemd_utils.subprocess.run",
               side_effect=_fake_systemctl_run("active")), \
         patch("causadb.cli._cmd_watch.get_daemon", return_value=daemon):
        payload = _watch_status_json(ledger, daemon)
    assert payload["harvest_unit"]["installed"] is True
    assert payload["harvest_unit"]["active"] is True
    assert payload["watch_forks"]["harvest"] == "skipped_by_unit"


def test_watch_harvest_unit_installed_inactive_falls_to_pid(tmp_path,
                                                            monkeypatch):
    """(iii) Unidad harvest instalada pero inactiva → por PID."""
    from causadb._workspace import WorkspaceManager
    from causadb._systemd_utils import SystemdUnitStatus
    proj = tmp_path / "proj"
    proj.mkdir()
    WorkspaceManager.init(str(proj))
    ledger = os.path.join(str(proj), ".causadb", "ledger.log")
    unit_dir = _watch_fixture_units(tmp_path, ledger, with_harvest_unit=True)

    def fake_get_unit_status(unit_name="causadb"):
        if unit_name == "causadb-harvest":
            return SystemdUnitStatus(
                installed=True, active=False, state="inactive",
                enabled="enabled", main_pid=None,
                exec_start=f"/usr/bin/causadb harvest start --ledger {ledger}",
                since=None, load_error=None)
        return SystemdUnitStatus(
            installed=True, active=True, state="active",
            enabled="enabled", main_pid=1234,
            exec_start=f"/usr/bin/causadb serve start --ledger {ledger}",
            since="Mon 2024-01-01", load_error=None)

    monkeypatch.setattr("causadb.cli._cmd_watch.get_unit_status",
                        fake_get_unit_status)
    # PID vivo → running (no skipped_by_unit aunque serve sí lo esté).
    daemon = _mock_daemon_for_watch({"vigilante": False, "mcp_proxy": False,
                                     "proxy_server": False, "harvest": True,
                                     "serve": False})
    with patch("causadb.cli._cmd_watch.get_daemon", return_value=daemon):
        payload = _watch_status_json(ledger, daemon)
    assert payload["harvest_unit"]["installed"] is True
    assert payload["harvest_unit"]["active"] is False
    assert payload["watch_forks"]["harvest"] == "running"
    assert payload["watch_forks"]["serve"] == "skipped_by_unit"
    # PID muerto → stopped.
    daemon2 = _mock_daemon_for_watch({"harvest": False})
    with patch("causadb.cli._cmd_watch.get_daemon", return_value=daemon2):
        payload2 = _watch_status_json(ledger, daemon2)
    assert payload2["watch_forks"]["harvest"] == "stopped"


# (g) foreground bloqueante para supervisión systemd --------------------------
# `harvest start` sin --daemon retornaba al instante (sin tick estable bajo
# systemd + Restart=always en loop). `--foreground` bloquea el hilo
# principal mientras los Timer ticks corren en background.

def test_harvest_service_template_uses_foreground():
    assert "--foreground" in ds.HARVEST_SERVICE_TEMPLATE
    assert "harvest start" in ds.HARVEST_SERVICE_TEMPLATE
    assert "Restart=always" in ds.HARVEST_SERVICE_TEMPLATE


def test_harvest_foreground_blocks_and_ticks(tmp_path, monkeypatch):
    """Foreground: no retorna antes de tiempo y escribe heartbeat."""
    from causadb.cli import _cmd_harvest as ch
    ledger = _ledger_in(tmp_path)

    orig_init = ds.HarvesterDaemon.__init__

    def _fast_init(self, ledger_path):
        orig_init(self, ledger_path)
        self.interval = 1  # 1s: intervalo corto solo para el test
        self.harvester.harvest_all = MagicMock(return_value={})

    monkeypatch.setattr(ds.HarvesterDaemon, "__init__", _fast_init)
    # signal.signal solo vale en el hilo principal: no-op en el test.
    monkeypatch.setattr(ch, "install_signal_handlers",
                        lambda ledger_path: None)
    captured = {}
    monkeypatch.setattr(ch, "set_current_harvester_daemon",
                        lambda d: captured.setdefault("daemon", d))
    mock_platform = MagicMock()
    mock_platform.is_running.return_value = False
    monkeypatch.setattr(ch, "get_daemon", lambda: mock_platform)

    args = argparse.Namespace(ledger=ledger, daemon=False, foreground=True)
    results = {}
    t = threading.Thread(
        target=lambda: results.update(
            {"ret": ch._start(ledger, args)}), daemon=True)
    t.start()
    try:
        hb = os.path.join(os.path.dirname(ledger), "harvest.heartbeat")
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.isfile(hb):
            time.sleep(0.1)
        assert os.path.isfile(hb), "foreground debe correr ticks (heartbeat)"
        epoch = float(open(hb).read().strip())
        assert abs(time.time() - epoch) < 60
        time.sleep(1.0)  # ventana extra: el entrypoint sigue bloqueado
        assert t.is_alive(), "foreground NO debe retornar antes de tiempo"
        assert "daemon" in captured
    finally:
        if "daemon" in captured:
            captured["daemon"].stop()
        t.join(timeout=10)
    assert not t.is_alive(), "stop() debe liberar el foreground"
    assert results["ret"][0] == 0
    assert json.loads(results["ret"][1])["status"] == "started"
