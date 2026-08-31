"""I.3 — Workspace isolation verification.

Tests that verify workspace hash chains are truly isolated.
Corrupting workspace A's hash chain does not affect workspace B.

Artículo III: Test-first.
Artículo IX: Anti-teatro — no stubs, no shared state leaks.
"""

import json
import os

import pytest

from causadb._workspace_manager import WorkspaceManager
from causadb._ledger_writer import LedgerWriter
from causadb._ledger_validator import LedgerValidator
from causadb._ledger_reader import LedgerReader
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType


# ---------------------------------------------------------------------------
# test_chain_independent
# ---------------------------------------------------------------------------


def test_chain_independent(tmp_path):
    """Workspaces A and B have independent hash chains.

    1. Create workspace A and B.
    2. Write events to both.
    3. Corrupt A's ledger by editing an intermediate hash.
    4. Validate A → fails (HASH_MISMATCH).
    5. Validate B → passes (hash chain intact).
    """
    wm = WorkspaceManager(tmp_path)
    wm.create("A")
    wm.create("B")

    ledger_a = wm.ledger_path("A")
    ledger_b = wm.ledger_path("B")

    writer_a = LedgerWriter(ledger_a)
    writer_b = LedgerWriter(ledger_b)

    base_event = CanonicalEvent(
        event_type=EventType.SYSTEM_BOOT,
        ctx_id="isolation-test",
        source="test:isolation",
        source_type="human",
        metadata=EventMetadata(trace_id="i3", session_id="i3"),
    )

    # Write 3 events to workspace A (genesis + 3 = 4 entries)
    for i in range(3):
        writer_a.append(base_event)

    # Write 3 events to workspace B
    for i in range(3):
        writer_b.append(base_event)

    # --- Corrupt workspace A's ledger ---
    # Read all lines, corrupt the hash of the 3rd line (index 2, 0-based)
    with open(ledger_a, "r") as f:
        lines_a = f.readlines()

    assert len(lines_a) >= 3, (
        f"Workspace A should have at least 3 entries, got {len(lines_a)}"
    )

    entry_2 = json.loads(lines_a[2].strip())
    original_hash = entry_2["hash"]
    entry_2["hash"] = original_hash + "CORRUPTED"
    lines_a[2] = json.dumps(entry_2, sort_keys=True) + "\n"

    with open(ledger_a, "w") as f:
        f.writelines(lines_a)

    # --- Verify workspace A fails validation ---
    validator_a = LedgerValidator(ledger_a)
    result_a = validator_a.validate_chain()
    assert not result_a.is_valid, (
        "Workspace A's chain should be invalid after corruption"
    )
    assert result_a.failure_type == "HASH_MISMATCH", (
        f"Expected HASH_MISMATCH, got {result_a.failure_type}"
    )

    # --- Verify workspace B passes validation ---
    validator_b = LedgerValidator(ledger_b)
    result_b = validator_b.validate_chain()
    assert result_b.is_valid, (
        "Workspace B's chain must remain valid after corrupting A"
    )


# ---------------------------------------------------------------------------
# test_anti_teatro_isolation_shared_state
# ---------------------------------------------------------------------------


def test_anti_teatro_isolation_shared_state(tmp_path):
    """Writing an event in workspace A with ctx_id does not leak to B.

    1. Create workspace A and B.
    2. Write an event in A with ctx_id="session-A".
    3. Query B by ctx_id="session-A" → empty (no leak).
    4. Mutant: if WorkspaceManager shared a LedgerIndex, B would see A's entries.
    """
    wm = WorkspaceManager(tmp_path)
    wm.create("A")
    wm.create("B")

    ledger_a = wm.ledger_path("A")
    ledger_b = wm.ledger_path("B")

    writer_a = LedgerWriter(ledger_a)

    # Write an event to A with a distinctive ctx_id
    event_a = CanonicalEvent(
        event_type=EventType.SYSTEM_BOOT,
        ctx_id="session-A",
        source="test:isolation",
        source_type="human",
        metadata=EventMetadata(trace_id="i3", session_id="i3"),
    )
    writer_a.append(event_a)

    # Also write a different event to A (to confirm A has content)
    event_a2 = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="other-session",
        source="test:isolation",
        source_type="human",
        metadata=EventMetadata(trace_id="i3", session_id="i3"),
    )
    writer_a.append(event_a2)

    # --- Query B: should see nothing with ctx_id="session-A" ---
    reader_b = LedgerReader(ledger_b)
    b_entries = list(reader_b.read_all_entries())

    # B only has the genesis event (SYSTEM_BOOT, ctx_id="genesis")
    for entry in b_entries:
        event_ctx = entry.get("event", {}).get("ctx_id", "")
        assert event_ctx != "session-A", (
            f"Workspace B leaked ctx_id 'session-A' from workspace A! "
            f"Found in entry: {json.dumps(entry, indent=2)}"
        )

    # B's entries with ctx_id="session-A" must be empty
    b_session_a = [
        e for e in b_entries
        if e.get("event", {}).get("ctx_id") == "session-A"
    ]
    assert len(b_session_a) == 0, (
        f"Workspace B should have 0 entries with ctx_id='session-A', "
        f"got {len(b_session_a)}"
    )

    # --- Verify A actually has the event (sanity check, not leak but presence) ---
    reader_a = LedgerReader(ledger_a)
    a_session_a = [
        e for e in reader_a.read_all_entries()
        if e.get("event", {}).get("ctx_id") == "session-A"
    ]
    assert len(a_session_a) >= 1, (
        "Workspace A should contain at least 1 entry with ctx_id='session-A' "
        "(sanity check)"
    )
