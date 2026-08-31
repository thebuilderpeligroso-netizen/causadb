import pytest
import os
import json
import hashlib
from causadb._ledger_validator import LedgerValidator, ReplayIntegrityError

@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")

def test_validate_empty_ledger_valid(ledger_path):
    open(ledger_path, "a").close()
    validator = LedgerValidator(ledger_path)
    assert validator.validate_chain().is_valid

def test_validate_corruption_detected(ledger_path):
    with open(ledger_path, "w") as f:
        f.write("invalid json\n")
    validator = LedgerValidator(ledger_path)
    result = validator.validate_chain()
    assert not result.is_valid
    assert result.failure_type == "CORRUPTION"

def test_validate_continuity_break(ledger_path):
    with open(ledger_path, "w") as f:
        json.dump({"prev_hash": "WRONG", "hash": "h1"}, f)
        f.write("\n")
    validator = LedgerValidator(ledger_path)
    result = validator.validate_chain()
    assert not result.is_valid
    assert result.failure_type == "CONTINUITY_BREAK"

def test_validate_hash_mismatch(ledger_path):
    with open(ledger_path, "w") as f:
        json.dump({"event": {}, "prev_hash": "GENESIS", "hash": "WRONG"}, f)
        f.write("\n")
    validator = LedgerValidator(ledger_path)
    result = validator.validate_chain()
    assert not result.is_valid
    assert result.failure_type == "HASH_MISMATCH"

def test_validate_valid_chain(ledger_path):
    prev = "GENESIS"
    with open(ledger_path, "w") as f:
        for _ in range(5):
            event = {}
            event_json = json.dumps(event, sort_keys=True)
            h = hashlib.sha256((event_json + prev).encode()).hexdigest()
            json.dump({"event": event, "prev_hash": prev, "hash": h}, f)
            f.write("\n")
            prev = h
    validator = LedgerValidator(ledger_path)
    assert validator.validate_chain().is_valid

def test_validate_or_raise_raises_on_corrupt(ledger_path):
    with open(ledger_path, "w") as f:
        f.write("invalid json\n")
    validator = LedgerValidator(ledger_path)
    with pytest.raises(ReplayIntegrityError):
        validator.validate_or_raise()

def test_validate_or_raise_ok_on_valid(ledger_path):
    open(ledger_path, "a").close()
    validator = LedgerValidator(ledger_path)
    validator.validate_or_raise()
import gzip

def test_validate_with_archive(ledger_path):
    archive_dir = os.path.dirname(ledger_path) + "/archive"
    os.makedirs(archive_dir)
    
    # Crear archivo archive con dos eventos válidos
    archive_path = os.path.join(archive_dir, "001.gz")
    prev_hash = "GENESIS"
    events = []
    for i in range(2):
        event = {"event_id": f"a{i}"}
        event_json = json.dumps(event, sort_keys=True)
        h = hashlib.sha256((event_json + prev_hash).encode()).hexdigest()
        events.append({"event": event, "prev_hash": prev_hash, "hash": h})
        prev_hash = h
        
    with gzip.open(archive_path, "wt") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    
    # Crear ledger activo continuando desde el último hash del archive
    with open(ledger_path, "w") as f:
        event = {"event_id": "active1"}
        event_json = json.dumps(event, sort_keys=True)
        h = hashlib.sha256((event_json + prev_hash).encode()).hexdigest()
        json.dump({"event": event, "prev_hash": prev_hash, "hash": h}, f)
        f.write("\n")
        
    validator = LedgerValidator(ledger_path)
    assert validator.validate_chain().is_valid
    
    # Romper continuidad
    with open(ledger_path, "w") as f:
        json.dump({"event": event, "prev_hash": "WRONG", "hash": h}, f)
        f.write("\n")
    assert not validator.validate_chain().is_valid
    assert validator.validate_chain().failure_type == "CONTINUITY_BREAK"
