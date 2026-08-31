import pytest
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._replay_engine import ReplayEngine
import uuid
from datetime import datetime

@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")

def test_canonical_event_requires_event_type():
    with pytest.raises(TypeError):
        CanonicalEvent()

def test_canonical_event_requires_ctx_id():
    # En el original ctx_id es None, pero para CausaDB debe ser obligatorio.
    # Ajustamos test para reflejar requerimiento:
    with pytest.raises(ValueError):
        CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id=None, source="test")

def test_canonical_event_requires_source():
    with pytest.raises(ValueError):
        CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source=None)

def test_canonical_event_parent_event_id_optional():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent1", parent_event_id=None)
    assert e.parent_event_id is None

def test_canonical_event_source_type_valid():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent1", source_type="agent")
    assert e.source_type == "agent"

def test_canonical_event_schema_version_default():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent1")
    assert e.schema_version == "0.1.0"

def test_canonical_event_payload_immutable():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent1", payload={"x": 1})
    with pytest.raises(TypeError):
        e.payload["x"] = 2

def test_canonical_event_to_dict_roundtrip():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent1", payload={"x": 1})
    d = e.to_dict()
    e2 = CanonicalEvent.from_dict(d)
    assert e == e2

def test_metadata_session_id():
    m = EventMetadata(trace_id="t", session_id="s")
    assert m.session_id == "s"


# --- BIT-CHR.35 P1: EventMetadata.priority (legacy genesis metadata) ---

def test_event_metadata_priority_roundtrip():
    """(BIT-CHR.35 P1) `EventMetadata` accepts `priority` and it survives a
    `CanonicalEvent` dict roundtrip: to_dict() emits it and from_dict()
    preserves it.

    Anti-teatro: before the fix, `EventMetadata(priority="high")` raises
    `TypeError: unexpected keyword argument 'priority'`, so this test fails.
    """
    m = EventMetadata(trace_id="t", session_id="s", priority="high")
    assert m.priority == "high"

    e = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b", metadata=m
    )
    d = e.to_dict()
    assert d["metadata"]["priority"] == "high"
    e2 = CanonicalEvent.from_dict(d)
    assert e2.metadata.priority == "high"
    assert e2.metadata.session_id == "s"
    assert e2.metadata.trace_id == "t"


def test_event_from_dict_metadata_priority_no_crash():
    """(BIT-CHR.35 P1) Anti-teatro: the genesis event written by
    `migrate_v0_0_to_v0_1` carries `metadata.priority` (legacy, not in the
    original dataclass). `from_dict` must NOT raise TypeError and must
    preserve `priority`.

    Against the old code this fails with:
    `TypeError: EventMetadata.__init__() got an unexpected keyword argument 'priority'`
    """
    data = {
        "event_id": "evt-priority-1",
        "event_type": "SYSTEM_BOOT",
        "ctx_id": "ctx",
        "source": "system",
        "metadata": {"priority": "high", "session_id": "x"},
    }
    e = CanonicalEvent.from_dict(data)
    assert e.metadata is not None
    assert e.metadata.priority == "high"
    assert e.metadata.session_id == "x"


def test_event_metadata_priority_defaults_to_none():
    """(BIT-CHR.35 P1) `priority` defaults to None, so existing call sites
    that build metadata without `priority` keep working unchanged."""
    m = EventMetadata(trace_id="t", session_id="s")
    assert m.priority is None


# --- F.2.3: Wire format event_id/timestamp históricos ---

def test_log_with_explicit_event_id_uses_it():
    """CanonicalEvent acepta event_id explícito en lugar de autogenerarlo."""
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b",
                       event_id="my-custom-id-123")
    assert e.event_id == "my-custom-id-123"

def test_log_without_event_id_autogenerates():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b")
    assert e.event_id is not None
    uuid.UUID(e.event_id)

def test_log_with_explicit_timestamp_uses_it():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b",
                       timestamp="2026-07-22T10:00:00Z")
    assert e.timestamp == "2026-07-22T10:00:00Z"

def test_log_without_timestamp_autogenerates():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b")
    assert e.timestamp is not None
    assert e.timestamp.endswith("Z")


# --- F.2.4: Sequence_number ---

def test_sequence_number_starts_at_0_genesis(ledger_path):
    import json
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b")
    writer.append(e)
    with open(writer.ledger_path, "r") as f:
        entry = json.loads(f.readline())
    assert entry["event"]["sequence_number"] == 0

def test_sequence_number_increments(ledger_path):
    import json
    writer = LedgerWriter(ledger_path)
    for i in range(3):
        e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b")
        writer.append(e)
    with open(writer.ledger_path, "r") as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        entry = json.loads(line)
        assert entry["event"]["sequence_number"] == i

def test_sequence_number_survives_replay(ledger_path):
    writer = LedgerWriter(ledger_path)
    for i in range(3):
        e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b",
                           payload={"path": f"/f{i}.py", "action": "create"})
        writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert state["events_applied"] == 3
    assert state["files_modified"][0]["path"] == "/f0.py"
