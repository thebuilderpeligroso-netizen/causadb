import sqlite3
import pytest
import os
from causadb._drift_detector import check_hermes_schema_drift

@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test.db"
    con = sqlite3.connect(db_path)
    # Crear schema v22 completo
    con.executescript("""
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
        tool_calls TEXT, tool_name TEXT, effect_disposition TEXT,
        timestamp REAL, token_count INTEGER, finish_reason TEXT,
        reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
        codex_reasoning_items TEXT, codex_message_items TEXT,
        platform_message_id TEXT, observed INTEGER, active INTEGER,
        compacted INTEGER, api_content TEXT
    );
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY, source TEXT, user_id TEXT, session_key TEXT,
        chat_id TEXT, chat_type TEXT, thread_id TEXT, display_name TEXT,
        origin_json TEXT, expiry_finalized INTEGER, model TEXT,
        model_config TEXT, system_prompt TEXT, parent_session_id TEXT,
        started_at REAL, ended_at REAL, end_reason TEXT, message_count INTEGER,
        tool_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
        cache_read_tokens INTEGER, cache_write_tokens INTEGER,
        reasoning_tokens INTEGER, cwd TEXT, git_branch TEXT,
        git_repo_root TEXT, billing_provider TEXT, billing_base_url TEXT,
        billing_mode TEXT, estimated_cost_usd REAL, actual_cost_usd REAL,
        cost_status TEXT, cost_source TEXT, pricing_version TEXT,
        title TEXT, api_call_count INTEGER, handoff_state TEXT,
        handoff_platform TEXT, handoff_error TEXT,
        compression_failure_cooldown_until REAL, compression_failure_error TEXT,
        compression_fallback_streak INTEGER, profile_name TEXT,
        rewind_count INTEGER, archived INTEGER
    );
    """)
    con.commit()
    con.close()
    return str(db_path)

def test_drift_detector_valid(temp_db):
    report = check_hermes_schema_drift(temp_db)
    assert report.is_valid is True
    assert report.summary == "NO_SCHEMA_DRIFT"

def test_drift_detector_missing_column(tmp_path):
    db_path = tmp_path / "missing.db"
    con = sqlite3.connect(db_path)
    # Crear schema v22 sin 'effect_disposition' en messages
    con.executescript("""
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
        tool_calls TEXT, tool_name TEXT, 
        timestamp REAL, token_count INTEGER, finish_reason TEXT,
        reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
        codex_reasoning_items TEXT, codex_message_items TEXT,
        platform_message_id TEXT, observed INTEGER, active INTEGER,
        compacted INTEGER, api_content TEXT
    );
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY, source TEXT, user_id TEXT, session_key TEXT,
        chat_id TEXT, chat_type TEXT, thread_id TEXT, display_name TEXT,
        origin_json TEXT, expiry_finalized INTEGER, model TEXT,
        model_config TEXT, system_prompt TEXT, parent_session_id TEXT,
        started_at REAL, ended_at REAL, end_reason TEXT, message_count INTEGER,
        tool_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
        cache_read_tokens INTEGER, cache_write_tokens INTEGER,
        reasoning_tokens INTEGER, cwd TEXT, git_branch TEXT,
        git_repo_root TEXT, billing_provider TEXT, billing_base_url TEXT,
        billing_mode TEXT, estimated_cost_usd REAL, actual_cost_usd REAL,
        cost_status TEXT, cost_source TEXT, pricing_version TEXT,
        title TEXT, api_call_count INTEGER, handoff_state TEXT,
        handoff_platform TEXT, handoff_error TEXT,
        compression_failure_cooldown_until REAL, compression_failure_error TEXT,
        compression_fallback_streak INTEGER, profile_name TEXT,
        rewind_count INTEGER, archived INTEGER
    );
    """)
    con.commit()
    con.close()
    report = check_hermes_schema_drift(str(db_path))
    assert report.is_valid is False
    assert "mensajes" in report.summary and "effect_disposition" in report.summary

def test_drift_detector_sessions_missing_column(tmp_path):
    db_path = tmp_path / "missing_s.db"
    con = sqlite3.connect(db_path)
    # Schema completo messages, sesiones sin billing_provider
    con.executescript("""
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
        tool_calls TEXT, tool_name TEXT, effect_disposition TEXT,
        timestamp REAL, token_count INTEGER, finish_reason TEXT,
        reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
        codex_reasoning_items TEXT, codex_message_items TEXT,
        platform_message_id TEXT, observed INTEGER, active INTEGER,
        compacted INTEGER, api_content TEXT
    );
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY, source TEXT, user_id TEXT, session_key TEXT,
        chat_id TEXT, chat_type TEXT, thread_id TEXT, display_name TEXT,
        origin_json TEXT, expiry_finalized INTEGER, model TEXT,
        model_config TEXT, system_prompt TEXT, parent_session_id TEXT,
        started_at REAL, ended_at REAL, end_reason TEXT, message_count INTEGER,
        tool_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
        cache_read_tokens INTEGER, cache_write_tokens INTEGER,
        reasoning_tokens INTEGER, cwd TEXT, git_branch TEXT,
        git_repo_root TEXT, billing_base_url TEXT,
        billing_mode TEXT, estimated_cost_usd REAL, actual_cost_usd REAL,
        cost_status TEXT, cost_source TEXT, pricing_version TEXT,
        title TEXT, api_call_count INTEGER, handoff_state TEXT,
        handoff_platform TEXT, handoff_error TEXT,
        compression_failure_cooldown_until REAL, compression_failure_error TEXT,
        compression_fallback_streak INTEGER, profile_name TEXT,
        rewind_count INTEGER, archived INTEGER
    );
    """)
    con.commit()
    con.close()
    report = check_hermes_schema_drift(str(db_path))
    assert report.is_valid is False
    assert "sesiones" in report.summary and "billing_provider" in report.summary

def test_drift_detector_with_fixture(tmp_path):
    # Path relativo
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "hermes_fixture.db")
    report = check_hermes_schema_drift(fixture_path)
    assert report.is_valid is True
    assert "NO_SCHEMA_DRIFT" in report.summary
    assert "display_kind" in report.summary
    assert "display_metadata" in report.summary
    assert "compression_ineffective_count" in report.summary
    assert "pinned" in report.summary
