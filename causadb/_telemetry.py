"""Anonymous usage telemetry for CausaDB (#6, #9).

Respects ``telemetry.enabled`` opt-out flag. When disabled, all counter
operations are no-ops. Weekly reports are saved to ``~/.causadb/telemetry/``
for later export.

What is sent:
  - Anonymous usage counters (integers)
  - CausaDB version
  - OS platform name
  - Stack traces (in crash reports)

What is NOT sent:
  - File paths, event payloads, API keys, user data, workspace names
"""

import datetime
import json
import os
import sys
import threading
from typing import Optional

TELEMETRY_DIR: Optional[str] = None

# In-memory counters (thread-safe).
_COUNTERS: dict = {}
_COUNTERS_LOCK = threading.Lock()

# Seconds in a week.
_WEEK_SECONDS = 7 * 24 * 3600


def _get_telemetry_dir() -> str:
    """Lazy-resolve and create the telemetry storage directory."""
    global TELEMETRY_DIR
    if TELEMETRY_DIR is None:
        TELEMETRY_DIR = os.path.join(os.path.expanduser("~"), ".causadb", "telemetry")
    os.makedirs(TELEMETRY_DIR, exist_ok=True)
    return TELEMETRY_DIR


def _get_config() -> Optional[object]:
    """Load CausaDBConfig (returns None on failure)."""
    try:
        from causadb._config import CausaDBConfig
        return CausaDBConfig.from_env()
    except Exception:
        return None


def is_enabled() -> bool:
    """Check whether telemetry is enabled.

    Priority:
      1. ``CAUSADB_TELEMETRY_ENABLED`` env var
      2. ``~/.causadb/config.json`` user-level config
      3. Default ``True``
    """
    # 1. Env var
    env_val = os.getenv("CAUSADB_TELEMETRY_ENABLED")
    if env_val is not None:
        return env_val.lower() == "true"

    # 2. User-level config
    user_cfg_path = os.path.join(os.path.expanduser("~"), ".causadb", "config.json")
    if os.path.isfile(user_cfg_path):
        try:
            with open(user_cfg_path) as f:
                cfg = json.load(f)
            telemetry = cfg.get("telemetry", {})
            return telemetry.get("enabled", True)
        except (json.JSONDecodeError, OSError):
            pass

    # 3. Default
    return True


def set_enabled(enabled: bool) -> None:
    """Persist telemetry opt-out to user-level config.

    This writes to ``~/.causadb/config.json`` — the same file used by
    the crash reporter and other user-level settings.
    """
    user_cfg_dir = os.path.join(os.path.expanduser("~"), ".causadb")
    os.makedirs(user_cfg_dir, exist_ok=True)
    user_cfg_path = os.path.join(user_cfg_dir, "config.json")

    cfg: dict = {}
    if os.path.isfile(user_cfg_path):
        try:
            with open(user_cfg_path) as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            cfg = {}

    if "telemetry" not in cfg:
        cfg["telemetry"] = {}
    cfg["telemetry"]["enabled"] = enabled

    tmp = user_cfg_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, user_cfg_path)


def increment(counter_name: str, count: int = 1) -> None:
    """Increment an anonymous usage counter.

    No-op when telemetry is disabled.
    Thread-safe.
    """
    if not is_enabled():
        return
    with _COUNTERS_LOCK:
        _COUNTERS[counter_name] = _COUNTERS.get(counter_name, 0) + count


def get_counters() -> dict:
    """Return a snapshot of all in-memory counters (thread-safe)."""
    with _COUNTERS_LOCK:
        return dict(_COUNTERS)


def reset_counters() -> None:
    """Clear all in-memory counters (thread-safe)."""
    with _COUNTERS_LOCK:
        _COUNTERS.clear()


def _get_week_id() -> str:
    """Return ISO week identifier ``YYYY-WW`` (e.g. ``2026-W30``)."""
    now = datetime.datetime.now()
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def save_weekly_report() -> dict:
    """Aggregate current in-memory counters and persist to a weekly file.

    Returns the report dict. When telemetry is disabled, returns
    ``{"status": "disabled"}`` without writing anything.
    """
    if not is_enabled():
        return {"status": "disabled"}

    counters = get_counters()
    reset_counters()

    week_id = _get_week_id()
    report = {
        "week": week_id,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "counters": counters,
        "version": _read_version(),
        "platform": sys.platform,
    }

    path = os.path.join(_get_telemetry_dir(), f"week_{week_id}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(report, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

    return report


def _read_version() -> str:
    """Read version from ``pyproject.toml`` (same pattern as _crash_reporter)."""
    try:
        import causadb
        pkg_dir = os.path.dirname(causadb.__file__)
        pyproject_path = os.path.join(os.path.dirname(pkg_dir), "pyproject.toml")
        if os.path.isfile(pyproject_path):
            with open(pyproject_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("version"):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            val = parts[1].strip().strip('"').strip("'")
                            return val
    except Exception:
        pass
    return "0.0.0"


def list_weekly_reports() -> list:
    """Load and return all weekly telemetry reports."""
    d = _get_telemetry_dir()
    reports = []
    for fname in sorted(os.listdir(d)):
        if fname.startswith("week_") and fname.endswith(".json"):
            try:
                with open(os.path.join(d, fname)) as f:
                    reports.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
    return reports
