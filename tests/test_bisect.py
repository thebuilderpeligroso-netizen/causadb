"""Tests for F.12.5 — `causadb bisect --test cmd` (_bisect.py).

Artículo III: Test-first. Tests written BEFORE implementation.
Artículo IX: Anti-teatro — every test has discriminatory power.

The 6 tests below mirror the spec in
``CAUSADB_ROADMAP_FASE12_PLAN.md`` (F.12.5 section, lines 392-397).
A 7th test ``test_bisect_raises_on_no_snapshot`` is ADDED EXPLICITLY
to enforce Fall-Closed (Artículo V): an event lacking ``post_snapshot``
must raise ``BisectError`` rather than be silently skipped.

Strategy: build a synthetic ledger with N events, each carrying a real
``post_snapshot`` hash (stored in a real BlobStore) representing the
workspace state AFTER that event. The bisect algorithm restores each
candidate event's snapshot to a workspace dir, runs the test command
via ``subprocess.run``, and binary-searches for the first event whose
state fails the test.
"""

import json
import os
import shutil
import subprocess
import sys
from types import MappingProxyType

import pytest

from causadb._blob_store import BlobStore
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._config import CausaDBConfig


# ---------------------------------------------------------------------------
# Helpers — build a synthetic ledger with real snapshots
# ---------------------------------------------------------------------------

def _make_workspace(tmp_path, name="workspace"):
    """Create a clean workspace dir + a BlobStore rooted at tmp_path/blobs."""
    ws = str(tmp_path / name)
    os.makedirs(ws, exist_ok=True)
    store = BlobStore(base_path=str(tmp_path / "blobs"))
    return ws, store


def _write_file(path, content):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _take_and_store_snapshot(ws, store):
    """Take a snapshot of *ws* and store it (with content) in *store*.
    Returns the snapshot blob hash."""
    from causadb._snapshot import WorkspaceSnapshot
    snap = WorkspaceSnapshot.take(ws)
    return WorkspaceSnapshot.store(snap, store, root_dir=ws)


def _append_event(writer, post_snapshot_hash, event_id=None, source="agent:test",
                  payload_action="edit"):
    """Append a FILE_MODIFIED event carrying *post_snapshot_hash* to the ledger."""
    event = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="bisect-test",
        source=source,
        source_type="agent",
        payload=MappingProxyType({
            "action": payload_action,
            "post_snapshot": post_snapshot_hash,
        }),
        post_snapshot=post_snapshot_hash,
    )
    if event_id is not None:
        object.__setattr__(event, "event_id", event_id)
    writer.append(event)
    return event.event_id


def _build_ledger_with_states(tmp_path, states, breaking_from_index=None,
                              sentinel_file="sentinel.txt"):
    """Build a ledger of N events, one per state in *states*.

    Each *state* is a dict {filename: content} describing the workspace
    AFTER that event. We materialise the state on disk, snapshot it, then
    append an event carrying that snapshot. The test command checks for
    the presence of a sentinel file in the workspace — events at index
    >= *breaking_from_index* do NOT write the sentinel (so the test fails
    for those states).

    Returns (ledger_path, workspace_dir, blob_store, event_ids, test_cmd).
    """
    ws, store = _make_workspace(tmp_path)
    ledger = str(tmp_path / "ledger.log")

    config = CausaDBConfig(
        ledger_path=ledger,
        blob_store_enabled=True,
        blob_store_path=str(tmp_path / "blobs"),
    )

    writer = LedgerWriter(ledger, config=config)

    event_ids = []
    for i, state in enumerate(states):
        # Materialise the state on disk.
        for fname in os.listdir(ws):
            full = os.path.join(ws, fname)
            if os.path.isfile(full):
                os.remove(full)
        for fname, content in state.items():
            _write_file(os.path.join(ws, fname), content)

        # If this state is "good" (test passes), include the sentinel.
        if breaking_from_index is None or i < breaking_from_index:
            _write_file(os.path.join(ws, sentinel_file), "ok\n")
        else:
            # Ensure sentinel is absent for "bad" states.
            sentinel_path = os.path.join(ws, sentinel_file)
            if os.path.exists(sentinel_path):
                os.remove(sentinel_path)

        snap_hash = _take_and_store_snapshot(ws, store)
        eid = _append_event(writer, snap_hash)
        event_ids.append(eid)

    # Test command: exit 0 iff sentinel exists in CWD (we'll run with cwd=ws).
    # NOTE: sys.executable may contain spaces (e.g. /home/user/Recupero Linux/...)
    # so we single-quote it for the shell. The sentinel filename is a fixed
    # safe identifier (no spaces).
    py = sys.executable
    test_cmd = (
        f"'{py}' -c "
        f"\"import os, sys; sys.exit(0 if os.path.exists('{sentinel_file}') else 1)\""
    )

    return ledger, ws, store, event_ids, test_cmd


def _run_cli(args, capsys):
    from causadb.cli.main import main
    rc = main(args=args)
    captured = capsys.readouterr()
    return rc, captured.out


# ---------------------------------------------------------------------------
# Test 1 — bisect finds the first bad event
# ---------------------------------------------------------------------------

def test_bisect_finds_first_bad_event(tmp_path):
    """5 events, event 3 (index 2) breaks the test → bisect returns event 3."""
    states = [
        {"a.py": "v1\n"},
        {"a.py": "v2\n"},
        {"a.py": "v3\n"},
        {"a.py": "v4\n"},
        {"a.py": "v5\n"},
    ]
    ledger, ws, store, event_ids, test_cmd = _build_ledger_with_states(
        tmp_path, states, breaking_from_index=2,
    )

    from causadb._bisect import bisect

    result = bisect(test_cmd, ledger, ws)

    assert result is not None, "bisect must return a result when a bad event exists"
    assert result["event_id"] == event_ids[2], (
        f"expected first bad event to be event index 2 ({event_ids[2]}), "
        f"got {result['event_id']}"
    )


# ---------------------------------------------------------------------------
# Test 2 — all pass → returns None
# ---------------------------------------------------------------------------

def test_bisect_all_pass_returns_none(tmp_path):
    """All events pass the test → bisect returns None."""
    states = [
        {"a.py": "v1\n"},
        {"a.py": "v2\n"},
        {"a.py": "v3\n"},
    ]
    # breaking_from_index=None → all states get the sentinel → all pass.
    ledger, ws, store, event_ids, test_cmd = _build_ledger_with_states(
        tmp_path, states, breaking_from_index=None,
    )

    from causadb._bisect import bisect

    result = bisect(test_cmd, ledger, ws)
    assert result is None, f"expected None when all pass, got {result}"


# ---------------------------------------------------------------------------
# Test 3 — all fail → returns the first event
# ---------------------------------------------------------------------------

def test_bisect_all_fail_returns_first(tmp_path):
    """All events fail the test → bisect returns the first event."""
    states = [
        {"a.py": "v1\n"},
        {"a.py": "v2\n"},
        {"a.py": "v3\n"},
    ]
    # breaking_from_index=0 → no state gets the sentinel → all fail.
    ledger, ws, store, event_ids, test_cmd = _build_ledger_with_states(
        tmp_path, states, breaking_from_index=0,
    )

    from causadb._bisect import bisect

    result = bisect(test_cmd, ledger, ws)
    assert result is not None, "expected a result when all fail"
    assert result["event_id"] == event_ids[0], (
        f"expected first event ({event_ids[0]}) when all fail, "
        f"got {result['event_id']}"
    )


# ---------------------------------------------------------------------------
# Test 4 — bisect restores workspace after (no intermediate state left)
# ---------------------------------------------------------------------------

def test_bisect_restores_workspace_after(tmp_path):
    """After bisect, the workspace must reflect the "first bad" event's
    state — NOT some intermediate state from the binary search.

    We verify by checking that the workspace content matches the snapshot
    of the returned event exactly (via a sentinel file that only exists
    in the "bad" state).
    """
    states = [
        {"a.py": "v1\n"},
        {"a.py": "v2\n"},
        {"a.py": "v3\n"},
        {"a.py": "v4\n"},
        {"a.py": "v5\n"},
    ]
    ledger, ws, store, event_ids, test_cmd = _build_ledger_with_states(
        tmp_path, states, breaking_from_index=2,
    )

    from causadb._bisect import bisect
    from causadb._snapshot import WorkspaceSnapshot

    result = bisect(test_cmd, ledger, ws)
    assert result is not None
    bad_event_id = result["event_id"]
    assert bad_event_id == event_ids[2]

    # The workspace must now be in the state of the "first bad" event.
    # That state (index 2) had a.py = "v3\n" and NO sentinel.txt.
    a_py = os.path.join(ws, "a.py")
    assert os.path.exists(a_py), "a.py must exist after restore"
    with open(a_py) as f:
        content = f.read()
    assert content == "v3\n", (
        f"workspace must be restored to event 3 state (a.py='v3\\n'), "
        f"got {content!r}"
    )

    # Sentinel must NOT exist (event 3 is "bad" → no sentinel in its snapshot).
    sentinel = os.path.join(ws, "sentinel.txt")
    assert not os.path.exists(sentinel), (
        "sentinel.txt must NOT exist after restore — event 3 is the bad state"
    )

    # Cross-check: restore the bad event's snapshot directly and compare.
    bad_snap_hash = result["post_snapshot"]
    # Restore to a SEPARATE dir to compare.
    verify_dir = str(tmp_path / "verify")
    os.makedirs(verify_dir, exist_ok=True)
    # Clean verify dir.
    for fname in os.listdir(verify_dir):
        full = os.path.join(verify_dir, fname)
        if os.path.isfile(full):
            os.remove(full)
    WorkspaceSnapshot.restore(bad_snap_hash, store, verify_dir)
    # Compare file sets (excluding sentinel which is absent in both).
    ws_files = sorted(
        f for f in os.listdir(ws)
        if os.path.isfile(os.path.join(ws, f))
    )
    verify_files = sorted(
        f for f in os.listdir(verify_dir)
        if os.path.isfile(os.path.join(verify_dir, f))
    )
    assert ws_files == verify_files, (
        f"workspace files {ws_files} != restored snapshot files {verify_files}"
    )
    for fname in ws_files:
        with open(os.path.join(ws, fname)) as f:
            ws_content = f.read()
        with open(os.path.join(verify_dir, fname)) as f:
            verify_content = f.read()
        assert ws_content == verify_content, (
            f"file {fname} differs: ws={ws_content!r} verify={verify_content!r}"
        )


# ---------------------------------------------------------------------------
# Test 5 — bisect runs a real command via subprocess
# ---------------------------------------------------------------------------

def test_bisect_runs_real_command(tmp_path):
    """`bisect --test "python -c 'print(1)'"` actually executes via subprocess.

    We use a test command that writes a marker file to prove it ran, and
    that always passes (exit 0) so bisect returns None but the marker
    proves the command was invoked.
    """
    states = [
        {"a.py": "v1\n"},
        {"a.py": "v2\n"},
    ]
    ledger, ws, store, event_ids, _ = _build_ledger_with_states(
        tmp_path, states, breaking_from_index=None,
    )

    marker = str(tmp_path / "ran.marker")
    # Command writes the marker file then exits 0.
    # Quote both the python path (may contain spaces) and the marker path.
    py = sys.executable
    test_cmd = (
        f"'{py}' -c "
        f"\"open('{marker}', 'w').write('ran'); exit(0)\""
    )

    from causadb._bisect import bisect

    result = bisect(test_cmd, ledger, ws)
    assert result is None, "all events pass → None"

    # The marker file proves the command actually ran via subprocess.
    assert os.path.exists(marker), (
        "test command must have actually executed via subprocess — "
        "marker file is missing"
    )
    with open(marker) as f:
        assert f.read() == "ran"


# ---------------------------------------------------------------------------
# Test 6 — anti-teatro: mutate bisect to skip final restore → test 4 breaks
# ---------------------------------------------------------------------------

def test_anti_teatro_bisect_skips_restore(tmp_path):
    """Mutate `bisect` to skip the final restore step → the restore
    contract must break. Then RESTORE the original implementation.

    Anti-teatro (Artículo IX): we prove the final restore step is real
    by counting `WorkspaceSnapshot.restore` calls. The real impl makes
    exactly 1 MORE restore call than a mutated impl that skips the final
    restore (N loop restores + 1 final restore vs. N loop restores).
    This is deterministic and proves the restore step is exercised —
    a stub that omits it would produce the same call count as the
    mutated version, failing this assertion.

    We ALSO verify the workspace-state contract directly: after the real
    bisect, the workspace matches the first-bad event's snapshot exactly.
    """
    from causadb._snapshot import WorkspaceSnapshot

    states = [
        {"a.py": "v1\n"},
        {"a.py": "v2\n"},
        {"a.py": "v3\n"},
        {"a.py": "v4\n"},
        {"a.py": "v5\n"},
    ]
    ledger, ws, store, event_ids, test_cmd = _build_ledger_with_states(
        tmp_path, states, breaking_from_index=2,
    )

    import causadb._bisect as bisect_mod
    original_bisect = bisect_mod.bisect
    original_restore = WorkspaceSnapshot.restore

    # --- Count restore calls for the REAL implementation ---
    real_calls = [0]

    def counting_real(snapshot_hash, blob_store, target_dir):
        real_calls[0] += 1
        return original_restore(snapshot_hash, blob_store, target_dir)

    WorkspaceSnapshot.restore = staticmethod(counting_real)
    try:
        real_result = original_bisect(test_cmd, ledger, ws)
    finally:
        WorkspaceSnapshot.restore = staticmethod(original_restore)

    assert real_result is not None
    assert real_result["event_id"] == event_ids[2], (
        f"real bisect must return event index 2, got {real_result['event_id']}"
    )
    real_call_count = real_calls[0]
    assert real_call_count >= 1, "real bisect must call restore at least once"

    # --- MUTATION: re-implement bisect identically EXCEPT it skips the
    #     final restore after the loop. ---
    def _bisect_skip_restore(test_cmd, ledger_path, watch_dir):
        from causadb._ledger_reader import LedgerReader
        from causadb._blob_store import BlobStore
        from causadb._config import CausaDBConfig
        from causadb._snapshot import WorkspaceSnapshot as _WS
        import subprocess

        config = CausaDBConfig(ledger_path=ledger_path)
        store = BlobStore(config.blob_store_path)
        events = list(LedgerReader(ledger_path).read_all())
        snap_events = []
        for ev in events:
            if ev.post_snapshot is None:
                raise bisect_mod.BisectError("event has no snapshot")
            snap_events.append(ev)
        if not snap_events:
            return None
        lo, hi = 0, len(snap_events) - 1
        first_bad = None
        while lo <= hi:
            mid = (lo + hi) // 2
            ev = snap_events[mid]
            _WS.restore(ev.post_snapshot, store, watch_dir)
            proc = subprocess.run(test_cmd, shell=True, cwd=watch_dir)
            if proc.returncode == 0:
                lo = mid + 1
            else:
                first_bad = ev
                hi = mid - 1
        # --- MUTATION: skip the final restore here ---
        if first_bad is None:
            return None
        payload = dict(first_bad.payload) if first_bad.payload else {}
        return {
            "event_id": first_bad.event_id,
            "post_snapshot": first_bad.post_snapshot,
            "prompt": payload.get("prompt"),
            "reasoning": payload.get("reasoning"),
            "agent": first_bad.source,
        }

    bisect_mod.bisect = _bisect_skip_restore
    try:
        # Count restore calls for the MUTATED implementation.
        mut_calls = [0]

        def counting_mut(snapshot_hash, blob_store, target_dir):
            mut_calls[0] += 1
            return original_restore(snapshot_hash, blob_store, target_dir)

        WorkspaceSnapshot.restore = staticmethod(counting_mut)
        try:
            mutated_result = _bisect_skip_restore(test_cmd, ledger, ws)
        finally:
            WorkspaceSnapshot.restore = staticmethod(original_restore)

        # The mutated impl must still find the same bad event.
        assert mutated_result is not None, "mutated bisect must still find the bad event"
        assert mutated_result["event_id"] == event_ids[2], (
            "mutated bisect must still return event 3 as first bad"
        )

        # ANTI-TEATRO CORE: the real impl makes exactly 1 MORE restore call
        # than the mutated (skip-final-restore) version. This proves the
        # real impl has a final restore step that the mutation lacks.
        mut_call_count = mut_calls[0]
        assert real_call_count == mut_call_count + 1, (
            f"real bisect must make exactly 1 MORE restore call than the "
            f"mutated (skip-restore) version: real={real_call_count}, "
            f"mut={mut_call_count}. If equal, the final restore is theatre."
        )

        # The mutation is real (function identity changed).
        assert bisect_mod.bisect is not original_bisect, (
            "mutation must replace bisect with a different implementation"
        )
    finally:
        # --- RESTORE ---
        bisect_mod.bisect = original_bisect

    # After restore, the real implementation works again and restores
    # the workspace to the first-bad event's state exactly.
    assert bisect_mod.bisect is original_bisect, "bisect must be restored"
    restored_result = original_bisect(test_cmd, ledger, ws)
    assert restored_result is not None
    assert restored_result["event_id"] == event_ids[2]
    a_py = os.path.join(ws, "a.py")
    with open(a_py) as f:
        restored_content = f.read()
    assert restored_content == "v3\n", (
        f"restored bisect must restore to event 3 state (a.py='v3\\n'), "
        f"got {restored_content!r}"
    )


# ---------------------------------------------------------------------------
# Test 7 (ADDED) — Fall-Closed: event with no snapshot raises BisectError
# ---------------------------------------------------------------------------

def test_bisect_raises_on_no_snapshot(tmp_path):
    """An event lacking ``post_snapshot`` must raise ``BisectError`` —
    Fall-Closed (Artículo V). No silent skip.

    This test is NOT in the plan's explicit list (lines 392-397) but is
    ADDED here to enforce the Fall-Closed contract stated in plan line
    382: "Si un evento no tiene snapshot, lanza BisectError explícito —
    no falla silenciosamente."
    """
    ws, store = _make_workspace(tmp_path)
    ledger = str(tmp_path / "ledger.log")

    config = CausaDBConfig(
        ledger_path=ledger,
        blob_store_enabled=True,
        blob_store_path=str(tmp_path / "blobs"),
    )
    writer = LedgerWriter(ledger, config=config)

    # Append an event with NO post_snapshot (and no `writes` so auto-snapshot
    # doesn't kick in).
    event = CanonicalEvent(
        event_type=EventType.REASONING_STEP,
        ctx_id="bisect-test",
        source="agent:test",
        source_type="agent",
        payload=MappingProxyType({"action": "think"}),
        # post_snapshot deliberately left as None.
    )
    writer.append(event)

    from causadb._bisect import bisect, BisectError

    py = sys.executable
    test_cmd = f"'{py}' -c \"exit(0)\""
    with pytest.raises(BisectError) as exc_info:
        bisect(test_cmd, ledger, ws)

    assert "snapshot" in str(exc_info.value).lower(), (
        f"BisectError message must mention 'snapshot', got: {exc_info.value}"
    )
