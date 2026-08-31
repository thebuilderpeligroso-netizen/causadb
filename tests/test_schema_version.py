import json
import os
import pytest
from causadb._schema_version import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    MIGRATION_PIPELINE,
    migrate_v0_0_to_v0_1,
    migrate_ledger,
)


def test_current_version():
    assert CURRENT_SCHEMA_VERSION == "0.2.0"


def test_migrate_v0_0_to_v0_1_adds_parent_event_id():
    event = {"event_id": "e1", "event_type": "FILE_MODIFIED", "ctx_id": "ctx", "source": "opencode:agent1"}
    migrated = migrate_v0_0_to_v0_1(event)
    assert migrated["parent_event_id"] is None
    # Input not mutated (idempotency contract — non-mutation of input)
    assert "parent_event_id" not in event


def test_migrate_v0_0_to_v0_1_adds_source_type():
    event = {"event_id": "e1", "event_type": "FILE_MODIFIED", "ctx_id": "ctx", "source": "opencode:agent1"}
    migrated = migrate_v0_0_to_v0_1(event)
    assert migrated["source_type"] == "agent"
    assert "source_type" not in event


def test_migrate_v0_0_to_v0_1_adds_schema_version():
    event = {"event_id": "e1", "event_type": "FILE_MODIFIED", "ctx_id": "ctx", "source": "opencode:agent1"}
    migrated = migrate_v0_0_to_v0_1(event)
    assert migrated["schema_version"] == "0.1.0"
    assert "schema_version" not in event


def test_migrate_idempotent():
    # Anti-teatro: call migrate twice, assert outputs equal AND input not
    # mutated. A stub that mutates input or returns random dicts fails.
    event = {"event_id": "e1", "event_type": "FILE_MODIFIED", "ctx_id": "ctx", "source": "opencode:agent1"}
    first = migrate_v0_0_to_v0_1(event)
    second = migrate_v0_0_to_v0_1(first)
    assert first == second
    # Input dict must still NOT have schema_version (non-mutation)
    assert "schema_version" not in event
    assert "parent_event_id" not in event
    assert "source_type" not in event


def test_migrate_v0_0_to_v0_1_adds_metadata_priority():
    # If metadata exists but lacks priority, add priority=None.
    event = {
        "event_id": "e1",
        "event_type": "FILE_MODIFIED",
        "ctx_id": "ctx",
        "source": "opencode:agent1",
        "metadata": {"trace_id": "t", "session_id": "s"},
    }
    migrated = migrate_v0_0_to_v0_1(event)
    assert migrated["metadata"]["priority"] is None
    # Input not mutated
    assert "priority" not in event["metadata"]


def test_migrate_v0_0_to_v0_1_preserves_existing_metadata_priority():
    event = {
        "event_id": "e1",
        "event_type": "FILE_MODIFIED",
        "ctx_id": "ctx",
        "source": "opencode:agent1",
        "metadata": {"trace_id": "t", "session_id": "s", "priority": "high"},
    }
    migrated = migrate_v0_0_to_v0_1(event)
    assert migrated["metadata"]["priority"] == "high"


def test_migration_logs_to_chronicle(tmp_path):
    # Anti-teatro: if migrate_ledger is mutated to skip the chronicle
    # append, this test MUST fail because the BIT-entry text won't appear.
    ledger_path = str(tmp_path / "ledger.log")
    chronicle_path = str(tmp_path / "chronicle.md")

    # Build a v0.0.0 ledger: entries with events lacking schema_version
    entries = []
    for i in range(3):
        event = {
            "event_id": f"e{i}",
            "event_type": "FILE_MODIFIED",
            "timestamp": "2026-07-21T00:00:00Z",
            "ctx_id": "ctx",
            "source": "opencode:agent1",
            "payload": {},
            "metadata": None,
        }
        entry = {"event": event, "prev_hash": "GENESIS" if i == 0 else f"h{i-1}", "hash": f"h{i}"}
        entries.append(entry)

    with open(ledger_path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry, sort_keys=True) + "\n")

    # Chronicle starts empty
    assert not os.path.exists(chronicle_path) or os.path.getsize(chronicle_path) == 0

    n = migrate_ledger(ledger_path, chronicle_path)
    assert n == 3

    # Ledger was migrated in place
    with open(ledger_path, "r") as f:
        lines = f.readlines()
    assert len(lines) == 3
    for line in lines:
        entry = json.loads(line)
        assert entry["event"]["schema_version"] == "0.2.0"
        assert entry["event"]["parent_event_id"] is None
        assert entry["event"]["source_type"] == "agent"

    # Chronicle has the BIT-entry at the end
    with open(chronicle_path, "r") as f:
        chronicle_text = f.read()
    assert "BIT-MIGRATE" in chronicle_text
    assert "older" in chronicle_text
    assert "0.2.0" in chronicle_text
    assert "Events migrated:" in chronicle_text
    # Verify the BIT-entry block appears at the tail of the file
    last_block = chronicle_text[chronicle_text.rfind("## BIT-MIGRATE"):]
    assert "**From:** older" in last_block
    assert "**To:** 0.2.0" in last_block
    assert "**Events migrated:** 3" in last_block
    # The BIT-entry must be the last thing in the chronicle file
    assert chronicle_text.rstrip().endswith("**Events migrated:** 3")


def test_migrate_ledger_idempotent_on_already_migrated(tmp_path):
    # If the ledger is already at 0.2.0, migrate_ledger should migrate 0
    # events (no-op) but still append a BIT-entry.
    ledger_path = str(tmp_path / "ledger.log")
    chronicle_path = str(tmp_path / "chronicle.md")

    event = {
        "event_id": "e1",
        "event_type": "FILE_MODIFIED",
        "timestamp": "2026-07-21T00:00:00Z",
        "ctx_id": "ctx",
        "source": "opencode:agent1",
        "parent_event_id": None,
        "source_type": "agent",
        "schema_version": "0.2.0",
        "payload": {},
        "metadata": None,
    }
    entry = {"event": event, "prev_hash": "GENESIS", "hash": "h1"}
    with open(ledger_path, "w") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")

    n = migrate_ledger(ledger_path, chronicle_path)
    assert n == 0


def test_migrations_registry_has_v0_0_to_v0_1():
    # Sanity: the MIGRATIONS dict has the expected key.
    assert ("0.0.0", "0.1.0") in MIGRATIONS
    assert MIGRATIONS[("0.0.0", "0.1.0")] is migrate_v0_0_to_v0_1


# --- F.2.3: Wire format event_id/timestamp históricos + schema version 0.2.0 ---

def test_migrate_v0_1_to_v0_2_updates_version():
    from causadb._schema_version import migrate_v0_1_to_v0_2
    event = {"event_id": "e1", "event_type": "FILE_MODIFIED", "ctx_id": "ctx", "source": "opencode:agent1",
             "schema_version": "0.1.0"}
    migrated = migrate_v0_1_to_v0_2(event)
    assert migrated["schema_version"] == "0.2.0"
    assert event["schema_version"] == "0.1.0"  # input not mutated

def test_migrate_v0_1_to_v0_2_idempotent():
    from causadb._schema_version import migrate_v0_1_to_v0_2
    event = {"event_id": "e1", "event_type": "FILE_MODIFIED", "ctx_id": "ctx", "source": "opencode:agent1",
             "schema_version": "0.1.0"}
    first = migrate_v0_1_to_v0_2(event)
    second = migrate_v0_1_to_v0_2(first)
    assert first == second
    assert event["schema_version"] == "0.1.0"  # input not mutated
