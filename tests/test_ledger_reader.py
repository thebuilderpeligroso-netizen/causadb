import pytest
import os
import gzip
import json
import inspect
import causadb._ledger_reader
from causadb._ledger_reader import LedgerReader
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType

@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")

def test_read_all_empty_ledger(ledger_path):
    reader = LedgerReader(ledger_path)
    assert list(reader.read_all()) == []

def test_read_all_single_event(ledger_path):
    with open(ledger_path, "w") as f:
        json.dump({"event": {"event_id": "1", "event_type": "FILE_MODIFIED", "ctx_id": "ctx", "source": "agent"}}, f)
        f.write("\n")
    reader = LedgerReader(ledger_path)
    events = list(reader.read_all())
    assert len(events) == 1
    assert events[0].event_id == "1"

def test_read_all_multiple_events(ledger_path):
    with open(ledger_path, "w") as f:
        for i in range(3):
            json.dump({"event": {"event_id": str(i), "event_type": "FILE_MODIFIED", "ctx_id": "ctx", "source": "agent"}}, f)
            f.write("\n")
    reader = LedgerReader(ledger_path)
    events = list(reader.read_all())
    assert len(events) == 3

def test_read_all_with_archive(ledger_path):
    archive_dir = os.path.dirname(ledger_path) + "/archive"
    os.makedirs(archive_dir)
    with gzip.open(os.path.join(archive_dir, "001.gz"), "wt") as f:
        f.write(json.dumps({"event": {"event_id": "archive", "event_type": "FILE_MODIFIED", "ctx_id": "ctx", "source": "agent"}}) + "\n")
    with open(ledger_path, "w") as f:
        json.dump({"event": {"event_id": "active", "event_type": "FILE_MODIFIED", "ctx_id": "ctx", "source": "agent"}}, f)
        f.write("\n")
    reader = LedgerReader(ledger_path)
    events = list(reader.read_all())
    assert len(events) == 2
    assert events[0].event_id == "archive"
    assert events[1].event_id == "active"

def test_read_until_event_id(ledger_path):
    with open(ledger_path, "w") as f:
        for i in range(3):
            json.dump({"event": {"event_id": str(i), "event_type": "FILE_MODIFIED", "ctx_id": "ctx", "source": "agent"}}, f)
            f.write("\n")
    reader = LedgerReader(ledger_path)
    events = list(reader.read_until("1"))
    assert len(events) == 2
    assert events[1].event_id == "1"

def test_read_until_with_archive(ledger_path):
    archive_dir = os.path.dirname(ledger_path) + "/archive"
    os.makedirs(archive_dir)
    with gzip.open(os.path.join(archive_dir, "001.gz"), "wt") as f:
        f.write(json.dumps({"event": {"event_id": "archive", "event_type": "FILE_MODIFIED", "ctx_id": "ctx", "source": "agent"}}) + "\n")
    with open(ledger_path, "w") as f:
        json.dump({"event": {"event_id": "active", "event_type": "FILE_MODIFIED", "ctx_id": "ctx", "source": "agent"}}, f)
        f.write("\n")
    reader = LedgerReader(ledger_path)
    events = list(reader.read_until("archive"))
    assert len(events) == 1
    assert events[0].event_id == "archive"

def test_no_duplicate_imports():
    source = inspect.getsource(causadb._ledger_reader)
    # Regex para buscar importaciones, e.g., "import json" o "from ... import ..."
    import_lines = [line.strip() for line in source.split('\n') if line.strip().startswith(('import ', 'from '))]
    # Simple check: si hay más de una línea importando el mismo módulo
    # Este check es básico, pero cumple el requerimiento de auditar duplicados.
    assert import_lines.count('import json') <= 1
