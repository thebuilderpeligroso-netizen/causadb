import json
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from causadb._ledger_reader import LedgerReader
from causadb._blob_store import BlobStore, resolve_payload


_NOISE_TYPES = {"REASONING_STEP", "TOOL_CALLED"}
_EXCERPT_CONTEXT = 120


def query_events(
    ledger_path: str,
    event_type: Optional[str] = None,
    ctx_id: Optional[str] = None,
    parent_event_id: Optional[str] = None,
    source: Optional[str] = None,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    text: Optional[str] = None,
    limit: Optional[int] = None,
    resolve_blobs: bool = True,
    intent_only: bool = True,
    include_excerpts: bool = False,
) -> List[Dict[str, Any]]:
    """Query ledger events with AND-combined filters.

    Filters are applied to event metadata BEFORE resolving blob payloads
    (BIT-CHR.35 P3): entries are read with ``resolve_blobs=False`` so blob
    storage is only touched for events that pass the cheap filters (and the
    ``limit`` cap, when given).

    Args:
        ledger_path: Absolute path to the ledger file.
        event_type: Exact match on event_type (e.g. "FILE_MODIFIED").
        ctx_id: Exact match on ctx_id.
        parent_event_id: Exact match on parent_event_id.
        source: Exact match on source (e.g. "harvester:opencode").
        from_time: ISO 8601 string (inclusive lower bound).
        to_time: ISO 8601 string (inclusive upper bound).
        text: Case-insensitive substring search in JSON-serialized payload.
            Requires blob resolution per candidate, so the matched events
            are returned with their payloads materialized.
        limit: Maximum number of events to return. Applied BEFORE blob
            resolution — truncated events never touch blob storage. If
            ``None`` no cap is applied (callers that need anti-gigantism
            pass an explicit limit).
        resolve_blobs: If ``True`` (default) payloads of the final results
            are materialized (``$blob`` refs resolved from disk). If
            ``False`` raw payloads are returned (``$blob`` refs kept as-is).

    Returns:
        List of matching event dicts. Empty list if no matches or if
        *from_time* is malformed (no crash).
    """
    # Pre-validate time filters.
    # from_time malformed → return [] per spec.
    if from_time is not None:
        try:
            _parse_iso(from_time)
        except (ValueError, TypeError):
            return []
    # to_time malformed → silently skip the filter (no crash).
    _to_time_valid = True
    if to_time is not None:
        try:
            _parse_iso(to_time)
        except (ValueError, TypeError):
            _to_time_valid = False

    # limit <= 0 → empty result (matches LedgerIndex.query semantics).
    if limit is not None and limit <= 0:
        return []

    reader = LedgerReader(ledger_path)
    blob_store = BlobStore(os.path.join(os.path.dirname(ledger_path), "blobs"))
    results: List[Dict[str, Any]] = []

    # Read entries WITHOUT resolving blobs: payloads stay raw ($blob ref or
    # inline) and cheap metadata filters run before any blob I/O. This keeps
    # the memory peak bounded by the filtered/truncated result set instead of
    # the whole ledger (43K resolved blobs per request, BIT-CHR.42).
    for entry in reader.read_all_entries(resolve_blobs=False):
        event = entry["event"]

        # Filter: event_type (exact match)
        if event_type is not None and event.get("event_type") != event_type:
            continue

        # Filter: ctx_id (exact match)
        if ctx_id is not None and event.get("ctx_id") != ctx_id:
            continue

        # Filter: parent_event_id (exact match)
        if parent_event_id is not None and event.get("parent_event_id") != parent_event_id:
            continue

        # Filter: source (exact match)
        if source is not None and event.get("source") != source:
            continue

        # Filter: from_time (inclusive)
        if from_time is not None:
            try:
                event_dt = _parse_iso(event.get("timestamp", ""))
                from_dt = _parse_iso(from_time)
                if event_dt < from_dt:
                    continue
            except (ValueError, TypeError):
                continue

        # Filter: to_time (inclusive) — only if valid
        if to_time is not None and _to_time_valid:
            try:
                event_dt = _parse_iso(event.get("timestamp", ""))
                to_dt = _parse_iso(to_time)
                if event_dt > to_dt:
                    continue
            except (ValueError, TypeError):
                continue

        # Filter: text — case-insensitive substring in JSON payload repr.
        # Requires the payload, so resolve the blob ONLY of this candidate.
        if text is not None:
            # Q.1 — intent_only: excluye REASONING_STEP/TOOL_CALLED de la búsqueda
            # de texto por defecto (son ruido de razonamiento y sus blobs son el
            # ~98% del peso). La exclusión ocurre ANTES de resolver el blob para
            # no tocar blob storage de eventos excluidos. Un event_type explícito
            # gana sobre la exclusión (el caller sabe lo que quiere).
            if (
                intent_only
                and event_type is None
                and event.get("event_type") in _NOISE_TYPES
            ):
                continue
            payload = resolve_payload(event.get("payload", {}), blob_store)
            payload_str = json.dumps(payload, sort_keys=True)
            lower_str = payload_str.lower()
            if text.lower() not in lower_str:
                continue
            if include_excerpts:
                event["excerpt"] = _extract_excerpt(payload_str, text.lower(), lower_str)
            event["payload"] = payload
            results.append(event)
            if limit is not None and len(results) >= limit:
                break
            continue

        results.append(event)
        if limit is not None and len(results) >= limit:
            break

    # Materialize payloads ONLY for events that passed the filters (and the
    # limit cap). With resolve_blobs=False the raw payload is kept as-is.
    if text is None and resolve_blobs:
        for event in results:
            event["payload"] = resolve_payload(event.get("payload", {}), blob_store)

    return results


def _parse_iso(iso_str: str) -> datetime:
    """Parse ISO 8601 string, handling 'Z' suffix for Python 3.10 compat.

    Returns a tz-aware UTC datetime: date-only strings (``YYYY-MM-DD``)
    parse to naive, which would break comparisons against ledger timestamps
    (aware UTC) with TypeError. Treat naive as UTC (BIT-CHR.114).
    """
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _extract_excerpt(payload_str: str, needle: str, lower_str: Optional[str] = None) -> str:
    """Q.2 — Extract a bounded excerpt (±_EXCERPT_CONTEXT chars) around the first
    occurrence of *needle* in the serialized payload.

    *payload_str* es el dump ORIGINAL (case preservado); *needle* va en
    lowercase. *lower_str* (si se da) es el dump lowercase — el índice del
    match se calcula sobre él pero la ventana se recorta sobre el original
    para no perder la fidelidad de display (MENOR-Checker Q.2).

    Anti-teatro (Art. IX): el excerpt SIEMPRE incluye el needle y su contexto
    adyacente — nunca es igual al needle solo (la ventana se expande a derecha
    para incluir el needle completo cuando el match está al inicio).
    """
    haystack = lower_str if lower_str is not None else payload_str.lower()
    idx = haystack.find(needle)
    if idx < 0:
        return payload_str
    start = max(0, idx - _EXCERPT_CONTEXT)
    end = min(len(payload_str), idx + len(needle) + _EXCERPT_CONTEXT)
    excerpt = payload_str[start:end]
    if start > 0:
        excerpt = f"...{excerpt}"
    if end < len(payload_str):
        excerpt = f"{excerpt}..."
    return excerpt
