"""CausaDB OTel adapter — Mapper EventType → spans OTel (F.6.1).

Artículo II — thin-wrapper: este módulo traduce CanonicalEvent a spans OTel
del SDK oficial. NO reimplementa lógica de spans, NO toca el núcleo
(LedgerWriter, ReplayEngine, etc.).

Artículo VIII — NO se crea una clase CausaDBOTelExporter. El mapper es
data (dict module-level) + función. F.6.2 y F.6.3 harán funciones, no clases.

Exports públicos:
    - EVENT_TYPE_TO_OTEL_SPAN: Dict[EventType, Tuple[str, SpanKind]]
    - event_to_span(event) -> ReadableSpan
"""

from causadb.otel._mapper import EVENT_TYPE_TO_OTEL_SPAN, event_to_span
from causadb.otel._importer import OTelImporter

__all__ = ["EVENT_TYPE_TO_OTEL_SPAN", "event_to_span", "OTelImporter"]
