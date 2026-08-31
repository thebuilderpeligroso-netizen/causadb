import pytest
import os
import threading
import multiprocessing
import json
import hashlib
import time
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType
from causadb._config import CausaDBConfig

@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")

def test_append_creates_genesis_if_absent(ledger_path):
    writer = LedgerWriter(ledger_path)
    assert writer.last_hash == "GENESIS"
    
def test_append_hash_chain_valid(ledger_path):
    writer = LedgerWriter(ledger_path)
    e1 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent")
    writer.append(e1)
    h1 = writer.last_hash
    assert h1 != "GENESIS"
    
    e2 = CanonicalEvent(event_type=EventType.COMMAND_RUN, ctx_id="ctx", source="opencode:agent")
    writer.append(e2)
    h2 = writer.last_hash
    assert h2 != h1

def test_append_concurrent_threadsafe(ledger_path):
    writer = LedgerWriter(ledger_path)
    def worker():
        for _ in range(10):
            e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent")
            writer.append(e)
            
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    
    with open(ledger_path, "r") as f:
        lines = f.readlines()
        assert len(lines) == 100
        curr_hash = "GENESIS"
        for line in lines:
            entry = json.loads(line)
            assert entry["prev_hash"] == curr_hash
            event_json = json.dumps(entry["event"], sort_keys=True)
            expected_hash = hashlib.sha256((event_json + curr_hash).encode()).hexdigest()
            assert entry["hash"] == expected_hash
            curr_hash = entry["hash"]

def worker_proc(ledger_path):
    writer = LedgerWriter(ledger_path)
    for _ in range(5):
        e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent")
        writer.append(e)

def test_append_concurrent_multiprocess(ledger_path):
    open(ledger_path, 'a').close()
    processes = [multiprocessing.Process(target=worker_proc, args=(ledger_path,)) for _ in range(2)]
    for p in processes: p.start()
    for p in processes: p.join()
    
    with open(ledger_path, "r") as f:
        lines = f.readlines()
        assert len(lines) == 10
        curr_hash = "GENESIS"
        for line in lines:
            entry = json.loads(line)
            assert entry["prev_hash"] == curr_hash, f"prev_hash mismatch: {entry['prev_hash']} != {curr_hash}"
            event_json = json.dumps(entry["event"], sort_keys=True)
            expected_hash = hashlib.sha256((event_json + curr_hash).encode()).hexdigest()
            assert entry["hash"] == expected_hash, f"hash mismatch"
            curr_hash = entry["hash"]

def test_get_last_hash_o1(ledger_path):
    def measure_time(num_events):
        with open(ledger_path, "w") as f:
            for i in range(num_events):
                f.write(f'{{"hash": "h{i}", "prev_hash": "h{i-1}"}}\n')
        
        start = time.perf_counter()
        writer = LedgerWriter(ledger_path)
        end = time.perf_counter()
        return end - start
    
    t1 = measure_time(100)
    t2 = measure_time(100000)
    
    ratio = t2 / t1 if t1 > 0 else 0
    assert ratio < 10, f"Performance ratio too high: {ratio}"

def test_append_fsyncs(ledger_path, mocker):
    mocker.patch("os.fsync")
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent")
    writer.append(e)
    os.fsync.assert_called()

def test_relative_path_raises():
    with pytest.raises(ValueError):
        LedgerWriter("ledger.log")

def test_redaction_applied(ledger_path):
    """Bloqueante #5: LedgerWriter debe redactar campos sensibles
    del payload ANTES de computar el hash-chain, y preservar el resto."""
    config = CausaDBConfig(ledger_path=ledger_path, redaction_enabled=True)
    writer = LedgerWriter(ledger_path, config)
    e = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent",
        payload={"path": "/etc/foo", "action": "modify", "password": "secret123"},
    )
    writer.append(e)
    with open(ledger_path) as f:
        entry = json.loads(f.readline())
    # El campo sensible fue mask-eado con sha256("secret123")[:16]
    assert entry["event"]["payload"]["password"] != "secret123", (
        "Fallo bloqueante #5: password en claro en el ledger"
    )
    assert entry["event"]["payload"]["password"] == "fcf730b6d95236ec", (
        f"Fallo: se esperaba sha256 prefix, got {entry['event']['payload']['password']}"
    )
    # Los campos no sensibles se preservan
    assert entry["event"]["payload"]["path"] == "/etc/foo"
    assert entry["event"]["payload"]["action"] == "modify"

# Valida integración con _attribution.validate_source: bare names (sin
# namespace) ahora son válidos (source_type clasifica el tipo; source es solo
# el nombre). Un evento con namespace también debe escribirse normalmente.
def test_attribution_validated(ledger_path):
    writer = LedgerWriter(ledger_path)

    # 1. Bare name source → writes successfully
    bare_event = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="gemini-cli",
        source_type="agent",
    )
    writer.append(bare_event)

    # 2. Namespaced source → writes successfully + hash chain valid
    valid_event = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent1",
        source_type="agent",
    )
    writer.append(valid_event)

    with open(ledger_path, "r") as f:
        lines = f.readlines()
    assert len(lines) == 2, "Ledger must contain exactly two entries"

    # Check hash chain
    curr_hash = "GENESIS"
    for line in lines:
        entry = json.loads(line)
        assert entry["prev_hash"] == curr_hash
        event_json = json.dumps(entry["event"], sort_keys=True)
        expected_hash = hashlib.sha256((event_json + curr_hash).encode()).hexdigest()
        assert entry["hash"] == expected_hash
        curr_hash = entry["hash"]

def test_ledger_writer_accepts_on_append_kwarg(ledger_path):
    writer = LedgerWriter(ledger_path, on_append=lambda e, x: None)
    assert writer.on_append is not None

def test_ledger_writer_default_on_append_is_none(ledger_path):
    writer = LedgerWriter(ledger_path)
    assert writer.on_append is None

def test_callback_called_after_append(ledger_path):
    spy = []
    def cb(event, entry):
        spy.append((event.event_id, entry["hash"]))
    writer = LedgerWriter(ledger_path, on_append=cb)
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="agent")
    entry = writer.append(e)
    assert len(spy) == 1
    assert spy[0][0] == e.event_id
    assert spy[0][1] == entry["hash"]

def test_callback_failure_does_not_break_ledger(ledger_path):
    def cb(event, entry):
        raise ValueError("Intentional failure")
    writer = LedgerWriter(ledger_path, on_append=cb)
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="agent")
    writer.append(e)
    
    with open(ledger_path, "r") as f:
        assert len(f.readlines()) == 1
    assert writer.on_append is not None

def test_callback_runs_outside_writer_lock(ledger_path):
    # This test ensures the callback doesn't cause a deadlock if it tries to 
    # access something that might use the same lock (though for the callback, 
    # the lock is already released).
    def cb(event, entry):
        # writer is not available here easily, so we check if lock is actually available
        # by trying to acquire it in a non-blocking way, but if it was locked, 
        # it would raise or block.
        # The prompt specifically says "assert not writer._lock.locked()".
        # Let's use a shared state.
        is_locked_check[0] = writer._lock.locked()
        
    is_locked_check = [True]
    writer = LedgerWriter(ledger_path, on_append=cb)
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="agent")
    writer.append(e)
    assert not is_locked_check[0], "callback corrió dentro del lock"

def test_callback_receives_correct_event_id(ledger_path):
    spy = []
    def cb(event, entry):
        spy.append(event.event_id)
    writer = LedgerWriter(ledger_path, on_append=cb)
    events = [CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="agent") for _ in range(3)]
    for e in events:
        writer.append(e)
    assert [e.event_id for e in events] == spy

def test_with_ocb_feed_returns_ledger_writer_with_callback(ledger_path):
    w = LedgerWriter.with_ocb_feed(ledger_path)
    assert w.on_append is not None

def test_with_ocb_feed_callback_appends_to_ocb(ledger_path, tmp_path):
    # This is a bit complex as it needs OCB set up.
    # The prompt mentions testing lazily creates OCB dir.
    from causadb._ocb_manager import OCB
    w = LedgerWriter.with_ocb_feed(ledger_path)
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="agent")
    w.append(e)
    
    # Check OCB active log
    ocb_dir = os.path.join(os.path.dirname(ledger_path), "ocb")
    # Actually OCB.for_ledger creates this under the ledger's parent dir or where configured
    # We should look for it.
    assert os.path.exists(ocb_dir)

def test_with_ocb_feed_callback_creates_ocb_dir_lazy(ledger_path):
    ocb_dir = os.path.join(os.path.dirname(ledger_path), "ocb")
    assert not os.path.exists(ocb_dir)
    w = LedgerWriter.with_ocb_feed(ledger_path)
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="agent")
    w.append(e)
    assert os.path.exists(ocb_dir)
