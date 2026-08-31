"""Mapper EventType → spans OTel (F.6.1).

Implementación del mapper que convierte un CanonicalEvent a un ReadableSpan
del SDK oficial `opentelemetry-sdk`. El mapper es específico de CausaDB
(EventType → `gen_ai.*` span names), pero usa las clases del SDK oficial
(Artículo II — thin-wrapper, Artículo VII — stdlib + SDK oficial).

Decisiones del operador (NO MODIFICAR):
- STREAM_INTERRUPTED SÍ se mapea a gen_ai.chat con attribute
  gen_ai.streaming.interrupted=true (bool, SIEMPRE true, implícito por el
  EventType — no se lee del payload).
- SANDBOX_STATE NO se mapea (va al skip-list, como los 12 físicos).
- MEMORY_OP con un solo span gen_ai.create_memory + attribute
  gen_ai.memory.operation (el subtipo viaja como attribute, no como span name).
- Artículo VIII — NO se crea clase. El mapper es dict module-level + función.

Cómo se obtiene el ReadableSpan:
    El SDK oficial NO expone una API directa para construir un ReadableSpan
    in-memory sin iniciar un tracer provider real. La forma confiable es:
    1. Crear un TracerProvider efímero (uno por llamada — sin estado global).
    2. Agregar un SimpleSpanProcessor con un exportador mock (ListExporter)
       que captura los spans en una lista.
    3. Iniciar un span con `tracer.start_as_current_span(name, kind=kind)`.
    4. Setear attributes con `span.set_attribute(k, v)`.
    5. Al cerrar el `with`, el SpanProcessor llama al exportador → el span
       capturado en la lista es el ReadableSpan.
    6. Retornar `captured[0]`.

    Esto preserva la doctrina: usamos el SDK oficial, no inventamos una
    representación de span propia. El TracerProvider efímero no contamina
    estado global (no se registra via trace.set_tracer_provider).
"""

import json
from typing import Dict, Tuple, Optional, Any

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType


# ---------------------------------------------------------------------------
# Mapping dict — EventType → (span_name, SpanKind)
# ---------------------------------------------------------------------------

# 11 EventTypes mapeados (de 22 totales). Los 13 restantes (físicos/sin
# mapeo OTel GenAI directo) se skip con count — no se exportan.
EVENT_TYPE_TO_OTEL_SPAN: Dict[EventType, Tuple[str, SpanKind]] = {
    EventType.LLM_INVOKED:         ("gen_ai.chat",              SpanKind.CLIENT),
    EventType.TOOL_CALLED:         ("gen_ai.execute_tool",      SpanKind.CLIENT),
    EventType.RETRIEVAL_DONE:      ("gen_ai.retrieval",         SpanKind.CLIENT),
    EventType.MEMORY_OP:           ("gen_ai.create_memory",     SpanKind.CLIENT),
    EventType.AGENT_HANDOFF:       ("gen_ai.invoke_agent",      SpanKind.CLIENT),
    EventType.REASONING_STEP:      ("gen_ai.plan",              SpanKind.INTERNAL),
    EventType.HUMAN_FEEDBACK:      ("gen_ai.evaluation.result", SpanKind.CLIENT),
    EventType.CONTEXT_COMPACTED:   ("gen_ai.chat",              SpanKind.INTERNAL),
    EventType.COST_ACCOUNTED:      ("gen_ai.chat",              SpanKind.INTERNAL),
    EventType.STREAM_INTERRUPTED:  ("gen_ai.chat",              SpanKind.INTERNAL),
}


# ---------------------------------------------------------------------------
# ListExporter — mock exportador que captura spans en una lista
# ---------------------------------------------------------------------------

class _ListExporter:
    """Exportador mock: captura ReadableSpans en una lista.

    Implementa la interfaz SpanExporter del SDK oficial (export, shutdown).
    No es una abstracción nueva de CausaDB — es el patrón estándar del SDK
    para testing/captura in-memory (documentado en la guía del SDK).
    """

    def __init__(self) -> None:
        self.captured: list = []

    def export(self, spans) -> None:
        # SpanExporter.export retorna None en el SDK 1.44 (la firma oficial
        # retorna SpanExportResult, pero None es aceptado por SimpleSpanProcessor).
        self.captured.extend(spans)
        return None

    def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# _derive_attributes — extrae attributes del payload + campos canónicos
# ---------------------------------------------------------------------------

def _derive_attributes(event: CanonicalEvent) -> Dict[str, Any]:
    """Deriva los attributes OTel de un CanonicalEvent.

    Atributos comunes (todos los spans):
        - event_id, ctx_id, source, timestamp (plain text)
        - causadb.parent_event_id (si existe — NO parent_span_id del SDK)
        - causadb.schema_version
        - gen_ai.system = "causadb" (identifica el emisor)

    Atributos específicos por EventType (del payload):
        Ver tabla en F.6.1 spec.
    """
    attrs: Dict[str, Any] = {
        # Atributos comunes — campos canónicos del CanonicalEvent
        "event_id": str(event.event_id),
        "ctx_id": str(event.ctx_id),
        "source": str(event.source),
        "timestamp": str(event.timestamp),
        "causadb.schema_version": str(event.schema_version),
        # gen_ai.system identifica el emisor del span (CausaDB en este caso)
        "gen_ai.system": "causadb",
    }

    # parent_event_id viaja como attribute propio (UUID de 36 chars).
    # NO usar parent_span_id del SDK OTel (16 bytes) — son espacios distintos.
    if event.parent_event_id is not None:
        attrs["causadb.parent_event_id"] = str(event.parent_event_id)

    # Atributos específicos por EventType (del payload)
    payload = event.payload
    et = event.event_type

    if et == EventType.LLM_INVOKED:
        # gen_ai.request.model — modelo invocado
        if "model" in payload:
            attrs["gen_ai.request.model"] = str(payload["model"])
        # El schema actual usa response_tokens (no input_tokens+output_tokens).
        # Mapeamos como usage.input_tokens para alinearlo con GenAI draft.
        if "response_tokens" in payload:
            attrs["gen_ai.usage.input_tokens"] = int(payload["response_tokens"])

    elif et == EventType.TOOL_CALLED:
        if "tool_name" in payload:
            attrs["gen_ai.tool.name"] = str(payload["tool_name"])
        # arguments serializado como JSON string
        if "arguments" in payload:
            attrs["gen_ai.tool.description"] = json.dumps(payload["arguments"])

    elif et == EventType.RETRIEVAL_DONE:
        if "query" in payload:
            attrs["gen_ai.retrieval.query"] = str(payload["query"])
        if "chunks" in payload:
            attrs["gen_ai.retrieval.chunk_count"] = len(payload["chunks"])

    elif et == EventType.MEMORY_OP:
        # El subtipo viaja como attribute (no como span name).
        # Span name es siempre gen_ai.create_memory.
        if "operation" in payload:
            attrs["gen_ai.memory.operation"] = str(payload["operation"])
        if "key" in payload:
            attrs["gen_ai.memory.key"] = str(payload["key"])

    elif et == EventType.AGENT_HANDOFF:
        if "from_agent" in payload:
            attrs["gen_ai.agent.from"] = str(payload["from_agent"])
        if "to_agent" in payload:
            attrs["gen_ai.agent.to"] = str(payload["to_agent"])
        if "trace_id" in payload:
            attrs["causadb.trace_id"] = str(payload["trace_id"])

    elif et == EventType.REASONING_STEP:
        if "step_type" in payload:
            attrs["gen_ai.plan.type"] = str(payload["step_type"])
        if "step_hash" in payload:
            attrs["gen_ai.plan.step_hash"] = str(payload["step_hash"])

    elif et == EventType.HUMAN_FEEDBACK:
        if "feedback_type" in payload:
            attrs["gen_ai.evaluation.result.type"] = str(payload["feedback_type"])
        if "target_event_id" in payload:
            attrs["causadb.human_feedback.target_event_id"] = str(
                payload["target_event_id"]
            )

    elif et == EventType.CONTEXT_COMPACTED:
        # Boolean attribute — casi-estándar GenAI draft
        attrs["gen_ai.conversation.compacted"] = True
        if "pre_token_count" in payload:
            attrs["gen_ai.usage.input_tokens"] = int(payload["pre_token_count"])
        if "post_token_count" in payload:
            attrs["gen_ai.usage.output_tokens"] = int(payload["post_token_count"])

    elif et == EventType.COST_ACCOUNTED:
        if "cost" in payload:
            attrs["gen_ai.usage.cost"] = float(payload["cost"])
        if "currency" in payload:
            attrs["gen_ai.usage.currency"] = str(payload["currency"])
        if "tokens_in" in payload and "tokens_out" in payload:
            attrs["gen_ai.usage.cost_model"] = (
                int(payload["tokens_in"]) + int(payload["tokens_out"])
            )

    elif et == EventType.STREAM_INTERRUPTED:
        # Bool SIEMPRE true acá — implícito por el EventType.
        # No se lee del payload (decisión operador).
        attrs["gen_ai.streaming.interrupted"] = True
        if "interrupt_reason" in payload:
            attrs["causadb.stream.interrupt_reason"] = str(
                payload["interrupt_reason"]
            )

    return attrs


# ---------------------------------------------------------------------------
# event_to_span — función pública del mapper
# ---------------------------------------------------------------------------

def event_to_span(event: CanonicalEvent) -> ReadableSpan:
    """Convierte un CanonicalEvent a un ReadableSpan del SDK oficial.

    Lee EVENT_TYPE_TO_OTEL_SPAN para el span name y SpanKind, deriva
    attributes del payload del evento + campos canónicos del CanonicalEvent.

    Fall-Closed (Artículo IX): si el EventType no tiene mapeo OTel,
    raise ValueError. No se silencia el error — el caller debe decidir
    si skip o aborta.

    Args:
        event: CanonicalEvent a convertir.

    Returns:
        ReadableSpan del SDK oficial (opentelemetry.sdk.trace.ReadableSpan).

    Raises:
        ValueError: si event.event_type no está en EVENT_TYPE_TO_OTEL_SPAN.
    """
    if event.event_type not in EVENT_TYPE_TO_OTEL_SPAN:
        raise ValueError(
            f"EventType {event.event_type} has no OTel mapping (skip). "
            f"Los 13 EventTypes físicos/sin mapeo no se exportan a OTel."
        )

    span_name, kind = EVENT_TYPE_TO_OTEL_SPAN[event.event_type]
    attrs = _derive_attributes(event)

    # TracerProvider efímero — uno por llamada, sin estado global.
    # No se registra via trace.set_tracer_provider, así que no contamina.
    provider = TracerProvider()
    exporter = _ListExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("causadb")

    with tracer.start_as_current_span(span_name, kind=kind) as span:
        # set_attribute ignora None silenciosamente, pero ya filtramos None
        # en _derive_attributes (no agregamos keys con valor None).
        for key, value in attrs.items():
            span.set_attribute(key, value)
        # Al salir del `with`, el span se cierra → SpanProcessor llama al
        # exportador → el ReadableSpan queda en exporter.captured[0].

    # captured[0] es el ReadableSpan ya cerrado (con end_time seteado).
    return exporter.captured[0]
