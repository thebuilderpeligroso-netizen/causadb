"""I.1 — Tests for multi-workspace isolation (_workspace_manager.py).

Artículo III: Test-first. Artículo IX: Anti-teatro — 5 tests that verify
workspace state does not leak across boundaries.
"""

import json
import os

import pytest

from causadb._workspace_manager import WorkspaceManager
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType
from causadb._ledger_index import LedgerIndex


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------


def test_workspace_create_isolated(tmp_path):
    """Create workspace B → ledger B is a different file from A.
    Writing to A does not affect B's content."""
    wm = WorkspaceManager(tmp_path)
    wm.create("A")
    wm.create("B")

    ledger_a = wm.ledger_path("A")
    ledger_b = wm.ledger_path("B")

    assert ledger_a != ledger_b, "Workspace A and B share the same ledger file"

    # Write an event to A
    writer_a = LedgerWriter(ledger_a)
    event = CanonicalEvent(
        event_type=EventType.SYSTEM_BOOT,
        ctx_id="test",
        source="test:workspace",
        source_type="human",
        metadata=EventMetadata(trace_id="test", session_id="test"),
    )
    writer_a.append(event)

    # A's ledger now has 2 lines (genesis + our event)
    with open(ledger_a) as f:
        lines_a = [l for l in f if l.strip()]
    assert len(lines_a) == 2, (
        f"A's ledger should have 2 entries, got {len(lines_a)}"
    )

    # B's ledger still has only 1 line (genesis from init)
    with open(ledger_b) as f:
        lines_b = [l for l in f if l.strip()]
    assert len(lines_b) == 1, (
        f"B's ledger should have 1 entry (genesis), got {len(lines_b)}"
    )


# ---------------------------------------------------------------------------
# Workspace list
# ---------------------------------------------------------------------------


def test_workspace_list(tmp_path):
    """Create 2 workspaces, list → returns both names (plus default)."""
    wm = WorkspaceManager(tmp_path)
    wm.create("alpha")
    wm.create("beta")

    workspaces = wm.list()

    assert "default" in workspaces, "default workspace missing from list"
    assert "alpha" in workspaces, "alpha missing from list"
    assert "beta" in workspaces, "beta missing from list"
    assert len(workspaces) == 3, (
        f"Expected 3 workspaces, got {len(workspaces)}: {workspaces}"
    )


# ---------------------------------------------------------------------------
# Workspace delete
# ---------------------------------------------------------------------------


def test_workspace_delete(tmp_path):
    """Create and delete a workspace → it no longer exists."""
    wm = WorkspaceManager(tmp_path)
    wm.create("todelete")
    assert wm.exists("todelete"), "Workspace should exist after create"

    wm.delete("todelete")
    assert not wm.exists("todelete"), "Workspace should not exist after delete"
    # Also verify it's not in the list
    assert "todelete" not in wm.list(), (
        "Deleted workspace still appears in list"
    )


# ---------------------------------------------------------------------------
# Workspace switch
# ---------------------------------------------------------------------------


def test_workspace_switch(tmp_path):
    """Create A and B, switch to B → current() returns B."""
    wm = WorkspaceManager(tmp_path)
    wm.create("A")
    wm.create("B")

    # Initially the first created workspace (default) is current.
    # After creating A and B, the last created one (B) should be current
    # because it auto-switches. Let's be explicit.
    wm.switch("B")
    assert wm.current() == "B", (
        f"Expected current='B', got {wm.current()}"
    )

    wm.switch("A")
    assert wm.current() == "A", (
        f"Expected current='A', got {wm.current()}"
    )


# ---------------------------------------------------------------------------
# Anti-teatro — causal leak across workspaces
# ---------------------------------------------------------------------------


def test_anti_teatro_workspace_leak(tmp_path):
    """Log an event in workspace A; B's ledger index does not change.

    Artículo IX anti-teatro: ensures LedgerIndex instances are properly
    isolated and no shared state leaks between workspaces.
    """
    wm = WorkspaceManager(tmp_path)
    wm.create("A")
    wm.create("B")

    ledger_a = wm.ledger_path("A")
    ledger_b = wm.ledger_path("B")

    # Build B's index before any mutation to A
    index_b = LedgerIndex(ledger_b)
    index_b.rebuild()
    b_index_path = index_b.index_path

    # Read B's index state as a baseline
    assert os.path.exists(b_index_path), (
        "B's index file should exist after rebuild"
    )
    with open(b_index_path) as f:
        before = json.load(f)

    # Log an event in workspace A
    writer_a = LedgerWriter(ledger_a)
    event = CanonicalEvent(
        event_type=EventType.SYSTEM_BOOT,
        ctx_id="test",
        source="test:workspace",
        source_type="human",
        metadata=EventMetadata(trace_id="test", session_id="test"),
    )
    writer_a.append(event)

    # B's index must be identical (no leak)
    with open(b_index_path) as f:
        after = json.load(f)

    assert before == after, (
        "B's ledger index changed after logging in workspace A — causal leak!\n"
        f"before: {before}\nafter:  {after}"
    )
