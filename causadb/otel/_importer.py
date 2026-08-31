"""OTel importer — reads OTLP JSON Lines and converts spans to CanonicalEvent (F.6.3).

Artículo I — toda escritura al ledger vía LedgerWriter.append().
Artículo II — thin-wrapper: no reimplementa HTTP, no toca el núcleo.
Artículo VII — stdlib + SDK oficial, sin deps nuevas.
Artículo VIII — OTelImporter es una clase concreta, sin base ni jerarquía.

Format: OTLP JSON Lines (mismo que produce FileSpanExporter del SDK oficial).
Each line is a JSON object with span data (name, attributes, start_time, etc.).

Reverse mapping:
  - 4 EventTypes share span name "gen_ai.chat":
      gen_ai.chat + gen_ai.conversation.compacted=true → CONTEXT_COMPACTED
      gen_ai.chat + gen_ai.streaming.interrupted=true → STREAM_INTERRUPTED
      gen_ai.chat + gen_ai.usage.cost=... → COST_ACCOUNTED
      gen_ai.chat (no special attrs) → LLM_INVOKED (default)
  - 7 other span names → direct mapping from SPAN_NAME_TO_EVENT_TYPE dict

Dedup: event_id = "otel-" + span["span_id"]. Before append,
LedgerIndex.get_offset(event_id) → if exists, skip (idempotence).

Timestamp: span["start_time_unix_nano"] → ISO 8601 string.

parent_event_id: read from attribute causadb.parent_event_id if present.
"""
import json
import uuid
from datetime import datetime, timezone, timedelta
from types import MappingProxyType
from typing import Dict, Optional, List, Any

from causadb._ledger_writer import LedgerWriter
from causadb._ledger_index import LedgerIndex
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType


OTEL_SPAN_NAME_TO_EVENT_TYPE: Dict[str, EventType] = {
    "gen_ai.chat":              EventType.LLM_INVOKED,
    "gen_ai.execute_tool":      EventType.TOOL_CALLED,
    "gen_ai.retrieval":         EventType.RETRIEVAL_DONE,
    "gen_ai.create_memory":     EventType.MEMORY_OP,
    "gen_ai.invoke_agent":      EventType.AGENT_HANDOFF,
    "gen_ai.plan":              EventType.REASONING_STEP,
    "gen_ai.evaluation.result": EventType.HUMAN_FEEDBACK,
}


def _nanos_to_iso(unix_nano: int) -> str:
    """Convert nanosecond Unix timestamp to ISO 8601 string."""
    seconds = unix_nano / 1_000_000_000.0
    dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return dt.isoformat()


def _extract_attrs(attributes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Convert OTLP attributes list to flat key→value dict."""
    attrs: Dict[str, Any] = {}
    for a in attributes:
        key = a.get("key", "")
        value_dict = a.get("value", {})
        if "stringValue" in value_dict:
            attrs[key] = value_dict["stringValue"]
        elif "boolValue" in value_dict:
            attrs[key] = value_dict["boolValue"]
        elif "intValue" in value_dict:
            attrs[key] = int(value_dict["intValue"])
        elif "doubleValue" in value_dict:
            attrs[key] = value_dict["doubleValue"]
    return attrs


def _span_to_event_type(span_name: str, attrs: Dict[str, Any]) -> Optional[EventType]:
    """Disambiguate event type from span name + attributes.

    4 EventTypes share gen_ai.chat — attributes determine the correct one:
      gen_ai.conversation.compacted=true → CONTEXT_COMPACTED
      gen_ai.streaming.interrupted=true → STREAM_INTERRUPTED
      gen_ai.usage.cost != None → COST_ACCOUNTED
      no special attrs → LLM_INVOKED (default)

    Other span names → direct lookup in OTEL_SPAN_NAME_TO_EVENT_TYPE.
    Unknown span names → None (skip).
    """
    if span_name not in OTEL_SPAN_NAME_TO_EVENT_TYPE:
        return None

    base_type = OTEL_SPAN_NAME_TO_EVENT_TYPE[span_name]
    if span_name == "gen_ai.chat":
        if attrs.get("gen_ai.conversation.compacted") is True:
            return EventType.CONTEXT_COMPACTED
        if attrs.get("gen_ai.streaming.interrupted") is True:
            return EventType.STREAM_INTERRUPTED
        if "gen_ai.usage.cost" in attrs:
            return EventType.COST_ACCOUNTED
        return EventType.LLM_INVOKED

    return base_type


def _span_to_event(span: Dict[str, Any]) -> CanonicalEvent:
    """Convert a single OTLP JSON span dict to a CanonicalEvent.

    event_id = "otel-" + hex(span["span_id"]) → deterministic.
    Timestamp from start_time_unix_nano → ISO 8601.
    parent_event_id from causadb.parent_event_id attribute.
    Payload contains span attributes + source span metadata.
    """
    span_id = span.get("span_id", "")
    event_id = "otel-" + span_id.lower()

    unix_nano = int(span.get("start_time_unix_nano", 0))
    timestamp = _nanos_to_iso(unix_nano)

    attrs = _extract_attrs(span.get("attributes", []))
    span_name = span.get("name", "")

    event_type = _span_to_event_type(span_name, attrs)
    if event_type is None:
        raise ValueError(f"No CausaDB mapping for span name: {span_name}")

    parent_event_id = attrs.get("causadb.parent_event_id")

    source = "otel:import"

    payload: Dict[str, Any] = {
        "otel_span_name": span_name,
        "otel_trace_id": span.get("trace_id", ""),
        "otel_span_id": span_id,
        "attributes": attrs,
    }

    metadata = EventMetadata(
        trace_id=span.get("trace_id", str(uuid.uuid4())),
        session_id=span.get("trace_id", str(uuid.uuid4())),
    )

    obj = CanonicalEvent.__new__(CanonicalEvent)
    object.__setattr__(obj, "event_id", event_id)
    object.__setattr__(obj, "event_type", event_type)
    object.__setattr__(obj, "timestamp", timestamp)
    object.__setattr__(obj, "ctx_id", attrs.get("ctx_id", "otel:import"))
    object.__setattr__(obj, "source", source)
    object.__setattr__(obj, "parent_event_id", parent_event_id)
    object.__setattr__(obj, "source_type", "agent")
    object.__setattr__(obj, "schema_version", "0.2.0")
    object.__setattr__(obj, "payload", MappingProxyType(payload))
    object.__setattr__(obj, "metadata", metadata)
    return obj


class OTelImporter:
    """Import OTLP JSON Lines span file into a CausaDB ledger.

    One class, no hierarchy (Article VIII). Delegates to LedgerWriter.append()
    for ledger writes (Article I).
    """

    def __init__(self, ledger_path: str) -> None:
        self.writer = LedgerWriter(ledger_path)
        self.ledger_path = ledger_path

    def import_file(self, file_path: str) -> dict:
        """Read OTLP JSON Lines file, convert spans to events, append to ledger.

        Dedup: checks LedgerIndex.get_offset("otel-" + span_id) before append.
        Returns summary dict with imported_events, skipped_unknown_spans, errors.
        """
        imported = 0
        skipped = 0
        errors = 0
        index = LedgerIndex(self.ledger_path)

        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    span = json.loads(line)
                    span_name = span.get("name", "")
                    span_id = span.get("span_id", "")
                    event_id = "otel-" + span_id.lower()

                    attrs_for_check = _extract_attrs(span.get("attributes", []))
                    event_type = _span_to_event_type(span_name, attrs_for_check)
                    if event_type is None:
                        skipped += 1
                        continue

                    if index.get_offset(event_id) is not None:
                        skipped += 1
                        continue

                    event = _span_to_event(span)
                    self.writer.append(event)
                    imported += 1
                except Exception:
                    errors += 1

        return {
            "imported_events": imported,
            "skipped_unknown_spans": skipped,
            "errors": errors,
        }