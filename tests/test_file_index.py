"""Tests for the persistent file index (`causadb._file_index`).

The index maps normalized file keys to the events whose POST snapshot
contains the file, so `attribute_line` (`causadb why`) is an O(1) lookup
plus resolving only the candidate events' snapshots — instead of a full
ledger read with blob resolution (~163K events, ~119MB, ~3s on the real
ledger). Closes BIT-CHR.115 residual debt item #1.

Artículo III: test-first. Artículo IX: anti-teatro — each test has
discriminatory power (build/persist, top-level-only gate, tail-extend,
rebuild on truncation/corruption/version-mismatch, never-touched gate).
"""
import json
import os
from types import MappingProxyType

from causadb._blob_store import BlobStore
from causadb._config import CausaDBConfig
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._file_index import (
    FILE_INDEX_SCHEMA_VERSION,
    _file_index_path_for,
    get_file_index,
)
from causadb._ledger_writer import LedgerWriter
from causadb._snapshot import WorkspaceSnapshot


# ---------------------------------------------------------------------------
# Helpers — build a workspace + ledger with pre/post snapshots
# ---------------------------------------------------------------------------

def _write_file(path, content):
    with open(path, "w") as f:
        f.write(content)


def _remove_if_exists(path):
    if os.path.exists(path):
        os.remove(path)


def _make_workspace(tmp_path, name="ws"):
    ws = str(tmp_path / name)
    os.makedirs(ws, exist_ok=True)
    ledger = str(tmp_path / "ledger.log")
    config = CausaDBConfig(
        ledger_path=ledger,
        blob_store_enabled=True,
        blob_store_path=str(tmp_path / "blobs"),
    )
    config.workspace_dir = ws
    os.makedirs(config.blob_store_path, exist_ok=True)
    store = BlobStore(config.blob_store_path)
    return ws, store, config, ledger


def _log_event(
    writer, store, ws, rel_path,
    pre_present: bool, pre_content: str,
    post_present: bool, post_content: str,
    source="opencode:agent1",
    source_type="agent",
    prompt=None, reasoning=None,
    parent_event_id=None,
    event_type=EventType.FILE_MODIFIED,
    ctx_id="ctx",
):
    """Append an event with explicit pre/post snapshots (mirrors the
    `test_causal_attrib` helper). Returns the CanonicalEvent."""
    target = os.path.join(ws, rel_path)

    if pre_present:
        _write_file(target, pre_content)
    else:
        _remove_if_exists(target)
    pre_snap = WorkspaceSnapshot.take(ws)
    pre_hash = WorkspaceSnapshot.store(pre_snap, store, ws)

    if post_present:
        _write_file(target, post_content)
    else:
        _remove_if_exists(target)
    post_snap = WorkspaceSnapshot.take(ws, prev_snapshot=pre_snap)
    post_hash = WorkspaceSnapshot.store(post_snap, store, ws)

    payload = {
        "action": "modified",
        "path": target,
        "writes": [rel_path],
        "pre_snapshot": pre_hash,
        "post_snapshot": post_hash,
    }
    if prompt is not None:
        payload["prompt"] = prompt
    if reasoning is not None:
        payload["reasoning"] = reasoning

    event = CanonicalEvent(
        event_type=event_type,
        ctx_id=ctx_id,
        source=source,
        source_type=source_type,
        payload=MappingProxyType(payload),
        parent_event_id=parent_event_id,
    )
    writer.append(event)
    return event


# ---------------------------------------------------------------------------
# 1. build + persist
# ---------------------------------------------------------------------------

def test_file_index_build_and_persist(tmp_path):
    """Ledger with 2 events touching main.py → the index contains the
    normalized key with 2 records in ledger order, correct first_event_id,
    and the index file exists on disk with index_hash + schema_version."""
    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
    )
    ev2 = _log_event(
        writer, store, ws, "main.py",
        pre_present=True, pre_content="x = 1\n",
        post_present=True, post_content="x = 1\ny = 2\n",
        parent_event_id=ev1.event_id,
    )

    index = get_file_index(ledger, store)

    assert index["schema_version"] == FILE_INDEX_SCHEMA_VERSION
    assert index["first_event_id"] == ev1.event_id
    assert index["last_offset"] == os.path.getsize(ledger)
    assert isinstance(index["last_hash"], str), "last_hash.json exists after append"
    assert index["covered_archives"] == []
    assert "main.py" in index["files"], (
        "main.py appeared in snapshots → key must be present"
    )
    records = index["files"]["main.py"]["post"]
    assert [r["event_id"] for r in records] == [ev1.event_id, ev2.event_id], (
        "records must be in ledger (append) order"
    )
    assert records[0]["post_snapshot"] == ev1.post_snapshot
    assert records[0]["event_type"] == "FILE_MODIFIED"
    assert records[0]["source"] == "opencode:agent1"

    path = _file_index_path_for(ledger)
    assert os.path.exists(path), "file_index.json must be persisted next to the ledger"
    with open(path) as f:
        on_disk = json.load(f)
    assert on_disk["schema_version"] == FILE_INDEX_SCHEMA_VERSION
    assert isinstance(on_disk["index_hash"], str) and on_disk["index_hash"]


# ---------------------------------------------------------------------------
# 2. only top-level snapshots are indexed
# ---------------------------------------------------------------------------

def test_file_index_only_top_level_snapshots(tmp_path):
    """An event whose payload carries pre/post_snapshot keys but whose
    TOP-LEVEL fields are absent must NOT be indexed (the build never
    touches the payload for snapshot detection)."""
    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # ev1: real top-level snapshots → main.py gets indexed.
    _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
    )

    # ev2: payload-only snapshots. No "writes" key → LedgerWriter does NOT
    # propagate the payload hashes onto the top-level fields. The payload
    # references a REAL snapshot that contains payload_only.py — if the
    # build wrongly resolved payloads, payload_only.py would be indexed.
    _write_file(os.path.join(ws, "payload_only.py"), "p = 1\n")
    snap = WorkspaceSnapshot.take(ws)
    snap_hash = WorkspaceSnapshot.store(snap, store, ws)
    ev2 = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent1",
        source_type="agent",
        payload=MappingProxyType({
            "action": "modified",
            "path": os.path.join(ws, "payload_only.py"),
            "pre_snapshot": snap_hash,
            "post_snapshot": snap_hash,
        }),
    )
    writer.append(ev2)
    assert ev2.pre_snapshot is None and ev2.post_snapshot is None, (
        "payload-only snapshots must NOT propagate to top-level"
    )

    index = get_file_index(ledger, store)

    assert "main.py" in index["files"]
    assert "payload_only.py" not in index["files"], (
        "payload-only snapshots must not be indexed (top-level gate)"
    )


# ---------------------------------------------------------------------------
# 3. tail-extend
# ---------------------------------------------------------------------------

def test_file_index_tail_extend(tmp_path):
    """Build the index, append a 3rd event via the normal LedgerWriter
    (which updates last_hash.json) → get_file_index tail-extends: the new
    record appears and last_offset grew."""
    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
    )
    ev2 = _log_event(
        writer, store, ws, "main.py",
        pre_present=True, pre_content="x = 1\n",
        post_present=True, post_content="x = 1\ny = 2\n",
        parent_event_id=ev1.event_id,
    )

    index1 = get_file_index(ledger, store)
    offset1 = index1["last_offset"]
    assert [r["event_id"] for r in index1["files"]["main.py"]["post"]] == [
        ev1.event_id, ev2.event_id,
    ]

    ev3 = _log_event(
        writer, store, ws, "main.py",
        pre_present=True, pre_content="x = 1\ny = 2\n",
        post_present=True, post_content="x = 1\ny = 2\nz = 3\n",
        parent_event_id=ev2.event_id,
    )

    index2 = get_file_index(ledger, store)

    assert index2["last_offset"] > offset1, "last_offset must grow after append"
    assert index2["first_event_id"] == index1["first_event_id"], (
        "first_event_id must not change on extend"
    )
    assert [r["event_id"] for r in index2["files"]["main.py"]["post"]] == [
        ev1.event_id, ev2.event_id, ev3.event_id,
    ], "extended index must include the 3rd event's record in order"


# ---------------------------------------------------------------------------
# 4. rebuild on truncation
# ---------------------------------------------------------------------------

def test_file_index_rebuild_on_truncation(tmp_path):
    """Build the index, then truncate the ledger (remove the last line AND
    restore last_hash.json to the new last line's hash — a consistent
    restore) → get_file_index rebuilds: the deleted event is gone."""
    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
    )
    ev2 = _log_event(
        writer, store, ws, "main.py",
        pre_present=True, pre_content="x = 1\n",
        post_present=True, post_content="x = 1\ny = 2\n",
        parent_event_id=ev1.event_id,
    )

    index1 = get_file_index(ledger, store)
    assert [r["event_id"] for r in index1["files"]["main.py"]["post"]] == [
        ev1.event_id, ev2.event_id,
    ]

    # Truncate: drop the last line and restore last_hash.json to the hash
    # of the new last line (ev1's entry).
    with open(ledger, "rb") as f:
        lines = f.read().split(b"\n")
    assert lines[-1] == b"", "ledger ends with a trailing newline"
    first_entry = json.loads(lines[0])
    with open(ledger, "wb") as f:
        f.write(b"\n".join(lines[:-2]) + b"\n")
    with open(ledger + ".last_hash.json", "w") as f:
        json.dump({"last_hash": first_entry["hash"]}, f)

    index2 = get_file_index(ledger, store)

    assert [r["event_id"] for r in index2["files"]["main.py"]["post"]] == [
        ev1.event_id,
    ], "rebuilt index must not include the truncated event"


# ---------------------------------------------------------------------------
# 5. rebuild on corruption
# ---------------------------------------------------------------------------

def test_file_index_rebuild_on_corruption(tmp_path):
    """Corrupt file_index.json (garbage) → get_file_index rebuilds and
    returns a correct index, replacing the corrupt file."""
    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
    )

    index1 = get_file_index(ledger, store)
    assert index1["files"]["main.py"]["post"][0]["event_id"] == ev1.event_id

    path = _file_index_path_for(ledger)
    with open(path, "w") as f:
        f.write("{ this is not valid json !!!")

    index2 = get_file_index(ledger, store)

    assert index2["files"]["main.py"]["post"][0]["event_id"] == ev1.event_id, (
        "corrupt index must be rebuilt, not reused"
    )
    with open(path) as f:
        on_disk = json.load(f)
    assert on_disk["schema_version"] == FILE_INDEX_SCHEMA_VERSION
    assert isinstance(on_disk["index_hash"], str) and on_disk["index_hash"]


# ---------------------------------------------------------------------------
# 6. rebuild on schema version mismatch
# ---------------------------------------------------------------------------

def test_file_index_version_mismatch(tmp_path):
    """file_index.json with schema_version 999 → get_file_index rebuilds
    with the current schema version."""
    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
    )

    get_file_index(ledger, store)
    path = _file_index_path_for(ledger)
    with open(path) as f:
        on_disk = json.load(f)
    on_disk["schema_version"] = 999
    with open(path, "w") as f:
        json.dump(on_disk, f)

    index2 = get_file_index(ledger, store)

    assert index2["schema_version"] == FILE_INDEX_SCHEMA_VERSION
    assert index2["files"]["main.py"]["post"][0]["event_id"] == ev1.event_id


# ---------------------------------------------------------------------------
# 7. never-touched gate
# ---------------------------------------------------------------------------

def test_file_index_never_touched(tmp_path):
    """`files` contains a key iff the file appeared in a pre OR post
    snapshot. Files only referenced in payloads (no snapshots) and files
    never referenced at all must NOT appear."""
    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # ev1: introduces main.py (post contains it).
    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
    )

    # ev2: DELETES main.py — present in pre, absent in post. The key must
    # still exist (pre-side gate) but ev2 must NOT get a post record.
    _log_event(
        writer, store, ws, "main.py",
        pre_present=True, pre_content="x = 1\n",
        post_present=False, post_content="",
        parent_event_id=ev1.event_id,
    )

    # ev3: no snapshots at all; the payload references no_snap.py. The
    # build must never consult the payload → no_snap.py must not appear.
    CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent1",
        source_type="agent",
        payload=MappingProxyType({
            "action": "modified",
            "path": "no_snap.py",
        }),
    )
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent1",
        source_type="agent",
        payload=MappingProxyType({"action": "modified", "path": "no_snap.py"}),
    ))

    index = get_file_index(ledger, store)

    assert "main.py" in index["files"], (
        "main.py appeared in snapshots → key must be present"
    )
    assert [r["event_id"] for r in index["files"]["main.py"]["post"]] == [
        ev1.event_id,
    ], "only events whose POST contains the file get a record"
    assert "no_snap.py" not in index["files"], (
        "payload-only file references must not create index keys"
    )
    assert "never_touched.py" not in index["files"], (
        "files never in any snapshot must not create index keys"
    )