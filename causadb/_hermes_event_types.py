"""Registro de los 6 EventTypes de Hermes Traceability (H0.2 — Bloqueante #4).

Define y registra los EventTypeSpec de Hermes (API_ATTEMPT, DELEGATION,
CRON_TRIGGERED, GATEWAY_ROUTED, MCP_TOOL_REGISTERED, PLUGIN_DISCOVERED) al
importarse, de modo que el harvester los reconozca y NO los degrade a
OBSERVATION cuando emite un raw con ``type`` custom.

Fuente de verdad de producción: los specs se definen UNA vez aquí y se
registran vía ``register_type``. El harvester importa este módulo antes de
emitir eventos.
"""

from causadb._event_registry import EventTypeSpec, register_type

DELEGATION_STATES = {
    "attempted", "started", "completed", "failed", "timeout",
    "cancelled", "interrupted", "unknown", "unobserved",
}

HERMES_EVENT_TYPES = {
    "API_ATTEMPT": EventTypeSpec(
        required_fields={
            "hermes_session_id", "provider", "model", "mode", "status", 
            "request_ref", "tokens_in", "tokens_out"
        },
        enum_rules={"status": {"attempted", "completed", "failed", "timeout", "cancelled", "unknown"}},
    ),
    "DELEGATION": EventTypeSpec(
        required_fields={
            "delegation_id", "origin_session", "parent_session_id",
            "task_json", "state", "result_json", "delivery_state",
            "created_at", "updated_at",
        },
        enum_rules={"state": DELEGATION_STATES},
    ),
    "CRON_TRIGGERED": EventTypeSpec(
        required_fields={
            "job_id", "schedule", "session_id_created",
            "triggered_at", "hermes_version",
        },
    ),
    "GATEWAY_ROUTED": EventTypeSpec(
        required_fields={
            "scope", "session_key", "platform", "thread_id",
            "delivery_state", "retry_count", "created_at",
        },
        enum_rules={"delivery_state": {"pending", "delivered", "failed", "unknown"}},
    ),
    "MCP_TOOL_REGISTERED": EventTypeSpec(
        required_fields={
            "server_name", "tool_name", "capability", "version", "discovered_at",
        },
    ),
    "PLUGIN_DISCOVERED": EventTypeSpec(
        required_fields={
            "plugin_name", "provider_type", "providers", "enabled", "discovered_at",
        },
    ),
}


def register_hermes_event_types() -> None:
    """Registra los 6 EventTypes de Hermes en el registry (idempotente)."""
    for name, spec in HERMES_EVENT_TYPES.items():
        register_type(name, spec)


register_hermes_event_types()
