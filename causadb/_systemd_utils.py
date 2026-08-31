"""Systemd unit status utilities for CausaDB.

Provides a typed helper to query systemd unit status without raising exceptions.
Used by `causadb watch status` and `causadb restart --dry-run`.
"""
import os
import subprocess
from dataclasses import dataclass
from typing import Optional, Literal

from causadb._daemon_service import SYSTEMD_USER_DIR


@dataclass(frozen=True)
class SystemdUnitStatus:
    """Typed representation of a systemd unit status.

    Never raises; returns degraded state on error.
    """
    installed: bool
    active: bool
    state: Literal["active", "inactive", "failed", "not-found"]
    enabled: str  # "enabled" | "disabled" | "masked" | "static" | "unknown"
    main_pid: Optional[int]
    exec_start: str
    since: Optional[str]
    load_error: Optional[str] = None


def get_unit_status(unit_name: str = "causadb") -> SystemdUnitStatus:
    """Detect state of a systemd user unit.

    Args:
        unit_name: Unit name without .service suffix (default: "causadb").

    Returns:
        SystemdUnitStatus with all fields populated. On any error,
        returns a degraded status with installed=False and load_error set.
    """
    unit_name = f"{unit_name}.service"
    unit_path = os.path.join(SYSTEMD_USER_DIR, unit_name)

    # Check if unit file exists
    if not os.path.exists(unit_path):
        return SystemdUnitStatus(
            installed=False,
            active=False,
            state="not-found",
            enabled="unknown",
            main_pid=None,
            exec_start="",
            since=None,
            load_error=f"Unit file not found at {unit_path}",
        )

    # Read ExecStart from unit file
    exec_start = ""
    try:
        with open(unit_path, "r") as f:
            for line in f:
                if line.strip().startswith("ExecStart="):
                    exec_start = line.strip()[len("ExecStart="):].strip()
                    break
    except OSError:
        pass

    # Query systemctl for status
    def _run_systemctl(args: list) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["systemctl", "--user"] + args,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            output = (result.stdout or "").strip()
            if result.returncode == 0:
                return True, output
            return False, output or (result.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return False, "systemctl timed out"
        except (OSError, FileNotFoundError) as exc:
            return False, f"systemctl not available: {exc}"

    # is-active
    active_ok, active_out = _run_systemctl(["is-active", unit_name])
    state = "active" if (active_ok and active_out == "active") else "inactive"
    if not active_ok and "failed" in (active_out or "").lower():
        state = "failed"

    # is-enabled
    enabled_ok, enabled_out = _run_systemctl(["is-enabled", unit_name])
    enabled = enabled_out if enabled_ok else "unknown"

    # show --property=MainPID,ExecStart,ActiveEnterTimestamp
    show_ok, show_out = _run_systemctl([
        "show", unit_name,
        "--property=MainPID,ExecStart,ActiveEnterTimestamp",
        "--value"
    ])

    main_pid = None
    since = None
    if show_ok and show_out:
        for line in show_out.splitlines():
            if line.startswith("MainPID="):
                pid_str = line[len("MainPID="):].strip()
                if pid_str and pid_str != "0":
                    try:
                        main_pid = int(pid_str)
                    except ValueError:
                        pass
            elif line.startswith("ActiveEnterTimestamp="):
                since = line[len("ActiveEnterTimestamp="):].strip() or None

    return SystemdUnitStatus(
        installed=True,
        active=(state == "active"),
        state=state,
        enabled=enabled,
        main_pid=main_pid,
        exec_start=exec_start,
        since=since,
        load_error=None,
    )