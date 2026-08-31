import pytest
from causadb._harvest_source_hermes import HermesHarvestSource
from causadb._harvester import Harvester
import sqlite3
import os

def _hermes_db(tmp_path):
    """Creates a minimal Hermes SQLite store (v22 schema)."""
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    # Simplified schema for testing the lossless fields.
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, started_at REAL, ended_at REAL, input_tokens INTEGER, output_tokens INTEGER)")
    # Schema matching the required 21 columns
    con.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
            tool_calls TEXT, tool_name TEXT, effect_disposition TEXT,
            timestamp REAL, token_count INTEGER, finish_reason TEXT,
            reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
            codex_reasoning_items TEXT, codex_message_items TEXT,
            platform_message_id TEXT, observed INTEGER, active INTEGER,
            compacted INTEGER, api_content TEXT
        )
    """)
    # Add data: Assistant with reasoning, tool_calls, and finish_reason
    con.execute("INSERT INTO sessions VALUES ('s1', 'qwen', 1000.0, 2000.0, 10, 20)")
    con.execute("""
        INSERT INTO messages 
        (id, session_id, role, content, reasoning_content, finish_reason, 
         effect_disposition, token_count, observed, active, compacted, api_content, timestamp)
        VALUES 
        (1, 's1', 'assistant', 'content', 'reasoning', 'stop', 
         'success', 5, 0, 1, 0, 'api_content', 1500.0)
    """)
    con.commit()
    con.close()
    return db_path

def test_harvest_hermes_lossless_fields(tmp_path):
    db = _hermes_db(tmp_path)
    ledger = tmp_path / "ledger.log"
    # Register and harvest all events
    harvester = Harvester(str(ledger), str(tmp_path / "cursors.json"))
    source = HermesHarvestSource(str(ledger), str(db))
    harvester.register_source(source)
    events = list(source.harvest(None))
    
    # Check for lossless fields in the assistant event (LLM_INVOKED)
    llm_events = [e for e in events if e["type"] == "LLM_INVOKED"]
    assert len(llm_events) > 0
    event = llm_events[0]
    
    assert event["message_id"] == 1
    assert event["message_role"] == "assistant"
    assert event["message_finish_reason"] == "stop"
    assert event["message_effect_disposition"] == "success"
    assert event["message_token_count"] == 5
    assert event["message_observed"] == 0
    assert event["message_active"] == 1
    assert event["message_compacted"] == 0
    assert event["message_api_content"] == "api_content"
    
    # Verify reasoning not duplicated
    assert "message_reasoning" not in event
