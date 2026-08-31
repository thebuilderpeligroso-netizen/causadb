import pytest
import os
import json
from causadb._ledger_index import LedgerIndex
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType

@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")

def test_rebuild_indexes_by_event_id(ledger_path):
    writer = LedgerWriter(ledger_path)
    for i in range(5):
        writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b", payload={"path": "p", "action": "a"}))
    
    index = LedgerIndex(ledger_path)
    index.rebuild()
    
    # Verificamos que index tiene 5 entradas
    # Para verificar el contenido, necesitamos acceder al index privado o su archivo
    index_file = ledger_path + ".index.json"
    with open(index_file, "r") as f:
        idx_data = json.load(f)
    assert len(idx_data["event_ids"]) == 5

def test_get_offset_returns_byte_offset(ledger_path):
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b", payload={"path": "p", "action": "a"})
    writer.append(e)
    
    index = LedgerIndex(ledger_path)
    offset = index.get_offset(e.event_id)
    assert isinstance(offset, int)
    assert offset == 0

def test_index_invalidated_on_new_event(ledger_path):
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b", payload={"path": "p", "action": "a"}))
    
    index = LedgerIndex(ledger_path)
    index.rebuild()
    
    e2 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b", payload={"path": "p", "action": "a"})
    writer.append(e2)
    
    # Debería reconstruirse automáticamente
    assert index.get_offset(e2.event_id) is not None # Se reconstruyó

def test_index_uses_hash_not_mtime(ledger_path, mocker):
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b", payload={"path": "p", "action": "a"})
    writer.append(e)
    
    index = LedgerIndex(ledger_path)
    index.rebuild()
    
    # Mockear mtime (no debería usarse)
    mocker.patch("os.path.getmtime", return_value=0)
    
    # Acceso no debe causar rebuild
    assert index.get_offset(e.event_id) is not None

def test_index_is_derivative(ledger_path):
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b", payload={"path": "p", "action": "a"})
    writer.append(e)
    
    index = LedgerIndex(ledger_path)
    index.rebuild()
    index_file = ledger_path + ".index.json"
    
    # Borrar index
    os.remove(index_file)
    
    # Debería reconstruirse
    assert index.get_offset(e.event_id) is not None


# --- F.2.4: LedgerIndex extendido + causadb query ---

def test_index_by_event_type(ledger_path):
    writer = LedgerWriter(ledger_path)
    for i in range(3):
        writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b"))
    writer.append(CanonicalEvent(event_type=EventType.COMMAND_RUN, ctx_id="ctx", source="a:b"))
    index = LedgerIndex(ledger_path)
    index.rebuild()
    result = index.query(event_type="FILE_MODIFIED")
    assert len(result) == 3
    result2 = index.query(event_type="COMMAND_RUN")
    assert len(result2) == 1

def test_index_by_ctx_id(ledger_path):
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx1", source="a:b"))
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx2", source="a:b"))
    index = LedgerIndex(ledger_path)
    index.rebuild()
    result = index.query(ctx_id="ctx1")
    assert len(result) == 1

def test_index_by_parent_event_id(ledger_path):
    writer = LedgerWriter(ledger_path)
    e1 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b")
    writer.append(e1)
    e2 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b", parent_event_id=e1.event_id)
    writer.append(e2)
    index = LedgerIndex(ledger_path)
    index.rebuild()
    result = index.query(parent_event_id=e1.event_id)
    assert len(result) == 1

def test_index_by_source(ledger_path):
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"))
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="causadb:vigilante"))
    index = LedgerIndex(ledger_path)
    index.rebuild()
    result = index.query(source="opencode:agent")
    assert len(result) == 1


def test_query_no_filters_returns_all_events(ledger_path):
    writer = LedgerWriter(ledger_path)
    for _ in range(3):
        writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b"))
    index = LedgerIndex(ledger_path)
    result = index.query()
    assert len(result) == 3, f"expected 3 events with no filters, got {len(result)}"


def test_query_detects_new_events_via_hash(ledger_path):
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b"))
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b"))
    index = LedgerIndex(ledger_path)
    index.rebuild()
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b"))
    result = index.query()
    assert len(result) == 3, f"expected 3 events after stale cache, got {len(result)}"


def test_query_with_filter_detects_new_events_via_hash(ledger_path):
    writer = LedgerWriter(ledger_path)
    for _ in range(5):
        writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b"))
    writer.append(CanonicalEvent(event_type=EventType.COMMAND_RUN, ctx_id="ctx", source="a:b"))
    index = LedgerIndex(ledger_path)
    index.rebuild()
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b"))
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b"))
    result = index.query(event_type="FILE_MODIFIED")
    assert len(result) == 7, f"expected 7 events, got {len(result)}"


def test_get_offset_detects_stale_cache(ledger_path):
    writer = LedgerWriter(ledger_path)
    e1 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b")
    writer.append(e1)
    index = LedgerIndex(ledger_path)
    index.rebuild()
    e2 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b")
    writer.append(e2)
    assert index.get_offset(e2.event_id) is not None


def test_query_time_filter_respects_duplicate_event_ids(ledger_path):
    """BIT-CHR.114 — la delegación a query_engine debe correlacionar por
    sequence_number, no por event_id.

    El ledger real tiene event_ids duplicados (el mismo event_id aparece en
    miles de seq). Si ``LedgerIndex.query`` correlaciona por event_id (set),
    el re-read trae TODAS las ocurrencias y el cap trunca las MÁS VIEJAS —
    devolviendo eventos fuera del rango temporal pedido.
    """
    shared_id = "dup-event-114"
    writer = LedgerWriter(ledger_path)
    # Viejo (fuera de rango) + nuevo (dentro de rango), mismo event_id.
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="a:b",
        event_id=shared_id,
        timestamp="2026-06-01T00:00:00Z",
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="a:b",
        event_id=shared_id,
        timestamp="2026-07-01T00:00:00Z",
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.COMMAND_RUN,
        ctx_id="ctx",
        source="a:b",
        timestamp="2026-07-10T00:00:00Z",
    ))
    index = LedgerIndex(ledger_path)
    index.rebuild()
    results = index.query(from_time="2026-06-15", limit=5)
    # Solo el evento nuevo (2026-07-01) y el COMMAND_RUN (2026-07-10)
    # están en rango. El duplicado viejo (2026-06-01) NO debe aparecer.
    timestamps = [
        r["event"]["timestamp"]
        for r in results
        if r["event"]["event_id"] == shared_id
    ]
    assert "2026-06-01T00:00:00Z" not in timestamps, (
        f"duplicado viejo filtró por error: {timestamps}"
    )
    assert "2026-07-01T00:00:00Z" in timestamps
    assert len(results) == 2
