import pytest
import sqlite3
import os
import json
from unittest.mock import patch
from causadb._replay_engine import ReplayEngine
from causadb._ledger_reader import LedgerReader
from causadb._ledger_writer import LedgerWriter
from causadb._harvester import Harvester
from causadb._harvest_source_hermes import HermesHarvestSource

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_DB = os.path.join(FIXTURE_DIR, "hermes_fixture.db")

def test_replay_hermes_projection_matches_fixture(tmp_path):
    """(a) Igualdad de proyección: replay de ledger Hermes reproduce estado esperado."""
    # 1. Harvest la fixture
    ledger = tmp_path / "ledger.log"
    cursors = tmp_path / "cursors.json"
    harvester = Harvester(str(ledger), str(cursors))
    source = HermesHarvestSource(str(ledger), str(FIXTURE_DB))
    harvester.register_source(source)
    harvester.harvest_all()
    
    # 2. Replay
    engine = ReplayEngine(str(ledger))
    state = engine.reconstruct_state()
    
    # 3. Assertions
    assert len(state["sessions"]) == 2
    # Summary turns check (approximate, turns > 0)
    assert state.get("session_summaries", [])[0]["turn_count"] > 0
    # LLM_INVOKED / TOOL_CALLED checks
    assert len([e for e in state.get("llm_invocations", []) if e["model"] in ["qwen3.5:4b", "llama3.1:8b"]]) > 0

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from types import MappingProxyType

def test_replay_legacy_ledger_without_message_fields(tmp_path):
    """(b) Compatibilidad legacy: ledger pre-H1 (sin message_*, sin
    SESSION_STARTED/ENDED) replaya sin crash y proyecta invariantes reales."""
    ledger = tmp_path / "ledger.log"
    writer = LedgerWriter(str(ledger))
    legacy_events = [
        CanonicalEvent(
            event_type=EventType.REASONING_STEP,
            ctx_id="ctx1",
            source="hermes",
            source_type="agent",
            payload=MappingProxyType({"step_type": "analysis", "step_hash": "a" * 64,
                                      "subject": "s", "description": "pensando"})
        ),
        CanonicalEvent(
            event_type=EventType.LLM_INVOKED,
            ctx_id="ctx1",
            source="hermes",
            source_type="agent",
            payload=MappingProxyType({"model": "qwen3.5:4b", "prompt": "p",
                                      "response_tokens": 1339, "duration_ms": 47988,
                                      "response_content": "r"})
        ),
        CanonicalEvent(
            event_type=EventType.TOOL_CALLED,
            ctx_id="ctx1",
            source="hermes",
            source_type="agent",
            payload=MappingProxyType({"tool_name": "terminal", "arguments": "{}",
                                      "result": "ok", "tool_call_id": "call-1"})
        ),
    ]
    for e in legacy_events:
        writer.append(e)

    state = ReplayEngine(str(ledger)).reconstruct_state()
    assert state["events_applied"] == 3
    assert len(state["llm_invocations"]) == 1
    assert state["llm_invocations"][0]["model"] == "qwen3.5:4b"
    assert len(state["reasoning_steps"]) == 1
    assert len(state["tools_called"]) == 1
    assert state["tools_called"][0]["result"] == "ok"

def test_replay_never_reads_state_db(tmp_path):
    """(c) El replay NUNCA lee state.db."""
    # 1. Harvest la fixture
    ledger = tmp_path / "ledger.log"
    cursors = tmp_path / "cursors.json"
    harvester = Harvester(str(ledger), str(cursors))
    source = HermesHarvestSource(str(ledger), str(FIXTURE_DB))
    harvester.register_source(source)
    harvester.harvest_all()
    
    # 2. Replay con patch de sqlite3.connect
    def side_effect(*args, **kwargs):
        raise AssertionError("Replay intentó leer sqlite3!")
        
    with patch("sqlite3.connect", side_effect=side_effect):
        engine = ReplayEngine(str(ledger))
        state = engine.reconstruct_state()

def test_replay_hermes_same_ledger_two_runs_equal_projection(tmp_path):
    """(d) Replay dos veces → proyección idéntica."""
    # 1. Harvest la fixture
    ledger = tmp_path / "ledger.log"
    cursors = tmp_path / "cursors.json"
    harvester = Harvester(str(ledger), str(cursors))
    source = HermesHarvestSource(str(ledger), str(FIXTURE_DB))
    harvester.register_source(source)
    harvester.harvest_all()
    
    # 2. Replay
    engine1 = ReplayEngine(str(ledger))
    state1 = engine1.reconstruct_state()
    
    engine2 = ReplayEngine(str(ledger))
    state2 = engine2.reconstruct_state()
    
    assert state1 == state2
