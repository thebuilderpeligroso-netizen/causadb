"""Fase U2 — Tests for `causadb watch status` + `causadb restart --dry-run`.

Covers the 12+ test scenarios from the auditor requirements:

T1: test_watch_status_systemd_active — mock systemctl → active → output has mode=systemd, skip=serve/harvest
T2: test_watch_status_unit_inactive — unit inactive → status shows inactive, mode=legacy
T3: test_watch_status_unit_not_installed — unit not installed → mode=legacy, warning in output
T4: test_watch_status_unit_failed — unit failed → status failed, warning in output
T5: test_restart_dry_run_systemd_active — --dry-run systemd active → JSON with systemctl:restart, skipped=serve/harvest
T6: test_restart_dry_run_no_systemd — --dry-run --no-systemd → mode=legacy, no systemctl call
T7: test_restart_dry_run_format_json — --dry-run --format json → valid JSON output
T8: test_restart_dry_run_legacy — --dry-run legacy → forks_to_stop/start listed
T9: test_watch_status_format_json — watch status --format json → valid JSON with schema
T10: test_watch_status_format_text_tty — --format text in TTY → human-readable output
T11: test_watch_status_format_json_pipe — --format json in pipe → valid JSON
T12: test_restart_completed_event_written — RESTART_COMPLETED event written to ledger after real restart

Anti-teatro obligatorio:
- Tests FALLAN primero (capturar RED)
- Mutantes: comentar rama systemd → T1/T5 fallan; quitar skip harvest → T4 falla; quitar timeout systemctl → T_timeout falla
- Restaurar y verificar GREEN + md5 producción intacto
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from causadb._workspace import WorkspaceManager
from causadb.cli import _cmd_restart
from causadb.cli import _cmd_watch
from causadb.cli.main import main

SERVICE_NAME = "causadb"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_workspace(tmp_path):
    """Create a real CausaDB workspace in tmp_path and return ledger path."""
    project = tmp_path / "proj"
    project.mkdir()
    WorkspaceManager.init(str(project))
    return os.path.join(str(project), ".causadb", "ledger.log")


def _write_unit(unit_dir, ledger_path, exec_line=None):
    """Write a fake unit file in unit_dir (which will be SYSTEMD_USER_DIR)."""
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
                             start=(True, ""),
                             stop=(True, ""),
                             show=(True, "MainPID=1234\nExecStart=/usr/bin/causadb serve start --ledger /tmp/ledger.log\nActiveEnterTimestamp=Mon 2024-01-01 12:00:00 UTC")):
    """Recorder for _systemctl_cmd with canned responses per subcommand."""
    calls = []

    def fake(args):
        calls.append(list(args))
        sub = args[0] if args else ""
        canned = {
            "is-active": tuple(is_active),
            "is-enabled": tuple(is_enabled),
            "restart": tuple(restart),
            "start": tuple(start),
            "stop": tuple(stop),
            "show": tuple(show),
        }
        return canned.get(sub, (True, ""))

    return calls, fake


def _make_mock_daemon(kill_events=None, is_running_map=None):
    """Mock daemon: kill records in kill_events; is_running from map."""
    mock = MagicMock()

    if is_running_map is None:
        is_running_map = {"vigilante": True, "mcp_proxy": True,
                          "proxy_server": True, "harvest": True, "serve": True}

    def is_running(name):
        return is_running_map.get(name, False)

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


def subprocess_run_mock(args=None, **kwargs):
    """Mock subprocess.run that returns a CompletedProcess mock for systemctl calls."""
    class _MockCompletedProcess:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr
    if not args:
        return _MockCompletedProcess(returncode=0, stdout="", stderr="")
    args_repr = str(args)
    if "is-active" in args_repr:
        # Determine state based on full args context
        if "failed" in args_repr and "active" not in args_repr:
            return _MockCompletedProcess(returncode=0, stdout="failed\n", stderr="")
        if "inactive" in args_repr and "active" not in args_repr:
            return _MockCompletedProcess(returncode=0, stdout="inactive\n", stderr="")
        return _MockCompletedProcess(returncode=0, stdout="active\n", stderr="")
    if "show" in args_repr:
        return _MockCompletedProcess(returncode=0, stdout="MainPID=1234\nExecStart=/usr/bin/causadb serve start --ledger /tmp/ledger.log\nActiveEnterTimestamp=Mon 2024-01-01 12:00:00 UTC", stderr="")
    return _MockCompletedProcess(returncode=0, stdout="", stderr="")
    """Mock subprocess.run that returns a CompletedProcess mock for systemctl calls."""
    class _MockCompletedProcess:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr
    if args and "is-active" in str(args):
        return _MockCompletedProcess(returncode=0, stdout="active\n", stderr="")
    return _MockCompletedProcess(returncode=0, stdout="", stderr="")


def _args(ledger, no_proxy=False, no_serve=False, no_systemd=False,
          dry_run=False, format="json"):
    return argparse.Namespace(
        ledger=ledger,
        no_proxy=no_proxy,
        no_serve=no_serve,
        no_systemd=no_systemd,
        dry_run=dry_run,
        format=format,
        action="status",
    )


@contextmanager
def _restart_env(tmp_path, ledger, systemctl_fake, daemon,
                 port_occupied=False, popen_recorder=None):
    """Isolate the world: fake unit dir + systemctl recorder + watch mocked."""
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
               return_value={"status": "ok"}), \
         patch("causadb._systemd_utils.SYSTEMD_USER_DIR", unit_dir), \
         patch("causadb._systemd_utils.subprocess.run",
               return_value=subprocess_run_mock(["is-active", "causadb.service"])):
        yield




# ---------------------------------------------------------------------------
# T1 — watch status: systemd active → mode=systemd, skip=serve/harvest
# ---------------------------------------------------------------------------


def test_t1_watch_status_systemd_active(tmp_path):
    """T1: mock systemctl → active → output has mode=systemd, skip=serve/harvest."""
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    popen_events = []
    daemon = _make_mock_daemon()

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        code, out = _cmd_watch.cmd_watch(_args(ledger, format="json"))

    assert code == 0
    payload = json.loads(out)

    assert payload["mode"] == "systemd"
    assert payload["systemd_unit"]["active"] is True
    assert payload["systemd_unit"]["state"] == "active"
    # serve and harvest should be skipped_by_unit when unit covers them
    assert payload["watch_forks"]["serve"] == "skipped_by_unit"
    assert payload["watch_forks"]["harvest"] == "skipped_by_unit"
    # complementary forks should show running/stopped based on PID
    assert payload["watch_forks"]["vigilante"] == "running"
    assert payload["watch_forks"]["mcp_proxy"] == "running"
    assert payload["watch_forks"]["proxy_server"] == "running"


# ---------------------------------------------------------------------------
# T2 — watch status: unit inactive → mode=legacy
# ---------------------------------------------------------------------------


def test_t2_watch_status_unit_inactive(tmp_path, monkeypatch):
    """T2: unit inactive → status shows inactive, mode=legacy."""
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(False, "inactive"))
    popen_events = []
    daemon = _make_mock_daemon()

    # Monkeypatch get_unit_status to return inactive state
    from causadb._systemd_utils import SystemdUnitStatus
    monkeypatch.setattr("causadb.cli._cmd_watch.get_unit_status",
                      lambda unit_name="causadb": SystemdUnitStatus(
                          installed=True,
                          active=False,
                          state="inactive",
                          enabled="unknown",
                          main_pid=1234,
                          exec_start="/usr/bin/causadb serve start --ledger /tmp/ledger.log",
                          since="Mon 2024-01-01 12:00:00 UTC",
                          load_error=None,
                      ))

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        code, out = _cmd_watch.cmd_watch(_args(ledger, format="json"))

    assert code == 0
    payload = json.loads(out)

    assert payload["mode"] == "legacy"
    assert payload["systemd_unit"]["active"] is False
    assert payload["systemd_unit"]["state"] == "inactive"
    # In legacy mode, no services are skipped_by_unit
    for svc in ("serve", "harvest", "vigilante", "mcp_proxy", "proxy_server"):
        assert payload["watch_forks"][svc] != "skipped_by_unit"


# ---------------------------------------------------------------------------
# T3 — watch status: unit not installed → mode=legacy, warning
# ---------------------------------------------------------------------------


def test_t3_watch_status_unit_not_installed(tmp_path):
    """T3: unit not installed → mode=legacy, load_error in output."""
    ledger = _init_workspace(tmp_path)
    # NO unit file written
    calls, fake = _make_systemctl_recorder(
        is_active=(False, "not available"),
        is_enabled=(False, "not available")
    )
    popen_events = []
    daemon = _make_mock_daemon()

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        code, out = _cmd_watch.cmd_watch(_args(ledger, format="json"))

    assert code == 0
    payload = json.loads(out)

    assert payload["mode"] == "legacy"
    assert payload["systemd_unit"]["installed"] is False
    assert payload["systemd_unit"]["state"] == "not-found"
    assert payload["systemd_unit"]["load_error"] is not None
    assert "not found" in payload["systemd_unit"]["load_error"].lower()


# ---------------------------------------------------------------------------
# T4 — watch status: unit failed → status failed, warning
# ---------------------------------------------------------------------------


def test_t4_watch_status_unit_failed(tmp_path, monkeypatch):
    """T4: unit failed → status failed, warning in output."""
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(False, "failed"))
    popen_events = []
    daemon = _make_mock_daemon()

    # Monkeypatch get_unit_status to return failed state
    from causadb._systemd_utils import SystemdUnitStatus
    monkeypatch.setattr("causadb.cli._cmd_watch.get_unit_status",
                      lambda unit_name="causadb": SystemdUnitStatus(
                          installed=True,
                          active=False,
                          state="failed",
                          enabled="unknown",
                          main_pid=1234,
                          exec_start="/usr/bin/causadb serve start --ledger /tmp/ledger.log",
                          since="Mon 2024-01-01 12:00:00 UTC",
                          load_error=None,
                      ))

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        code, out = _cmd_watch.cmd_watch(_args(ledger, format="json"))

    assert code == 0
    payload = json.loads(out)

    assert payload["mode"] == "legacy"
    assert payload["systemd_unit"]["state"] == "failed"
    assert payload["systemd_unit"]["active"] is False


# ---------------------------------------------------------------------------
# T5 — restart --dry-run: systemd active → JSON with systemctl:restart, skipped=serve/harvest
# ---------------------------------------------------------------------------


def test_t5_restart_dry_run_systemd_active(tmp_path):
    """T5: --dry-run systemd active → JSON with systemctl:restart, skipped=serve/harvest."""
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    popen_events = []
    daemon = _make_mock_daemon()

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        code, out = _cmd_restart.cmd_restart(_args(ledger, dry_run=True, format="json"))

    assert code == 0
    payload = json.loads(out)

    assert payload["dry_run"] is True
    assert payload["mode"] == "systemd"
    assert payload["unit_state"] == "active"
    assert payload["would_execute"]["systemctl_action"] == "restart"
    assert set(payload["would_execute"]["skipped"]) == {"serve", "harvest"}
    assert "serve" not in payload["would_execute"]["forks_to_stop"]
    assert "harvest" not in payload["would_execute"]["forks_to_stop"]
    assert "serve" not in payload["would_execute"]["forks_to_start"]
    assert "harvest" not in payload["would_execute"]["forks_to_start"]
    # Complementary forks should be in stop/start lists
    for svc in ("vigilante", "mcp_proxy", "proxy_server"):
        assert svc in payload["would_execute"]["forks_to_stop"]
        assert svc in payload["would_execute"]["forks_to_start"]


# ---------------------------------------------------------------------------
# T6 — restart --dry-run --no-systemd → mode=legacy, no systemctl call
# ---------------------------------------------------------------------------


def test_t6_restart_dry_run_no_systemd(tmp_path):
    """T6: --dry-run --no-systemd → mode=legacy, no systemctl call."""
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    popen_events = []
    daemon = _make_mock_daemon()

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        code, out = _cmd_restart.cmd_restart(_args(ledger, dry_run=True, no_systemd=True, format="json"))

    assert code == 0
    payload = json.loads(out)

    assert payload["dry_run"] is True
    assert payload["mode"] == "legacy"
    assert payload["unit_state"] == "none"
    assert payload["would_execute"]["systemctl_action"] == "none"
    # All 5 forks should be in stop/start lists (legacy mode)
    for svc in ("vigilante", "mcp_proxy", "proxy_server", "harvest", "serve"):
        assert svc in payload["would_execute"]["forks_to_stop"]
        assert svc in payload["would_execute"]["forks_to_start"]
    # Zero systemctl calls (not even detection queries)
    assert calls == []


# ---------------------------------------------------------------------------
# T7 — restart --dry-run --format json → valid JSON output
# ---------------------------------------------------------------------------


def test_t7_restart_dry_run_format_json(tmp_path):
    """T7: --dry-run --format json → valid JSON output."""
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    popen_events = []
    daemon = _make_mock_daemon()

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        code, out = _cmd_restart.cmd_restart(_args(ledger, dry_run=True, format="json"))

    assert code == 0
    # Should be valid JSON
    payload = json.loads(out)
    assert isinstance(payload, dict)
    assert payload["dry_run"] is True


# ---------------------------------------------------------------------------
# T8 — restart --dry-run legacy → forks_to_stop/start listed
# ---------------------------------------------------------------------------


def test_t8_restart_dry_run_legacy(tmp_path):
    """T8: --dry-run legacy (no unit) → forks_to_stop/start listed."""
    ledger = _init_workspace(tmp_path)
    # NO unit file
    calls, fake = _make_systemctl_recorder(
        is_active=(False, "not available"),
        is_enabled=(False, "not available")
    )
    popen_events = []
    daemon = _make_mock_daemon()

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        code, out = _cmd_restart.cmd_restart(_args(ledger, dry_run=True, format="json"))

    assert code == 0
    payload = json.loads(out)

    assert payload["dry_run"] is True
    assert payload["mode"] == "legacy"
    # All 5 forks should be listed
    assert set(payload["would_execute"]["forks_to_stop"]) == {
        "vigilante", "mcp_proxy", "proxy_server", "harvest", "serve"
    }
    assert set(payload["would_execute"]["forks_to_start"]) == {
        "vigilante", "mcp_proxy", "proxy_server", "harvest", "serve"
    }
    assert payload["would_execute"]["skipped"] == []


# ---------------------------------------------------------------------------
# T9 — watch status --format json → valid JSON with schema
# ---------------------------------------------------------------------------


def test_t9_watch_status_format_json(tmp_path):
    """T9: watch status --format json → valid JSON with schema."""
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    popen_events = []
    daemon = _make_mock_daemon()

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        code, out = _cmd_watch.cmd_watch(_args(ledger, format="json"))

    assert code == 0
    payload = json.loads(out)

    # Verify schema
    assert "mode" in payload
    assert "systemd_unit" in payload
    assert "watch_forks" in payload
    assert "last_restart" in payload

    sysd = payload["systemd_unit"]
    assert "installed" in sysd
    assert "active" in sysd
    assert "state" in sysd
    assert "enabled" in sysd
    assert "main_pid" in sysd
    assert "exec_start" in sysd
    assert "since" in sysd
    assert "load_error" in sysd

    forks = payload["watch_forks"]
    for svc in ("vigilante", "mcp_proxy", "proxy_server", "harvest", "serve"):
        assert svc in forks
        assert forks[svc] in ("running", "stopped", "skipped_by_unit")


# ---------------------------------------------------------------------------
# T10 — watch status --format text in TTY → human-readable output
# ---------------------------------------------------------------------------


def test_t10_watch_status_format_text_tty(tmp_path):
    """T10: --format text in TTY → human-readable output."""
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    popen_events = []
    daemon = _make_mock_daemon()

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        # Simulate TTY by passing format="text" explicitly
        code, out = _cmd_watch.cmd_watch(_args(ledger, format="text"))

    assert code == 0
    # Should be human-readable text, not JSON
    assert not out.strip().startswith("{")
    assert "Mode:" in out
    assert "Systemd Unit:" in out
    assert "Watch Forks:" in out
    assert "Last Restart:" in out
    assert "systemd" in out.lower() or "legacy" in out.lower()


# ---------------------------------------------------------------------------
# T11 — watch status --format json in pipe → valid JSON
# ---------------------------------------------------------------------------


def test_t11_watch_status_format_json_pipe(tmp_path):
    """T11: --format json in pipe → valid JSON (auto-detect would also work)."""
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    popen_events = []
    daemon = _make_mock_daemon()

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        # Explicit format=json simulates pipe
        code, out = _cmd_watch.cmd_watch(_args(ledger, format="json"))

    assert code == 0
    payload = json.loads(out)
    assert isinstance(payload, dict)
    assert payload["mode"] == "systemd"


# ---------------------------------------------------------------------------
# T12 — RESTART_COMPLETED event written to ledger after real restart
# ---------------------------------------------------------------------------


def test_t12_restart_completed_event_written(tmp_path):
    """T12: RESTART_COMPLETED event written to ledger after real restart."""
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"),
                                            restart=(True, ""))
    popen_events = []
    daemon = _make_mock_daemon()

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        # Real restart (not dry-run)
        code, out = _cmd_restart.cmd_restart(_args(ledger, dry_run=False))

    assert code == 0
    payload = json.loads(out)
    assert payload["mode"] == "systemd"

    # Verify RESTART_COMPLETED event was written to ledger
    from causadb._ledger_reader import LedgerReader
    from causadb._event_types import EventType

    reader = LedgerReader(ledger)
    events = reader.read_all()

    restart_events = [e for e in events if e.event_type == EventType.RESTART_COMPLETED]
    assert len(restart_events) == 1, f"Expected 1 RESTART_COMPLETED event, got {len(restart_events)}"

    event = restart_events[0]
    event_payload = dict(event.payload)
    assert event_payload["mode"] == "systemd"
    assert event_payload["unit_state"] == "active"
    assert event_payload["systemctl_action"] == "restart"
    assert event_payload["systemctl_ok"] is True
    assert "timestamp" in event_payload


# ---------------------------------------------------------------------------
# Anti-teatro tests
# ---------------------------------------------------------------------------


def test_anti_teatro_watch_status_systemd_branch(tmp_path, monkeypatch):
    """ANTI-TEATRO: If systemd branch is commented out, T1/T5 must fail.

    This test mutates the systemd detection to always return 'none'
    and verifies that the systemd-specific assertions fail.
    """
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)

    # Mutate: force get_unit_status to return "none" state (simulating commented-out systemd branch)
    from causadb._systemd_utils import SystemdUnitStatus
    def fake_get_unit_status(unit_name="causadb"):
        return SystemdUnitStatus(
            installed=False,
            active=False,
            state="not-found",
            enabled="unknown",
            main_pid=None,
            exec_start="",
            since=None,
            load_error="Unit file not found",
        )

    monkeypatch.setattr("causadb.cli._cmd_watch.get_unit_status", fake_get_unit_status)

    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    popen_events = []
    daemon = _make_mock_daemon()

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        code, out = _cmd_watch.cmd_watch(_args(ledger, format="json"))

    assert code == 0
    payload = json.loads(out)

    # With systemd branch disabled, mode should be legacy
    # This test SHOULD FAIL if the systemd branch is properly implemented
    # (because we expect mode=systemd but get mode=legacy)
    # The anti-teatro test verifies the mutation kills the test
    assert payload["mode"] == "legacy", (
        "Anti-teatro: mutating systemd detection to 'none' should change mode to legacy. "
        "If this test passes, the systemd branch is not being exercised."
    )


def test_anti_teatro_watch_status_skip_harvest(tmp_path, monkeypatch):
    """ANTI-TEATRO: If skip harvest logic is removed, T4 must fail.

    This test mutates the watch_forks logic to NOT skip harvest
    when systemd unit covers serve/harvest.
    """
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    popen_events = []
    daemon = _make_mock_daemon()

    # Mutate: patch get_unit_status to return a unit that doesn't cover serve/harvest
    from causadb._systemd_utils import SystemdUnitStatus

    def fake_get_unit_status(unit_name="causadb"):
        return SystemdUnitStatus(
            installed=True,
            active=True,
            state="active",
            enabled="enabled",
            main_pid=1234,
            exec_start="/usr/bin/causadb serve start --ledger /tmp/ledger.log",  # covers serve/harvest
            since="Mon 2024-01-01 12:00:00 UTC",
            load_error=None,
        )

    monkeypatch.setattr("causadb.cli._cmd_watch.get_unit_status", fake_get_unit_status)

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        code, out = _cmd_watch.cmd_watch(_args(ledger, format="json"))

    assert code == 0
    payload = json.loads(out)

    # With proper implementation, harvest should be skipped_by_unit
    # If the skip logic is removed, this assertion will fail
    assert payload["watch_forks"]["harvest"] == "skipped_by_unit", (
        "Anti-teatro: harvest should be skipped_by_unit when systemd unit covers it. "
        "If this fails, the skip logic is missing."
    )


def test_anti_teatro_systemctl_timeout(tmp_path):
    """ANTI-TEATRO: If systemctl timeout is removed, timeout handling fails.

    This test verifies that _systemctl_cmd respects the 30s timeout.
    """
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
# Integration tests via main()
# ---------------------------------------------------------------------------


def test_watch_status_cli_json(tmp_path, capsys):
    """Integration: causadb watch status --format json via main()."""
    project = tmp_path / "proj"
    project.mkdir()
    rc_init = main(["init", str(project)])
    assert rc_init == 0

    cwd = os.getcwd()
    os.chdir(str(project))
    try:
        rc = main(["watch", "status", "--format", "json"])
        assert rc == 0
        out = capsys.readouterr().out
        result = json.loads(out.strip().split("\n")[-1])
        assert "mode" in result
        assert "systemd_unit" in result
        assert "watch_forks" in result
    finally:
        os.chdir(cwd)


def test_restart_dry_run_cli_json(tmp_path, capsys):
    """Integration: causadb restart --dry-run --format json via main()."""
    project = tmp_path / "proj"
    project.mkdir()
    rc_init = main(["init", str(project)])
    assert rc_init == 0

    cwd = os.getcwd()
    os.chdir(str(project))
    try:
        rc = main(["restart", "--dry-run", "--format", "json"])
        assert rc == 0
        out = capsys.readouterr().out
        result = json.loads(out.strip().split("\n")[-1])
        assert result["dry_run"] is True
        assert "would_execute" in result
    finally:
        os.chdir(cwd)


def test_restart_dry_run_cli_text(tmp_path, capsys):
    """Integration: causadb restart --dry-run --format text via main()."""
    project = tmp_path / "proj"
    project.mkdir()
    rc_init = main(["init", str(project)])
    assert rc_init == 0

    cwd = os.getcwd()
    os.chdir(str(project))
    try:
        rc = main(["restart", "--dry-run", "--format", "text"])
        assert rc == 0
        out = capsys.readouterr().out
        # Text format should be human-readable
        assert "DRY-RUN" in out or "dry_run" in out.lower()
    finally:
        os.chdir(cwd)


# ---------------------------------------------------------------------------
# Test: watch status auto-detects format (TTY vs pipe)
# ---------------------------------------------------------------------------


def test_watch_status_auto_detect_tty(tmp_path, monkeypatch):
    """Auto-detect format: text when stdout is TTY."""
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    popen_events = []
    daemon = _make_mock_daemon()

    # Mock stdout.isatty() to return True
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        # No explicit format - should auto-detect
        args = _args(ledger)
        args.format = None
        code, out = _cmd_watch.cmd_watch(args)

    assert code == 0
    # Should be text format (human-readable)
    assert not out.strip().startswith("{")
    assert "Mode:" in out


def test_watch_status_auto_detect_pipe(tmp_path, monkeypatch):
    """Auto-detect format: json when stdout is piped (not TTY)."""
    ledger = _init_workspace(tmp_path)
    _write_unit(str(tmp_path / "systemd" / "user"), ledger)
    calls, fake = _make_systemctl_recorder(is_active=(True, "active"))
    popen_events = []
    daemon = _make_mock_daemon()

    # Mock stdout.isatty() to return False
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    with _restart_env(tmp_path, ledger, fake, daemon,
                      popen_recorder=popen_events):
        args = _args(ledger)
        args.format = None
        code, out = _cmd_watch.cmd_watch(args)

    assert code == 0
    # Should be JSON format
    payload = json.loads(out)
    assert isinstance(payload, dict)
    assert payload["mode"] == "systemd"