import pytest
from causadb._schema_validator import validate_event_schema
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType
import uuid

def test_validate_event_type_in_enum():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b", payload={"path": "p", "action": "a"})
    assert validate_event_schema(e).is_valid

def test_validate_event_type_not_in_enum():
    # CanonicalEvent.from_dict usa EventType(str), así que un tipo inválido fallará en la creación
    with pytest.raises(ValueError):
        CanonicalEvent(event_type="INVALID", ctx_id="ctx", source="a:b")

def test_validate_source_namespace_valid():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="agent:ns", payload={"path": "p", "action": "a"})
    assert validate_event_schema(e).is_valid

def test_validate_source_bare_name_valid():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="agent", payload={"path": "p", "action": "a"})
    res = validate_event_schema(e)
    assert res.is_valid

def test_validate_parent_event_id_none_ok():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b", payload={"path": "p", "action": "a"}, parent_event_id=None)
    assert validate_event_schema(e).is_valid

def test_validate_parent_event_id_valid_uuid():
    pid = str(uuid.uuid4())
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b", payload={"path": "p", "action": "a"}, parent_event_id=pid)
    assert validate_event_schema(e).is_valid

def test_validate_parent_event_id_malformed():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b", payload={"path": "p", "action": "a"}, parent_event_id="bad-uuid")
    res = validate_event_schema(e)
    assert not res.is_valid
    assert res.failure_type == "INVALID_PARENT_EVENT_ID"

def test_validate_required_fields_file_modified():
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b", payload={"path": "p"}) # falta action
    res = validate_event_schema(e)
    assert not res.is_valid
    assert res.failure_type == "MISSING_FIELD"

def test_validate_required_fields_command_run():
    e = CanonicalEvent(event_type=EventType.COMMAND_RUN, ctx_id="ctx", source="a:b", payload={}) # falta command
    res = validate_event_schema(e)
    assert not res.is_valid
    assert res.failure_type == "MISSING_FIELD"

def test_validate_required_fields_commit_made():
    e = CanonicalEvent(event_type=EventType.COMMIT_MADE, ctx_id="ctx", source="a:b", payload={}) # falta commit_hash
    res = validate_event_schema(e)
    assert not res.is_valid
    assert res.failure_type == "MISSING_FIELD"


# --- F.2.2: Schema estricto para los 8 EventType sin SCHEMA_RULES ---

def test_validate_tool_called_requires_tool_name():
    e = CanonicalEvent(event_type=EventType.TOOL_CALLED, ctx_id="ctx", source="a:b", payload={})
    res = validate_event_schema(e)
    assert not res.is_valid
    assert res.failure_type == "MISSING_FIELD"
    assert "tool_name" in res.description

def test_validate_session_started_requires_session_id():
    e = CanonicalEvent(event_type=EventType.SESSION_STARTED, ctx_id="ctx", source="a:b", payload={})
    res = validate_event_schema(e)
    assert not res.is_valid
    assert res.failure_type == "MISSING_FIELD"
    assert "session_id" in res.description

def test_validate_session_ended_requires_session_id():
    e = CanonicalEvent(event_type=EventType.SESSION_ENDED, ctx_id="ctx", source="a:b", payload={})
    res = validate_event_schema(e)
    assert not res.is_valid
    assert res.failure_type == "MISSING_FIELD"
    assert "session_id" in res.description

def test_validate_mutation_applied_requires_mutation_id():
    e = CanonicalEvent(event_type=EventType.MUTATION_APPLIED, ctx_id="ctx", source="a:b", payload={})
    res = validate_event_schema(e)
    assert not res.is_valid
    assert res.failure_type == "MISSING_FIELD"
    assert "mutation_id" in res.description

def test_validate_mutation_reverted_requires_revert_target_event_id():
    e = CanonicalEvent(event_type=EventType.MUTATION_REVERTED, ctx_id="ctx", source="a:b", payload={})
    res = validate_event_schema(e)
    assert not res.is_valid
    assert res.failure_type == "MISSING_FIELD"
    assert "revert_target_event_id" in res.description

def test_validate_system_boot_requires_boot_id():
    e = CanonicalEvent(event_type=EventType.SYSTEM_BOOT, ctx_id="ctx", source="a:b", payload={})
    res = validate_event_schema(e)
    assert not res.is_valid
    assert res.failure_type == "MISSING_FIELD"
    assert "boot_id" in res.description

def test_validate_checkpoint_created_requires_checkpoint_id():
    e = CanonicalEvent(event_type=EventType.CHECKPOINT_CREATED, ctx_id="ctx", source="a:b", payload={})
    res = validate_event_schema(e)
    assert not res.is_valid
    assert res.failure_type == "MISSING_FIELD"
    assert "checkpoint_id" in res.description

def test_validate_context_updated_requires_context():
    e = CanonicalEvent(event_type=EventType.CONTEXT_UPDATED, ctx_id="ctx", source="a:b", payload={})
    res = validate_event_schema(e)
    assert not res.is_valid
    assert res.failure_type == "MISSING_FIELD"
    assert "context" in res.description

# --- F.3.5: TOOL_CALLED mejorado — arguments y result son obligatorios ---

def test_validate_tool_called_requires_arguments():
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.TOOL_CALLED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({"tool_name": "test"}),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "arguments" in result.description

def test_validate_tool_called_requires_result():
    event = CanonicalEvent(
        event_type=EventType.TOOL_CALLED,
        ctx_id="test",
        source="causadb:test",
        payload={"tool_name": "test", "arguments": {}},
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert result.failure_type == "MISSING_FIELD"
    assert "result" in result.description

def test_validate_cost_accounted_requires_cost(tmp_path):
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.COST_ACCOUNTED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({}),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "cost" in result.description

def test_validate_llm_invoked_requires_model():
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.LLM_INVOKED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({}),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "model" in result.description

def test_validate_memory_op_requires_operation():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.MEMORY_OP,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({}),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "operation" in result.description

def test_validate_retrieval_done_requires_query(tmp_path):
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.RETRIEVAL_DONE,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({}),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "query" in result.description

def test_validate_agent_handoff_requires_from_agent():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.AGENT_HANDOFF,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({}),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "from_agent" in result.description

# --- F.5.1: HUMAN_FEEDBACK EventType ---

def test_validate_human_feedback_requires_feedback_type():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.HUMAN_FEEDBACK,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({}),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert result.failure_type == "MISSING_FIELD"
    assert "feedback_type" in result.description

def test_validate_human_feedback_invalid_type_fails():
    """Anti-teatro: payload COMPLETO con feedback_type='foobar' (no en enum cerrado).
    Debe fallar con INVALID_FEEDBACK_TYPE, NO con MISSING_FIELD."""
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    import uuid
    event = CanonicalEvent(
        event_type=EventType.HUMAN_FEEDBACK,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "feedback_type": "foobar",
            "target_event_id": str(uuid.uuid4()),
        }),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert result.failure_type == "INVALID_FEEDBACK_TYPE"

# --- F.5.2: SANDBOX_STATE EventType ---

def test_validate_sandbox_state_requires_mutation_type():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.SANDBOX_STATE,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({}),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert result.failure_type == "MISSING_FIELD"
    assert "mutation_type" in result.description

def test_validate_sandbox_state_requires_path_or_resource():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.SANDBOX_STATE,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({"mutation_type": "file_write_outside_workspace"}),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert result.failure_type == "MISSING_FIELD"
    assert "path_or_resource" in result.description

# --- F.5.3: REASONING_STEP EventType ---

def test_validate_reasoning_step_requires_step_type():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.REASONING_STEP,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({}),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert result.failure_type == "MISSING_FIELD"
    assert "step_type" in result.description

def test_validate_reasoning_step_invalid_type_fails():
    """Anti-teatro: payload COMPLETO con step_type='foobar' (no en enum cerrado).
    Debe fallar con INVALID_STEP_TYPE, NO con MISSING_FIELD."""
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.REASONING_STEP,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "step_type": "foobar",
            "step_hash": "c5d6e7",
        }),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "INVALID_STEP_TYPE" in result.failure_type

# --- F.5.4: CONTEXT_COMPACTED EventType ---

def test_validate_context_compacted_requires_pre_post_counts():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.CONTEXT_COMPACTED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({}),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert result.failure_type == "MISSING_FIELD"
    assert "pre_token_count" in result.description

# --- F.5.5: STREAM_INTERRUPTED EventType ---

def test_validate_stream_interrupted_requires_reason():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.STREAM_INTERRUPTED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({}),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert result.failure_type == "MISSING_FIELD"
    assert "interrupt_reason" in result.description

def test_validate_stream_interrupted_invalid_reason_fails():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.STREAM_INTERRUPTED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "interrupt_reason": "foobar",
            "partial_completion_hash": "d8e9f0",
        }),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "INVALID_INTERRUPT_REASON" in result.failure_type


# --- GOVERNANCE_DECISION EventType ---

def test_validate_governance_decision_requires_reasoning():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({}),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert result.failure_type == "MISSING_FIELD"
    assert "reasoning" in result.description


def test_validate_governance_decision_requires_impact():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "reasoning": "test reasoning",
            # missing impact
        }),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "impact" in result.description


def test_validate_governance_decision_requires_decision_type():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "reasoning": "test reasoning",
            "impact": "high",
            # missing decision_type
        }),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "decision_type" in result.description


def test_validate_governance_decision_requires_origin():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "reasoning": "test reasoning",
            "impact": "high",
            "decision_type": "strategic",
            # missing origin
        }),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "origin" in result.description


def test_validate_governance_decision_invalid_impact():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "reasoning": "test",
            "impact": "invalid_impact",
            "decision_type": "strategic",
            "origin": "agent",
        }),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "INVALID_IMPACT" in result.failure_type


def test_validate_governance_decision_invalid_decision_type():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "reasoning": "test",
            "impact": "high",
            "decision_type": "invalid_decision",
            "origin": "agent",
        }),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "INVALID_DECISION_TYPE" in result.failure_type


def test_validate_governance_decision_invalid_origin():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "reasoning": "test",
            "impact": "high",
            "decision_type": "strategic",
            "origin": "invalid_origin",
        }),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "INVALID_ORIGIN" in result.failure_type


# --- R.1.2: OBSERVATION EventType (Loop State) ---

def test_validate_observation_requires_fields():
    """Reject payload missing file_path."""
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    event = CanonicalEvent(
        event_type=EventType.OBSERVATION,
        ctx_id="test",
        source="opencode:agent",
        source_type="agent",
        payload={"line_number": 10, "description": "missing path", "severity": "info"},
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert result.failure_type == "MISSING_FIELD"
    assert "file_path" in result.description

def test_validate_observation_rejects_invalid_severity():
    """Reject payload with severity out of enum."""
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.OBSERVATION,
        ctx_id="test",
        source="opencode:agent",
        source_type="agent",
        payload=MappingProxyType({
            "file_path": "src/foo.py",
            "line_number": 10,
            "description": "test",
            "severity": "catastrophic",  # not in enum
        }),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "severity" in result.description.lower() or "ENUM" in result.failure_type

def test_validate_observation_accepts_valid_payload():
    """Accept a valid OBSERVATION event."""
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.OBSERVATION,
        ctx_id="test",
        source="opencode:agent",
        source_type="agent",
        payload=MappingProxyType({
            "file_path": "src/foo.py",
            "line_number": 10,
            "description": "potential null pointer",
            "severity": "minor",
        }),
    )
    result = validate_event_schema(event)
    assert result.is_valid, f"Should be valid: {result.failure_type} {result.description}"


# --- R.2.2: GOVERNANCE_DECISION_STATUS_CHANGED schema + ENUM_RULES ---

def test_validate_GDSC_rechaza_missing_new_status():
    """R.2.2 - GOVERNANCE_DECISION_STATUS_CHANGED sin new_status -> MISSING_FIELD."""
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION_STATUS_CHANGED,
        ctx_id="test",
        source="opencode:agent",
        source_type="agent",
        parent_event_id="12345678-1234-1234-1234-123456789012",
        payload=MappingProxyType({
            "reason": "decision moved to in_progress",
        }),
    )
    result = validate_event_schema(event)
    assert not result.is_valid, f"Should be invalid: {result.__dict__}"
    assert result.failure_type == "MISSING_FIELD"
    assert "new_status" in result.description


def test_validate_GDSC_rechaza_invalid_status():
    """R.2.2 - GOVERNANCE_DECISION_STATUS_CHANGED con new_status fuera del enum -> INVALID_NEW_STATUS."""
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION_STATUS_CHANGED,
        ctx_id="test",
        source="opencode:agent",
        source_type="agent",
        parent_event_id="12345678-1234-1234-1234-123456789012",
        payload=MappingProxyType({
            "new_status": "deleted",  # not in enum
            "reason": "invalid transition",
        }),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert "new_status" in result.description.lower() or "NEW_STATUS" in result.failure_type


def test_validate_GDSC_acepta_valido():
    """R.2.2 - GOVERNANCE_DECISION_STATUS_CHANGED con new_status='done' + parent_event_id -> valido."""
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION_STATUS_CHANGED,
        ctx_id="test",
        source="opencode:agent",
        source_type="agent",
        parent_event_id="12345678-1234-1234-1234-123456789012",
        payload=MappingProxyType({
            "new_status": "done",
            "reason": "work complete",
        }),
    )
    result = validate_event_schema(event)
    assert result.is_valid, f"Should be valid: {result.failure_type} {result.description}"


def test_anti_teatro_GDSC_sin_ENUM_RULES(tmp_path, monkeypatch):
    """R.2.2 Anti-teatro: si se quita GOVERNANCE_DECISION_STATUS_CHANGED del ENUM_RULES,
    test_validate_GDSC_rechaza_invalid_status deberia fallar porque deja de rechazar.
    Verificamos aca directamente mutando el dict."""
    from causadb._schema_validator import ENUM_RULES, validate_event_schema
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from types import MappingProxyType
    # Si el EventType no esta en ENUM_RULES, el validator no rechaza 'deleted'
    assert EventType.GOVERNANCE_DECISION_STATUS_CHANGED in ENUM_RULES, (
        "R.2.2 falla: GOVERNANCE_DECISION_STATUS_CHANGED debe estar en ENUM_RULES"
    )
    assert "new_status" in ENUM_RULES[EventType.GOVERNANCE_DECISION_STATUS_CHANGED], (
        "R.2.2 falla: new_status debe ser campo enum en ENUM_RULES"
    )
    allowed = ENUM_RULES[EventType.GOVERNANCE_DECISION_STATUS_CHANGED]["new_status"]
    assert "proposed" in allowed and "done" in allowed and "deleted" not in allowed, (
        f"R.2.2 falla: enum_new_status incorrecto: {allowed}"
    )

# --- R.3.1: PROJECT_SNAPSHOT schema ---

def test_project_snapshot_requires_fields():
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._schema_validator import validate_event_schema
    from types import MappingProxyType
    # payload vacío, debería fallar por missing fields
    event = CanonicalEvent(
        event_type=EventType.PROJECT_SNAPSHOT,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({}),
    )
    result = validate_event_schema(event)
    assert not result.is_valid
    assert result.failure_type == "MISSING_FIELD"
