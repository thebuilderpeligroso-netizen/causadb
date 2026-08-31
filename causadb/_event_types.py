from enum import Enum

class EventType(Enum):
    @classmethod
    def _missing_(cls, value):
        from causadb._event_registry import is_registered
        if isinstance(value, str) and is_registered(value):
            member = object.__new__(cls)
            member._name_ = value
            member._value_ = value
            return member
        return None

    FILE_MODIFIED = "FILE_MODIFIED"
    COMMAND_RUN = "COMMAND_RUN"
    COMMIT_MADE = "COMMIT_MADE"
    TOOL_CALLED = "TOOL_CALLED"
    DB_QUERY = "DB_QUERY"
    CONFIG_CHANGED = "CONFIG_CHANGED"
    SESSION_STARTED = "SESSION_STARTED"
    SESSION_ENDED = "SESSION_ENDED"
    MUTATION_APPLIED = "MUTATION_APPLIED"
    MUTATION_REVERTED = "MUTATION_REVERTED"
    SYSTEM_BOOT = "SYSTEM_BOOT"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    CONTEXT_UPDATED = "CONTEXT_UPDATED"
    LLM_INVOKED = "LLM_INVOKED"
    COST_ACCOUNTED = "COST_ACCOUNTED"
    RETRIEVAL_DONE = "RETRIEVAL_DONE"
    MEMORY_OP = "MEMORY_OP"
    AGENT_HANDOFF = "AGENT_HANDOFF"
    HUMAN_FEEDBACK = "HUMAN_FEEDBACK"
    SANDBOX_STATE = "SANDBOX_STATE"
    REASONING_STEP = "REASONING_STEP"
    CONTEXT_COMPACTED = "CONTEXT_COMPACTED"
    STREAM_INTERRUPTED = "STREAM_INTERRUPTED"
    SKILL_CREATED = "SKILL_CREATED"
    SKILL_PRUNED = "SKILL_PRUNED"
    SCORE_RECORDED = "SCORE_RECORDED"
    GOVERNANCE_DECISION = "GOVERNANCE_DECISION"
    GOVERNANCE_DECISION_STATUS_CHANGED = "GOVERNANCE_DECISION_STATUS_CHANGED"
    OBSERVATION = "OBSERVATION"
    OBSERVATION_RESOLVED = "OBSERVATION_RESOLVED"
    PROJECT_SNAPSHOT = "PROJECT_SNAPSHOT"
    CHRONICLE_ENTRY = "CHRONICLE_ENTRY"
    SESSION_SUMMARY = "SESSION_SUMMARY"
    API_ATTEMPT = "API_ATTEMPT"
    RESTART_COMPLETED = "RESTART_COMPLETED"

# Auto-registro de built-in types en el EventRegistry
from causadb._event_registry import register_type, EventTypeSpec

_BUILTIN_SPECS = [
    ("FILE_MODIFIED", {"path", "action"}),
    ("COMMAND_RUN", {"command"}),
    ("COMMIT_MADE", {"commit_hash"}),
    ("TOOL_CALLED", {"tool_name", "arguments", "result"}),
    ("DB_QUERY", {"query"}),
    ("CONFIG_CHANGED", {"path", "key"}),
    ("SESSION_STARTED", {"session_id"}),
    ("SESSION_ENDED", {"session_id"}),
    ("MUTATION_APPLIED", {"mutation_id"}),
    ("MUTATION_REVERTED", {"revert_target_event_id"}),
    ("SYSTEM_BOOT", {"boot_id"}),
    ("CHECKPOINT_CREATED", {"checkpoint_id"}),
    ("CONTEXT_UPDATED", {"context"}),
    ("LLM_INVOKED", {"model", "prompt", "response_tokens", "duration_ms"}),
    ("COST_ACCOUNTED", {"model", "tokens_in", "tokens_out", "cost", "currency"}),
    ("RETRIEVAL_DONE", {"query", "chunks", "scores"}),
    ("MEMORY_OP", {"operation", "key"}),
    ("AGENT_HANDOFF", {"from_agent", "to_agent", "trace_id"}),
    ("HUMAN_FEEDBACK", {"feedback_type", "target_event_id"}),
    ("SANDBOX_STATE", {"mutation_type", "path_or_resource"}),
    ("REASONING_STEP", {"step_type", "step_hash"}),
    ("CONTEXT_COMPACTED", {"pre_token_count", "post_token_count"}),
    ("STREAM_INTERRUPTED", {"interrupt_reason", "partial_completion_hash"}),
    ("SKILL_CREATED", {"skill_id", "skill_type", "skill_name", "content"}),
    ("SKILL_PRUNED", {"skill_id"}),
    ("SCORE_RECORDED", {"overall_score", "churn_score", "waste_score", "survival_score"}),
    ("GOVERNANCE_DECISION", {"reasoning", "impact", "decision_type", "origin"}),
    ("GOVERNANCE_DECISION_STATUS_CHANGED", {"new_status"}),
    ("OBSERVATION", {"file_path", "line_number", "description", "severity"}),
    ("OBSERVATION_RESOLVED", set()),
    ("PROJECT_SNAPSHOT", {"total_events", "total_tests", "fases_completadas", "bloqueantes_resueltos", "notas"}),
    ("CHRONICLE_ENTRY", {"bit_id", "title", "date", "maker", "checker", "summary", "files_touched"}),
    ("SESSION_SUMMARY", {"tool", "session_id", "turn_count", "summary_lines", "decisions", "errors", "files_touched", "tokens_used", "duration_s"}),
    ("API_ATTEMPT", {"hermes_session_id", "provider", "model", "mode", "status", "request_ref", "tokens_in", "tokens_out"}),
    ("RESTART_COMPLETED", {"mode", "unit_state", "timestamp", "systemctl_action", "systemctl_ok"}),
]

for name, fields in _BUILTIN_SPECS:
    try:
        register_type(name, EventTypeSpec(required_fields=fields), builtin=True)
    except Exception:
        pass  # built-in registration failure is non-fatal

