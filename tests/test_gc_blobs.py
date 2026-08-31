import json
import os
from types import MappingProxyType

import pytest

from causadb._blob_store import BlobStore
from causadb._config import CausaDBConfig
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._gc_blobs import BlobGC, GarbageCollectionReport
from causadb._init import causadb_init
from causadb._ledger_writer import LedgerWriter


def _setup_ledger(tmp_path, events, orphan_blobs=None, corrupt_ledger=False):
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


class TestBlobGCImport:
    def test_import_blob_gc(self):
        gc = BlobGC("/tmp/fake/ledger.log")
        assert gc is not None

    def test_import_report_dataclass(self):
        r = GarbageCollectionReport(executed=False, total_blobs=0, orphan_count=0)
        assert r.orphan_count == 0


class TestDryRun:
    def test_reports_orphan_does_not_move(self, tmp_path):
        ledger, blob_dir, _ = _setup_ledger(
            tmp_path,
            [CanonicalEvent(event_type=EventType.GOVERNANCE_DECISION, ctx_id="t",
                            source="causadb:t", payload=MappingProxyType({"inline": True}))],
            [{"orphan": True}],
        )
        trash = os.path.join(blob_dir, ".trash")
        report = BlobGC(ledger).collect(dry_run=True)
        assert report.orphan_count == 1
        assert not os.path.exists(trash)

    def test_default_is_dry_run(self, tmp_path):
        ledger, _, _ = _setup_ledger(
            tmp_path,
            [CanonicalEvent(event_type=EventType.GOVERNANCE_DECISION, ctx_id="t",
                            source="s", payload=MappingProxyType({"a": 1}))],
            [{"orphan": True}],
        )
        report = BlobGC(ledger).collect()
        assert not report.executed

    def test_never_creates_trash(self, tmp_path):
        ledger, blob_dir, _ = _setup_ledger(
            tmp_path,
            [CanonicalEvent(event_type=EventType.GOVERNANCE_DECISION, ctx_id="t",
                            source="s", payload=MappingProxyType({"a": 1}))],
            [{"orphan": True}],
        )
        trash = os.path.join(blob_dir, ".trash")
        BlobGC(ledger).collect(dry_run=True)
        assert not os.path.exists(trash)


class TestExecute:
    def test_moves_orphan_to_trash(self, tmp_path):
        ledger, blob_dir, _ = _setup_ledger(
            tmp_path,
            [CanonicalEvent(event_type=EventType.GOVERNANCE_DECISION, ctx_id="t",
                            source="s", payload=MappingProxyType({"inline": True}))],
            [{"orphan": True}],
        )
        report = BlobGC(ledger).collect(dry_run=False)
        assert len(report.moved) > 0
        assert os.path.exists(os.path.join(blob_dir, ".trash"))

    def test_referenced_blobs_preserved(self, tmp_path):
        ledger, blob_dir, _ = _setup_ledger(
            tmp_path,
            [CanonicalEvent(event_type=EventType.GOVERNANCE_DECISION, ctx_id="t",
                            source="s", payload=MappingProxyType({"reasoning": "X" * 3000}))],
            [{"orphan": True}],
        )
        with open(ledger) as f:
            for line in f:
                entry = json.loads(line)
                p = entry["event"].get("payload", {})
                if isinstance(p, dict) and "$blob" in p:
                    ref_hash = p["$blob"]
                    break
        blob_path = os.path.join(blob_dir, ref_hash[:2], ref_hash[2:4], f"{ref_hash}.json")
        assert os.path.exists(blob_path)

        BlobGC(ledger).collect(dry_run=False)
        assert os.path.exists(blob_path)

    def test_preserves_shard_in_trash(self, tmp_path):
        ledger, blob_dir, _ = _setup_ledger(
            tmp_path,
            [CanonicalEvent(event_type=EventType.GOVERNANCE_DECISION, ctx_id="t",
                            source="s", payload=MappingProxyType({"inline": True}))],
            [{"orphan": True}],
        )
        report = BlobGC(ledger).collect(dry_run=False)
        for shard, bhash in report.moved:
            trash_path = os.path.join(blob_dir, ".trash", shard, f"{bhash}.json")
            assert os.path.exists(trash_path)


class TestHarvestExclusion:
    def test_not_collected_as_orphan(self, tmp_path):
        blob = {"path": "/a/b", "content": "xx", "size": 2, "mtime": 1}
        ledger, _, _ = _setup_ledger(
            tmp_path,
            [CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="t",
                            source="harvester:filesystem", payload=MappingProxyType(blob))],
            [blob],
        )
        report = BlobGC(ledger).collect(dry_run=True)
        assert report.orphan_count == 0

    def test_not_moved_on_execute(self, tmp_path):
        blob = {"path": "/c/d", "content": "yy", "size": 2, "mtime": 2}
        ledger, blob_dir, _ = _setup_ledger(
            tmp_path,
            [CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="t",
                            source="harvester:filesystem", payload=MappingProxyType(blob))],
            [blob],
        )
        harvest_hash = None
        for dirpath, _, filenames in os.walk(blob_dir):
            for fn in filenames:
                if fn.endswith(".json") and ".trash" not in dirpath:
                    harvest_hash = fn.replace(".json", "")
        report = BlobGC(ledger).collect(dry_run=False)
        blob_path = os.path.join(blob_dir, harvest_hash[:2], harvest_hash[2:4],
                                 harvest_hash + ".json")
        assert os.path.exists(blob_path)
        assert report.by_class["harvest"] >= 1

    def test_report_shows_harvest_class(self, tmp_path):
        blob = {"path": "/e/f", "content": "zz", "size": 2, "mtime": 3}
        ledger, _, _ = _setup_ledger(
            tmp_path,
            [CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="t",
                            source="harvester:filesystem", payload=MappingProxyType(blob))],
            [blob],
        )
        report = BlobGC(ledger).collect()
        assert report.by_class["harvest"] == 1


class TestPrecondition:
    def test_execute_refuses_corrupt_ledger(self, tmp_path):
        ledger, _, _ = _setup_ledger(tmp_path, [], [{"orphan": True}], corrupt_ledger=True)
        with pytest.raises(RuntimeError, match="Cannot execute GC"):
            BlobGC(ledger).collect(dry_run=False)

    def test_dry_run_works_on_corrupt_ledger(self, tmp_path):
        ledger, _, _ = _setup_ledger(tmp_path, [], [{"a": 1}], corrupt_ledger=True)
        report = BlobGC(ledger).collect(dry_run=True)
        assert report.total_blobs >= 1
        assert report.orphan_count >= 1


class TestAfterExecute:
    def test_validate_ok(self, tmp_path):
        ledger, _, _ = _setup_ledger(
            tmp_path,
            [CanonicalEvent(event_type=EventType.GOVERNANCE_DECISION, ctx_id="t",
                            source="s", payload=MappingProxyType({"inline": True}))],
            [{"orphan": True}],
        )
        report = BlobGC(ledger).collect(dry_run=False)
        assert len(report.moved) > 0
        from causadb._ledger_validator import LedgerValidator
        vr = LedgerValidator(ledger).validate_chain()
        assert vr.is_valid

    def test_revive_ok(self, tmp_path):
        ledger, _, _ = _setup_ledger(
            tmp_path,
            [CanonicalEvent(event_type=EventType.GOVERNANCE_DECISION, ctx_id="t",
                            source="s", payload=MappingProxyType({"inline": True}))],
            [{"orphan": True}],
        )
        report = BlobGC(ledger).collect(dry_run=False)
        assert len(report.moved) > 0
        from causadb.cli._cmd_revive import _run_revive
        rcode, _ = _run_revive(ledger_path=ledger, output_format="markdown", max_decisions=10)
        assert rcode == 0


class TestReport:
    def test_has_all_fields(self, tmp_path):
        ledger, _, _ = _setup_ledger(tmp_path, [], [{"orphan": True}])
        report = BlobGC(ledger).collect()
        assert hasattr(report, "executed")
        assert hasattr(report, "total_blobs")
        assert hasattr(report, "orphan_count")
        assert hasattr(report, "by_class")
        assert hasattr(report, "moved")
        assert hasattr(report, "orphans")

    def test_counts_match_disk(self, tmp_path):
        ledger, _, _ = _setup_ledger(tmp_path, [], [{"a": 1}, {"b": 2}])
        report = BlobGC(ledger).collect()
        assert report.total_blobs == 2
        assert report.orphan_count == 2


class TestSnapshotRefs:
    def test_snapshot_blob_not_orphan(self, tmp_path):
        from causadb._snapshot import WorkspaceSnapshot

        ws = tmp_path / "ws"
        result = causadb_init(str(ws))
        ledger = result["ledger_path"]
        blobs_dir = os.path.join(os.path.dirname(ledger), "blobs")
        store = BlobStore(blobs_dir)

        (ws / "file1.txt").write_text("contenido_snap")
        snap = WorkspaceSnapshot.take(str(ws))
        snap_hash = WorkspaceSnapshot.store(snap, store, root_dir=str(ws))

        config = CausaDBConfig(ledger_path=ledger, blob_store_enabled=True)
        writer = LedgerWriter(ledger, config=config)
        writer.append(CanonicalEvent(
            event_type="PROJECT_SNAPSHOT",
            ctx_id="test", source="test",
            payload=MappingProxyType({"note": "with snap"}),
            pre_snapshot=snap_hash,
        ))

        orphan_hash = store.put({"extra_orphan_snap": True})

        gc = BlobGC(ledger)
        report = gc.collect(dry_run=True)
        orphan_list = [bhash for shard, bhash in report.orphans]

        assert snap_hash not in orphan_list
        file_hashes = [entry["hash"] for entry in snap["files"].values()]
        for fhash in file_hashes:
            assert fhash not in orphan_list
        assert orphan_hash in orphan_list
        assert report.by_class["snapshot"] > 0


class TestOrphanDetection:
    def test_orphan_blob_is_collected(self, tmp_path):
        ledger, blobs_dir, store = _setup_ledger(tmp_path, [])
        orphan_hash = store.put({"data": "garbage"})
        gc = BlobGC(ledger)
        report = gc.collect(dry_run=True)
        assert orphan_hash in [bhash for shard, bhash in report.orphans]


class TestHarvestKeysTolerantToExtraKeys:
    """FIX.3 — _HARVEST_BLOB_KEYS must tolerate extra keys (post-FIX.5 format).

    The NEW FilesystemSource blob format has 6 keys (path, content, size,
    mtime, action, content_hash) instead of the original 4. The GC must
    identify these as harvest blobs via SUBSET match, not exact match.

    This test MUST FAIL in Red because the current implementation uses
    ``frozenset(d.keys()) == _HARVEST_BLOB_KEYS`` (exact match of 4 keys).
    """

    def test_gc_harvest_keys_tolerant_to_extra_keys(self, tmp_path):
        blob = {
            "path": "/x/y", "content": "data", "size": 4, "mtime": 1,
            "action": "modified", "content_hash": "deadbeef",
        }
        ledger, _, _ = _setup_ledger(
            tmp_path,
            [CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="t",
                            source="harvester:filesystem",
                            payload=MappingProxyType(blob))],
            [blob],
        )
        report = BlobGC(ledger).collect(dry_run=True)
        assert report.by_class["harvest"] >= 1, \
            f"6-key harvest blob must be classified as harvest, " \
            f"got by_class={report.by_class!r}"
        assert report.orphan_count == 0, \
            f"6-key harvest blob must NOT be orphan, " \
            f"got orphan_count={report.orphan_count}"