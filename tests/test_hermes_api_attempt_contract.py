import pytest
import os
import sys
from causadb import _event_types, _event_registry, _schema_validator, _replay_engine, _cost_rollup
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._replay_engine import ReplayEngine
from causadb._cost_rollup import CostRollup

# Add helpers to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'helpers'))
from _build_synthetic_hermes_store import build_synthetic_hermes_store

@pytest.fixture
def ledger(tmp_path):
    ledger_path = str(tmp_path / "ledger.log")
    return ledger_path

def test_api_attempt_is_registered():
    assert hasattr(_event_types.EventType, 'API_ATTEMPT')
    # Use list_registered() instead of get_all_registered_types()
    assert _event_types.EventType.API_ATTEMPT.value in _event_registry.list_registered()
    assert _event_registry.get_spec(_event_types.EventType.API_ATTEMPT.value) is not None

def test_api_attempt_schema_required_fields():
    # Test required fields
    payload = {
        "hermes_session_id": "session1",
        "provider": "openai",
        "model": "gpt-4",
        "mode": "chat",
        "status": "completed",
        "request_ref": "req1",
        "tokens_in": 100,
        "tokens_out": 50
    }
    event = CanonicalEvent(event_type=_event_types.EventType.API_ATTEMPT, ctx_id="test_ctx", source="test", payload=payload)
    result = _schema_validator.validate_event_schema(event)
    assert result.is_valid, result.description

    # Missing fields
    for field in ["status", "model", "tokens_in", "tokens_out"]:
        invalid_payload = payload.copy()
        del invalid_payload[field]
        event = CanonicalEvent(event_type=_event_types.EventType.API_ATTEMPT, ctx_id="test_ctx", source="test", payload=invalid_payload)
        result = _schema_validator.validate_event_schema(event)
        assert not result.is_valid

def test_api_attempt_status_enum():
    valid_statuses = ["attempted", "completed", "failed", "timeout", "cancelled", "unknown"]
    for status in valid_statuses:
        payload = {
            "hermes_session_id": "session1",
            "provider": "openai",
            "model": "gpt-4",
            "mode": "chat",
            "status": status,
            "request_ref": "req1",
            "tokens_in": 100,
            "tokens_out": 50
        }
        event = CanonicalEvent(event_type=_event_types.EventType.API_ATTEMPT, ctx_id="test_ctx", source="test", payload=payload)
        result = _schema_validator.validate_event_schema(event)
        assert result.is_valid, result.description

    for invalid_status in ["interrupted", "unobserved", "bogus"]:
        payload = {
            "hermes_session_id": "session1",
            "provider": "openai",
            "model": "gpt-4",
            "mode": "chat",
            "status": invalid_status,
            "request_ref": "req1",
            "tokens_in": 100,
            "tokens_out": 50
        }
        event = CanonicalEvent(event_type=_event_types.EventType.API_ATTEMPT, ctx_id="test_ctx", source="test", payload=payload)
        result = _schema_validator.validate_event_schema(event)
        assert not result.is_valid


def test_replay_projects_api_attempts(ledger):
    writer = LedgerWriter(ledger)
    payload = {
        "hermes_session_id": "session1",
        "provider": "openai",
        "model": "gpt-4",
        "mode": "chat",
        "status": "completed",
        "request_ref": "req1",
        "tokens_in": 100,
        "tokens_out": 50,
        "cost_usd": 0.05
    }
    event = CanonicalEvent(event_type=_event_types.EventType.API_ATTEMPT, ctx_id="test_ctx", source="test", payload=payload)
    writer.append(event)
    
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["api_attempts"]) == 1
    assert state["api_attempts"][0]["hermes_session_id"] == "session1"

def test_costrollup_rollup_api_attempts():
    events = [
        {"hermes_session_id": "s1", "model": "m1", "cost_usd": 0.1, "tokens_in": 10, "tokens_out": 5},
        {"hermes_session_id": "s1", "model": "m1", "cost_usd": 0.2, "tokens_in": 20, "tokens_out": 10},
        {"hermes_session_id": "s2", "model": "m2", "cost_usd": 0.3, "tokens_in": 30, "tokens_out": 15},
    ]
    rollup = CostRollup.rollup_api_attempts(events)
    assert rollup[("s1", "m1")]["cost_usd"] == pytest.approx(0.3)
    assert rollup[("s1", "m1")]["tokens_in"] == 30
    assert rollup[("s1", "m1")]["api_calls"] == 2
    assert rollup[("s2", "m2")]["cost_usd"] == pytest.approx(0.3)

def test_api_attempt_never_leaks_base_url_secrets():
    """Art. V + Art. I: LedgerWriter redacta claves sensibles (api_key/secret/
    token) ANTES de escribir al ledger; un payload con base_url conteniendo
    credencial no las deja en claro en el archivo del ledger."""
    import tempfile
    from causadb._ledger_writer import LedgerWriter
    from causadb._config import CausaDBConfig

    tmp = tempfile.mkdtemp()
    ledger = os.path.join(tmp, "ledger.log")
    config = CausaDBConfig(ledger_path=ledger, blob_store_enabled=False)
    writer = LedgerWriter(ledger, config=config)

    payload = {
        "hermes_session_id": "s1",
        "provider": "custom",
        "model": "qwen3.5:4b",
        "mode": "chat",
        "status": "completed",
        "request_ref": "req1",
        "tokens_in": 100,
        "tokens_out": 50,
        "base_url": "https://user:supersecretpassword@api.example.com/v1",
        "api_key": "sk-super-secret-key-1234567890",
    }
    event = CanonicalEvent(
        event_type=_event_types.EventType.API_ATTEMPT,
        ctx_id="test_ctx",
        source="test",
        payload=payload,
    )
    writer.append(event)

    raw = open(ledger).read()
    assert "supersecretpassword" not in raw
    assert "sk-super-secret-key-1234567890" not in raw
    assert "api_key" in raw  # la clave existe, su valor está enmascarado

def test_api_attempt_large_payload_blobified():
    """Payload >1KB se blob-ifica por LedgerWriter y el replay lo resuelve."""
    import tempfile

    tmp = tempfile.mkdtemp()
    ledger = os.path.join(tmp, "ledger.log")
    writer = LedgerWriter(ledger)

    payload = {
        "hermes_session_id": "s1",
        "provider": "custom",
        "model": "qwen3.5:4b",
        "mode": "chat",
        "status": "failed",
        "request_ref": "req1",
        "tokens_in": 100,
        "tokens_out": 0,
        "error": "E" * 5000,
    }
    event = CanonicalEvent(
        event_type=_event_types.EventType.API_ATTEMPT,
        ctx_id="test_ctx",
        source="test",
        payload=payload,
    )
    writer.append(event)

    raw = open(ledger).read()
    assert "$blob" in raw
    assert ("E" * 5000) not in raw

    state = ReplayEngine(ledger).reconstruct_state()
    assert len(state["api_attempts"]) == 1
    assert state["api_attempts"][0]["error"] == "E" * 5000

def test_api_attempt_legacy_source_version_backward_compat():
    """Un API_ATTEMPT con source_version antiguo (pre-H2) replaya sin romper."""
    import tempfile

    tmp = tempfile.mkdtemp()
    ledger = os.path.join(tmp, "ledger.log")
    writer = LedgerWriter(ledger)

    payload = {
        "hermes_session_id": "s1",
        "provider": "custom",
        "model": "qwen3.5:4b",
        "mode": "chat",
        "status": "completed",
        "request_ref": "req1",
        "tokens_in": 2050,
        "tokens_out": 1339,
        "source_version": "hermes-0.16.0",
    }
    event = CanonicalEvent(
        event_type=_event_types.EventType.API_ATTEMPT,
        ctx_id="test_ctx",
        source="test",
        payload=payload,
    )
    writer.append(event)

    state = ReplayEngine(ledger).reconstruct_state()
    assert len(state["api_attempts"]) == 1
    assert state["api_attempts"][0]["source_version"] == "hermes-0.16.0"
    assert state["api_attempts"][0]["tokens_in"] == 2050

def test_synthetic_store_harvestable(tmp_path):
    """El store sintético es un store Hermes v22 REAL y cosechable: el harvester
    lo cosecha y produce los mismos conteos H1 que la fixture (9), sin que
    session_model_usage altere la cosecha."""
    from causadb._harvest_source_hermes import HermesHarvestSource
    from causadb._harvester import Harvester

    db_path = str(tmp_path / "hermes_store.db")
    build_synthetic_hermes_store(db_path)
    assert os.path.exists(db_path)

    ledger = str(tmp_path / "ledger.log")
    cursors = str(tmp_path / "cursors.json")
    harvester = Harvester(ledger, cursors)
    source = HermesHarvestSource(ledger, db_path)
    harvester.register_source(source)
    result = harvester.harvest_all()
    assert result["hermes"] == 9
