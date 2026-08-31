import pytest
import os
import json
from causadb import _chronicle_index

@pytest.fixture
def ledger_path(tmp_path):
    ledger = tmp_path / "ledger.log"
    ledger.write_text("dummy")
    return str(ledger)

def test_index_path(ledger_path):
    expected = os.path.join(os.path.dirname(ledger_path), "chronicle_index.json")
    assert _chronicle_index.index_path(ledger_path) == expected

def test_save_and_load(ledger_path):
    index = {"version": 1, "by_bit": {"BIT-1": {"event_ids": ["e1"]}}, "by_event": {"e1": ["BIT-1"]}}
    _chronicle_index.save_index(ledger_path, index)
    loaded = _chronicle_index.load_index(ledger_path)
    assert loaded == index

def test_load_missing_file(ledger_path):
    # ledger_path doesn't have a chronicle_index.json yet
    loaded = _chronicle_index.load_index(ledger_path)
    assert loaded == {"version": 1, "by_bit": {}, "by_event": {}}

def test_load_corrupt_json(ledger_path):
    index_file = _chronicle_index.index_path(ledger_path)
    with open(index_file, "w") as f:
        f.write("{ invalid json")
    loaded = _chronicle_index.load_index(ledger_path)
    assert loaded == {"version": 1, "by_bit": {}, "by_event": {}}

def test_link_events(ledger_path):
    _chronicle_index.link_events(ledger_path, "BIT-1", ["e1", "e2"])
    loaded = _chronicle_index.load_index(ledger_path)
    assert "e1" in loaded["by_bit"]["BIT-1"]["event_ids"]
    assert "BIT-1" in loaded["by_event"]["e1"]

def test_unlink_events(ledger_path):
    _chronicle_index.link_events(ledger_path, "BIT-1", ["e1", "e2"])
    _chronicle_index.unlink_events(ledger_path, "BIT-1", ["e1"])
    loaded = _chronicle_index.load_index(ledger_path)
    assert "e1" not in loaded["by_bit"]["BIT-1"]["event_ids"]
    assert "e1" not in loaded["by_event"]

def test_query_by_bit(ledger_path):
    _chronicle_index.link_events(ledger_path, "BIT-1", ["e1"])
    assert _chronicle_index.query_by_bit(ledger_path, "BIT-1") == ["e1"]

def test_query_by_event(ledger_path):
    _chronicle_index.link_events(ledger_path, "BIT-1", ["e1"])
    _chronicle_index.link_events(ledger_path, "BIT-2", ["e1"])
    assert "BIT-1" in _chronicle_index.query_by_event(ledger_path, "e1")
    assert "BIT-2" in _chronicle_index.query_by_event(ledger_path, "e1")

def test_list_entries(ledger_path):
    _chronicle_index.link_events(ledger_path, "BIT-1", ["e1"])
    _chronicle_index.link_events(ledger_path, "BIT-2", ["e2", "e3"])
    entries = _chronicle_index.list_entries(ledger_path)
    assert len(entries) == 2
    assert entries[0]["bit_name"] == "BIT-1"
    assert entries[0]["event_count"] == 1
    assert entries[1]["bit_name"] == "BIT-2"
    assert entries[1]["event_count"] == 2

def test_rebuild_from_scratch(ledger_path):
    chronicle_path = os.path.join(os.path.dirname(ledger_path), "CAUSADB_CHRONICLE.md")
    with open(chronicle_path, "w") as f:
        f.write("## BIT-R.1\nDescripción\n## BIT-R.2\n")
    
    _chronicle_index.rebuild_index(ledger_path)
    loaded = _chronicle_index.load_index(ledger_path)
    assert "BIT-R.1" in loaded["by_bit"]
    assert "BIT-R.2" in loaded["by_bit"]
    assert loaded["by_bit"]["BIT-R.1"]["description"] == "Descripción"

def test_version_mismatch(ledger_path):
    index = {"version": 999, "by_bit": {}, "by_event": {}}
    _chronicle_index.save_index(ledger_path, index)
    # Loading it should trigger rebuild
    loaded = _chronicle_index.load_index(ledger_path)
    assert loaded["version"] == 1


# ---------------------------------------------------------------------------
# GAP-02 — rebuild con ledger como autoridad + prosa (docs/plan_gaps_01_02.md)
# ---------------------------------------------------------------------------

def _append_event(ledger_path, event_type, payload, event_id=None, timestamp=None):
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    writer = LedgerWriter(ledger_path)
    kwargs = dict(event_type=EventType(event_type), ctx_id="test", source="test",
                  payload=payload)
    if event_id:
        kwargs["event_id"] = event_id
    if timestamp:
        kwargs["timestamp"] = timestamp
    entry = writer.append(CanonicalEvent(**kwargs))
    return entry["event"]["event_id"]


def test_rebuild_links_prose_event_ids_per_bit(tmp_path):
    """t14 — los `event_id` citados en prosa del bloque Referencias de cada
    BIT se enlazan (UUID o 32-hex), acotados al bloque del BIT."""
    ledger_path = str(tmp_path / "ledger.log")
    uuid_a = "6a90323f-6a90-4323-a903-23f6a90323f6"
    hex_b = "e9d0b314" + "0" * 24
    _append_event(ledger_path, "FILE_MODIFIED", {"path": "/a", "action": "create"}, event_id=uuid_a)
    _append_event(ledger_path, "FILE_MODIFIED", {"path": "/b", "action": "create"}, event_id=hex_b)

    chronicle_path = os.path.join(os.path.dirname(ledger_path), "CAUSADB_CHRONICLE.md")
    with open(chronicle_path, "w") as f:
        f.write(
            "## BIT-CHR.85 — GAP-01\n"
            "Descripción del bit.\n"
            "**Referencias:**\n"
            f"  event_id `{uuid_a}`\n"
            "\n"
            "## BIT-CHR.86 — GAP-02\n"
            "Descripción del bit.\n"
            "**Referencias:**\n"
            f"  event_id `{hex_b}`\n"
        )

    idx = _chronicle_index.rebuild_index(ledger_path)
    assert idx["by_bit"]["BIT-CHR.85"]["event_ids"] == [uuid_a]
    assert idx["by_bit"]["BIT-CHR.86"]["event_ids"] == [hex_b]
    assert idx["by_event"][uuid_a] == ["BIT-CHR.85"]
    assert idx["by_event"][hex_b] == ["BIT-CHR.86"]
    assert idx["by_bit"]["BIT-CHR.85"]["description"] == "Descripción del bit."


def test_rebuild_ignores_non_link_occurrences(tmp_path):
    """t15 — campos que CONTIENEN 'event_id' (genesis_event_id,
    parent_event_id, target_event_id, event_ids) no se enlazan; el id citado
    correctamente en otro BIT sí."""
    ledger_path = str(tmp_path / "ledger.log")
    ghost = "aaaaaaaa-0000-0000-0000-000000000000"
    _append_event(ledger_path, "FILE_MODIFIED", {"path": "/g", "action": "create"}, event_id=ghost)

    chronicle_path = os.path.join(os.path.dirname(ledger_path), "CAUSADB_CHRONICLE.md")
    with open(chronicle_path, "w") as f:
        f.write(
            "## BIT-X\n"
            "Desc.\n"
            "**Referencias:**\n"
            f"  genesis_event_id `{ghost}`\n"
            f"  parent_event_id `{ghost}`\n"
            f"  target_event_id `{ghost}`\n"
            "  event_ids: [uno, dos]\n"
            "## BIT-Y\n"
            "Desc.\n"
            "**Referencias:**\n"
            f"  event_id `{ghost}`\n"
        )
    idx = _chronicle_index.rebuild_index(ledger_path)
    assert idx["by_bit"]["BIT-X"]["event_ids"] == []
    assert idx["by_bit"]["BIT-Y"]["event_ids"] == [ghost]


def test_rebuild_ledger_is_authority_for_links(tmp_path):
    """t16 — CHRONICLE_ENTRY/GOVERNANCE_DECISION con payload.bit_id enlazan
    por ledger (autoridad); el rebuild no pisa esos links."""
    ledger_path = str(tmp_path / "ledger.log")
    eid_a = _append_event(ledger_path, "CHRONICLE_ENTRY", {
        "bit_id": "BIT-A", "title": "t", "date": "2026-08-13", "maker": "m",
        "checker": "c", "summary": "s", "files_touched": [],
    })
    eid_b = _append_event(ledger_path, "GOVERNANCE_DECISION", {
        "reasoning": "r", "impact": "low", "decision_type": "tactical",
        "origin": "agent", "bit_id": "BIT-B",
    })
    # BIT-A/BIT-B NO existen en el markdown → solo el ledger los conoce
    chronicle_path = os.path.join(os.path.dirname(ledger_path), "CAUSADB_CHRONICLE.md")
    with open(chronicle_path, "w") as f:
        f.write("## BIT-C\nDesc.\n**Referencias:**\n")

    idx = _chronicle_index.rebuild_index(ledger_path)
    assert eid_a in idx["by_bit"]["BIT-A"]["event_ids"]
    assert eid_b in idx["by_bit"]["BIT-B"]["event_ids"]
    assert idx["by_bit"]["BIT-C"]["event_ids"] == []
    # los links del ledger sobreviven a un rebuild posterior
    idx2 = _chronicle_index.rebuild_index(ledger_path)
    assert idx2["by_bit"]["BIT-A"]["event_ids"] == idx["by_bit"]["BIT-A"]["event_ids"]
    assert idx2["by_bit"]["BIT-B"]["event_ids"] == idx["by_bit"]["BIT-B"]["event_ids"]


def test_rebuild_fail_closed_without_chronicle(tmp_path):
    """t20 — rebuild sin chronicle en ninguna ubicación → FileNotFoundError
    (FAIL-CLOSED). load_index lo captura y devuelve índice vacío (no crashea)."""
    ledger_path = str(tmp_path / "ledger.log")
    with open(ledger_path, "w") as f:
        f.write("dummy")
    with pytest.raises(FileNotFoundError):
        _chronicle_index.rebuild_index(ledger_path)
    # load_index (auto-recovery) nunca crashea → índice vacío
    loaded = _chronicle_index.load_index(ledger_path)
    assert loaded == {"version": 1, "by_bit": {}, "by_event": {}}


def test_reconstruct_state_until_event_id(tmp_path):
    """t19 — reconstruct_state(until_event_id=...) aplica solo el prefijo de
    append (orden del ledger), excluyendo eventos posteriores aunque su
    timestamp sea anterior (D1: append-order, no time-order)."""
    from causadb._replay_engine import ReplayEngine
    ledger_path = str(tmp_path / "ledger.log")
    eid_a = _append_event(ledger_path, "FILE_MODIFIED", {"path": "/A", "action": "create"},
                          timestamp="2026-08-13T10:00:00Z")
    eid_c = _append_event(ledger_path, "FILE_MODIFIED", {"path": "/C", "action": "create"},
                          timestamp="2026-08-13T10:00:02Z")
    # B se apendea DESPUÉS de C pero con timestamp ANTERIOR (backdated)
    _append_event(ledger_path, "FILE_MODIFIED", {"path": "/B", "action": "create"},
                  timestamp="2026-08-13T10:00:01Z")

    state = ReplayEngine(ledger_path).reconstruct_state(until_event_id=eid_c)
    paths = [f["path"] for f in state["files_modified"]]
    assert paths == ["/A", "/C"], f"B debe quedar excluido (append-order), got {paths}"
    assert state["events_applied"] == 2


# ---------------------------------------------------------------------------
# Capa B — chronicle list --unlinked (plan_memoria_visible.md)
# ---------------------------------------------------------------------------

def _make_entries(ledger_path, linked_bits, unlinked_bits):
    """Crea un índice con BITs enlazados y sin enlazar."""
    for bit in linked_bits:
        _chronicle_index.link_events(ledger_path, bit, [f"e-{bit}"])
    for bit in unlinked_bits:
        _chronicle_index.link_events(ledger_path, bit, [])
    return ledger_path


def _run_list(args):
    from causadb.cli._cmd_chronicle import cmd_chronicle
    return cmd_chronicle(args)


class _Args:
    pass


def test_chronicle_list_unlinked_filters_zeros(tmp_path):
    """Capa B — --unlinked devuelve solo los BITs con event_count == 0."""
    ledger_path = str(tmp_path / "ledger.log")
    with open(ledger_path, "w") as f:
        f.write("dummy")
    _make_entries(ledger_path, ["BIT-L1", "BIT-L2"], ["BIT-U1", "BIT-U2"])

    args = _Args()
    args.action = "list"
    args.ledger = ledger_path
    args.unlinked = True
    code, out = _run_list(args)
    assert code == 0
    entries = json.loads(out)
    assert [e["bit_name"] for e in entries] == ["BIT-U1", "BIT-U2"]
    assert all(e["event_count"] == 0 for e in entries)


def test_chronicle_list_without_unlinked_returns_all(tmp_path):
    """Capa B — sin --unlinked, list devuelve todo (regresión del default)."""
    ledger_path = str(tmp_path / "ledger.log")
    with open(ledger_path, "w") as f:
        f.write("dummy")
    _make_entries(ledger_path, ["BIT-L1"], ["BIT-U1"])

    args = _Args()
    args.action = "list"
    args.ledger = ledger_path
    args.unlinked = False
    code, out = _run_list(args)
    assert code == 0
    entries = json.loads(out)
    assert len(entries) == 2


def test_chronicle_list_unlinked_empty_index(tmp_path):
    """Capa B — con índice vacío, --unlinked no crashea y devuelve []."""
    ledger_path = str(tmp_path / "ledger.log")
    with open(ledger_path, "w") as f:
        f.write("dummy")

    args = _Args()
    args.action = "list"
    args.ledger = ledger_path
    args.unlinked = True
    code, out = _run_list(args)
    assert code == 0
    assert json.loads(out) == []
