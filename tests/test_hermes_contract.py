"""H0.1 — Tests RED del contrato lossless de Hermes Traceability.

Definen los 6 EventTypes nuevos de Hermes (API_ATTEMPT, DELEGATION,
CRON_TRIGGERED, GATEWAY_ROUTED, MCP_TOOL_REGISTERED, PLUGIN_DISCOVERED) y
documentan, con tests que FALLAN contra el código actual, los huecos del
contrato: enums de tipos custom no validados, ausencia de lifecycle de
DELEGATION, y replay genérico sin semántica de transición.
"""

from types import MappingProxyType

import pytest

from causadb._event_registry import EventTypeSpec, register_type
from causadb._event_schema import CanonicalEvent
from causadb._schema_validator import validate_event_schema

DELEGATION_STATES = {
    "attempted", "started", "completed", "failed", "timeout",
    "cancelled", "interrupted", "unknown", "unobserved",
}

HERMES_EVENT_TYPES = {
    "API_ATTEMPT": EventTypeSpec(
        required_fields={
            "provider", "model", "base_url", "mode", "request_id",
            "tokens_in", "tokens_out", "cache_read", "cache_write",
            "reasoning_tokens", "latency_ms", "cost_usd", "status", "error",
        },
        enum_rules={"status": {"success", "error", "timeout", "cancelled"}},
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


@pytest.fixture
def register_hermes_types():
    """Registra los 6 EventTypes de Hermes como tipos custom (setup de test)."""
    for name, spec in HERMES_EVENT_TYPES.items():
        register_type(name, spec)


def _delegation_event(state, delegation_id="d1", **extra):
    payload = {
        "delegation_id": delegation_id,
        "origin_session": "s0",
        "parent_session_id": None,
        "task_json": "{}",
        "state": state,
        "result_json": None,
        "delivery_state": "pending",
        "created_at": "2026-08-12T00:00:00Z",
        "updated_at": "2026-08-12T00:00:00Z",
    }
    payload.update(extra)
    return CanonicalEvent(
        event_type="DELEGATION",
        ctx_id="hermes",
        source="hermes",
        payload=MappingProxyType(payload),
    )


def _replay_delegations(events):
    """Escribe eventos DELEGATION a un ledger fresco y reconstruye el estado."""
    import os
    import tempfile

    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._replay_engine import ReplayEngine

    tmp = tempfile.mkdtemp()
    ws = os.path.join(tmp, "ws")
    result = causadb_init(ws)
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    for ev in events:
        writer.append(ev)
    return ReplayEngine(ledger).reconstruct_state()


def test_lossless_states_enum_validated(register_hermes_types):
    """El enum `state` de DELEGATION debe rechazar un valor inválido.

    Hoy el validator solo valida ENUM_RULES estático de tipos builtin
    (_schema_validator.py:110-119); los enum_rules de un tipo custom
    registrado NO se validan. Un `state` inválido debe ser rechazado.
    """
    event = _delegation_event(state="bogus_state")
    result = validate_event_schema(event)
    assert result.is_valid is False


def test_delegation_lifecycle_states_persisted(register_hermes_types):
    """Los 9 estados del lifecycle DELEGATION deben persistirse y consultarse.

    Hoy DELEGATION no existe como EventType con semántica propia: cae en
    `_apply_custom_event` genérico (_replay_engine.py:445-452) y se guarda
    en `custom_events[]`. No hay un estado estructurado de delegaciones que
    permita consultar el lifecycle por delegation_id.
    """
    events = [_delegation_event(state=s, delegation_id=f"d{i}")
              for i, s in enumerate(sorted(DELEGATION_STATES))]
    state = _replay_delegations(events)
    delegations = state.get("delegations")
    assert delegations is not None
    assert len(delegations) == len(DELEGATION_STATES)
    persisted = {d["delegation_id"]: d["state"] for d in delegations}
    assert set(persisted.values()) == DELEGATION_STATES


def test_unobserved_explicit_when_no_evidence(register_hermes_types):
    """Sin confirmación de un efecto, el estado debe persistirse como `unobserved`.

    Un DELEGATION que arrancó (state=started) pero nunca confirmó su efecto
    no debe omitirse silenciosamente: debe resolverse a `unobserved` en el
    replay. Hoy no hay lifecycle de delegación que resuelva este estado.
    """
    state = _replay_delegations([_delegation_event(state="started")])
    delegations = state.get("delegations")
    assert delegations is not None
    assert delegations[0]["state"] == "unobserved"


def test_unknown_preserves_uncertainty(register_hermes_types):
    """Datos parciales -> estado `unknown` con los campos presentes conservados.

    Un DELEGATION con datos parciales (state=unknown) debe persistirse como
    `unknown` y conservar los campos presentes (result_json), no descartarlos.
    Hoy no hay lifecycle de delegación que preserve esta incertidumbre.
    """
    state = _replay_delegations([
        _delegation_event(state="unknown", result_json="partial"),
    ])
    delegations = state.get("delegations")
    assert delegations is not None
    assert delegations[0]["state"] == "unknown"
    assert delegations[0]["result_json"] == "partial"


def test_state_transitions_replay_deterministic(register_hermes_types):
    """Replay determinista: `started->completed` vs `started->failed` difieren.

    El replay debe resolver el estado final de la delegación según la
    transición observada (completed vs failed) y ser reproducible: misma
    entrada -> mismo output. Hoy el replay trata los tipos custom como
    genéricos en `custom_events[]` sin semántica de transición.
    """
    completed = _replay_delegations([
        _delegation_event(state="started"),
        _delegation_event(state="completed"),
    ])
    failed = _replay_delegations([
        _delegation_event(state="started"),
        _delegation_event(state="failed"),
    ])
    completed_again = _replay_delegations([
        _delegation_event(state="started"),
        _delegation_event(state="completed"),
    ])

    c_delegations = completed.get("delegations")
    f_delegations = failed.get("delegations")
    assert c_delegations is not None
    assert f_delegations is not None
    assert c_delegations[0]["state"] == "completed"
    assert f_delegations[0]["state"] == "failed"
    assert c_delegations[0]["state"] != f_delegations[0]["state"]
    assert completed_again.get("delegations") == c_delegations
