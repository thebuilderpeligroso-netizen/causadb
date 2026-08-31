"""Health metrics watchdog — tracks uptime, event count, and last event timestamp.

Uses LedgerIndex for event count (Article I: Ledger Monism compliance).
Last event timestamp is read from the ledger directly.
Uptime is tracked from HealthMetrics instantiation.
"""

import json
import os
import time
from typing import Optional

from causadb._ledger_index import LedgerIndex


class HealthMetrics:
    """Tracks health metrics derived from the causal ledger.

    Metrics:
        uptime_seconds: seconds since this instance was created.
        total_events: number of events in the ledger (via LedgerIndex).
        last_event_timestamp: ISO-8601 timestamp of the most recent event.
    """

    def __init__(
        self, ledger_path: str, index: Optional[LedgerIndex] = None
    ):
        self._ledger_path = ledger_path
        self._index = index or LedgerIndex(ledger_path)
        self._start_time = time.time()

    # -- public query methods (method-call style for easy monkeypatching) --

    def get_uptime_seconds(self) -> float:
        """Seconds elapsed since this HealthMetrics instance was created."""
        return time.time() - self._start_time

    def get_total_events(self) -> int:
        """Number of events in the ledger, read through LedgerIndex."""
        self._index._validate_cache()
        if not self._index.event_ids:
            self._index.rebuild()
        return len(self._index.event_ids)

    def get_last_event_timestamp(self) -> Optional[str]:
        """ISO-8601 timestamp of the most recent event, or None if empty."""
        if not os.path.exists(self._ledger_path) or os.path.getsize(self._ledger_path) == 0:
            return None
        # Read the last line of the ledger efficiently (seek from end)
        with open(self._ledger_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            pos = size - 1
            if pos >= 0:
                f.seek(pos)
                if f.read(1) == b'\n':
                    pos -= 1
            while pos > 0:
                f.seek(pos)
                if f.read(1) == b'\n':
                    break
                pos -= 1
            line_start = pos + 1 if pos > 0 else 0
            f.seek(line_start)
            line = f.readline().decode().strip()
            if line:
                try:
                    entry = json.loads(line)
                    return entry.get("event", {}).get("timestamp")
                except json.JSONDecodeError:
                    return None
        return None

    def to_dict(self) -> dict:
        """Return all metrics as a flat dict (without a ``status`` key)."""
        return {
            "uptime_seconds": round(self.get_uptime_seconds(), 1),
            "total_events": self.get_total_events(),
            "last_event_timestamp": self.get_last_event_timestamp(),
        }
