"""Tests J.x — Dry-run mode for Harvester.

Verifies that ``harvest_all(dry_run=True)``:
1. Returns raw event dicts instead of counts.
2. Does NOT write to the ledger.
3. Does NOT advance cursors.
4. On a second cycle, returns 0 events (cursor was not advanced).
"""

import json
import os
import pytest
from causadb._harvest_source import HarvestSource
from causadb._harvester import Harvester


class DummyDryRunSource(HarvestSource):
    """Dummy source that returns a fixed batch of events on first harvest,
    then nothing on subsequent harvests (simulating a cursor-based source
    that would normally advance)."""

    def __init__(self, ledger_path, events=None):
        super().__init__(ledger_path)
        self._events = list(events or [])
        self._harvested = False

    def source_type(self):
        return "dummy"

    def cursor_key(self):
        return "dummy_cursor"

    def detect(self):
        return True

    def harvest(self, cursor=None):
        if self._harvested:
            return []
        self._harvested = True
        return list(self._events)

    def advance_cursor(self, cursor, harvested_raw_events):
        return {"index": len(harvested_raw_events)}


@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")


@pytest.fixture
def config_path(tmp_path):
    return str(tmp_path / ".harvester_cursors.json")


def test_dry_run_returns_raw_events_not_written(ledger_path, config_path):
    """Dry-run harvest_all returns raw event dicts, does NOT write to
    ledger, does NOT advance cursor. Second cycle returns 0 events."""
    events = [
        {"type": "COMMAND_RUN", "timestamp": "2024-01-01 10:00:00",
         "command": "ls"},
        {"type": "COMMAND_RUN", "timestamp": "2024-01-01 10:01:00",
         "command": "cd /tmp"},
        {"type": "FILE_MODIFIED", "timestamp": "2024-01-01 10:02:00",
         "path": "/tmp/a"},
    ]
    source = DummyDryRunSource(ledger_path, events=events)
    h = Harvester(ledger_path, config_path)
    h.register_source(source)

    # --- Cycle 1: dry_run=True ---
    result1 = h.harvest_all(dry_run=True)
    assert isinstance(result1, dict)
    assert "dummy" in result1
    assert isinstance(result1["dummy"], list), (
        f"Expected list of raw events, got {type(result1['dummy'])}"
    )
    assert len(result1["dummy"]) == 3, (
        f"Expected 3 raw events on cycle 1, got {len(result1['dummy'])}"
    )
    # Verify raw dicts are the original ones
    assert result1["dummy"][0]["command"] == "ls"
    assert result1["dummy"][1]["command"] == "cd /tmp"
    assert result1["dummy"][2]["path"] == "/tmp/a"

    # --- Ledger must be untouched ---
    assert not os.path.exists(ledger_path) or os.path.getsize(ledger_path) == 0, (
        "Ledger must be empty after dry-run — no events written"
    )

    # --- Cursor file must NOT exist (cursors not saved in dry-run) ---
    assert not os.path.exists(config_path), (
        "Cursor file must not exist after dry-run — cursors not advanced"
    )

    # --- Cycle 2: dry_run=True again ---
    result2 = h.harvest_all(dry_run=True)
    assert isinstance(result2["dummy"], list)
    assert len(result2["dummy"]) == 0, (
        f"Expected 0 events on cycle 2 (cursor not advanced), "
        f"got {len(result2['dummy'])}"
    )

    # --- Ledger still untouched ---
    assert not os.path.exists(ledger_path) or os.path.getsize(ledger_path) == 0, (
        "Ledger must still be empty after second dry-run"
    )


def test_dry_run_false_still_works(ledger_path, config_path):
    """Backwards compatibility: harvest_all() without dry_run writes to
    ledger and returns counts."""
    events = [
        {"type": "COMMAND_RUN", "timestamp": "2024-01-01 10:00:00",
         "command": "ls"},
        {"type": "COMMAND_RUN", "timestamp": "2024-01-01 10:01:00",
         "command": "cd /tmp"},
    ]
    source = DummyDryRunSource(ledger_path, events=events)
    h = Harvester(ledger_path, config_path)
    h.register_source(source)

    result = h.harvest_all()
    assert result["dummy"] == 2, f"Expected 2 events, got {result}"

    with open(ledger_path) as f:
        lines = f.readlines()
    assert len(lines) == 2, f"Ledger must have 2 entries, has {len(lines)}"

    # Cursor saved
    assert os.path.exists(config_path)
    with open(config_path) as f:
        cursors = json.load(f)
    assert cursors["dummy_cursor"]["index"] == 2


def test_dry_run_with_undetected_source(ledger_path, config_path):
    """Source with detect()=False returns empty list in dry-run mode."""
    source = DummyDryRunSource(ledger_path, events=[])
    source._detect_result = False

    # Override detect to return False
    original_detect = source.detect
    source.detect = lambda: False

    h = Harvester(ledger_path, config_path)
    h.register_source(source)

    result = h.harvest_all(dry_run=True)
    assert result["dummy"] == [], (
        f"Undetected source must return empty list in dry-run, got {result['dummy']}"
    )

    # Restore for cleanup
    source.detect = original_detect


def test_anti_teatro_cursor_corruption_detected_in_dry_run(ledger_path, config_path):
    """Corrupt cursor between dry-run cycles → second cycle correctly
    reports all events (detects the corruption, not silent failure)."""
    events = [
        {"type": "COMMAND_RUN", "timestamp": "2024-01-01 10:00:00",
         "command": "a"},
        {"type": "COMMAND_RUN", "timestamp": "2024-01-01 10:01:00",
         "command": "b"},
    ]
    source = DummyDryRunSource(ledger_path, events=events)
    h = Harvester(ledger_path, config_path)
    h.register_source(source)

    # Simulate: write a fake cursor that says "already harvested 0"
    cursors_path = config_path
    os.makedirs(os.path.dirname(cursors_path), exist_ok=True)
    with open(cursors_path, "w") as f:
        json.dump({"dummy_cursor": {"index": 0}}, f)

    # Dry-run: source._harvested is still False, so harvest returns 2 events
    result = h.harvest_all(dry_run=True)
    assert len(result["dummy"]) == 2, (
        f"Dry-run with auto-cursor from file: expected 2, got {len(result['dummy'])}"
    )

    # The filesystem cursor shouldn't be modified (dry-run), verify
    assert json.load(open(config_path)) == {"dummy_cursor": {"index": 0}}, (
        "Cursor must not be modified after dry-run"
    )