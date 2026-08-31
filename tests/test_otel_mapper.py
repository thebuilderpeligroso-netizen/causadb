"""Tests para el mapper EventType → spans OTel (F.6.1).

Artículo III — test-first: estos tests se escriben ANTES de la implementación.
Artículo IX — cada test debe romperse si se muta la implementación.

Decisiones del operador (NO MODIFICAR):
- OTel como dep de test (0 skips). Tests corren siempre.
- STREAM_INTERRUPTED SÍ se mapea a gen_ai.chat con attribute gen_ai.streaming.interrupted=true.
- SANDBOX_STATE NO se mapea (va al skip-list, como los 12 físicos).
- MEMORY_OP con un solo span gen_ai.create_memory + attribute gen_ai.memory.operation.
- Artículo VIII — NO crear CausaDBOTelExporter como clase.
"""

import json
import pytest
from opentelemetry.trace import SpanKind
from opentelemetry.sdk.trace import ReadableSpan

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb.otel import EVENT_TYPE_TO_OTEL_SPAN, event_to_span


# ---------------------------------------------------------------------------
# Test 1 — LLM_INVOKED → gen_ai.chat (CLIENT)
# ---------------------------------------------------------------------------

def test_otel_mapper_llm_invoked_to_chat_span():
    event = CanonicalEvent(
        event_type=EventType.LLM_INVOKED,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={
            "model": "gpt-4",
            "prompt": "hello",
            "response_tokens": 100,
            "duration_ms": 250,
        },
    )
    span = event_to_span(event)
    assert isinstance(span, ReadableSpan)
    assert span.name == "gen_ai.chat"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["gen_ai.system"] == "causadb"
    assert span.attributes["gen_ai.request.model"] == "gpt-4"
    assert span.attributes["event_id"] == event.event_id


# ---------------------------------------------------------------------------
# Test 2 — TOOL_CALLED → gen_ai.execute_tool (CLIENT)
# ---------------------------------------------------------------------------

def test_otel_mapper_tool_called_to_execute_tool_span():
    event = CanonicalEvent(
        event_type=EventType.TOOL_CALLED,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={
            "tool_name": "read_file",
            "arguments": {"path": "/tmp/foo.txt"},
        },
    )
    span = event_to_span(event)
    assert span.name == "gen_ai.execute_tool"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["gen_ai.tool.name"] == "read_file"
    # arguments serializado como JSON string
    assert "path" in span.attributes["gen_ai.tool.description"]


# ---------------------------------------------------------------------------
# Test 3 — RETRIEVAL_DONE → gen_ai.retrieval (CLIENT)
# ---------------------------------------------------------------------------

def test_otel_mapper_retrieval_done_to_retrieval_span():
    event = CanonicalEvent(
        event_type=EventType.RETRIEVAL_DONE,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={
            "query": "how to use causadb",
            "chunks": ["chunk-1", "chunk-2", "chunk-3"],
        },
    )
    span = event_to_span(event)
    assert span.name == "gen_ai.retrieval"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["gen_ai.retrieval.query"] == "how to use causadb"
    assert span.attributes["gen_ai.retrieval.chunk_count"] == 3


# ---------------------------------------------------------------------------
# Test 4 — MEMORY_OP → gen_ai.create_memory (CLIENT) — 6 subtipos en un test
# ---------------------------------------------------------------------------

def test_otel_mapper_memory_op_to_memory_spans():
    """6 subtipos de operation en un SOLO test (no parametrize — preserva cuenta)."""
    operations = ["create", "search", "update", "upsert", "delete", "store"]
    for op in operations:
        event = CanonicalEvent(
            event_type=EventType.MEMORY_OP,
            ctx_id="ctx-1",
            source="opencode:agent1",
            payload={"operation": op, "key": f"key-{op}"},
        )
        span = event_to_span(event)
        assert span.name == "gen_ai.create_memory", (
            f"operation={op}: expected gen_ai.create_memory, got {span.name}"
        )
        assert span.kind == SpanKind.CLIENT
        assert span.attributes["gen_ai.memory.operation"] == op, (
            f"operation={op}: gen_ai.memory.operation mismatch"
        )
        assert span.attributes["gen_ai.memory.key"] == f"key-{op}"


# ---------------------------------------------------------------------------
# Test 5 — AGENT_HANDOFF → gen_ai.invoke_agent (CLIENT)
# ---------------------------------------------------------------------------

def test_otel_mapper_agent_handoff_to_invoke_agent_span():
    event = CanonicalEvent(
        event_type=EventType.AGENT_HANDOFF,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={
            "from_agent": "orchestrator",
            "to_agent": "coder",
            "trace_id": "trace-abc",
        },
    )
    span = event_to_span(event)
    assert span.name == "gen_ai.invoke_agent"
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["gen_ai.agent.from"] == "orchestrator"
    assert span.attributes["gen_ai.agent.to"] == "coder"
    assert span.attributes["causadb.trace_id"] == "trace-abc"


# ---------------------------------------------------------------------------
# Test 6 — STREAM_INTERRUPTED → gen_ai.chat (INTERNAL) con attribute bool
# ---------------------------------------------------------------------------

def test_otel_mapper_stream_interrupted_to_chat_span():
    event = CanonicalEvent(
        event_type=EventType.STREAM_INTERRUPTED,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={"interrupt_reason": "user_cancelled"},
    )
    span = event_to_span(event)
    assert span.name == "gen_ai.chat"
    assert span.kind == SpanKind.INTERNAL
    # bool, NO string — Artículo IX: si se hardcodea a string, este assert falla
    assert span.attributes["gen_ai.streaming.interrupted"] is True
    assert isinstance(span.attributes["gen_ai.streaming.interrupted"], bool)
    assert span.attributes["causadb.stream.interrupt_reason"] == "user_cancelled"


# ---------------------------------------------------------------------------
# Test 7 — Fall-Closed: EventType sin mapeo → ValueError
# ---------------------------------------------------------------------------

def test_otel_mapper_unknown_event_type_raises():
    event = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={"path": "/tmp/foo.txt"},
    )
    with pytest.raises(ValueError, match="no OTel mapping"):
        event_to_span(event)


# ---------------------------------------------------------------------------
# Test 8 — 13 EventTypes físicos/sin mapeo NO están en el dict
# ---------------------------------------------------------------------------

def test_otel_mapper_skips_physical_event_types():
    """Los 13 EventTypes sin mapeo OTel directo NO deben estar en
    EVENT_TYPE_TO_OTEL_SPAN. Se skip con count, no se exportan."""
    skipped = [
        EventType.FILE_MODIFIED,
        EventType.COMMAND_RUN,
        EventType.COMMIT_MADE,
        EventType.DB_QUERY,
        EventType.CONFIG_CHANGED,
        EventType.SESSION_STARTED,
        EventType.SESSION_ENDED,
        EventType.MUTATION_APPLIED,
        EventType.MUTATION_REVERTED,
        EventType.SYSTEM_BOOT,
        EventType.CHECKPOINT_CREATED,
        EventType.CONTEXT_UPDATED,
        EventType.SANDBOX_STATE,
    ]
    assert len(skipped) == 13, "Este test cubre exactamente 13 EventTypes"
    for et in skipped:
        assert et not in EVENT_TYPE_TO_OTEL_SPAN, (
            f"{et} NO debe estar mapeado (skip-list). "
            f"Decisión operador: SANDBOX_STATE no se mapea."
        )


# ---------------------------------------------------------------------------
# Test 9 — Anti-stub: attributes derivados del payload (no hardcodeados)
# ---------------------------------------------------------------------------

def test_otel_mapper_attributes_from_payload():
    """Si event_to_span hardcodea gen_ai.request.model='gpt-4', este test
    falla para el segundo evento. Anti-stub (Artículo IX)."""
    event_gpt4 = CanonicalEvent(
        event_type=EventType.LLM_INVOKED,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={"model": "gpt-4", "response_tokens": 100},
    )
    event_claude = CanonicalEvent(
        event_type=EventType.LLM_INVOKED,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={"model": "claude-3", "response_tokens": 200},
    )
    span_gpt4 = event_to_span(event_gpt4)
    span_claude = event_to_span(event_claude)
    assert span_gpt4.attributes["gen_ai.request.model"] == "gpt-4"
    assert span_claude.attributes["gen_ai.request.model"] == "claude-3"
    # Si estuviera hardcodeado, ambos serían iguales → AssertionError.


# ---------------------------------------------------------------------------
# Test 10 — parent_event_id se propaga como attribute (NO parent_span_id)
# ---------------------------------------------------------------------------

def test_otel_mapper_propagates_parent_event_id_as_attribute():
    """CausaDB usa UUIDs de 36 chars para parent_event_id. El parent_span_id
    del SDK OTel es 16 bytes. NO se mezclan: viaja como attribute propio."""
    event = CanonicalEvent(
        event_type=EventType.LLM_INVOKED,
        ctx_id="ctx-1",
        source="opencode:agent1",
        parent_event_id="abc-123",
        payload={"model": "gpt-4", "response_tokens": 50},
    )
    span = event_to_span(event)
    assert span.attributes["causadb.parent_event_id"] == "abc-123"


# ---------------------------------------------------------------------------
# Test 11 — Anti-teatro: mutar el mapping → test 1 debe fallar
# ---------------------------------------------------------------------------

def test_anti_teatro_otel_mapper_wrong_mapping():
    """Mutar EVENT_TYPE_TO_OTEL_SPAN[LLM_INVOKED] para que apunte a
    ('gen_ai.execute_tool', CLIENT) → el test 1 (replicado acá) debe fallar.
    Restaurar con try/finally (Artículo IX — anti-mutación)."""
    original = EVENT_TYPE_TO_OTEL_SPAN[EventType.LLM_INVOKED]
    try:
        # Mutar el mapping
        EVENT_TYPE_TO_OTEL_SPAN[EventType.LLM_INVOKED] = (
            "gen_ai.execute_tool",
            SpanKind.CLIENT,
        )
        event = CanonicalEvent(
            event_type=EventType.LLM_INVOKED,
            ctx_id="ctx-1",
            source="opencode:agent1",
            payload={"model": "gpt-4", "response_tokens": 100},
        )
        span = event_to_span(event)
        # Este assert DEBE fallar si el mapping está mutado:
        assert span.name == "gen_ai.chat", (
            f"Anti-teatro falló: mapping mutado devolvió {span.name} "
            f"en vez de gen_ai.chat"
        )
        pytest.fail("Anti-teatro: el assert anterior debió fallar con mapping mutado")
    except AssertionError as e:
        # Si el error menciona 'gen_ai.execute_tool' o 'Anti-teatro', está bien:
        # significa que el test detectó la mutación correctamente.
        assert "Anti-teatro" in str(e) or "gen_ai.execute_tool" in str(e), (
            f"Anti-teatro no detectó la mutación correctamente: {e}"
        )
    finally:
        # Restaurar el mapping original
        EVENT_TYPE_TO_OTEL_SPAN[EventType.LLM_INVOKED] = original
