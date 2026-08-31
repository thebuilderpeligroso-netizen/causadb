import os
from dataclasses import dataclass
from typing import List, Optional
from causadb._ledger_reader import LedgerReader
from causadb._replay_engine import ReplayEngine
from causadb._ledger_validator import LedgerValidator
import json
import sqlite3

@dataclass
class DriftReport:
    is_valid: bool
    summary: str = ""

def load_events(ledger_path: str):
    # read_all_entries lanza JSONDecodeError si el ledger está corrupto
    # Se debe manejar para que check_causal_drift no falle con excepción
    try:
        return list(LedgerReader(ledger_path).read_all_entries())
    except json.JSONDecodeError:
        return []

def check_hash_chain(ledger_path: str) -> DriftReport:
    validator = LedgerValidator(ledger_path)
    result = validator.validate_chain()
    return DriftReport(is_valid=result.is_valid, summary=result.failure_type or "")

def check_replay_consistency(ledger_path: str) -> DriftReport:
    engine = ReplayEngine(ledger_path)
    try:
        state = engine.reconstruct_state()
        ledger_has_content = (
            os.path.exists(ledger_path) and os.path.getsize(ledger_path) > 0
        )
        if ledger_has_content and state["events_applied"] == 0:
            return DriftReport(
                is_valid=False,
                summary="REPLAY_CONSISTENCY_FAILED",
            )
        return DriftReport(is_valid=True)
    except Exception as e:
        # En caso de corrupción JSON, reconstruct_state levantará error
        # El test test_replay_fails espera que esto falle
        return DriftReport(is_valid=False, summary=str(e))

def check_causal_drift(ledger_path: str) -> DriftReport:
    try:
        entries = load_events(ledger_path)
    except Exception:
        return DriftReport(is_valid=False, summary="CORRUPTION")
        
    event_ids = {entry["event"]["event_id"] for entry in entries}
    for entry in entries:
        parent_id = entry["event"].get("parent_event_id")
        if parent_id and parent_id != "GENESIS" and parent_id not in event_ids:
            return DriftReport(
                is_valid=False,
                summary=f"ORPHAN_EVENT: {entry['event']['event_id']} refers to {parent_id}",
            )
    return DriftReport(is_valid=True)

def check_hermes_schema_drift(db_path: str) -> DriftReport:
    """Compara el schema de un SQLite Hermes contra v22 esperado."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        # Tablas esperadas y columnas v22
        expected_messages = {
            'id', 'session_id', 'role', 'content', 'tool_call_id', 'tool_calls', 
            'tool_name', 'effect_disposition', 'timestamp', 'token_count', 
            'finish_reason', 'reasoning', 'reasoning_content', 'reasoning_details', 
            'codex_reasoning_items', 'codex_message_items', 'platform_message_id', 
            'observed', 'active', 'compacted', 'api_content'
        }
        expected_sessions = {
            'id', 'source', 'user_id', 'session_key', 'chat_id', 'chat_type', 'thread_id', 
            'display_name', 'origin_json', 'expiry_finalized', 'model', 'model_config', 
            'system_prompt', 'parent_session_id', 'started_at', 'ended_at', 'end_reason', 
            'message_count', 'tool_call_count', 'input_tokens', 'output_tokens', 
            'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens', 'cwd', 
            'git_branch', 'git_repo_root', 'billing_provider', 'billing_base_url', 
            'billing_mode', 'estimated_cost_usd', 'actual_cost_usd', 'cost_status', 
            'cost_source', 'pricing_version', 'title', 'api_call_count', 'handoff_state', 
            'handoff_platform', 'handoff_error', 'compression_failure_cooldown_until', 
            'compression_failure_error', 'compression_fallback_streak', 'profile_name', 
            'rewind_count', 'archived'
        }
        
        # Obtener columnas reales
        actual_messages = {row[1] for row in con.execute('PRAGMA table_info(messages)')}
        actual_sessions = {row[1] for row in con.execute('PRAGMA table_info(sessions)')}
        
        missing_m = expected_messages - actual_messages
        missing_s = expected_sessions - actual_sessions

        
        if missing_m or missing_s:
            return DriftReport(False, f"Columnas faltantes: mensajes={missing_m}, sesiones={missing_s}")
        
        summary = "NO_SCHEMA_DRIFT"
        extra_m = actual_messages - expected_messages
        extra_s = actual_sessions - expected_sessions
        if extra_m or extra_s:
            summary = f"NO_SCHEMA_DRIFT (columnas extra: mensajes={extra_m}, sesiones={extra_s})"
            
        con.close()
        return DriftReport(True, summary)
    except Exception as e:
        return DriftReport(False, f"Error: {str(e)}")
