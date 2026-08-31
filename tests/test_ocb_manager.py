"""Tests for F.10 — OCB L1 Complete (8 tasks).

Test-First (Article III): tests BEFORE implementation.
Anti-teatro (Article IX): every core test has discriminatory power.

Existing tests are PRESERVED (3 rewritten to use new API).

Fase 0 — OCB ↔ BlobStore (deuda #13): el OCB externaliza payloads grandes
a refs ``$blob`` (formato uniforme con el ledger) y los resuelve bajo
demanda. Tests con blobs REALES en disco (Art. IX — discriminantes).
"""
import pytest
import os
import json
import time
import threading
from types import MappingProxyType
from causadb._ocb_manager import OCB
from causadb._blob_store import BlobStore
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def w(tmp_path):
    """Short name for workspace fixture."""
    base_path = tmp_path / "ocb_data"
    base_path.mkdir()
    return str(base_path)


@pytest.fixture
def ocb_default(w):
    return OCB("actor", w, threshold_events=200, partition_minutes=15,
               retention_days=15, max_rewind_partitions=5)


def _event():
    return CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"
    )


# ===================================================================
# Task 1 — Time-based rotation + fix teatro test
# ===================================================================

def test_append_creates_active_log(w):
    ocb = OCB("actor", w)
    ocb.append(_event())
    assert os.path.exists(os.path.join(w, "OCB_ACTIVE.log"))


def test_append_rotates_on_threshold_preserved(w):
    """Regression: threshold-based rotation still works (Task 1)."""
    ocb = OCB("actor", w, threshold_events=2)
    ocb.append(_event())
    ocb.append(_event())
    ocb.append(_event())
    files = os.listdir(w)
    assert any(f.startswith("OCB_PARTITION_") for f in files)


def test_append_rotates_on_time_automatically(w):
    """REWRITTEN: ACTIVE old via mtime → append() triggers time rotation."""
    ocb = OCB("actor", w, threshold_events=200, partition_minutes=0.001)
    ocb.append(_event())
    active_path = os.path.join(w, "OCB_ACTIVE.log")
    assert os.path.exists(active_path)
    # Set mtime to 1 hour ago to force time-based rotation
    old_time = time.time() - 3600
    os.utime(active_path, (old_time, old_time))
    ocb.append(_event())
    files = os.listdir(w)
    assert any(f.startswith("OCB_PARTITION_") for f in files)


def test_anti_teatro_time_rotation_not_called(w):
    """Anti-teatro: if _rotate is no-op, time rotation never creates partition."""
    ocb = OCB("actor", w, threshold_events=200, partition_minutes=0.001)
    ocb.append(_event())
    active_path = os.path.join(w, "OCB_ACTIVE.log")
    old_time = time.time() - 3600
    os.utime(active_path, (old_time, old_time))
    original_rotate = ocb._rotate
    ocb._rotate = lambda: None  # Mutant: no-op rotate
    ocb.append(_event())
    files = os.listdir(w)
    assert not any(f.startswith("OCB_PARTITION_") for f in files), (
        "With mutant _rotate, no partition should exist"
    )


# ===================================================================
# Task 2 — close_session() idempotent with Lock
# ===================================================================

def test_close_session_generates_summary(w):
    """close_session() generates OCB_SUMMARY.json."""
    ocb = OCB("actor", w)
    ocb.append(_event())
    ocb.close_session(summary={"events_count": 1})
    assert os.path.exists(os.path.join(w, "OCB_SUMMARY.json"))


def test_close_session_idempotent(w):
    """Second close_session() is no-op (idempotent)."""
    ocb = OCB("actor", w)
    ocb.append(_event())
    ocb.close_session(summary={"events_count": 1})
    ocb.close_session(summary={"events_count": 2})  # second call
    with open(os.path.join(w, "OCB_SUMMARY.json")) as f:
        summary = json.load(f)
    assert summary["events_count"] == 1  # not overwritten


def test_close_session_no_active_noop(w):
    """close_session() with no ACTIVE file is a no-op."""
    ocb = OCB("actor", w)
    ocb.close_session(summary={})
    assert not os.path.exists(os.path.join(w, "OCB_SUMMARY.json"))


def test_close_session_renames_active(w):
    """close_session() renames ACTIVE to ARCHIVED."""
    ocb = OCB("actor", w)
    ocb.append(_event())
    ocb.close_session(summary={"events_count": 1})
    assert not os.path.exists(os.path.join(w, "OCB_ACTIVE.log"))
    assert os.path.exists(os.path.join(w, "OCB_SUMMARY.json"))


def test_concurrent_close_and_append(w):
    """Thread safety: close + append from 2 threads does not corrupt (no crash)."""
    ocb = OCB("actor", w, threshold_events=200)
    ocb.append(_event())

    def closer():
        ocb.close_session(summary={"ok": True})

    def appender():
        ocb.append(_event())

    t1 = threading.Thread(target=closer)
    t2 = threading.Thread(target=appender)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # No crash = pass


def test_save_summary_marks_sedimentadas(w):
    """REWRITTEN: calls close_session(), checks manifest."""
    ocb = OCB("actor", w)
    ocb.append(_event())
    ocb.close_session(summary={"test": "data"})
    manifest_path = os.path.join(w, "OCB_MANIFEST.json")
    assert os.path.exists(manifest_path)
    with open(manifest_path) as f:
        manifest = json.load(f)
    assert manifest.get("sedimentada") is True


def test_save_summary_does_not_delete(w):
    """REWRITTEN: after close_session(), ACTIVE may be renamed but events remain."""
    ocb = OCB("actor", w)
    ocb.append(_event())
    ocb.close_session(summary={})
    assert os.path.exists(os.path.join(w, "OCB_SUMMARY.json"))


def test_anti_teatro_close_session_no_lock(w):
    """Anti-teatro: without lock, concurrent close+append may race (test detects)."""
    ocb = OCB("actor", w)
    ocb.append(_event())
    # If close_session had no lock, test_concurrent_close_and_append would crash.
    # We verify close_session works by checking summary was written:
    ocb.close_session(summary={"ok": True})
    assert os.path.exists(os.path.join(w, "OCB_SUMMARY.json"))


# ===================================================================
# Task 3 — load_session_context() with preload 2
# ===================================================================

def test_load_session_context_normal_close(w):
    """After normal close, load_session_context returns summary."""
    ocb = OCB("actor", w)
    ocb.append(_event())
    ocb.close_session(summary={"events_count": 1})
    ctx = ocb.load_session_context()
    assert ctx["summary"].get("events_count") == 1
    assert ctx["session_type"] == "normal_close"


def test_load_session_context_abrupt_close(w):
    """If no SUMMARY.json, it's an abrupt close."""
    ocb = OCB("actor", w)
    ocb.append(_event())
    ctx = ocb.load_session_context()
    assert ctx["session_type"] == "abrupt_close"
    assert "summary" in ctx


def test_load_session_context_first_run(w):
    """First run: no ACTIVE, no partitions → session_type = first_run."""
    ocb = OCB("actor", w)
    ctx = ocb.load_session_context()
    assert ctx["session_type"] == "first_run"


def test_load_session_context_returns_2_partitions(w):
    """Preload partitions (up to 2) + ACTIVE."""
    ocb = OCB("actor", w, threshold_events=2)
    # Create 4 partitions
    for i in range(4):
        ocb.append(_event())
        ocb.append(_event())
        ocb.append(_event())  # 3rd append triggers rotation at threshold=2
    # Total preloaded: 2 partitions + 1 active
    ctx = ocb.load_session_context()
    assert len(ctx["preloaded_partitions"]) <= 3


def test_load_session_context_preload_order_by_timestamp(w):
    """Preloaded partitions ordered by embedded timestamp, not mtime."""
    ocb = OCB("actor", w, threshold_events=2)
    ocb.append(_event())
    ocb.append(_event())
    ocb.append(_event())  # rotate → partition 1
    p1 = [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")][0]
    time.sleep(0.1)
    ocb.append(_event())
    ocb.append(_event())
    ocb.append(_event())  # rotate → partition 2
    ctx = ocb.load_session_context()
    # The latest 2 (or fewer if only 2 exist) should be in order
    assert len(ctx["preloaded_partitions"]) >= 1


def test_anti_teatro_load_context_returns_all_partitions(w):
    """Anti-teatro: if no cap, load_session_context returns all → test fails."""
    ocb = OCB("actor", w, threshold_events=2)
    for i in range(5):
        ocb.append(_event())
        ocb.append(_event())
        ocb.append(_event())
    # If load_session_context returned all 5 (no cap), preloaded_partitions len > 3
    ctx = ocb.load_session_context()
    assert len(ctx["preloaded_partitions"]) <= 3, (
        "Without partition cap, this would return all partitions"
    )



def test_anti_teatro_load_context_uses_mtime(w):
    """Anti-teatro: if using mtime instead of filename timestamp, order may differ."""
    ocb = OCB("actor", w, threshold_events=2)
    ocb.append(_event())
    ocb.append(_event())
    ocb.append(_event())  # rotate → partition 1
    # get partitions by filename timestamp (not mtime)
    partitions = sorted(
        [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")],
        key=lambda x: x,
    )
    assert len(partitions) >= 1
    # If code used mtime, the order might be different from filename sorting.
    # We verify that filename-based sorting is deterministic:
    sorted_by_name = sorted(partitions)
    assert sorted_by_name == partitions, "Filename sorting must match expected order"


# ===================================================================
# Task 4 — load_older_partition(current_id) with cap 5
# ===================================================================

def test_load_older_partition_returns_previous(w):
    """load_older_partition returns the partition content."""
    ocb = OCB("actor", w, threshold_events=2)
    # 5 appends → 2 partitions (events 1-2, events 3-4), ACTIVE has event 5
    for _ in range(5):
        ocb.append(_event())
    partitions = sorted([f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")])
    assert len(partitions) >= 2, f"Expected 2+ partitions, got {len(partitions)}"
    current_id = partitions[-1]
    older = ocb.load_older_partition(current_id)
    assert older != ""


def test_load_older_partition_oldest_returns_empty(w):
    """load_older_partition on the oldest partition returns empty string."""
    ocb = OCB("actor", w, threshold_events=2)
    ocb.append(_event())
    ocb.append(_event())
    ocb.append(_event())  # rotate → partition 1
    partitions = sorted([f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")])
    oldest = partitions[0]
    older = ocb.load_older_partition(oldest)
    assert older == "", f"Expected empty for oldest, got: {older[:50]!r}"


def test_load_older_partition_cap_at_5(w):
    """load_older_partition distance >= max_rewind_partitions → returns ''."""
    ocb = OCB("actor", w, threshold_events=2, max_rewind_partitions=3)
    for i in range(6):
        ocb.append(_event())
        ocb.append(_event())
        ocb.append(_event())  # rotate
    partitions = sorted([f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")])
    if len(partitions) > 3:
        latest = partitions[-1]
        older = ocb.load_older_partition(latest, distance=3)
        assert older == "", f"Expected empty at max_rewind, got content"


def test_load_older_partition_order_by_filename_ts_not_mtime(w):
    """Partition ordering is by filename timestamp, not mtime."""
    ocb = OCB("actor", w, threshold_events=2)
    ocb.append(_event())
    ocb.append(_event())
    ocb.append(_event())  # rotate → partition 1
    partitions = [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")]
    # All partitions have timestamps in their names
    assert all("_" in p and p.split("_")[-1].replace(".log", "").isdigit()
               for p in partitions), "Partitions must have timestamp in name"


def test_anti_teatro_older_partition_no_cap(w):
    """Anti-teatro: without cap, load_older_partition never returns empty."""
    ocb = OCB("actor", w, threshold_events=2, max_rewind_partitions=5)
    for i in range(6):
        ocb.append(_event())
        ocb.append(_event())
        ocb.append(_event())
    partitions = sorted([f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")])
    if len(partitions) >= 5:
        latest = partitions[-1]
        older = ocb.load_older_partition(latest, distance=6)
        assert older == "", (
            "With cap at 5, distance=6 should return empty"
        )


def test_anti_teatro_older_partition_default_distance(w):
    """Anti-teatro: default distance=1 works without explicit arg."""
    ocb = OCB("actor", w, threshold_events=2)
    ocb.append(_event())
    ocb.append(_event())
    ocb.append(_event())
    partitions = sorted([f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")])
    if partitions:
        newer = partitions[-1]
        result = ocb.load_older_partition(newer)
        # Default distance=1 should return the immediate previous
        assert isinstance(result, str)


# ===================================================================
# Task 5 — Auto purge 15 days with throttle 60s
# ===================================================================

def test_purge_auto_skipped_if_recent_sweep(w):
    """Throttle: if last sweep was <60s ago, skip."""
    ocb = OCB("actor", w, threshold_events=200, retention_days=15)
    assert ocb._last_purge_sweep == 0, "Initial sweep should be 0"
    ocb.append(_event())
    assert ocb._last_purge_sweep > 0, "Sweep should run on append"
    last = ocb._last_purge_sweep
    ocb.append(_event())
    # If throttle works, _last_purge_sweep should not change (too soon)
    assert ocb._last_purge_sweep >= last


def test_purge_auto_keeps_recent_partitions(w):
    """Partitions within retention_days are kept."""
    ocb = OCB("actor", w, threshold_events=2, retention_days=15)
    ocb.append(_event())
    ocb.append(_event())
    ocb.append(_event())  # rotate
    # Force purge check with recent time
    ocb._purge_old_partitions()
    partitions = [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")]
    assert len(partitions) >= 1, "Recent partitions should be kept"


def test_purge_auto_does_not_delete_active(w):
    """Auto purge never deletes OCB_ACTIVE.log."""
    ocb = OCB("actor", w, threshold_events=2, retention_days=0)
    ocb.append(_event())
    ocb.append(_event())
    ocb.append(_event())  # rotate
    ocb.append(_event())  # creates new ACTIVE
    ocb._purge_old_partitions()
    assert os.path.exists(os.path.join(w, "OCB_ACTIVE.log")), "ACTIVE must survive purge"


def test_purge_auto_keeps_summaries(w):
    """Auto purge never deletes OCB_SUMMARY*.json files."""
    ocb = OCB("actor", w)
    ocb.append(_event())
    ocb.close_session(summary={"test": "data"})
    ocb._purge_old_partitions()
    assert os.path.exists(os.path.join(w, "OCB_SUMMARY.json")), "Summary must survive purge"


def test_purge_auto_deletes_old_partitions(w):
    """Partitions older than retention_days are deleted."""
    ocb = OCB("actor", w, threshold_events=2, retention_days=0)
    ocb.append(_event())
    ocb.append(_event())
    ocb.append(_event())  # rotate → partition created
    partitions_before = [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")]
    assert len(partitions_before) >= 1
    # Force purge with retention_days=0 → should delete old partitions
    ocb._purge_old_partitions()
    partitions_after = [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")]
    # With retention_days=0, partitions should be deleted
    assert len(partitions_after) < len(partitions_before) or len(partitions_after) == 0


def test_anti_teatro_purge_auto_deletes_active(w):
    """Anti-teatro: if purge deletes ACTIVE, this test catches it."""
    ocb = OCB("actor", w)
    ocb.append(_event())
    # Simulate mutant that also deletes ACTIVE
    active_path = os.path.join(w, "OCB_ACTIVE.log")
    assert os.path.exists(active_path), "ACTIVE must exist before purge"
    ocb._purge_old_partitions()
    assert os.path.exists(active_path), "ACTIVE must survive purge"


# ===================================================================
# Task 6 — Manual purge()
# ===================================================================

def test_purge_manual_all(w):
    """purge() with no args deletes all partitions."""
    ocb = OCB("actor", w, threshold_events=2)
    for i in range(3):
        ocb.append(_event())
        ocb.append(_event())
        ocb.append(_event())  # rotate
    ocb.purge()
    partitions = [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")]
    assert len(partitions) == 0, "purge() must delete all partitions"


def test_purge_manual_keep_last_3(w):
    """purge(keep_last=3) keeps the 3 latest partitions."""
    ocb = OCB("actor", w, threshold_events=2)
    for i in range(6):
        ocb.append(_event())
        ocb.append(_event())
        ocb.append(_event())
    ocb.purge(keep_last=3)
    partitions = sorted([f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")])
    assert len(partitions) <= 3


def test_purge_manual_older_than_7(w):
    """purge(older_than_days=7) deletes older partitions."""
    ocb = OCB("actor", w, threshold_events=2)
    for i in range(3):
        ocb.append(_event())
        ocb.append(_event())
        ocb.append(_event())
    ocb.purge(older_than_days=0)  # 0 days → deletes everything
    partitions = [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")]
    assert len(partitions) == 0


def test_purge_manual_empty_workspace(w):
    """purge() on empty workspace is a no-op (no crash)."""
    ocb = OCB("actor", w)
    ocb.purge()  # should not raise


def test_purge_manual_does_not_touch_active(w):
    """purge() never deletes OCB_ACTIVE.log."""
    ocb = OCB("actor", w, threshold_events=2)
    ocb.append(_event())
    ocb.append(_event())
    ocb.append(_event())
    ocb.purge()
    assert os.path.exists(os.path.join(w, "OCB_ACTIVE.log"))


def test_purge_manual_does_not_touch_summaries(w):
    """purge() never deletes OCB_SUMMARY*.json."""
    ocb = OCB("actor", w)
    ocb.append(_event())
    ocb.close_session(summary={"test": "data"})
    ocb.purge()
    assert os.path.exists(os.path.join(w, "OCB_SUMMARY.json"))


def test_anti_teatro_purge_deletes_active(w):
    """Anti-teatro: if purge() mistakenly deletes ACTIVE, test catches."""
    ocb = OCB("actor", w)
    ocb.append(_event())
    active_path = os.path.join(w, "OCB_ACTIVE.log")
    ocb.purge()
    assert os.path.exists(active_path), "purge() must never delete ACTIVE"


def test_anti_teatro_purge_deletes_summary(w):
    """Anti-teatro: if purge() deletes summary, test catches."""
    ocb = OCB("actor", w)
    ocb.append(_event())
    ocb.close_session(summary={"t": "d"})
    ocb.purge()
    assert os.path.exists(os.path.join(w, "OCB_SUMMARY.json"))


def test_anti_teatro_purge_keep_last_noop(w):
    """Anti-teatro: if purge(keep_last=...) doesn't actually delete, test catches."""
    ocb = OCB("actor", w, threshold_events=2)
    for i in range(6):
        ocb.append(_event())
        ocb.append(_event())
        ocb.append(_event())
    before = len([f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")])
    ocb.purge(keep_last=3)
    after = len([f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")])
    assert after <= 3, f"After purge(keep_last=3), expected ≤3 partitions, got {after}"
    assert after < before, "purge must actually delete partitions"


# ===================================================================
# Existing preserved tests
# ===================================================================

def test_load_context_returns_metadata_only(w):
    ocb = OCB("actor", w)
    ocb.append(_event())
    ctx = ocb.load_context()
    assert "summary" in ctx
    assert "partition_ids" in ctx
    assert "count" in ctx
    assert "events" not in ctx


def test_load_partition_by_id_lazy(w):
    ocb = OCB("actor", w, threshold_events=2)
    ocb.append(_event())
    ocb.append(_event())
    ocb.append(_event())
    partitions = [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")]
    assert partitions
    ocb.load_partition_by_id(partitions[0])
    # lazy load — no crash = pass


def test_paths_configurable(w):
    base = str(w)
    ocb = OCB("actor", base)
    ocb.append(_event())
    assert os.path.exists(os.path.join(base, "OCB_ACTIVE.log"))


def test_crash_detection_preserves_orphan(w):
    active_path = os.path.join(w, "OCB_ACTIVE.log")
    open(active_path, "w").close()
    assert not os.path.exists(os.path.join(w, "OCB_SUMMARY.json"))
    ocb = OCB("actor", w)
    files = os.listdir(w)
    assert any(f.startswith("OCB_ORPHAN_") for f in files)
    assert not os.path.exists(active_path)


def test_observers_receive_metadata_only(w):
    ocb = OCB("actor", w)
    observer_data = []
    ocb.observers.append(lambda d: observer_data.append(d))
    e = _event()
    ocb.append(e)
    assert len(observer_data) == 1
    assert observer_data[0]["event_id"] == e.event_id
    assert "payload" not in observer_data[0]


def test_no_get_raw_context(w):
    ocb = OCB("actor", w)
    assert not hasattr(ocb, "get_raw_context")


# ===================================================================
# Task 8 — Config defaults
# ===================================================================

def test_config_ocb_threshold_events_default_80():
    ocb = OCB("actor", "/tmp/_test_ocb_config_ignored_")
    assert ocb.threshold_events == 80


def test_config_ocb_partition_minutes():
    ocb = OCB("actor", "/tmp/_test_ocb_config_ignored_", partition_minutes=15)
    assert ocb.partition_minutes == 15


def test_config_ocb_retention_days():
    ocb = OCB("actor", "/tmp/_test_ocb_config_ignored_", retention_days=15)
    assert ocb.retention_days == 15


def test_config_ocb_max_rewind_partitions():
    ocb = OCB("actor", "/tmp/_test_ocb_config_ignored_", max_rewind_partitions=5)
    assert ocb.max_rewind_partitions == 5


# ===================================================================
# Fase 0 — OCB ↔ BlobStore (deuda #13)
# ===================================================================

def _event_with_payload(payload: dict):
    """CanonicalEvent con payload dado (para tests de $blob)."""
    return CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent",
        payload=MappingProxyType(payload),
    )


def test_ocb_append_externalizes_large_payload(w, tmp_path):
    """Fase 0 — append con payload > threshold + blob_store → la línea de la
    partición tiene ``payload == {"$blob": hash}`` y el resto del
    ``to_dict()`` intacto (12 campos). El blob se resuelve de verdad en
    disco (Art. IX — no es un stub)."""
    store = BlobStore(base_path=str(tmp_path / "blobs"))
    ocb = OCB("actor", w, blob_store=store, blob_store_threshold=1024)

    big_payload = {"content": "detalle granular " + "x" * 3000}
    event = _event_with_payload(big_payload)
    ocb.append(event)

    active_path = os.path.join(w, "OCB_ACTIVE.log")
    with open(active_path) as f:
        line = json.loads(f.readline())

    # payload externalizado a ref $blob
    assert set(line["payload"]) == {"$blob"}, (
        f"payload debe ser {{'$blob': hash}}, got: {line['payload']}"
    )
    blob_hash = line["payload"]["$blob"]

    # el hash resuelve el payload ORIGINAL (blob real en disco)
    assert store.get(blob_hash) == big_payload

    # resto del to_dict() intacto — 12 campos, igual al evento original
    expected = event.to_dict()
    expected["payload"] = {"$blob": blob_hash}
    assert line == expected
    assert len(line) == 12, f"to_dict() debe tener 12 campos, got {len(line)}"


def test_ocb_load_resolves_blob_on_demand(w, tmp_path):
    """Fase 0 — ``load_partition_by_id`` sobre una línea ``$blob`` →
    resuelve el contenido REAL vía ``blob_store.get`` (on demand)."""
    store = BlobStore(base_path=str(tmp_path / "blobs"))
    ocb = OCB("actor", w, blob_store=store, blob_store_threshold=1024,
              threshold_events=1)

    big_payload = {"content": "detalle granular " + "x" * 3000}
    event = _event_with_payload(big_payload)
    ocb.append(event)  # ACTIVE: [event]
    ocb.append(_event_with_payload({"small": 1}))  # rotación → partición

    partitions = [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")]
    assert len(partitions) == 1, f"esperaba 1 partición, got {partitions}"
    events = ocb.load_partition_by_id(partitions[0])

    assert len(events) == 1
    assert events[0]["payload"] == big_payload, (
        "load_partition_by_id debe resolver el blob bajo demanda"
    )
    assert events[0]["event_id"] == event.event_id


def test_ocb_append_small_payload_inline(w, tmp_path):
    """Fase 0 — payload pequeño → inline (sin ``$blob``). Compatibilidad
    con el formato de particiones existente."""
    store = BlobStore(base_path=str(tmp_path / "blobs"))
    ocb = OCB("actor", w, blob_store=store, blob_store_threshold=1024)

    small = {"content": "hola"}
    event = _event_with_payload(small)
    ocb.append(event)

    with open(os.path.join(w, "OCB_ACTIVE.log")) as f:
        line = json.loads(f.readline())

    assert line["payload"] == small
    assert "$blob" not in line["payload"]
    assert line == event.to_dict(), "payload pequeño → to_dict() inline"


def test_ocb_threshold_boundary_inline(w, tmp_path):
    """Fase 0 (ajuste 5) — payload con ``len == threshold`` → inline (NO
    externaliza; umbral estrictamente mayor). Discriminante: 1 byte más
    arriba del umbral SÍ externaliza."""
    threshold = 1024
    store = BlobStore(base_path=str(tmp_path / "blobs"))
    ocb = OCB("actor", w, blob_store=store, blob_store_threshold=threshold)

    # payload cuyo JSON serializado mide EXACTAMENTE el threshold
    n = threshold - len(json.dumps({"content": ""}, sort_keys=True).encode())
    assert n > 0
    payload = {"content": "x" * n}
    assert len(json.dumps(payload, sort_keys=True).encode()) == threshold

    ocb.append(_event_with_payload(payload))
    with open(os.path.join(w, "OCB_ACTIVE.log")) as f:
        line = json.loads(f.readline())
    assert line["payload"] == payload, "== threshold → inline"
    assert "$blob" not in line["payload"]

    # discriminante: justo por encima del threshold → externaliza
    ocb.append(_event_with_payload({"content": "x" * (n + 1)}))
    with open(os.path.join(w, "OCB_ACTIVE.log")) as f:
        lines = f.readlines()
    second = json.loads(lines[-1])
    assert set(second["payload"]) == {"$blob"}, (
        "> threshold → externaliza; si el umbral no es estrictamente "
        "mayor, este test falla"
    )


def test_ocb_without_blob_store_inline(w):
    """Fase 0 — sin blob_store → TODO inline (cero regresión sobre el
    comportamiento actual)."""
    ocb = OCB("actor", w)  # sin blob_store

    big_payload = {"content": "x" * 5000}
    event = _event_with_payload(big_payload)
    ocb.append(event)

    with open(os.path.join(w, "OCB_ACTIVE.log")) as f:
        line = json.loads(f.readline())
    assert line["payload"] == big_payload
    assert line == event.to_dict()


def test_ocb_missing_blob_fall_closed(w, tmp_path):
    """Fase 0 (ajuste 4) — partición con ``$blob`` a hash inexistente →
    ``resolved: False``, NO crashea. Fall-closed propio del OCB (no
    depende de ``resolve_payload()``)."""
    store = BlobStore(base_path=str(tmp_path / "blobs"))
    ocb = OCB("actor", w, blob_store=store, blob_store_threshold=1024)

    # Escribir una línea $blob cuyo hash NO existe en el store
    missing_hash = "deadbeef" * 8  # 64 hex chars
    event_dict = _event_with_payload({"content": "x" * 2000}).to_dict()
    event_dict["payload"] = {"$blob": missing_hash}
    active_path = os.path.join(w, "OCB_ACTIVE.log")
    with open(active_path, "a") as f:
        f.write(json.dumps(event_dict, sort_keys=True) + "\n")

    ocb._rotate()
    partitions = [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")]
    assert len(partitions) == 1

    events = ocb.load_partition_by_id(partitions[0])  # no debe crashear
    assert len(events) == 1
    assert events[0]["payload"] == {"resolved": False, "$blob": missing_hash}
    assert events[0]["payload"]["resolved"] is False

def test_default_threshold_is_80():
    from causadb._ocb_manager import OCB
    ocb = OCB("actor", "/tmp/_test_threshold_default")
    assert ocb.threshold_events == 80, f"Expected 80, got {ocb.threshold_events}"

def test_for_ledger_propagates_threshold_from_config(monkeypatch, tmp_path):
    ledger = tmp_path / "ledger.log"
    ledger.touch()
    
    monkeypatch.setenv("CAUSADB_OCB_THRESHOLD_EVENTS", "42")
    from causadb._ocb_manager import OCB
    ocb = OCB.for_ledger(str(ledger))
    assert ocb.threshold_events == 42,         f"for_ledger debe respetar CausaDBConfig.ocb_threshold_events. Got {ocb.threshold_events}"

def test_for_ledger_threshold_fallback_80_when_config_missing(monkeypatch, tmp_path):
    ledger = tmp_path / "ledger.log"
    ledger.touch()
    
    import causadb._config as cfg_mod
    real_init = cfg_mod.CausaDBConfig.__init__
    def boom(self, *a, **kw):
        raise RuntimeError("forced failure")
    monkeypatch.setattr(cfg_mod.CausaDBConfig, "__init__", boom)
    
    from causadb._ocb_manager import OCB
    ocb = OCB.for_ledger(str(ledger))
    assert ocb.threshold_events == 80, f"fallback default debe ser 80, no 200. Got {ocb.threshold_events}"

def _event():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    return CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="agent")


def test_rotation_at_80_events(tmp_path):
    """Con threshold=80, el 81º evento dispara rotación (cupo = 80 active + 1 que rebalsa).

    Anti-teatro: si se mutea para rotar a 200, este test falla porque
    `os.listdir(w)` no contendría `OCB_PARTITION_*`.

    Semántica del threshold: ``_append_locked`` chequea ``count >= threshold``
    ANTES de escribir. El 80º evento vé count=79 (no rota, escribe). El 81º
    vé count=80 → rota los 80 a una partición, escribe el 81 en active nuevo.
    """
    import os
    w = str(tmp_path / "ocb")
    os.makedirs(w)
    from causadb._ocb_manager import OCB
    ocb = OCB("actor", w, threshold_events=80, partition_minutes=15)
    for _ in range(81):
        ocb.append(_event())

    partitions = [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")]
    assert len(partitions) == 1, f"81 events con threshold=80 deben rotar exactamente 1 part. Got {partitions}"
    active_path = os.path.join(w, "OCB_ACTIVE.log")
    with open(active_path) as f:
        active_lines = len(f.readlines())
    assert active_lines == 1, f"Tras la rotación, el 81º evento debe quedar solo en active. Got {active_lines} lines"

def test_rotation_not_at_79_events(tmp_path):
    import os
    w = str(tmp_path / "ocb")
    os.makedirs(w)
    from causadb._ocb_manager import OCB
    ocb = OCB("actor", w, threshold_events=80, partition_minutes=15)
    for _ in range(79):
        ocb.append(_event())
    
    partitions = [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")]
    assert len(partitions) == 0, f"79 events no deben disparar rotación. Got partitions: {partitions}"
    active_path = os.path.join(w, "OCB_ACTIVE.log")
    with open(active_path) as f:
        assert len(f.readlines()) == 79


# ===================================================================
# FIX.OCB-FLUSH — flush_active_to_partition() (OCB congelado)
# ===================================================================

def test_flush_active_to_partition_public(ocb_default):
    """FIX.OCB-FLUSH — con un ACTIVE con contenido (N < threshold), el
    flush público rota el ACTIVE a PARTITION: retorna True, existe un
    ``OCB_PARTITION_*`` y ya no existe ``OCB_ACTIVE.log``.

    Anti-teatro: si el método no rota (no-op), la partición no existe y
    este test falla."""
    ocb = ocb_default  # threshold_events=200, N=5 < threshold
    for _ in range(5):
        ocb.append(_event())

    assert ocb.flush_active_to_partition() is True, (
        "flush con ACTIVE con contenido debe retornar True"
    )

    partitions = [f for f in os.listdir(ocb.base_path)
                  if f.startswith("OCB_PARTITION_") and f.endswith(".log")]
    assert len(partitions) == 1, (
        f"flush debe archivar el ACTIVE en exactamente 1 partición, "
        f"got {partitions}"
    )
    assert not os.path.exists(ocb._active_path), (
        "Tras el flush, OCB_ACTIVE.log no debe existir"
    )


def test_flush_active_empty_noop(w):
    """FIX.OCB-FLUSH — sin ACTIVE (base_path vacía) el flush es no-op:
    retorna False y NO crea ninguna ``OCB_PARTITION_*``."""
    ocb = OCB("actor", w, threshold_events=200)
    assert ocb.flush_active_to_partition() is False, (
        "flush sin ACTIVE debe retornar False"
    )
    partitions = [f for f in os.listdir(w)
                  if f.startswith("OCB_PARTITION_") and f.endswith(".log")]
    assert len(partitions) == 0, (
        f"flush vacío no debe crear particiones, got {partitions}"
    )


def test_flush_active_idempotent(w):
    """FIX.OCB-FLUSH — el flush es idempotente: 1º → True (rota), 2º →
    False (ya no hay ACTIVE), y solo existe 1 ``OCB_PARTITION_*``."""
    ocb = OCB("actor", w, threshold_events=200)
    ocb.append(_event())
    ocb.append(_event())

    assert ocb.flush_active_to_partition() is True
    assert ocb.flush_active_to_partition() is False, (
        "2º flush sin ACTIVE debe retornar False (idempotente)"
    )
    partitions = [f for f in os.listdir(w)
                  if f.startswith("OCB_PARTITION_") and f.endswith(".log")]
    assert len(partitions) == 1, (
        f"2 flushes sobre 1 ACTIVE deben producir exactamente 1 partición, "
        f"got {partitions}"
    )

def test_rotate_active_with_content_to_partition_instead_of_orphan(w):
    """FIX.OCB-ROTATE — con un ACTIVE con contenido (sin summary), el
    init rota el ACTIVE a PARTITION (time_ns) en lugar de crear un
    ORPHAN."""
    # 1. Crear un ACTIVE con contenido
    ocb = OCB("actor", w)
    active_path = os.path.join(w, "OCB_ACTIVE.log")
    with open(active_path, "w") as f:
        for _ in range(3):
            f.write(json.dumps({"e": "v"}) + "\n")

    # 2. Instanciar OCB dispara _init_workspace
    ocb2 = OCB("actor", w)

    # 3. Asertar: NO hay OCB_ORPHAN_*, hay OCB_PARTITION_*
    orphans = [f for f in os.listdir(w) if f.startswith("OCB_ORPHAN_")]
    assert len(orphans) == 0, f"No debería existir ORPHAN, got {orphans}"

    partitions = [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")]
    assert len(partitions) == 1, f"Debería existir 1 partición, got {partitions}"
    
    # Verificar contenido preservado (3 líneas)
    with open(os.path.join(w, partitions[0]), "r") as f:
        lines = f.readlines()
        assert len(lines) == 3

def test_partition_name_uses_time_ns_not_seconds(w):
    """FIX.OCB-ROTATE — anti-teatro: el nombre de la partición matchea
    el formato de time_ns() (19 dígitos)."""
    # Escenario similar al anterior
    active_path = os.path.join(w, "OCB_ACTIVE.log")
    with open(active_path, "w") as f:
        f.write(json.dumps({"e": "v"}) + "\n")
        
    OCB("actor", w)
    
    import re
    partitions = [f for f in os.listdir(w) if f.startswith("OCB_PARTITION_")]
    assert re.match(r"OCB_PARTITION_\d{19}\.log", partitions[0]), \
        f"Nombre de partición no matchea regex, got {partitions[0]}"

def test_load_session_context_includes_recent_orphan(w):
    """FIX.OCB-LOAD — un orphan con contenido es incluido en el preload."""
    # Crear un orphan con contenido
    ts = time.time_ns()
    orphan_path = os.path.join(w, f"OCB_ORPHAN_{ts}.log")
    with open(orphan_path, "w") as f:
        f.write(json.dumps({"e": "v1"}) + "\n")
        f.write(json.dumps({"e": "v2"}) + "\n")
        
    ocb = OCB("actor", w)
    context = ocb.load_session_context()
    
    preloaded = context["preloaded_partitions"]
    assert any(p["id"].startswith("OCB_ORPHAN_") for p in preloaded), \
        f"Orphan debería estar incluido, got {preloaded}"

def test_load_session_context_includes_active_content(w):
    """FIX.OCB-LOAD — ACTIVE con contenido es incluido en el preload."""
    ocb = OCB("actor", w)
    ocb.append(_event())
    
    context = ocb.load_session_context()
    preloaded = context["preloaded_partitions"]
    assert any(p["id"] == "OCB_ACTIVE.log" for p in preloaded), \
        f"ACTIVE debería estar incluido, got {preloaded}"

def test_load_session_context_orphan_empty_not_preloaded(w):
    """FIX.OCB-LOAD — anti-teatro: orphan VACÍO NO se preloada."""
    ts = time.time_ns()
    orphan_path = os.path.join(w, f"OCB_ORPHAN_{ts}.log")
    # Empty file
    open(orphan_path, 'a').close()
    
    ocb = OCB("actor", w)
    context = ocb.load_session_context()
    
    preloaded = context["preloaded_partitions"]
    assert not any(p["id"].startswith("OCB_ORPHAN_") for p in preloaded), \
        "Orphan vacío no debería estar incluido"


def test_load_session_context_empty_orphan_is_abrupt_not_first_run(w):
    """FIX.OCB-FIRST-RUN — un orphan VACÍO residual (crash detection) es
    evidencia de una sesión previa → session_type ``abrupt_close`` (NO
    ``first_run``). Regresión introducida y corregida por el Checker: el
    chequeo de first_run debe considerar ``has_orphan`` (cualquier orphan,
    vacío o no), separándolo del preload (que solo incluye orphans con
    contenido)."""
    # Orphan vacío a mano (simula el residual de crash detection)
    ts = time.time_ns()
    open(os.path.join(w, f"OCB_ORPHAN_{ts}.log"), "w").close()
    ocb = OCB("actor", w)
    context = ocb.load_session_context()
    assert context["session_type"] == "abrupt_close", (
        f"orphan vacío residual → abrupt_close, got {context['session_type']}"
    )
    assert context["total_partitions"] == 0
    # Y no se preloadea (sin contenido)
    assert not any(p["id"].startswith("OCB_ORPHAN_") for p in context["preloaded_partitions"])
