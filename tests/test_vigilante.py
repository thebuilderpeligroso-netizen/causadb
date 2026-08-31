"""Tests for Modo Vigilante (F.2.5) — filesystem watcher.

Test-First discipline (Article III): written BEFORE implementation.
Anti-teatro (Article IX): every test has discriminatory power.
"""
import json
import os
import threading
import time

import pytest

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._vigilante import VigilanteWatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ledger_events(ledger_path: str):
    """Read all events from a ledger file, return list of event dicts."""
    if not os.path.exists(ledger_path) or os.path.getsize(ledger_path) == 0:
        return []
    events = []
    with open(ledger_path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                entry = json.loads(line)
                events.append(entry["event"])
    return events


def _start_watcher(ledger_path, watch_dir, extra_excludes=None):
    """Start a VigilanteWatcher in a daemon thread; return (watcher, thread)."""
    stop_event = threading.Event()
    watcher = VigilanteWatcher(
        ledger_path=ledger_path,
        watch_dir=watch_dir,
        stop_event=stop_event,
        extra_excludes=extra_excludes or [],
    )
    t = threading.Thread(target=watcher.start, daemon=True)
    t.start()
    time.sleep(0.4)  # give watcher time to initialise
    return watcher, t, stop_event


def _stop_watcher(watcher, t, stop_event):
    """Stop a running watcher and join the thread."""
    stop_event.set()
    t.join(timeout=5)
    watcher.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestVigilanteDetection:
    """Tests 1-3: file creation / modification / deletion detection."""

    def test_vigilante_logs_file_created(self, tmp_path):
        """Create file in watch_dir → ledger has FILE_MODIFIED with action=created."""
        ledger = str(tmp_path / "ledger.log")
        watch_dir = str(tmp_path / "watch")
        os.makedirs(watch_dir)

        watcher, t, stop = _start_watcher(ledger, watch_dir)

        try:
            # Create a file
            test_file = os.path.join(watch_dir, "hello.txt")
            with open(test_file, "w") as f:
                f.write("world")
            time.sleep(0.6)

            events = _ledger_events(ledger)
            assert len(events) >= 1, "No events logged"
            # Find the event for our file
            matched = [e for e in events if e["payload"].get("path", "").endswith("hello.txt")]
            assert len(matched) >= 1, f"No event found for hello.txt in {events}"
            assert matched[0]["event_type"] == "FILE_MODIFIED"
            assert matched[0]["payload"]["action"] == "created"
            assert matched[0]["source"] == "causadb:vigilante"
        finally:
            _stop_watcher(watcher, t, stop)

    def test_vigilante_logs_file_modified(self, tmp_path):
        """Modify existing file → action=modified."""
        ledger = str(tmp_path / "ledger.log")
        watch_dir = str(tmp_path / "watch")
        os.makedirs(watch_dir)
        test_file = os.path.join(watch_dir, "edit.txt")
        with open(test_file, "w") as f:
            f.write("v1")

        watcher, t, stop = _start_watcher(ledger, watch_dir)

        try:
            time.sleep(0.3)
            # Modify the file
            with open(test_file, "w") as f:
                f.write("v2")
            time.sleep(0.6)

            events = _ledger_events(ledger)
            matched = [e for e in events if e["payload"].get("path", "").endswith("edit.txt")]
            assert len(matched) >= 1, f"No event found for edit.txt"
            # The latest event for the file should be 'modified'
            assert matched[-1]["payload"]["action"] == "modified", f"Expected modified, got {matched[-1]['payload']['action']}"
        finally:
            _stop_watcher(watcher, t, stop)

    def test_vigilante_logs_file_deleted(self, tmp_path):
        """Delete file → action=deleted."""
        ledger = str(tmp_path / "ledger.log")
        watch_dir = str(tmp_path / "watch")
        os.makedirs(watch_dir)
        test_file = os.path.join(watch_dir, "delete_me.txt")
        with open(test_file, "w") as f:
            f.write("bye")

        watcher, t, stop = _start_watcher(ledger, watch_dir)

        try:
            time.sleep(0.3)
            os.remove(test_file)
            time.sleep(0.6)

            events = _ledger_events(ledger)
            matched = [e for e in events if e["payload"].get("path", "").endswith("delete_me.txt")]
            assert len(matched) >= 1, f"No event found for delete_me.txt"
            assert matched[-1]["payload"]["action"] == "deleted", f"Expected deleted, got {matched[-1]['payload']['action']}"
        finally:
            _stop_watcher(watcher, t, stop)


class TestVigilanteExclusions:
    """Tests 4-5: gitignore and default exclusions."""

    def test_vigilante_respects_gitignore(self, tmp_path):
        """.gitignore with 'secret*' → secret.key not logged, normal.txt logged."""
        ledger = str(tmp_path / "ledger.log")
        watch_dir = str(tmp_path / "watch")
        os.makedirs(watch_dir)

        # Create .gitignore
        with open(os.path.join(watch_dir, ".gitignore"), "w") as f:
            f.write("secret*\n")

        watcher, t, stop = _start_watcher(ledger, watch_dir)

        try:
            time.sleep(0.3)
            # Create a normal file and a secret file
            with open(os.path.join(watch_dir, "normal.txt"), "w") as f:
                f.write("ok")
            with open(os.path.join(watch_dir, "secret.key"), "w") as f:
                f.write("shhh")
            time.sleep(0.6)

            events = _ledger_events(ledger)
            paths_logged = [e["payload"].get("path", "") for e in events]
            normal_logged = any(p.endswith("normal.txt") for p in paths_logged)
            secret_logged = any(p.endswith("secret.key") for p in paths_logged)
            assert normal_logged, "normal.txt should have been logged"
            assert not secret_logged, "secret.key should have been excluded by .gitignore"
        finally:
            _stop_watcher(watcher, t, stop)

    def test_vigilante_excludes_git_dir_by_default(self, tmp_path):
        """.git/config excluded, real_file.py is logged."""
        ledger = str(tmp_path / "ledger.log")
        watch_dir = str(tmp_path / "watch")
        os.makedirs(watch_dir)

        # Create .git dir (simulated)
        git_dir = os.path.join(watch_dir, ".git")
        os.makedirs(git_dir)
        with open(os.path.join(git_dir, "config"), "w") as f:
            f.write("[core]\n")

        watcher, t, stop = _start_watcher(ledger, watch_dir)

        try:
            time.sleep(0.3)
            with open(os.path.join(watch_dir, "real_file.py"), "w") as f:
                f.write("x = 1")
            time.sleep(0.6)

            events = _ledger_events(ledger)
            paths_logged = [e["payload"].get("path", "") for e in events]
            git_logged = any(".git" in p for p in paths_logged)
            real_logged = any(p.endswith("real_file.py") for p in paths_logged)
            assert not git_logged, ".git files should be excluded by default"
            assert real_logged, "real_file.py should have been logged"
        finally:
            _stop_watcher(watcher, t, stop)


class TestVigilanteCLI:
    """Test 6: CLI start/stop lifecycle."""

    def test_vigilante_cli_start_stop(self, tmp_path):
        """`causadb vigilante start` starts thread; `vigilante stop` stops it; events logged."""
        from causadb.cli._cmd_vigilante import cmd_vigilante

        ledger = str(tmp_path / "ledger.log")
        watch_dir = str(tmp_path / "watch")
        os.makedirs(watch_dir)

        # Simulate start
        fake_start = lambda: None
        fake_start.ledger = ledger
        fake_start.watch = watch_dir
        fake_start.action = "start"
        fake_start.foreground = False
        # Skip baseline snapshot: under heavy test suite load, the fork subprocess
        # in __init__ can block the watcher thread for 3-5s, exceeding our polling
        # window. The CLI lifecycle (start/stop) is what this test exercises;
        # snapshot behaviour is covered by its own tests. (deuda #14)
        fake_start.skip_baseline = True

        rc, out = cmd_vigilante(fake_start)
        assert rc == 0, f"start failed: {out}"

        time.sleep(0.3)

        # Create file while watcher is running
        test_file = os.path.join(watch_dir, "cli_test.txt")
        with open(test_file, "w") as f:
            f.write("data")

        # Wait actively for the event (polling, robust under load).
        deadline = time.time() + 5.0
        matched = []
        while time.time() < deadline:
            events = _ledger_events(ledger)
            matched = [e for e in events if e["payload"].get("path", "").endswith("cli_test.txt")]
            if matched:
                break
            time.sleep(0.1)

        # Stop
        fake_stop = lambda: None
        fake_stop.ledger = ledger
        fake_stop.action = "stop"

        rc2, out2 = cmd_vigilante(fake_stop)
        assert rc2 == 0, f"stop failed: {out2}"

        # Verify events logged
        assert len(matched) >= 1, f"No events for cli_test.txt: {events}"


class TestVigilanteSequence:
    """Test 7: sequence numbers are consecutive and ordered."""

    def test_vigilante_logs_sequence_increments(self, tmp_path):
        """3 files created → sequence_numbers are consecutive and ordered."""
        ledger = str(tmp_path / "ledger.log")
        watch_dir = str(tmp_path / "watch")
        os.makedirs(watch_dir)

        watcher, t, stop = _start_watcher(ledger, watch_dir)

        try:
            time.sleep(0.3)
            for i in range(3):
                with open(os.path.join(watch_dir, f"seq_{i}.txt"), "w") as f:
                    f.write(str(i))
                time.sleep(0.2)
            time.sleep(0.5)

            events = _ledger_events(ledger)
            seqs = [e["sequence_number"] for e in events if e["payload"].get("action") == "created"]
            assert len(seqs) >= 3, f"Expected >= 3 created events, got {len(seqs)}"
            # Must be strictly increasing (not necessarily starting at 0 if there were other events)
            for i in range(1, len(seqs)):
                assert seqs[i] == seqs[i - 1] + 1, \
                    f"Sequence numbers not consecutive: {seqs}"
        finally:
            _stop_watcher(watcher, t, stop)


class TestVigilanteAntiTeatro:
    """Test 8: anti-teatro — mutating LedgerWriter.append to no-op must fail."""

    def test_anti_teatro_vigilante_skips_append(self, tmp_path, mocker):
        """Mutate LedgerWriter.append to no-op → ledger is empty."""
        ledger = str(tmp_path / "ledger.log")
        watch_dir = str(tmp_path / "watch")
        os.makedirs(watch_dir)

        # Patch LedgerWriter.append to be a no-op
        original_append = LedgerWriter.append
        mocker.patch.object(LedgerWriter, "append", return_value=None)

        watcher, t, stop = _start_watcher(ledger, watch_dir)

        try:
            test_file = os.path.join(watch_dir, "anti_teatro.txt")
            with open(test_file, "w") as f:
                f.write("evade")
            time.sleep(0.6)

            events = _ledger_events(ledger)
            assert len(events) == 0, \
                f"With append no-op'd, ledger should be empty, got {len(events)} events"
        finally:
            _stop_watcher(watcher, t, stop)
