"""``causadb telemetry`` subcommand — status, on, off, export.

Artículo II: thin wrapper over ``causadb._telemetry``.
Artículo III: test-first.
"""
import json
from typing import Tuple

from causadb._telemetry import (
    is_enabled,
    set_enabled,
    list_weekly_reports,
    get_counters,
)


def cmd_telemetry(args) -> Tuple[int, str]:
    """Route ``causadb telemetry status|on|off|export``.

    Returns (exit_code, output_json).
    """
    action = args.telemetry_action

    if action == "status":
        return _telemetry_status()
    elif action == "on":
        return _telemetry_on()
    elif action == "off":
        return _telemetry_off()
    elif action == "export":
        return _telemetry_export()
    else:
        return (1, json.dumps({"error": f"Unknown action: {action}"}))


def _telemetry_status() -> Tuple[int, str]:
    """Show current telemetry status and active counters."""
    enabled = is_enabled()
    counters = get_counters()
    return (0, json.dumps({
        "enabled": enabled,
        "counters": counters,
        "total_events": sum(counters.values()),
    }, indent=2, sort_keys=True))


def _telemetry_on() -> Tuple[int, str]:
    """Enable telemetry."""
    set_enabled(True)
    return (0, json.dumps({"status": "enabled"}))


def _telemetry_off() -> Tuple[int, str]:
    """Disable telemetry."""
    set_enabled(False)
    return (0, json.dumps({"status": "disabled"}))


def _telemetry_export() -> Tuple[int, str]:
    """Export all weekly telemetry reports as JSON."""
    reports = list_weekly_reports()
    return (0, json.dumps(reports, indent=2, sort_keys=True))
