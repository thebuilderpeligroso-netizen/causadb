import pytest
import json
import os
from causadb._event_registry import register_type, is_registered, load_from_config, EventTypeSpec
from causadb._event_types import EventType
from causadb._event_schema import CanonicalEvent
from causadb._replay_engine import ReplayEngine
from types import MappingProxyType

@pytest.fixture
def temp_config(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"custom_event_types": {"TEST": {"required_fields": ["f1"]}}}))
    return str(config)

def test_register_and_is_registered():
    spec = EventTypeSpec(required_fields={"f1"})
    register_type("TEST", spec)
    assert is_registered("TEST") is True

def test_is_registered_accepts_enum():
    assert is_registered(EventType.FILE_MODIFIED) is True

def test_load_from_config(temp_config):
    count = load_from_config(temp_config)
    assert count == 1
    assert is_registered("TEST") is True

def test_corrupt_config_does_not_break_builtins(tmp_path):
    config = tmp_path / "corrupt.json"
    config.write_text("invalid json")
    count = load_from_config(str(config))
    assert count == 0
    assert is_registered(EventType.FILE_MODIFIED) is True

def test_canonical_event_with_custom_type():
    register_type("CUSTOM_TYPE", EventTypeSpec(required_fields={"f1"}))
    e = CanonicalEvent(event_type="CUSTOM_TYPE", ctx_id="t", source="t", payload=MappingProxyType({"f1": "val"}))
    assert e.event_type.value == "CUSTOM_TYPE"

def test_canonical_event_backward_compat_enum():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="t", source="t", payload=MappingProxyType({"path": "p", "action": "a"}))
    assert e.event_type is EventType.FILE_MODIFIED

def test_to_dict_with_custom_type():
    register_type("CUSTOM", EventTypeSpec(required_fields={"f1"}))
    e = CanonicalEvent(event_type="CUSTOM", ctx_id="t", source="t", payload=MappingProxyType({"f1": "v"}))
    d = e.to_dict()
    assert d["event_type"] == "CUSTOM"
    assert isinstance(d["event_type"], str)

def test_from_dict_custom_type():
    register_type("CUSTOM", EventTypeSpec(required_fields={"f1"}))
    data = {"event_id": "id", "event_type": "CUSTOM", "ctx_id": "t", "source": "t", "payload": {"f1": "v"}}
    e = CanonicalEvent.from_dict(data)
    assert e.event_type.value == "CUSTOM"

def test_from_dict_unknown_type_fallback():
    # Unknown type falls back to string (forward compat)
    e = CanonicalEvent.from_dict({"event_id": "id", "event_type": "BOGUS", "ctx_id": "t", "source": "t"})
    assert e.event_type == "BOGUS"

def test_apply_custom_event(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    register_type("CUSTOM", EventTypeSpec(required_fields={"f1"}))
    e = CanonicalEvent(event_type="CUSTOM", ctx_id="t", source="t", payload=MappingProxyType({"f1": "v"}))
    writer.append(e)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["custom_events"]) == 1
    assert state["custom_events"][0]["event_type"] == "CUSTOM"

def test_apply_builtin_still_works(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="t", source="t", payload=MappingProxyType({"path": "p", "action": "a"}))
    writer.append(e)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["files_modified"]) == 1
    assert len(state["custom_events"]) == 0

def test_is_builtin():
    from causadb._event_registry import is_builtin
    assert is_builtin("FILE_MODIFIED") is True
    register_type("CUSTOM", EventTypeSpec(required_fields={"f1"}))
    assert is_builtin("CUSTOM") is False

def test_register_builtin_name_does_not_overwrite():
    # Attempting to register builtin again should not overwrite
    register_type("FILE_MODIFIED", EventTypeSpec(required_fields={"x"}), builtin=False)
    # This might be tricky because of threading/registry state, but let's test if it's still builtin
    from causadb._event_registry import is_builtin
    assert is_builtin("FILE_MODIFIED") is True

def test_thread_safety():
    import threading
    def worker(i):
        register_type(f"THREAD_{i}", EventTypeSpec(required_fields={}))
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    for i in range(10):
        assert is_registered(f"THREAD_{i}") is True
