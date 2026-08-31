"""Crash Reporter for CausaDB.

Saves crash reports locally to ``~/.causadb/crashes/crash_TIMESTAMP.json``.
Deduplication: same normalized stack trace → increments ``occurrences`` counter
on the existing file instead of creating a new one.

Stack traces are normalized (absolute user paths stripped) and hashed via
SHA256 for dedup.

Contents per crash file (no secrets, no payloads, no API keys):

- ``timestamp``: ISO-8601
- ``exception_type``: e.g. ``ValueError``
- ``exception_msg``: the exception message
- ``stack_text``: the full normalized stack trace
- ``stack_hash``: SHA256 of normalized stack trace
- ``os``: ``platform.platform()``
- ``version``: from ``pyproject.toml``
- ``occurrences``: int (incremented on dedup)
"""

import datetime
import hashlib
import json
import os
import platform
import re
import sys
import traceback
from typing import Optional, Tuple

try:
    from datetime import UTC
    utcnow = lambda: datetime.datetime.now(UTC)
except ImportError:
    # Python < 3.11 compatibility
    utcnow = lambda: datetime.datetime.utcnow()

CRASHES_DIR = None  # resolved on first use


def _get_crashes_dir() -> str:
    global CRASHES_DIR
    if CRASHES_DIR is None:
        CRASHES_DIR = os.path.join(os.path.expanduser("~"), ".causadb", "crashes")
    os.makedirs(CRASHES_DIR, exist_ok=True)
    return CRASHES_DIR


def _read_version() -> str:
    """Read the current version from pyproject.toml (same pattern as _updater.py)."""
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


def _normalize_stack(text: str) -> str:
    """Normalize a stack trace by stripping absolute user paths.

    Replaces ``/home/<user>/...`` with ``~/...`` for consistent hashing
    across different machines.
    """
    normalized = re.sub(r'/home/[^/]+/', '~/', text)
    return normalized


def _stack_hash(stack_text: str) -> str:
    """SHA256 of normalized stack trace for dedup."""
    normalized = _normalize_stack(stack_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _collect_exc_info(exc_info=None):
    """Collect exception info from *exc_info* or ``sys.exc_info()``.

    Returns ``(etype, value, tb)`` tuple or ``(None, None, None)`` if
    no exception is available.
    """
    if exc_info is None:
        exc_info = sys.exc_info()
    if exc_info is None or all(e is None for e in exc_info):
        return (None, None, None)
    return exc_info


def save_crash(exc_info=None) -> dict:
    """Save a crash report anonymously.

    Args:
        exc_info: ``sys.exc_info()`` tuple. If ``None``, uses
            ``sys.exc_info()`` automatically.

    Returns:
        dict with ``crash_id`` (timestamp-based), ``path`` (file path).
    """
    info = _collect_exc_info(exc_info)
    etype, value, tb = info

    crashes_dir = _get_crashes_dir()
    timestamp = utcnow().isoformat() + "Z"

    # Build stack text
    if tb is not None:
        stack_text = "".join(traceback.format_exception(etype, value, tb))
    elif etype is not None:
        stack_text = f"{etype.__name__}: {value}"
    else:
        stack_text = "(no exception info)"

    # Normalize and hash
    normalized_stack = _normalize_stack(stack_text)
    new_hash = _stack_hash(normalized_stack)

    exception_type = etype.__name__ if etype is not None else "Unknown"
    exception_msg = str(value) if value is not None else "(no message)"

    version = _read_version()
    os_info = platform.platform()

    crash_data = {
        "timestamp": timestamp,
        "exception_type": exception_type,
        "exception_msg": exception_msg,
        "stack_text": normalized_stack,
        "stack_hash": new_hash,
        "os": os_info,
        "version": version,
        "occurrences": 1,
    }

    # Dedup: walk existing crash files for matching stack_hash
    for filename in sorted(os.listdir(crashes_dir)):
        if not filename.endswith(".json"):
            continue
        fpath = os.path.join(crashes_dir, filename)
        try:
            with open(fpath) as f:
                existing = json.load(f)
            if existing.get("stack_hash") == new_hash:
                # Increment occurrences on existing file
                existing["occurrences"] = existing.get("occurrences", 1) + 1
                existing["timestamp"] = timestamp  # update to latest occurrence
                _atomic_write(fpath, existing)
                return {
                    "crash_id": filename.replace(".json", ""),
                    "path": fpath,
                    "dedup": True,
                }
        except (json.JSONDecodeError, OSError):
            continue

    # No match — create new file
    crash_id = f"crash_{utcnow().strftime('%Y%m%d_%H%M%S_%f')}"
    filepath = os.path.join(crashes_dir, f"{crash_id}.json")
    _atomic_write(filepath, crash_data)
    return {"crash_id": crash_id, "path": filepath, "dedup": False}


def _atomic_write(path: str, data: dict) -> None:
    """Write *data* as JSON to *path* atomically (write to tmp, rename)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def list_crashes() -> list:
    """Return list of crash dicts sorted by timestamp descending.

    Each entry: ``{crash_id, timestamp, os, version, exception_type,
    exception_msg, occurrences}``.
    """
    crashes_dir = _get_crashes_dir()
    result = []
    for filename in os.listdir(crashes_dir):
        if not filename.endswith(".json"):
            continue
        if filename == "export_all.json":
            continue
        filepath = os.path.join(crashes_dir, filename)
        try:
            with open(filepath) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        crash_id = filename.replace(".json", "")
        result.append({
            "crash_id": crash_id,
            "timestamp": data.get("timestamp", ""),
            "os": data.get("os", ""),
            "version": data.get("version", ""),
            "exception_type": data.get("exception_type", ""),
            "exception_msg": data.get("exception_msg", ""),
            "occurrences": data.get("occurrences", 1),
            "stack_hash": data.get("stack_hash", ""),
            "path": filepath,
        })
    # Sort by timestamp descending (most recent first)
    result.sort(key=lambda c: c.get("timestamp", ""), reverse=True)
    return result


def get_crash(crash_id: str) -> Optional[dict]:
    """Return a single crash dict or ``None``."""
    crashes_dir = _get_crashes_dir()
    filepath = os.path.join(crashes_dir, f"{crash_id}.json")
    if not os.path.isfile(filepath):
        return None
    try:
        with open(filepath) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return {
        "crash_id": crash_id,
        "timestamp": data.get("timestamp", ""),
        "os": data.get("os", ""),
        "version": data.get("version", ""),
        "exception_type": data.get("exception_type", ""),
        "exception_msg": data.get("exception_msg", ""),
        "occurrences": data.get("occurrences", 1),
        "stack_hash": data.get("stack_hash", ""),
        "path": filepath,
    }


def delete_crash(crash_id: str) -> bool:
    """Remove a crash file. Returns ``True`` if removed, ``False`` if not found."""
    crashes_dir = _get_crashes_dir()
    filepath = os.path.join(crashes_dir, f"{crash_id}.json")
    if not os.path.isfile(filepath):
        return False
    os.remove(filepath)
    return True


def delete_all_crashes() -> int:
    """Remove all crash files. Returns the number of files removed."""
    crashes_dir = _get_crashes_dir()
    count = 0
    for filename in os.listdir(crashes_dir):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(crashes_dir, filename)
        try:
            os.remove(filepath)
            count += 1
        except OSError:
            continue
    return count


def save_global_excepthook() -> None:
    """Install ``sys.excepthook`` to auto-capture unhandled exceptions.

    This wraps the existing excepthook so that our save runs first, then
    the original handler (if any) is called.
    """
    original_hook = sys.excepthook

    def _crash_excepthook(etype, value, tb):
        """Custom excepthook that saves crash before delegating."""
        try:
            save_crash((etype, value, tb))
        except Exception:
            pass  # Don't crash in the crash handler
        if original_hook is not None:
            original_hook(etype, value, tb)

    sys.excepthook = _crash_excepthook


def crash_to_github_issue(crash: dict) -> str:
    """Format a crash as GitHub issue markdown (local, no API call).

    Returns a markdown string suitable for a GitHub issue body.
    """
    lines = []
    lines.append(f"## 🐛 Crash Report: {crash.get('exception_type', 'Unknown')}")
    lines.append("")
    lines.append(f"**Exception:** `{crash.get('exception_type', 'Unknown')}`")
    lines.append(f"**Message:** `{crash.get('exception_msg', 'N/A')}`")
    lines.append(f"**Occurrences:** {crash.get('occurrences', 1)}")
    lines.append(f"**OS:** {crash.get('os', 'Unknown')}")
    lines.append(f"**Version:** {crash.get('version', 'Unknown')}")
    lines.append("")
    lines.append("### Stack Trace")
    lines.append("```")
    lines.append(crash.get("stack_text", "(empty)"))
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("*This report was automatically generated by the CausaDB Crash Reporter.*")
    lines.append("*100% anonymous — no user data, paths, or API keys are included.*")
    return "\n".join(lines)


def crashes_to_export_file() -> str:
    """Export all crashes as a single JSON file.

    Returns the path to the exported file.
    """
    crashes_dir = _get_crashes_dir()
    export_path = os.path.join(crashes_dir, "export_all.json")
    crashes = list_crashes()
    # Strip stack_text from export (too large) but keep summary
    export_data = []
    for c in crashes:
        export_data.append({
            "crash_id": c["crash_id"],
            "timestamp": c["timestamp"],
            "exception_type": c["exception_type"],
            "exception_msg": c["exception_msg"],
            "os": c["os"],
            "version": c["version"],
            "occurrences": c["occurrences"],
            "stack_hash": c["stack_hash"],
        })
    _atomic_write(export_path, {"crashes": export_data, "total": len(export_data)})
    return export_path
