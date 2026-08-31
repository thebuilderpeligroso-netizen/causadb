import re
from typing import Dict, Set
from causadb._event_schema import CanonicalEvent
from causadb._validation_result import ValidationResult
from causadb._event_types import EventType
import uuid

SCHEMA_RULES: Dict[EventType, Set[str]] = {
    EventType.GOVERNANCE_DECISION: {"reasoning", "impact", "decision_type", "origin"},
    EventType.FILE_MODIFIED: {"path", "action"},
    EventType.COMMAND_RUN: {"command"},
    EventType.COMMIT_MADE: {"commit_hash"},
    EventType.DB_QUERY: {"query"},
    EventType.CONFIG_CHANGED: {"path", "key"},
    EventType.TOOL_CALLED: {"tool_name", "arguments", "result"},
    EventType.SESSION_STARTED: {"session_id"},
    EventType.SESSION_ENDED: {"session_id"},
    EventType.MUTATION_APPLIED: {"mutation_id"},
    EventType.MUTATION_REVERTED: {"revert_target_event_id"},
    EventType.SYSTEM_BOOT: {"boot_id"},
    EventType.CHECKPOINT_CREATED: {"checkpoint_id"},
    EventType.CONTEXT_UPDATED: {"context"},
    EventType.LLM_INVOKED: {"model", "prompt", "response_tokens", "duration_ms"},
    EventType.COST_ACCOUNTED: {"model", "tokens_in", "tokens_out", "cost", "currency"},
    EventType.RETRIEVAL_DONE: {"query", "chunks", "scores"},
    EventType.MEMORY_OP: {"operation", "key"},
    EventType.AGENT_HANDOFF: {"from_agent", "to_agent", "trace_id"},
    EventType.HUMAN_FEEDBACK: {"feedback_type", "target_event_id"},
    EventType.SANDBOX_STATE: {"mutation_type", "path_or_resource"},
    EventType.REASONING_STEP: {"step_type", "step_hash"},
    EventType.CONTEXT_COMPACTED: {"pre_token_count", "post_token_count"},
    EventType.STREAM_INTERRUPTED: {"interrupt_reason", "partial_completion_hash"},
    EventType.OBSERVATION: {"file_path", "line_number", "description", "severity"},
    EventType.OBSERVATION_RESOLVED: set(),
    EventType.GOVERNANCE_DECISION_STATUS_CHANGED: {"new_status"},
    EventType.PROJECT_SNAPSHOT: {"total_events", "total_tests", "fases_completadas", "bloqueantes_resueltos", "notas"},
    EventType.CHRONICLE_ENTRY: {"bit_id", "title", "date", "maker", "checker", "summary", "files_touched"},
    EventType.SESSION_SUMMARY: {"tool", "session_id", "turn_count", "summary_lines", "decisions", "errors", "files_touched", "tokens_used", "duration_s"},
    EventType.API_ATTEMPT: {"hermes_session_id", "provider", "model", "mode", "status", "request_ref", "tokens_in", "tokens_out"},
}

def get_merged_schema_rules() -> dict:
    """Retorna SCHEMA_RULES + tipos custom del registry."""
    from causadb._event_registry import get_all_schema_rules
    return get_all_schema_rules()

# Enum cerrado: mapea EventType → set de valores permitidos para un campo dado.
# Un EventType puede tener múltiples campos con enum cerrado; cada entrada es
# {field_name: {allowed_values}}.
ENUM_RULES: Dict[EventType, Dict[str, Set[str]]] = {
    EventType.HUMAN_FEEDBACK: {
        "feedback_type": {"approval", "rejection", "edit", "rating"},
    },
    EventType.REASONING_STEP: {
        "step_type": {"plan", "analysis", "decision", "reflection"},
    },
    EventType.STREAM_INTERRUPTED: {
        "interrupt_reason": {"user_cancel", "timeout", "error", "max_tokens_reached"},
    },
    EventType.GOVERNANCE_DECISION: {
        "impact": {"critical", "high", "medium", "low"},
        "decision_type": {"strategic", "architectural", "tactical", "revert"},
        "origin": {"agent", "distill"},
    },
    EventType.OBSERVATION: {
        "severity": {"info", "minor", "major", "blocker"},
    },
    EventType.GOVERNANCE_DECISION_STATUS_CHANGED: {
        "new_status": {"proposed", "in_progress", "done", "superseded", "rejected"},
    },
    EventType.API_ATTEMPT: {
        "status": {"attempted", "completed", "failed", "timeout", "cancelled", "unknown"},
    },
}

def validate_event_schema(event: CanonicalEvent) -> ValidationResult:
    # 1. Validar EventType
    from causadb._event_types import EventType
    from causadb._event_registry import is_registered, get_spec
    
    et = event.event_type
    if isinstance(et, EventType):
        et = et.value
    
    if not is_registered(et):
        return ValidationResult(is_valid=False, failure_type="INVALID_EVENT_TYPE", description=f"Got {et}")
    
    # 2. Validar Source (namespace)
    if not re.match(r"^[a-z][a-z0-9_-]*(:[a-z][a-z0-9_-]*)?$", event.source):
        return ValidationResult(is_valid=False, failure_type="INVALID_SOURCE_NAMESPACE", description=f"Invalid format: {event.source}")
        
    # 3. Validar parent_event_id
    if event.parent_event_id and event.parent_event_id != "GENESIS":
        try:
            uuid.UUID(event.parent_event_id)
        except ValueError:
            return ValidationResult(is_valid=False, failure_type="INVALID_PARENT_EVENT_ID", description=event.parent_event_id)
            
    # 4. Validar campos requeridos
    try:
        event_type_enum = EventType(et)
    except ValueError:
        event_type_enum = None
        
    if event_type_enum in SCHEMA_RULES:
        required = SCHEMA_RULES[event_type_enum]
    else:
        spec = get_spec(et)
        required = spec.required_fields if spec else set()
    missing = required - set(event.payload.keys())
    if missing:
        return ValidationResult(is_valid=False, failure_type="MISSING_FIELD", description=f"Missing fields: {missing}")
    
    # 5. Validar enums cerrados (ej: HUMAN_FEEDBACK.feedback_type)
    #    Aditivo: los builtins se validan contra ENUM_RULES estático; los tipos
    #    custom registrados contra su spec.enum_rules. Un campo sin enum no se
    #    valida. (H0.2 — Bloqueante #2: enums de tipos custom.)
    enum_rules = {}
    if event_type_enum in ENUM_RULES:
        enum_rules.update(ENUM_RULES[event_type_enum])
    spec = get_spec(et)
    if spec and spec.enum_rules:
        enum_rules.update(spec.enum_rules)
    for field_name, allowed_values in enum_rules.items():
        value = event.payload.get(field_name)
        if value is not None and value not in allowed_values:
            return ValidationResult(
                is_valid=False,
                failure_type="INVALID_FEEDBACK_TYPE" if field_name == "feedback_type" else f"INVALID_{field_name.upper()}",
                description=f"Invalid {field_name}: {value!r}. Allowed: {sorted(allowed_values)}",
            )
            
    return ValidationResult(is_valid=True)
