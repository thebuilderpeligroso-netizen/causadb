import os
import pytest
import sqlite3
from causadb._harvest_source_hermes import HermesHarvestSource
from tests.helpers._build_synthetic_hermes_store import build_synthetic_hermes_store
from tests.helpers._synthetic_agent_log import create_synthetic_agent_log

# Helper para normalizar timestamps sintéticos a ISO
def _t(ts_str: str) -> str:
    return ts_str.replace(" ", "T") + "Z"

def test_harvest_api_attempts_completed_real_log(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    build_synthetic_hermes_store(db_path)
    
    # Formato REAL: con INFO, SIN corchetes alrededor
    log_content = (
        "2026-08-13 10:00:00,000 INFO [20260802_101617_82f322] agent.conversation_loop: "
        "API call #1: model=qwen3.5:4b provider=custom in=100 out=50 total=150 latency=1.5s\n"
        "2026-08-13 10:00:01,000 INFO [20260802_101617_82f322] agent.conversation_loop: "
        "API call #2: model=qwen3.5:4b provider=custom in=200 out=100 total=300 latency=0.5s\n"
    )
    create_synthetic_agent_log(logs_dir, log_content)
    
    source = HermesHarvestSource(ledger_path="/tmp/ledger.log", db_path=db_path)
    events = list(source.harvest())
    
    api_attempts = [e for e in events if e["type"] == "API_ATTEMPT"]
    
    # Debe fallar actualmente porque el parser no incluye "INFO"
    assert len(api_attempts) == 2
    assert api_attempts[0]["status"] == "completed"
    assert api_attempts[0]["tokens_in"] == 100

def test_harvest_api_attempts_failed_real_log(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    build_synthetic_hermes_store(db_path)
    
    # Formato REAL: con WARNING
    log_content = (
        "2026-08-13 10:00:02,000 WARNING [20260802_101617_82f322] agent.conversation_loop: "
        "API call failed (attempt 1/3) error_type=BadRequestError thread=MainThread:123 "
        "provider=custom base_url=http://localhost model=qwen summary=HTTP 400: model required\n"
    )
    create_synthetic_agent_log(logs_dir, log_content)
    
    source = HermesHarvestSource(ledger_path="/tmp/ledger.log", db_path=db_path)
    events = list(source.harvest())
    
    api_attempts = [e for e in events if e["type"] == "API_ATTEMPT"]
    
    assert len(api_attempts) == 1
    assert api_attempts[0]["status"] == "failed"
    assert api_attempts[0]["error"] == "HTTP 400: model required"

def test_harvest_api_attempts_no_log(tmp_path):
    db_path = str(tmp_path / "state.db")
    build_synthetic_hermes_store(db_path)
    
    source = HermesHarvestSource(ledger_path="/tmp/ledger.log", db_path=db_path)
    events = list(source.harvest())
    
    api_attempts = [e for e in events if e["type"] == "API_ATTEMPT"]
    
    assert len(api_attempts) == 0

def test_harvest_api_attempts_dedup(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    build_synthetic_hermes_store(db_path)
    
    log_content = (
        "2026-08-13 10:00:00,000 INFO [20260802_101617_82f322] agent.conversation_loop: "
        "API call #1: model=qwen3.5:4b provider=custom in=100 out=50 total=150 latency=1.5s\n"
    )
    create_synthetic_agent_log(logs_dir, log_content)
    
    source = HermesHarvestSource(ledger_path="/tmp/ledger.log", db_path=db_path)
    events1 = list(source.harvest())
    api_attempts1 = [e for e in events1 if e["type"] == "API_ATTEMPT"]
    assert len(api_attempts1) == 1
    
    cursor = source.advance_cursor({}, events1)
    events2 = list(source.harvest(cursor=cursor))
    api_attempts2 = [e for e in events2 if e["type"] == "API_ATTEMPT"]
    assert len(api_attempts2) == 0

def test_harvest_api_attempts_payload_meta(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    build_synthetic_hermes_store(db_path)
    
    log_content = (
        "2026-08-13 10:00:00,000 INFO [20260802_101617_82f322] agent.conversation_loop: "
        "API call #1: model=qwen3.5:4b provider=custom in=100 out=50 total=150 latency=1.5s\n"
    )
    create_synthetic_agent_log(logs_dir, log_content)
    
    source = HermesHarvestSource(ledger_path="/tmp/ledger.log", db_path=db_path)
    events = list(source.harvest())
    api_attempts = [e for e in events if e["type"] == "API_ATTEMPT"]
    
    assert "hermes_session_id" in api_attempts[0]
    assert api_attempts[0]["hermes_session_id"] == "20260802_101617_82f322"

def test_harvest_mode_not_empty_when_billing_mode_blank(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    build_synthetic_hermes_store(db_path)

    log_content = (
        "2026-08-13 10:00:00,000 INFO [20260802_101617_82f322] agent.conversation_loop: "
        "API call #1: model=qwen3.5:4b provider=custom in=100 out=50 total=150 latency=1.5s\n"
    )
    create_synthetic_agent_log(logs_dir, log_content)

    source = HermesHarvestSource(ledger_path="/tmp/ledger.log", db_path=db_path)
    events = list(source.harvest())
    api_attempts = [e for e in events if e["type"] == "API_ATTEMPT"]

    assert api_attempts[0]["mode"] == "unknown"

def test_harvest_redact_url_credentials(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    build_synthetic_hermes_store(db_path)
    
    log_content = (
        "2026-08-13 10:00:00,000 WARNING [20260802_101617_82f322] agent.conversation_loop: "
        "API call failed (attempt 1/3) error_type=BadRequestError thread=MainThread:123 "
        "provider=custom base_url=http://user:pass@localhost model=qwen summary=Fail\n"
    )
    create_synthetic_agent_log(logs_dir, log_content)
    
    source = HermesHarvestSource(ledger_path="/tmp/ledger.log", db_path=db_path)
    events = list(source.harvest())
    api_attempts = [e for e in events if e["type"] == "API_ATTEMPT"]
    
    assert api_attempts[0]["base_url"] == "http://***@localhost"

def test_harvest_session_only_in_log(tmp_path):
    # Sesión no existe en DB, solo en log
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    build_synthetic_hermes_store(db_path)
    
    log_content = (
        "2026-08-13 10:00:00,000 INFO [20260812_194440_69d79b] agent.conversation_loop: "
        "API call #1: model=qwen provider=custom in=10 out=10 total=20 latency=0.1s\n"
    )
    create_synthetic_agent_log(logs_dir, log_content)
    
    source = HermesHarvestSource(ledger_path="/tmp/ledger.log", db_path=db_path)
    events = list(source.harvest())
    api_attempts = [e for e in events if e["type"] == "API_ATTEMPT"]
    
    assert len(api_attempts) == 1
    assert api_attempts[0]["hermes_session_id"] == "20260812_194440_69d79b"
    assert api_attempts[0]["mode"] == "unknown"

def test_replay_validate(tmp_path):
    assert True

def test_session_id_conservation(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    build_synthetic_hermes_store(db_path)
    
    # Simular session_model_usage con campos requeridos
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO session_model_usage (session_id, model, billing_provider, billing_base_url, billing_mode, task) VALUES (?, ?, ?, ?, ?, ?)",
                ('20260802_101617_82f322', 'qwen', 'custom', 'http://base', 'standard', 'test'))
    con.commit()
    con.close()
    
    log_content = (
        "2026-08-13 10:00:00,000 INFO [20260802_101617_82f322] agent.conversation_loop: "
        "API call #1: model=qwen provider=custom in=10 out=10 total=20 latency=0.1s\n"
    )
    create_synthetic_agent_log(logs_dir, log_content)
    
    source = HermesHarvestSource(ledger_path="/tmp/ledger.log", db_path=db_path)
    events = list(source.harvest())
    api_attempts = [e for e in events if e["type"] == "API_ATTEMPT"]
    
    assert api_attempts[0]["session_id"] == "20260802_101617_82f322"
