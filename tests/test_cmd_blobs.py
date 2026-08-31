"""Tests for `causadb blobs gc` CLI subcommand (FIX.3 — LOOP_ENGINEERING).

Test-First discipline (Artículo III): these tests were written BEFORE the
implementation. They exercise the CLI as a thin delegator to ``BlobGC`` —
no logic is reimplemented here.

Anti-teatro (Artículo IX, regla #7): every test has discriminatory power.
A stub CLI that returns empty dicts or skips the ``by_class`` desglose will
fail at least one assertion in this file.

Cobertura:
1. Dry-run default (no mutation, ``by_class`` con 4 clases).
2. ``--execute`` mueve huérfanos a ``.trash/``.
3. Output contiene ``by_class`` con conteos coherentes.
4. ``--execute`` rechaza ledger corrupto; dry-run lo permite.
5. Blob harvest (formato VIEJO 4 claves) → ``harvest``, no ``orphan``.
6. Blob harvest (formato NUEVO 6 claves post-FIX.5) → ``harvest``, no ``orphan``.
"""
import json
import os
from types import MappingProxyType

import pytest

from causadb._blob_store import BlobStore
from causadb._config import CausaDBConfig
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._gc_blobs import BlobGC
from causadb._init import causadb_init
from causadb._ledger_writer import LedgerWriter
from causadb.cli.main import main


def _setup_ledger(tmp_path, events=None, orphan_blobs=None, corrupt_ledger=False):
    """Setup helper: init workspace, write events, add orphan blobs."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    config = CausaDBConfig(ledger_path=ledger, blob_store_enabled=True)
    writer = LedgerWriter(ledger, config=config)
    blob_dir = os.path.join(os.path.dirname(ledger), "blobs")
    blob_store = BlobStore(blob_dir)

    if orphan_blobs:
        for bd in orphan_blobs:
            blob_store.put(bd)

    if events:
        for evt in events:
            writer.append(evt)

    if corrupt_ledger:
        with open(ledger, "w") as f:
            f.write("NOT_JSON\n")

    return ledger, blob_dir, blob_store


def _run(args, capsys):
    """Run the CLI with the given args list, return (exit_code, stdout_str)."""
    rc = main(args=args)
    captured = capsys.readouterr()
    return rc, captured.out


# ---------------------------------------------------------------------------
# Test 1 — Dry-run default: no mutation, by_class with 4 classes
# ---------------------------------------------------------------------------

def test_cli_blobs_gc_dry_run_default(tmp_path, capsys):
    """`causadb blobs gc --ledger <path>` (no --execute) → dry-run, no .trash/."""
    ledger, blob_dir, _ = _setup_ledger(
        tmp_path,
        events=[CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION, ctx_id="t",
            source="s", payload=MappingProxyType({"inline": True}))],
        orphan_blobs=[{"orphan": True}],
    )
    trash = os.path.join(blob_dir, ".trash")

    rc, out = _run(["blobs", "gc", "--ledger", ledger], capsys)

    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    assert not os.path.exists(trash), ".trash/ must NOT be created on dry-run"
    payload = json.loads(out)
    # Output must mention all 4 classes
    by_class = payload.get("by_class", {})
    for cls in ("payload", "snapshot", "harvest", "orphan"):
        assert cls in by_class, f"by_class missing '{cls}': {by_class!r}"


# ---------------------------------------------------------------------------
# Test 2 — --execute moves orphans to .trash/
# ---------------------------------------------------------------------------

def test_cli_blobs_gc_execute_moves_orphans(tmp_path, capsys):
    """`causadb blobs gc --execute` → moves orphans to .trash/, rc==0."""
    ledger, blob_dir, _ = _setup_ledger(
        tmp_path,
        events=[CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION, ctx_id="t",
            source="s", payload=MappingProxyType({"inline": True}))],
        orphan_blobs=[{"orphan": True}],
    )
    trash = os.path.join(blob_dir, ".trash")

    rc, out = _run(["blobs", "gc", "--ledger", ledger, "--execute"], capsys)

    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    payload = json.loads(out)
    assert payload.get("executed") is True, "executed must be True with --execute"
    moved = payload.get("moved_count", 0)
    assert moved > 0, f"expected moved_count > 0, got {moved}"
    assert os.path.exists(trash), ".trash/ must be created on --execute"


# ---------------------------------------------------------------------------
# Test 3 — by_class output with coherent counts
# ---------------------------------------------------------------------------

def test_cli_blobs_gc_by_class_output(tmp_path, capsys):
    """Output JSON contains by_class with 4 keys; counts sum to total_blobs."""
    ledger, _, _ = _setup_ledger(
        tmp_path,
        events=[CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION, ctx_id="t",
            source="s", payload=MappingProxyType({"inline": True}))],
        orphan_blobs=[{"a": 1}, {"b": 2}],
    )

    rc, out = _run(["blobs", "gc", "--ledger", ledger], capsys)

    assert rc == 0
    payload = json.loads(out)
    by_class = payload.get("by_class", {})
    assert set(by_class.keys()) == {"payload", "snapshot", "harvest", "orphan"}, \
        f"by_class must have exactly 4 keys, got: {by_class!r}"
    total = payload.get("total_blobs", 0)
    summed = sum(by_class.values())
    assert summed == total, \
        f"sum(by_class)={summed} must equal total_blobs={total}"


# ---------------------------------------------------------------------------
# Test 4 — --execute refuses corrupt ledger; dry-run works
# ---------------------------------------------------------------------------

def test_cli_blobs_gc_refuses_execute_on_corrupt_ledger(tmp_path, capsys):
    """Corrupt ledger + --execute → rc != 0 with error; dry-run → rc==0."""
    ledger, _, _ = _setup_ledger(
        tmp_path, events=[], orphan_blobs=[{"orphan": True}],
        corrupt_ledger=True,
    )

    # --execute on corrupt ledger must fail
    rc_exec, out_exec = _run(
        ["blobs", "gc", "--ledger", ledger, "--execute"], capsys)
    assert rc_exec != 0, \
        f"expected rc != 0 on corrupt ledger + --execute, got {rc_exec}"
    payload_exec = json.loads(out_exec)
    assert "error" in payload_exec, \
        f"expected 'error' key in output, got: {payload_exec!r}"

    # dry-run on same corrupt ledger must succeed
    rc_dry, out_dry = _run(["blobs", "gc", "--ledger", ledger], capsys)
    assert rc_dry == 0, \
        f"expected rc == 0 on corrupt ledger + dry-run, got {rc_dry}"


# ---------------------------------------------------------------------------
# Test 5 — Harvest blob (OLD format: 4 keys) excluded from orphan
# ---------------------------------------------------------------------------

def test_cli_blobs_gc_harvest_excluded_from_orphan(tmp_path, capsys):
    """Harvest blob (OLD format: path/content/size/mtime) → harvest, not orphan."""
    blob = {"path": "/a/b", "content": "xx", "size": 2, "mtime": 1}
    ledger, _, _ = _setup_ledger(
        tmp_path,
        events=[CanonicalEvent(
            event_type=EventType.FILE_MODIFIED, ctx_id="t",
            source="harvester:filesystem", payload=MappingProxyType(blob))],
        orphan_blobs=[blob],
    )

    rc, out = _run(["blobs", "gc", "--ledger", ledger], capsys)

    assert rc == 0
    payload = json.loads(out)
    by_class = payload.get("by_class", {})
    assert by_class.get("harvest", 0) >= 1, \
        f"expected harvest >= 1, got by_class={by_class!r}"
    assert by_class.get("orphan", 0) == 0, \
        f"expected orphan == 0, got by_class={by_class!r}"


# ---------------------------------------------------------------------------
# Test 6 — Harvest blob (NEW format post-FIX.5: 6 keys) excluded from orphan
# ---------------------------------------------------------------------------

def test_cli_blobs_gc_harvest_post_fix5_excluded(tmp_path, capsys):
    """Harvest blob (NEW format: 6 keys post-FIX.5) → harvest, not orphan.

    This test MUST FAIL in Red because ``_HARVEST_BLOB_KEYS`` uses exact
    match of 4 keys, rejecting the 6-key format.
    """
    blob = {
        "path": "/a/b", "content": "xx", "size": 2, "mtime": 1,
        "action": "modified", "content_hash": "abc123",
    }
    ledger, _, _ = _setup_ledger(
        tmp_path,
        events=[CanonicalEvent(
            event_type=EventType.FILE_MODIFIED, ctx_id="t",
            source="harvester:filesystem", payload=MappingProxyType(blob))],
        orphan_blobs=[blob],
    )

    rc, out = _run(["blobs", "gc", "--ledger", ledger], capsys)

    assert rc == 0
    payload = json.loads(out)
    by_class = payload.get("by_class", {})
    assert by_class.get("harvest", 0) >= 1, \
        f"expected harvest >= 1 (6-key format), got by_class={by_class!r}"
    assert by_class.get("orphan", 0) == 0, \
        f"expected orphan == 0 (6-key format), got by_class={by_class!r}"


# ---------------------------------------------------------------------------
# Test 7 — `causadb blobs` (sin sub-subcomando) → rc=2, error limpio, no crash
# ---------------------------------------------------------------------------

def test_cli_blobs_without_subcommand_returns_usage_error(capsys):
    """`causadb blobs` (sin sub-subcomando) → rc=2 con mensaje, no AttributeError.

    Bug detectado en Checker: sin ``set_defaults(func=cmd_blobs)`` en el parser
    padre, ``main.py`` crasheaba con ``AttributeError: 'Namespace' object has
    no attribute 'func'`` antes de llegar al handler.
    """
    rc, out = _run(["blobs"], capsys)

    assert rc == 2
    assert "Unknown blobs action" in out
