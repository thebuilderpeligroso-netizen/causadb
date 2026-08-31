import json
import os
import shutil

from causadb._config import CausaDBConfig
from causadb._harvester import Harvester
from causadb._harvest_source_opencode import OpenCodeHarvestSource
from causadb._ledger_writer import LedgerWriter
from causadb._replay_engine import ReplayEngine


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "opencode_fixture.db")


def _source(tmp_path, ledger):
    db = tmp_path / "opencode.db"
    shutil.copy(FIXTURE, db)
    return OpenCodeHarvestSource(str(ledger), str(db))


def test_opencode_first_event_gets_exact_conversation_ref(tmp_path):
    ledger = tmp_path / "ledger.log"
    h = Harvester(str(ledger), str(tmp_path / "cursor.json"))
    h.register_source(_source(tmp_path, ledger))

    assert h.harvest_all()["opencode"] == 2
    entries = [json.loads(line)["event"] for line in ledger.read_text().splitlines()]
    refs = [event["payload"].get("conversation_ref") for event in entries]
    assert refs[0] == {
        "provider": "opencode",
        "native_id": "ses_05f83630bffe1Rzk65A7C0KPys",
        "locator_kind": "sqlite",
        "locator": "opencode_default",
        "resolver": "opencode",
        "confidence": "verified",
        "content_class": "transcript_complete",
        "privacy_class": "raw_sensitive",
    }
    assert refs[1] is None
    assert entries[0]["payload"]["session_id"] == entries[1]["payload"]["session_id"]
    assert entries[0]["event_type"] in {"REASONING_STEP", "TOOL_CALLED", "LLM_INVOKED"}


def test_opencode_without_session_id_does_not_invent_reference(tmp_path):
    h = Harvester(str(tmp_path / "ledger.log"))
    event = h._event_from_raw("opencode", {"type": "TOOL_CALLED", "timestamp": "2026-01-01T00:00:00Z"})
    assert "conversation_ref" not in event.payload
    assert "session_id" not in event.payload


def test_opencode_deduplicates_reference_across_harvester_restarts(tmp_path):
    ledger = tmp_path / "ledger.log"
    cursor_path = tmp_path / "cursor.json"
    source = _source(tmp_path, ledger)

    first = Harvester(str(ledger), str(cursor_path))
    first.register_source(source)
    assert first.harvest_all()["opencode"] == 2

    second = Harvester(str(ledger), str(cursor_path))
    second.register_source(source)
    assert second.harvest_all()["opencode"] == 0

    cursor = json.loads(cursor_path.read_text())
    assert cursor["agent:opencode"]["conversation_ref_sessions"] == [
        "ses_05f83630bffe1Rzk65A7C0KPys"
    ]


def test_old_and_new_payloads_replay_and_blob_preserve_reference(tmp_path):
    ledger = tmp_path / "ledger.log"
    config = CausaDBConfig(ledger_path=str(ledger), blob_store_enabled=True, blob_store_threshold=1)
    writer = LedgerWriter(str(ledger), config=config)
    ref = {
        "provider": "opencode", "native_id": "ses-x", "locator_kind": "sqlite",
        "locator": "opencode_default", "resolver": "opencode", "confidence": "verified",
        "content_class": "transcript_complete", "privacy_class": "raw_sensitive",
    }
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    writer.append(CanonicalEvent(event_type=EventType.TOOL_CALLED, ctx_id="ctx", source="harvester:opencode",
                                 payload={"session_id": "ses-x", "conversation_ref": ref, "result": "chat stays out"}))
    # A legacy event without the optional key remains valid.
    writer.append(CanonicalEvent(event_type=EventType.TOOL_CALLED, ctx_id="ctx", source="harvester:opencode",
                                 payload={"session_id": "ses-old", "tool_name": "x"}))
    state = ReplayEngine(str(ledger)).reconstruct_state()
    assert state["events_applied"] == 2
    raw = json.loads(ledger.read_text().splitlines()[0])["event"]["payload"]
    assert "$blob" in raw
    from causadb._blob_store import BlobStore
    blob = BlobStore(str(tmp_path / "blobs")).get(raw["$blob"])
    assert blob["conversation_ref"] == ref
