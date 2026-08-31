import pytest
import os
import causadb._ledger_reader
from causadb._drift_detector import check_hash_chain, check_replay_consistency, check_causal_drift, load_events
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
import json

@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")

def test_check_hash_chain_valid(ledger_path):
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"))
    report = check_hash_chain(ledger_path)
    assert report.is_valid

def test_check_hash_chain_empty(ledger_path):
    open(ledger_path, "a").close()
    report = check_hash_chain(ledger_path)
    assert report.is_valid

def test_check_hash_chain_broken(ledger_path):
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"))
    with open(ledger_path, "r+") as f:
        content = f.read()
        f.seek(0)
        f.write(content.replace("hash", "hosh"))
    report = check_hash_chain(ledger_path)
    assert not report.is_valid

def test_check_replay_consistency_valid(ledger_path):
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"))
    report = check_replay_consistency(ledger_path)
    assert report.is_valid

def test_check_replay_consistency_corrupt(ledger_path):
    """Articulo IX: el test debe distinguir 'NameError capturado' de
    'replay consistency fallida por razón legitima'. El bug del Hallazgo 1
    hacia que NameError de 'os' se guardara como summary del DriftReport."""
    # Ledger con JSON inválido -> replay levanta 0 events
    with open(ledger_path, "w") as f:
        f.write("{invalido}\n")
    report = check_replay_consistency(ledger_path)
    assert not report.is_valid
    # Summary específica: debe mencionar corrupción / consistency, no 'os'
    assert "CORRUPTION" in report.summary or "REPLAY" in report.summary or "JSON" in report.summary, (
        f"summary inesperada: {report.summary}"
    )
    assert "name 'os'" not in report.summary, (
        f"BUG: NameError enmascarado como replay inconsistency. summary={report.summary}"
    )

def test_check_causal_drift_no_orphans(ledger_path):
    writer = LedgerWriter(ledger_path)
    e1 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent")
    writer.append(e1)
    e2 = CanonicalEvent(event_type=EventType.COMMAND_RUN, ctx_id="ctx", source="opencode:agent", parent_event_id=e1.event_id)
    writer.append(e2)
    report = check_causal_drift(ledger_path)
    assert report.is_valid

def test_check_causal_drift_orphan(ledger_path):
    writer = LedgerWriter(ledger_path)
    e2 = CanonicalEvent(event_type=EventType.COMMAND_RUN, ctx_id="ctx", source="opencode:agent", parent_event_id="nonexistent")
    writer.append(e2)
    report = check_causal_drift(ledger_path)
    assert not report.is_valid

def test_check_causal_drift_genesis_ok(ledger_path):
    writer = LedgerWriter(ledger_path)
    e1 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent")
    writer.append(e1)
    report = check_causal_drift(ledger_path)
    assert report.is_valid

def test_load_events_uses_ledger_reader(ledger_path, mocker):
    mocker.patch("causadb._ledger_reader.LedgerReader.read_all_entries", return_value=[])
    load_events(ledger_path)
    assert causadb._ledger_reader.LedgerReader.read_all_entries.called

def test_drift_detector_no_cortex_runtime_import():
    """Anti-regresión: DriftDetector no debe importar cortex_runtime."""
    import causadb._drift_detector as dd
    import inspect
    src = inspect.getsource(dd)
    assert "from cortex_runtime" not in src
    assert "from cortex_runtime_node" not in src
    assert "working_set.resolver" not in src
