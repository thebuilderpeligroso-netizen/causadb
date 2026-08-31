"""Thin wrapper sobre `OTLPSpanExporter` del SDK oficial (F.6.2).

Artículo II — thin-wrapper: este módulo NO reimplementa HTTP, retry, batching
ni compression. Delega todo eso al `OTLPSpanExporter` oficial del paquete
`opentelemetry-exporter-otlp-proto-http`.

Artículo VIII — Antiabstraction: `export_ledger` es FUNCIÓN, no clase. El
operador decidió función explícitamente. No crear `CausaDBOTelExporter`.

Artículo I — el adapter NO escribe al ledger. Lee via `LedgerReader`.

Flujo:
    1. `LedgerReader.read_all_entries()` → iterador de entries del ledger
       (incluye archivos en `archive/` gzipped + ledger.log activivo).
    2. Para cada entry, reconstruir el `CanonicalEvent` via `from_dict`.
    3. Si el `EventType` está en `EVENT_TYPE_TO_OTEL_SPAN` (mapper F.6.1),
       mapear a `ReadableSpan` via `event_to_span`. Si no, count en
       `skipped_unknown_types`.
    4. Si hay spans, llamar `OTLPSpanExporter.export(spans)` (un solo
       request HTTP con todos los spans — el SDK oficial maneja el batch).
    5. Retornar summary dict con `exported_spans`, `skipped_unknown_types`,
       `errors`.

Returns:
    {
        "exported_spans": <int>,
        "skipped_unknown_types": <int>,
        "errors": <int (0 si success, 1 si export failure)>
    }
"""

from typing import Optional, Dict

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import SpanExportResult

from causadb._ledger_reader import LedgerReader
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb.otel._mapper import event_to_span, EVENT_TYPE_TO_OTEL_SPAN


def export_ledger(
    ledger_path: str,
    endpoint: str,
    headers: Optional[Dict[str, str]] = None,
) -> dict:
    """Lee el ledger entero, mapea a spans OTel, envía via OTLPSpanExporter oficial.

    Args:
        ledger_path: Path absoluto al `ledger.log`.
        endpoint: OTLP HTTP endpoint (ej: `http://localhost:6006/v1/traces`).
        headers: Headers HTTP opcionales (ej: auth bearer tokens).

    Returns:
        Dict con keys `exported_spans`, `skipped_unknown_types`, `errors`.
        `errors` es 0 si el export fue SUCCESS, 1 si fue FAILURE.
    """
    reader = LedgerReader(ledger_path)
    spans = []
    skipped = 0

    for entry in reader.read_all_entries():
        event_dict = entry["event"]
        event_type_str = event_dict.get("event_type", "")

        # Convertir string → EventType para consultarlo en EVENT_TYPE_TO_OTEL_SPAN.
        # Si el string no es un EventType válido (ledger corrupto o futuro),
        # count como skipped y continuar (Fall-Closed pero no aborta todo el export).
        try:
            event_type = EventType(event_type_str)
        except ValueError:
            skipped += 1
            continue

        if event_type not in EVENT_TYPE_TO_OTEL_SPAN:
            skipped += 1
            continue

        # Reconstruct CanonicalEvent from dict — el mapper espera CanonicalEvent.
        event = CanonicalEvent.from_dict(event_dict)

        try:
            span = event_to_span(event)
            spans.append(span)
        except ValueError:
            # El mapper raise ValueError si el EventType no tiene mapeo.
            # Ya checkeamos arriba, pero defensive: count como skipped.
            skipped += 1

    # Si no hay spans para exportar, no hacemos request HTTP (Artículo VII —
    # no inventamos trabajo innecesario). Retornar summary con 0 spans.
    if not spans:
        return {
            "exported_spans": 0,
            "skipped_unknown_types": skipped,
            "errors": 0,
        }

    # Delegar al exporter oficial — Artículo II. El SDK maneja HTTP, retry,
    # batching, compression, serialization protobuf. No reimplementamos nada.
    exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
    result = exporter.export(spans)
    exporter.shutdown()

    success = (result == SpanExportResult.SUCCESS)

    return {
        "exported_spans": len(spans) if success else 0,
        "skipped_unknown_types": skipped,
        "errors": 0 if success else 1,
    }
