"""Tests for D.3 — Graceful shutdown via signal handlers.

Artículo III: Test-first. Artículo IX: Anti-teatro (real fork + real signal).

These tests verify that:
1. ``install_signal_handlers`` registers a SIGTERM handler that flushes a
   ``SYSTEM_BOOT`` shutdown event to the ledger before exiting (D.3).
2. A no-op signal handler (mutant) would NOT write the event — proving that
   the real handler has discriminatory power (not theatre).
"""

import json
import os
import signal
import time
from unittest.mock import patch

import pytest

from causadb._daemon_service import install_signal_handlers
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter


# ---------------------------------------------------------------------------
# D.3 — Real SIGTERM handler flushes the ledger
# ---------------------------------------------------------------------------


def test_sigterm_flushes_ledger(tmp_path):
    """Send SIGTERM to a child process that has signal handlers installed.
    Verify the ledger contains the shutdown event (last line, not truncated).

    The child:
    1. Installs signal handlers via ``install_signal_handlers(ledger_path)``.
    2. Writes a ``SYSTEM_BOOT`` event with ``{"action": "start"}``.
    3. Sleeps until SIGTERM arrives.

    The parent:
    1. Waits briefly for the child to write the start event.
    2. Sends SIGTERM.
    3. Waits for the child to exit (expected exit code 0).
    4. Reads the ledger and asserts the last entry is the shutdown event.

    Artículo IX: this test exercises real signal delivery (``os.kill``) and
    real ledger I/O via ``LedgerWriter.append``. A stub ``install_signal_handlers``
    that installs a no-op handler would make this test fail.
    """
    ledger_path = str(tmp_path / "ledger.log")

    pid = os.fork()
    if pid == 0:
        # ---- Child process ----
        install_signal_handlers(ledger_path)

        # Write a regular event to prove the ledger works
        writer = LedgerWriter(ledger_path)
        event = CanonicalEvent(
            event_type=EventType.SYSTEM_BOOT,
            ctx_id="test",
            source="pytest:test_sigterm_flushes_ledger",
            source_type="agent",
            payload={"action": "start"},
        )
        writer.append(event)

        # Wait indefinitely for SIGTERM
        while True:
            time.sleep(1)
    else:
        # ---- Parent process ----
        try:
            # Give child time to install handlers and write the start event
            time.sleep(1.0)

            # Send SIGTERM
            os.kill(pid, signal.SIGTERM)

            # Wait for child to exit (timeout after 5s)
            _, status = os.waitpid(pid, 0)
            assert os.WIFEXITED(status), (
                f"Child should exit cleanly after SIGTERM, got status {status}"
            )
            assert os.WEXITSTATUS(status) == 0, (
                f"Child should exit with code 0 after SIGTERM, "
                f"got {os.WEXITSTATUS(status)}"
            )

            # Verify the ledger has the shutdown event as the last entry
            assert os.path.exists(ledger_path), "Ledger file must exist"
            with open(ledger_path, "r") as f:
                lines = f.readlines()

            assert len(lines) >= 1, "Ledger must have at least one event"

            # The last line should be the shutdown event
            last_entry = json.loads(lines[-1])
            event_type = last_entry["event"]["event_type"]
            payload = last_entry["event"]["payload"]

            assert event_type == "SYSTEM_BOOT", (
                f"Last event type must be SYSTEM_BOOT (shutdown), got {event_type!r}"
            )
            assert payload == {"action": "shutdown"}, (
                f"Last event payload must be shutdown, got {payload!r}"
            )
        finally:
            # Ensure child is dead even if assertions fail
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass


# ---------------------------------------------------------------------------
# Anti-teatro — a no-op signal handler would NOT flush the ledger
# ---------------------------------------------------------------------------


def test_anti_teatro_sigterm_ignored(tmp_path):
    """Anti-teatro (Artículo IX): if the signal handler is a no-op (does
    nothing on SIGTERM), the shutdown event is NOT written.

    This test installs a mutated handler that simply calls ``os._exit(0)``
    without writing to the ledger. It then verifies that the last event in
    the ledger is the *start* event, NOT a shutdown event.

    If ``test_sigterm_flushes_ledger`` were run against this mutant handler,
    it would FAIL (the last event would not be ``{"action": "shutdown"}``).
    This proves that ``test_sigterm_flushes_ledger`` has real discriminatory
    power and is not theatre.
    """
    ledger_path = str(tmp_path / "ledger.log")

    pid = os.fork()
    if pid == 0:
        # ---- Child process with MUTATED handler ----
        # Mutant: no-op signal handler — just exits, does NOT write to ledger
        signal.signal(signal.SIGTERM, lambda sig, frame: os._exit(0))

        # Write a regular event
        writer = LedgerWriter(ledger_path)
        event = CanonicalEvent(
            event_type=EventType.SYSTEM_BOOT,
            ctx_id="test",
            source="pytest:test_anti_teatro_sigterm_ignored",
            source_type="agent",
            payload={"action": "start"},
        )
        writer.append(event)

        while True:
            time.sleep(1)
    else:
        # ---- Parent process ----
        try:
            time.sleep(1.0)

            # Send SIGTERM
            os.kill(pid, signal.SIGTERM)

            # Wait for child
            _, status = os.waitpid(pid, 0)
            assert os.WIFEXITED(status), (
                f"Child must exit cleanly, got status {status}"
            )
            assert os.WEXITSTATUS(status) == 0, (
                f"Child must exit with code 0, got {os.WEXITSTATUS(status)}"
            )

            # Verify the last event is NOT the shutdown event
            assert os.path.exists(ledger_path), "Ledger file must exist"
            with open(ledger_path, "r") as f:
                lines = f.readlines()

            assert len(lines) == 1, (
                f"With a no-op handler there should be exactly 1 event (start). "
                f"Got {len(lines)} events: "
                f"{[json.loads(l)['event']['event_type'] for l in lines]}"
            )

            last_entry = json.loads(lines[-1])
            payload = last_entry["event"]["payload"]

            assert payload != {"action": "shutdown"}, (
                "Mutant no-op handler should NOT have written a shutdown event. "
                "If this assertion fails, the mutant is not actually a no-op "
                "(it's writing events), and the anti-teatro test is invalid."
            )
            assert payload == {"action": "start"}, (
                f"Expected the start event payload, got {payload!r}"
            )
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass
