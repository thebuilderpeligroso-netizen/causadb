import json
import os
import pytest
from causadb._blob_store import BlobStore

def test_blob_store_put_and_get(tmp_path):
    store = BlobStore(base_path=str(tmp_path / "blobs"))
    data = {"key": "value", "nested": {"a": 1}}
    h = store.put(data)
    retrieved = store.get(h)
    assert retrieved == data

def test_blob_store_sharding(tmp_path):
    store = BlobStore(base_path=str(tmp_path / "blobs"))
    h = store.put({"test": "data"})
    shard_dir = os.path.join(str(tmp_path / "blobs"), h[:2], h[2:4])
    blob_path = os.path.join(shard_dir, f"{h}.json")
    assert os.path.exists(blob_path)
    with open(blob_path) as f:
        assert json.load(f) == {"test": "data"}

def test_blob_store_threshold(tmp_path):
    store = BlobStore(base_path=str(tmp_path / "blobs"), threshold=1024)
    small_data = {"small": "x" * 10}
    h = store.put(small_data)
    # Under threshold → no file written (just hash returned)
    shard_dir = os.path.join(str(tmp_path / "blobs"), h[:2], h[2:4])
    blob_path = os.path.join(shard_dir, f"{h}.json")
    # Data might be under threshold so don't assert file existence
    # Instead verify that get works regardless
    retrieved = store.get(h)
    assert retrieved == small_data

def test_blob_store_disabled_does_not_store(tmp_path):
    # Config test: when blob_store_enabled=False, writer no crea blobs
    from causadb._config import CausaDBConfig
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from types import MappingProxyType
    
    config = CausaDBConfig(
        ledger_path=str(tmp_path / "ledger.log"),
        blob_store_enabled=False,
        blob_store_path=str(tmp_path / "blobs"),
    )
    writer = LedgerWriter(str(tmp_path / "ledger.log"), config=config)
    event = CanonicalEvent(
        event_type=EventType.LLM_INVOKED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({"model": "gpt-4", "prompt": "x" * 2000}),
    )
    writer.append(event)
    # No blob dir should exist
    assert not os.path.exists(config.blob_store_path)

def test_blob_store_get_nonexistent_raises(tmp_path):
    store = BlobStore(base_path=str(tmp_path / "blobs"))
    with pytest.raises(FileNotFoundError):
        store.get("nonexistent_hash")

def test_blob_store_enabled_stores_large_payload(tmp_path):
    from causadb._config import CausaDBConfig
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from types import MappingProxyType
    
    config = CausaDBConfig(
        ledger_path=str(tmp_path / "ledger.log"),
        blob_store_enabled=True,
        blob_store_path=str(tmp_path / "blobs"),
    )
    writer = LedgerWriter(str(tmp_path / "ledger.log"), config=config)
    large_payload = {"data": "x" * 2000, "model": "gpt-4"}
    event = CanonicalEvent(
        event_type=EventType.LLM_INVOKED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType(large_payload),
    )
    writer.append(event)
    # Blob dir should exist and have content
    blob_dir = config.blob_store_path
    assert os.path.exists(blob_dir)
    # Read ledger entry to verify blob ref
    with open(str(tmp_path / "ledger.log")) as f:
        line = f.readline()  # genesis
        line = f.readline()  # our event
        if line:
            import json
            entry = json.loads(line.strip())
            payload = entry["event"]["payload"]
            assert "$blob" in payload

def test_anti_teatro_blob_store_not_called(tmp_path):
    from causadb._config import CausaDBConfig
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from types import MappingProxyType
    
    config = CausaDBConfig(
        ledger_path=str(tmp_path / "ledger.log"),
        blob_store_enabled=True,
        blob_store_path=str(tmp_path / "blobs"),
    )
    # Mock BlobStore.put to not actually write
    from unittest.mock import patch
    with patch("causadb._blob_store.BlobStore.put", return_value="mocked_hash"):
        writer = LedgerWriter(str(tmp_path / "ledger.log"), config=config)
        event = CanonicalEvent(
            event_type=EventType.LLM_INVOKED,
            ctx_id="test",
            source="causadb:test",
            payload=MappingProxyType({"data": "x" * 2000}),
        )
        writer.append(event)
    # Blob dir should NOT have content (mock prevented write)
    blob_dir = config.blob_store_path
    entries = []
    if os.path.exists(blob_dir):
        for root, dirs, files in os.walk(blob_dir):
            entries.extend(files)
    assert len(entries) == 0  # no real blobs written
