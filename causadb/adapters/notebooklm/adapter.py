"""
CausaDB → NotebookLM / Gemini Adapter (G.1).

Adapter liviano para que NotebookLM / Gemini pueda consultar CausaDB
via tool ``causadb_query``.

Delega en ``causadb.adapters.template.query_events()`` (Artículo II —
thin wrapper). No reimplementa lógica.
"""

from typing import Any, Dict, List, Optional

from causadb.adapters.template import query_events


def query(
    query_params: Dict[str, Optional[str]],
    ledger_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Query events from CausaDB delegating to the template adapter.

    Thin wrapper around ``causadb.adapters.template.query_events()``.

    Args:
        query_params: Diccionario con filtros opcionales:
            ``event_type``, ``ctx_id``, ``parent_event_id``, ``source``.
            Los valores ``None`` se ignoran.
        ledger_path: Ruta absoluta al archivo ledger. Si se omite, se lee
            de la variable de entorno ``CAUSADB_LEDGER_PATH``.

    Returns:
        Lista de entradas de ledger (evento + hash + prev_hash) que
        coinciden con todos los filtros, ordenadas por sequence_number.

    Raises:
        ValueError: Si no se puede resolver la ruta del ledger.
    """
    return query_events(query_params, ledger_path=ledger_path)


def _get_event_field(entry: Dict[str, Any], field: str, default: Any = "?") -> Any:
    """Extract a field from a ledger entry.

    ``LedgerIndex.query()`` returns entries with the event data nested
    under ``entry["event"]``.  This helper extracts ``field`` from that
    nested dict, falling back to the top-level key, then to ``default``.
    """
    inner = entry.get("event", {})
    if isinstance(inner, dict) and field in inner:
        return inner[field]
    return entry.get(field, default)


def format_for_notebooklm(events: List[Dict[str, Any]]) -> str:
    """Format a list of ledger events as markdown for NotebookLM / Gemini.

    Cada evento se convierte en un bullet point que incluye:
      - ``event_id`` (para respuestas citadas)
      - ``event_type``
      - ``timestamp``
      - Un snippet del ``payload`` (primeros 120 caracteres)

    Args:
        events: Lista de entradas de ledger (formato devuelto por
            ``LedgerIndex.query()``, con ``event``, ``hash``, ``prev_hash``).

    Returns:
        String markdown con un bullet point por evento.
    """
    if not events:
        return "_No events found._\n"

    lines: List[str] = []
    for ev in events:
        eid = _get_event_field(ev, "event_id")
        etype = _get_event_field(ev, "event_type")
        ts = _get_event_field(ev, "timestamp")

        raw_payload = _get_event_field(ev, "payload", {})
        if isinstance(raw_payload, dict):
            payload_snippet = _truncate(str(raw_payload), 120)
        else:
            payload_snippet = _truncate(str(raw_payload), 120)

        lines.append(
            f"- **{eid}** | {etype} | {ts} | payload: `{payload_snippet}`"
        )

    return "\n".join(lines) + "\n"


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to ``max_len`` chars, appending ``...`` if trimmed."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
