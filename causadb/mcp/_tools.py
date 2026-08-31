"""MCP tool implementations for CausaDB (P.15).

Thin wrappers that delegate to the existing nucleus — NO logic is
reimplemented here (Article II). Each tool accepts `ledger_path` as a
parameter so multiple ledgers can be served from one server instance
(bloqueante #7).

Fall-Closed (Article VIII): on ANY error (JSON parse, schema fail, append
ValueError), tools raise `ValueError` with a descriptive message. FastMCP
converts raised exceptions into MCP error responses.
"""
import json
import os
from typing import Any, Dict, Optional
from types import MappingProxyType

from causadb._config import CausaDBConfig
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType
from causadb._ledger_index import LedgerIndex, _slim_payload
from causadb._ledger_validator import LedgerValidator
from causadb._ledger_writer import LedgerWriter
from causadb._replay_engine import ReplayEngine
from causadb._schema_validator import validate_event_schema
from causadb._sentinel_rules import evaluate_rules


# C1 — Cap de bytes server-side (Gap de producto). ``MAX_RESPONSE_BYTES`` es
# el tope por defecto para respuestas de tools MCP que serializan eventos
# (query, feedback, stream, ocb_load_partition, resource causadb://events).
# Configurable vía env ``CAUSADB_MAX_RESPONSE_BYTES`` — se lee en call-time
# (patrón ``_tools.py:104``: os.getenv directo) para permitir ajuste por
# deployment y monkeypatch en tests.
MAX_RESPONSE_BYTES = 300_000


def _max_response_bytes() -> int:
    """Resolver el cap de bytes efectivo (env > default, mínimo 1)."""
    raw = os.getenv("CAUSADB_MAX_RESPONSE_BYTES")
    if raw is None:
        return MAX_RESPONSE_BYTES
    try:
        return max(1, int(raw))
    except ValueError:
        return MAX_RESPONSE_BYTES


def _apply_byte_cap(events: list, max_bytes: int) -> tuple:
    """Aplicar cap de bytes a una lista de eventos (C1).

    Serializa cada evento UNA sola vez (eficiencia — corrige la doble
    serialización), acumula bytes y corta al exceder el cap. Devuelve
    ``(kept_events, cap_info)`` con ``cap_info = {"truncated": bool,
    "bytes": int, "dropped_events": int, "slim_fallback": bool}``.

    Slim fallback (nunca perder el evento): si el PRIMER evento ya excede
    el cap, se degrada a ficha slim (``_slim_payload`` — claves de
    trazabilidad, sin contenido) y se mantiene como único evento.
    """
    if max_bytes < 1:
        max_bytes = 1
    kept: list = []
    total = 0
    dropped = 0
    for ev in events:
        serialized = json.dumps(ev, default=str, sort_keys=True)
        ev_bytes = len(serialized)
        if total + ev_bytes > max_bytes:
            if kept:
                # Ya hay eventos previos → este no cabe, se omite.
                dropped += 1
                continue
            # El PRIMER evento excede el cap solo → no se agrega: el slim
            # fallback (abajo) lo degrada a ficha y lo conserva.
            break
        kept.append(ev)
        total += ev_bytes

    slim_fallback = False
    if not kept and events:
        # El primer evento excede el cap → degradar a ficha slim y
        # mantenerlo (nunca devolver vacío).
        first = events[0]
        slim = dict(first)
        event = slim.get("event")
        if isinstance(event, dict) and isinstance(event.get("payload"), dict):
            slim = dict(slim)
            slim["event"] = dict(event)
            slim["event"]["payload"] = _slim_payload(event["payload"])
        kept = [slim]
        total = len(json.dumps(slim, default=str, sort_keys=True))
        dropped = len(events) - 1
        slim_fallback = True

    return kept, {
        "truncated": bool(dropped) or slim_fallback,
        "bytes": total,
        "dropped_events": dropped,
        "slim_fallback": slim_fallback,
    }


def _truncation_message(cap_info: dict) -> str:
    """Mensaje de aviso para respuestas truncadas por bytes (C1).

    Explica qué se cortó y cómo pedir más (paginación vía
    ``from_time``/``to_time``/``limit`` o ``include_payloads=false``).
    """
    parts = []
    if cap_info.get("slim_fallback"):
        parts.append(
            "El primer evento excedió el cap de bytes y fue degradado a "
            "ficha slim (include_payloads=false)."
        )
    if cap_info["dropped_events"] > 0:
        parts.append(f"{cap_info['dropped_events']} eventos omitidos por tamaño.")
    parts.append(
        "Usá include_payloads=false, include_excerpts=true, o paginá con "
        "from_time/to_time/limit para obtener el resto."
    )
    return " ".join(parts)


def _cap_array_response(events: list, max_bytes: int) -> str:
    """Serializar una lista de eventos como JSON array con cap de bytes.

    Forma preservada (array) cuando no se trunca — no rompe los callers
    existentes. Cuando el cap de bytes se dispara, devuelve un dict
    marcador ``{"events": [...], "truncated": True, "count": N,
    "message": ...}`` (mismo patrón que el cap de conteo de
    ``causadb_ocb_load_partition``).
    """
    kept, cap_info = _apply_byte_cap(events, max_bytes)
    if not cap_info["truncated"]:
        return json.dumps(kept, default=str, sort_keys=True)
    return json.dumps({
        "events": kept,
        "truncated": True,
        "count": len(events),
        "message": _truncation_message(cap_info),
    }, default=str, sort_keys=True)


def _event_from_dict(data: Dict[str, Any]) -> CanonicalEvent:
    """Build a `CanonicalEvent` from a parsed JSON dict.

    Shared conversion pipeline — mirrors `cli/_cmd_log.py`. This is NOT
    duplication of nucleus logic; it is a thin adapter that translates
    wire-format JSON to a `CanonicalEvent`. Both CLI and MCP need this
    translation, so it lives here as a private helper (could be factored
    out to `causadb/_event_from_json.py` if CLI is refactored to share it).
    """
    try:
        event_type = EventType(data["event_type"])
    except (KeyError, ValueError) as e:
        raise ValueError(f"Invalid or missing event_type: {e}")

    metadata = None
    if data.get("metadata") is not None:
        try:
            metadata = EventMetadata(**data["metadata"])
        except TypeError as e:
            raise ValueError(f"Invalid metadata: {e}")

    try:
        kwargs = dict(
            event_type=event_type,
            ctx_id=data["ctx_id"],
            source=data["source"],
            source_type=data.get("source_type", "agent"),
            payload=data.get("payload", {}),
            parent_event_id=data.get("parent_event_id"),
            schema_version=data.get("schema_version", "0.1.0"),
            metadata=metadata,
        )
        if "event_id" in data:
            kwargs["event_id"] = data["event_id"]
        if "timestamp" in data:
            kwargs["timestamp"] = data["timestamp"]
        event = CanonicalEvent(**kwargs)
    except (KeyError, ValueError, TypeError) as e:
        raise ValueError(str(e))
    return event


def causadb_log(event_json: str, ledger_path: str) -> str:
    """Append an event to the ledger.

    Pipeline (Fall-Closed):
      1. Parse `event_json` as JSON.
      2. Build a `CanonicalEvent` via `_event_from_dict`.
      3. Validate with `validate_event_schema` BEFORE appending.
      4. Append via `LedgerWriter(ledger_path, config)` where *config*
         carries `workspace_dir` from `CAUSADB_WORKSPACE_DIR` so auto
         snapshots (F.12.1) trigger on `writes` events.
      5. Return JSON string with `{"event_id", "hash", "timestamp"}`.

    On ANY error, raises `ValueError` (FastMCP converts to error response).
    """
    # 1. Parse JSON
    try:
        data = json.loads(event_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")

    # 2. Build CanonicalEvent
    event = _event_from_dict(data)

    # 3. Validate schema BEFORE appending (Fall-Closed)
    vr = validate_event_schema(event)
    if not vr.is_valid:
        raise ValueError(
            f"Schema validation failed: {vr.failure_type} — {vr.description}"
        )

    # 4. Append via LedgerWriter (Article I — Ledger Monism)
    try:
        config = CausaDBConfig(
            ledger_path=ledger_path,
            workspace_dir=os.getenv("CAUSADB_WORKSPACE_DIR"),
        )
        writer = LedgerWriter(ledger_path, config)
        writer.append(event)
    except ValueError as e:
        raise ValueError(str(e))

    # 5. Return JSON string with event_id, hash, timestamp
    return json.dumps({
        "event_id": event.event_id,
        "hash": writer.last_hash,
        "timestamp": event.timestamp,
    }, sort_keys=True)


def causadb_replay(ledger_path: str) -> str:
    """Reconstruct state from the ledger.

    Delegates to `ReplayEngine(ledger_path).reconstruct_state()` and returns
    a JSON string of the state dict.
    """
    state = ReplayEngine(ledger_path).reconstruct_state()
    return json.dumps(state, default=str, sort_keys=True)


def causadb_sentinel(ledger_path: str) -> str:
    """Run sentinel rules against the ledger.

    Delegates to `evaluate_rules(ledger_path)` and returns a JSON string with
    `{"all_rules_pass", "summary", "results": [{"rule_name", "passed"}]}`.
    """
    rep = evaluate_rules(ledger_path)
    return json.dumps({
        "all_rules_pass": rep.all_rules_pass,
        "summary": rep.summary,
        "results": [
            {"rule_name": r.rule_name, "passed": r.passed}
            for r in rep.results
        ],
    }, sort_keys=True)


def causadb_query(
    ledger_path: str,
    event_type: Optional[str] = None,
    ctx_id: Optional[str] = None,
    parent_event_id: Optional[str] = None,
    source: Optional[str] = None,
    text: Optional[str] = None,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    limit: Optional[int] = None,
    include_payloads: bool = True,
    intent_only: bool = True,
    include_excerpts: bool = False,
) -> str:
    """Query the ledger by event_type, ctx_id, parent_event_id, source,
    text, from_time, or to_time.

    All filters are optional and AND-combined. Returns a JSON string with
    an ALWAYS-present envelope: ``{"events": [...], "truncated": bool,
    "bytes": int, "dropped_events": int, "message": str}`` (nunca array
    pelado — consistente para el cliente).

    Args:
        ledger_path: absolute path to the ledger file.
        event_type: filter by event type (e.g. FILE_MODIFIED).
        ctx_id: filter by context ID.
        parent_event_id: filter by parent event ID.
        source: filter by source string.
        text: case-insensitive substring search in event payload.
        from_time: ISO 8601 string (inclusive lower bound).
        to_time: ISO 8601 string (inclusive upper bound).
        limit: máximo número de entradas a devolver. ``None`` aplica
            el cap por defecto (``DEFAULT_QUERY_LIMIT = 1000``) para
            evitar outputs gigantes (anti-gigantismo BIT-CHR.35 P3).
            Valores mayores que ``MAX_QUERY_LIMIT`` se clampean (no error).
            El cap se aplica ANTES de resolver blobs — los eventos
            truncados no tocan disco de blobs.
        include_payloads: si ``True`` (default) los payloads se resuelven
            completos (incluye ``$blob`` → contenido en disco). Si
            ``False``, los payloads se reducen a claves de trazabilidad
            (``content_hash``, ``$blob``, ``path``, etc.) sin resolver
            blobs — corta ~90% de bytes. Útil para exploración rápida
            sin saturar memoria/latencia.
        intent_only: si ``True`` (default) las búsquedas por ``text``
            excluyen REASONING_STEP y TOOL_CALLED ANTES de resolver blobs.
            Un ``event_type`` explícito gana sobre la exclusión.
        include_excerpts: si ``True`` y hay ``text``, cada resultado lleva
            ``excerpt`` (ventana ±120 chars alrededor del match).

    Returns:
        JSON string con envelope ``{"events", "truncated", "bytes",
        "dropped_events", "message"}``. ``truncated: true`` cuando el
        output serializado excede ``MAX_RESPONSE_BYTES`` (env
        ``CAUSADB_MAX_RESPONSE_BYTES``): los eventos se cortan por bytes
        (``dropped_events`` cuenta los omitidos) y ``message`` explica
        cómo pedir más. Si el PRIMER evento ya excede el cap, se degrada
        a ficha slim (``include_payloads=false``) — nunca se pierde el
        evento. Para exploración de ledgers grandes se recomienda
        ``include_payloads=false`` + ``include_excerpts=true``.
    """
    index = LedgerIndex(ledger_path)
    results = index.query(
        event_type=event_type,
        ctx_id=ctx_id,
        parent_event_id=parent_event_id,
        source=source,
        text=text,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        include_payloads=include_payloads,
        intent_only=intent_only,
        include_excerpts=include_excerpts,
    )
    # BIT-CHR.99 Gap #1 — capturar el hint antes del byte-cap (el byte-cap
    # puede truncar results, pero el hint se seteó en index.query basado
    # en el cap de CONTEO, no de bytes — son ortogonales).
    recent_hint = getattr(index, "last_query_hint", None)
    max_bytes = _max_response_bytes()
    kept, cap_info = _apply_byte_cap(results, max_bytes)
    # Extensión BIT-CHR.99 Gap #1 — el byte-cap también puede ocultar
    # recientes cuando hay pocos eventos gordos (cap de conteo no dispara,
    # pero el cap de bytes trunca los últimos). El hint del byte-cap se
    # setea solo si:
    #  - caller no usó filtros explícitos (from_time/to_time/text)
    #  - el byte-cap realmente truncó (cap_info["truncated"])
    #  - hay eventos en results no en kept (len(kept) < len(results))
    #  - el count_cap no disparó (not recent_hint — anti-duplicación)
    # Sin tocar _apply_byte_cap helper (anti-abstracción Art VIII).
    byte_cap_hint = None
    filters_used_by_caller = (
        from_time is not None or to_time is not None or text is not None
    )
    if (
        not recent_hint                       # el de conteo ya larga si aplica
        and cap_info["truncated"]
        and not filters_used_by_caller
        and len(kept) < len(results)
    ):
        max_seq_in_ledger = max(
            (r.get("event", {}).get("sequence_number", 0) for r in results),
            default=None,
        )
        last_seq_returned = (
            kept[-1].get("event", {}).get("sequence_number")
            if kept else None
        )
        if (
            max_seq_in_ledger is not None
            and last_seq_returned is not None
            and max_seq_in_ledger > last_seq_returned
        ):
            byte_cap_hint = {
                "hint": "result_capped_recents_hidden",
                "reason": "byte_cap",
                "max_seq_in_ledger": max_seq_in_ledger,
                "last_seq_returned": last_seq_returned,
                "use_from_time_to_get_recent": True,
            }
    # BIT-CHR.99 Gap #1 — propagar el hint en el envelope cuando aplica.
    # Si recent_hint is None (caller explícito, ledger < cap, sin
    # resultados), NO agregar las 3 keys — el envelope queda limpio.
    envelope = {
        "events": kept,
        "truncated": cap_info["truncated"],
        "bytes": cap_info["bytes"],
        "dropped_events": cap_info["dropped_events"],
        "message": _truncation_message(cap_info) if cap_info["truncated"] else "",
    }
    if recent_hint:
        envelope["hint"] = recent_hint["hint"]
        envelope["max_seq_in_ledger"] = recent_hint["max_seq_in_ledger"]
        envelope["last_seq_returned"] = recent_hint["last_seq_returned"]
    elif byte_cap_hint:
        envelope["hint"] = byte_cap_hint["hint"]
        envelope["reason"] = byte_cap_hint["reason"]
        envelope["max_seq_in_ledger"] = byte_cap_hint["max_seq_in_ledger"]
        envelope["last_seq_returned"] = byte_cap_hint["last_seq_returned"]
    return json.dumps(envelope, default=str, sort_keys=True)


def causadb_validate(ledger_path: str) -> str:
    """Validate the ledger hash chain integrity.

    Returns JSON with `{"is_valid", "failure_type", "position", "description"}`.
    """
    vr = LedgerValidator(ledger_path).validate_chain()
    return json.dumps({
        "is_valid": vr.is_valid,
        "failure_type": vr.failure_type,
        "position": vr.position,
        "description": vr.description,
    }, sort_keys=True)


def causadb_feedback(ledger_path: str) -> str:
    """List HUMAN_FEEDBACK events from the ledger.

    Returns a JSON string list of matching entries (array). Si el cap de
    bytes (``MAX_RESPONSE_BYTES``, env ``CAUSADB_MAX_RESPONSE_BYTES``) se
    dispara, devuelve un dict marcador ``{"events": [...], "truncated":
    True, "count": N, "message": ...}`` en vez del array.
    """
    index = LedgerIndex(ledger_path)
    results = index.query(event_type="HUMAN_FEEDBACK")
    return _cap_array_response(results, _max_response_bytes())


def causadb_sandbox(ledger_path: str) -> str:
    """Reconstruct state and return sandbox violations summary.

    Returns JSON with `{"violations", "total_mutations"}`.
    """
    state = ReplayEngine(ledger_path).reconstruct_state()
    return json.dumps({
        "violations": state["sandbox_violations"],
        "total_mutations": len(state["sandbox_mutations"]) + len(state["sandbox_violations"]),
    }, default=str, sort_keys=True)


def causadb_stream(ledger_path: str) -> str:
    """List STREAM_INTERRUPTED events from the ledger.

    Returns a JSON string list of matching entries (array). Si el cap de
    bytes (``MAX_RESPONSE_BYTES``, env ``CAUSADB_MAX_RESPONSE_BYTES``) se
    dispara, devuelve un dict marcador ``{"events": [...], "truncated":
    True, "count": N, "message": ...}`` en vez del array.
    """
    index = LedgerIndex(ledger_path)
    results = index.query(event_type="STREAM_INTERRUPTED")
    return _cap_array_response(results, _max_response_bytes())


def causadb_why(file_path: str, line_number: int, ledger_path: str) -> str:
    """Attribute a line to the event that introduced it (F.12.2).

    Thin wrapper around ``causadb._causal_attrib.attribute_line`` —
    delegates to the nucleus (Article II), no logic reimplemented here.

    Args:
        file_path: relative path of the file within the workspace snapshot.
        line_number: 1-based line number to attribute.
        ledger_path: absolute path to the ledger file.

    Returns:
        JSON string with ``{"introducer": {...}}`` or ``{"introducer": null}``.

    Raises:
        ValueError: if the file was never touched in the ledger (Fall-Closed).
    """
    from causadb._causal_attrib import attribute_line
    result = attribute_line(file_path, line_number, ledger_path)
    return json.dumps({"introducer": result}, sort_keys=True, default=str)


# F.12.4 impact — downstream causal cone.
def causadb_impact(event_id: str, ledger_path: str) -> str:
    """Return the downstream causal cone of *event_id* as a JSON string."""
    from causadb._causal_cone import trace_downstream
    result = trace_downstream(event_id, ledger_path)
    return json.dumps({
        "source_event_id": event_id,
        "tainted_count": len(result),
        "tainted_events": result,
    }, sort_keys=True)


# F.12.3 trace — upstream causal cone of a line.
def causadb_trace(file_path: str, line_number: int, ledger_path: str) -> str:
    """Return the upstream causal cone of a line as a JSON string."""
    from causadb._causal_cone import trace_upstream
    result = trace_upstream(file_path, line_number, ledger_path)
    serialisable = {
        "writer_event": result["writer_event"],
        "cone": result["cone"],
        "visited": sorted(result["visited"]),
        "depth": result["depth"],
    }
    return json.dumps(serialisable, sort_keys=True, default=str)


# F.13.3 score — efficiency score (churn + waste + survival).
def causadb_score(ledger_path: str, session: Optional[str] = None) -> str:
    """Compute the efficiency score for the given ledger (or a specific session).

    Delegates to ``compute_score`` from ``causadb._score``. The score combines
    churn/waste/survival into a 0-100 quality metric.

    Args:
        ledger_path: absolute path to the ledger file.
        session: optional ctx_id to compute score for a single session only.

    Returns:
        JSON string with ``{overall_score, churn_score, waste_score,
        survival_score, weights_used, correlation_method}``.

    Output advertises ``correlation_method: "timestamp_proximity"`` when
    applicable (LLM-waste correlation by timestamp is inherently imprecise).
    """
    from causadb._score import compute_score
    result = compute_score(ledger_path)
    if session is not None:
        # Filter per-session if requested
        per_session = result.get("per_session", {})
        result["session_filter"] = session
        result["session_result"] = per_session.get(session)
    return json.dumps(result, default=str, sort_keys=True)


# F.13.4 skill_list — list available CausaDB skills (learned context patterns).
def causadb_skill_list(ledger_path: str, skill_types: Optional[str] = None,
                      limit: Optional[int] = None, order: str = "desc") -> str:
    """List the available CausaDB skills (compressed context patterns).

    Delegates to ``load_skills`` from ``causadb._skill_registry``. Skills are
    learned patterns from previous sessions, persisted as ``SKILL_CREATED``
    events in the ledger (ledger-first design).

    Args:
        ledger_path: absolute path to the ledger file.
        skill_types: optional comma-separated list of skill types to filter
                    (e.g. ``"file_tree,conventions"``). Valid types:
                    ``file_tree | conventions | tool_patterns | decisions |
                    procedural``.
        limit: optional int — return only the first ``limit`` skills
               (after ordering). Si None, retorna todos.
        order: orden por timestamp — ``"desc"`` (default, mas recientes
               primero) o ``"asc"``.

    Returns:
        JSON string with ``{count, skills: [...], limit, order}``.
    """
    from causadb._skill_registry import load_skills

    types_filter = None
    if skill_types:
        types_filter = [t.strip() for t in skill_types.split(",") if t.strip()]

    skills = load_skills(ledger_path, types=types_filter, limit=limit, order=order)
    return json.dumps({
        "count": len(skills),
        "skills": skills,
        "limit": limit,
        "order": order,
    }, default=str, sort_keys=True)


def causadb_log_decision(
    reasoning: str,
    impact: str,
    decision_type: str,
    origin: str,
    ledger_path: str,
    alternatives_considered: Optional[list[str]] = None,
    intent_hash: Optional[str] = None,
    confidence: Optional[float] = None,
    ctx_id: Optional[str] = None,
    bit: Optional[str] = None,
) -> str:
    """Append a GOVERNANCE_DECISION event to the ledger (Capa 1 — Agent).

    Thin wrapper (Article II):
      1. Build payload from args.
      2. Create CanonicalEvent(event_type=GOVERNANCE_DECISION, ...).
      3. Validate with validate_event_schema BEFORE appending.
      4. Append via LedgerWriter.append.
      5. Return JSON string with {"event_id", "hash", "timestamp"}.

    GAP-02: si ``bit`` se pasa, el payload incluye ``bit_id`` (el ledger es
    la autoridad) y el evento se enlaza al BIT en el chronicle index
    (best-effort: si el link falla el evento ya está escrito → linked=False).

    Args:
        reasoning: The decision reasoning text (required).
        impact: Impact level — "critical", "high", "medium", or "low" (required).
        decision_type: Type of decision — "strategic", "architectural",
                      "tactical", or "revert" (required).
        origin: Origin of the decision — "agent" (explicit) or "distill"
               (automatic heuristic) (required).
        ledger_path: absolute path to the ledger file.
        alternatives_considered: optional list of alternative approaches.
        intent_hash: optional hash linking to a REASONING_STEP.
        confidence: optional float in [0.0, 1.0].
        ctx_id: optional context ID (defaults to "governance").
        bit: optional BIT name to link the decision to (GAP-02).

    Raises:
        ValueError: on schema validation failure or append failure
        (Fall-Closed — FastMCP converts to error response).
    """
    # 1. Build payload
    payload: Dict[str, Any] = {
        "reasoning": reasoning,
        "impact": impact,
        "decision_type": decision_type,
        "origin": origin,
    }
    if alternatives_considered is not None:
        payload["alternatives_considered"] = alternatives_considered
    if intent_hash is not None:
        payload["intent_hash"] = intent_hash
    if confidence is not None:
        payload["confidence"] = confidence
    if bit:
        payload["bit_id"] = bit

    # 2. Create CanonicalEvent
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id=ctx_id or "governance",
        source="causadb:agent",
        source_type="agent",
        payload=MappingProxyType(payload),
    )

    # 3. Validate schema BEFORE appending (Fall-Closed)
    vr = validate_event_schema(event)
    if not vr.is_valid:
        raise ValueError(
            f"Schema validation failed: {vr.failure_type} — {vr.description}"
        )

    # 4. Append via LedgerWriter (Article I — Ledger Monism)
    try:
        config = CausaDBConfig(
            ledger_path=ledger_path,
            workspace_dir=os.getenv("CAUSADB_WORKSPACE_DIR"),
        )
        writer = LedgerWriter(ledger_path, config)
        writer.append(event)
    except ValueError as e:
        raise ValueError(str(e))

    # 5. Return event_id, hash, timestamp (+ link best-effort al BIT, GAP-02)
    linked = False
    if bit:
        try:
            from causadb._chronicle_index import link_events
            link_events(ledger_path, bit, [event.event_id])
            linked = True
        except Exception:
            linked = False
    return json.dumps({
        "event_id": event.event_id,
        "hash": writer.last_hash,
        "timestamp": event.timestamp,
        "bit_id": bit,
        "linked": linked,
    }, sort_keys=True)


def causadb_chronicle_append(
    ledger_path: str,
    bit: str,
    title: str,
    date: str,
    author: str,
    nature: str,
    summary: Optional[str] = None,
    files: Optional[list[str]] = None,
    body: str = "",
    event_id: Optional[str] = None,
) -> str:
    """Sedimentar un BIT-entry al CAUSADB_CHRONICLE.md (template curado).

    Reemplaza el edit manual del agente sobre el Chronicle (Layer 3, humana).
    El ledger NO se toca — la alineación ledger ↔ .md la garantiza el
    ``bit_id`` compartido + el ``event_id`` opcional citado en
    ``**Referencias:**`` (formato que ``_PROSE_EVENT_ID_RE`` captura).

    Idempotente: si el ``bit_id`` exacto ya existe en el .md → retorna
    ``{"status": "already_exists", ...}`` sin duplicar.

    FAIL-CLOSED (Art. VIII): chronicle no resuelto (auto-discovery) o campos
    requeridos faltantes (bit, title, date, author, body) → ``ValueError``
    (FastMCP lo convierte en error response).

    Args:
        ledger_path: absolute path to the ledger file.
        bit: BIT id (e.g. "BIT-CHR.106").
        title: entry title.
        date: entry date YYYY-MM-DD.
        author: entry author (Maker/Checker).
        nature: entry nature (e.g. "FIX CERRADO").
        summary: optional summary (el template curado lo cubre en el body).
        files: optional list of files touched (cubierto por el body).
        body: markdown body of the entry (required).
        event_id: optional event_id to cite in **Referencias:**.

    Returns:
        JSON string with ``{"status", "bit_id", "chronicle_path"}``.

    Raises:
        ValueError: on FAIL-CLOSED (chronicle no resuelto / campos faltantes).
    """
    from causadb._chronicle_append import append_entry

    if not all([bit, title, date, author, body]):
        raise ValueError(
            "Missing required fields. Required: bit, title, date, author, body"
        )

    try:
        result = append_entry(
            ledger_path,
            chronicle_path=None,
            bit_id=bit,
            title=title,
            date=date,
            author=author,
            nature=nature,
            summary=summary,
            files=files or [],
            body=body,
            event_id=event_id,
        )
    except ValueError as e:
        raise ValueError(str(e))

    return json.dumps(result, sort_keys=True)


def causadb_revive(
    ledger_path: str,
    output_format: str = "markdown",
    max_decisions: int = 10,
) -> str:
    """Generate volatile revival context from the CausaDB ledger.

    Combines technical state (resume) with governance decisions and tool
    instructions into a single revival context for agent bootstrap.

    Args:
        ledger_path: absolute path to the ledger file.
        output_format: "markdown" (default) or "json".
        max_decisions: maximum number of governance decisions to include (default 10).

    Returns:
        Markdown or JSON string with the revival context.
    """
    from causadb.cli._cmd_revive import _run_revive
    exit_code, output = _run_revive(
        ledger_path=ledger_path,
        output_format=output_format,
        max_decisions=max_decisions,
        write_path=None,
    )
    if exit_code != 0:
        raise ValueError(output)
    return output


# F.13 — recover: storyboard completo de una sesión desde la fuente cruda.
# Thin wrapper (Article II): delega en `recover_session` / `search_stories`
# de `_recover_session.py` (Fase 13). Read-only sobre la fuente cruda
# (mode=ro) — NO toca el ledger (Art. I). Sin lógica nueva: solo dispatch.
def causadb_recover(
    ledger_path: str,
    session_id: str = "",
    tool: Optional[str] = None,
    search: Optional[str] = None,
) -> str:
    """Recupera el storyboard completo de una sesión de agente desde la fuente cruda.

    Dada una ``session_id`` (o un keyword de búsqueda), recupera el detalle
    completo de la sesión desde la FUENTE CRUDA de cada herramienta (9
    extractores: opencode, gemini, claude, grok, hermes, openjarvis, codex,
    cursor, windsurf) — NO desde el harvest normalizado, que es lossy por
    diseño. Paridad CLI ``causadb recover`` (``_cmd_recover.py``).

    Args:
        ledger_path: absolute path to the ledger file.
        session_id: id de la sesión a recuperar (ver docstring por tool).
        tool: herramienta explícita (opencode|gemini|claude|grok|hermes|
            openjarvis|codex|cursor|windsurf). Si se omite, se auto-detecta.
        search: keyword a buscar en los storyboards persistidos (Fase 12).

    Returns:
        JSON string con el envelope ``{"tool", "storyboard"}`` (recuperación
        por session_id) o ``{"matches": [...]}`` (búsqueda por keyword) —
        consistente con el CLI.

    Notes:
        - ``search`` gana sobre ``session_id`` si ambos vienen (paridad CLI
          ``_cmd_recover.py:30-31``).
        - ``session_id`` sin ``tool`` explícito: usa el ``conversation_ref``
          del state de replay para resolver el provider sin recorrer las 9
          fuentes (paridad CLI ``_cmd_recover.py:36-51``, contrato C.4.1).
          Si el ref no está disponible (sesión legacy, ledger pre-C.2),
          degrada al recorrido de fuentes (auto-detección).

    Raises:
        ValueError: on ANY error (Fall-Closed, Article VIII) — incluye
            ``SessionNotFoundError`` y ``AmbiguousSessionError``.
    """
    from causadb._recover_session import (
        AmbiguousSessionError,
        SessionNotFoundError,
        recover_session,
        search_stories,
    )
    if search:
        try:
            matches = search_stories(ledger_path, search, tool=tool)
        except (ValueError, OSError) as e:
            raise ValueError(f"recover search failed: {e}")
        return json.dumps({"matches": matches}, default=str, sort_keys=True)
    if not session_id:
        raise ValueError("Either session_id or search is required.")
    # C.4.1 — Lookup por locator: si el session_id tiene un
    # conversation_ref en el state de replay, lo pasamos a
    # recover_session para que resuelva el provider SIN recorrer
    # las 9 fuentes (paridad CLI _cmd_recover.py:36-51). `recover`
    # es operación de auditoría (no hot path), el costo de
    # reconstruct_state es aceptable (el CLI ya lo hace).
    conversation_ref = None
    if not tool:
        try:
            from causadb._replay_engine import ReplayEngine
            state = ReplayEngine(ledger_path).reconstruct_state()
            convs = state.get("conversations_recoverable", {})
            # .get en cada nivel: clave puede existir sin ref (sesión
            # con session_locator pero sin conversation_ref).
            conversation_ref = convs.get(session_id, {}).get("conversation_ref")
        except Exception:
            # Degrade gracefully: ledger corrupto, schema mismatch,
            # KeyError/AttributeError — cae al recorrido de fuentes
            # (paridad CLI:50 `except Exception`).
            conversation_ref = None
    try:
        tool_out, storyboard = recover_session(
            ledger_path, session_id, tool=tool, conversation_ref=conversation_ref
        )
    except (SessionNotFoundError, AmbiguousSessionError, ValueError) as e:
        raise ValueError(str(e))
    return json.dumps({"tool": tool_out, "storyboard": storyboard}, default=str, sort_keys=True)


# F1 (M2) — OCB status: contexto de sesión + overview de particiones.
# Thin wrapper (Article II): mergea `load_session_context()` +
# `load_context(include_metadata=...)` del OCB existente. Cap
# anti-gigantismo (BIT-CHR.35 P3): partition_metadata se captea a las
# 50 particiones más recientes; `all_partition_ids` es la lista completa.
def causadb_ocb_status(ledger_path: str, include_metadata: bool = True) -> str:
    """Return OCB session context + partition overview as a JSON string.

    Merges ``OCB.for_ledger(ledger_path).load_session_context()`` (session
    type, summary, the 2 most recent preloaded partitions) with
    ``load_context(include_metadata=...)`` (partition list + per-partition
    metadata: ``id``, ``first_timestamp``, ``last_timestamp``,
    ``session_ids``, ``sources``, ``event_types``, ``event_count``).

    Args:
        ledger_path: absolute path to the ledger file.
        include_metadata: if True (default) includes ``partition_metadata``
            (capped to the 50 most recent partitions, BIT-CHR.35 P3). If
            False, only the partition IDs are returned.

    Returns:
        JSON string with ``{session_type, summary, preloaded_partitions,
        all_partition_ids, total_partitions, [partition_metadata],
        [truncated]}``.

    Raises:
        ValueError: on ANY error (Fall-Closed, Article VIII).
    """
    from causadb._ocb_manager import OCB
    try:
        ocb = OCB.for_ledger(ledger_path)
        session_ctx = ocb.load_session_context()
        ctx = ocb.load_context(include_metadata=include_metadata)

        preloaded = session_ctx.get("preloaded_partitions", []) or []
        preloaded_ids = [
            p.get("id") if isinstance(p, dict) else p
            for p in preloaded
        ]

        result = {
            "session_type": session_ctx.get("session_type", "first_run"),
            "summary": session_ctx.get("summary", {}),
            "preloaded_partitions": preloaded_ids,
            "all_partition_ids": ctx.get("partition_ids", []),
            "total_partitions": ctx.get("count", len(ctx.get("partition_ids", []))),
        }

        if include_metadata:
            metadata = ctx.get("partition_metadata", [])
            if len(metadata) > 50:
                # Cap anti-gigantismo (BIT-CHR.35 P3): solo las 50 más
                # recientes (sort por id = time_ns = cronológico).
                metadata = sorted(metadata, key=lambda m: m.get("id", ""))[-50:]
                result["truncated"] = True
            result["partition_metadata"] = metadata

        return json.dumps(result, default=str, sort_keys=True)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"ocb_status failed: {e}")


# F1 (M2) — OCB load_partition: detalle granular de una partición.
# Thin wrapper (Article II): delega en
# ``OCB.for_ledger(ledger_path).load_partition_by_id(partition_id,
# resolve_blobs=resolve_blobs)``. Cap anti-gigantismo (BIT-CHR.35 P3):
# si la lista excede 1000 eventos, trunca a 1000.
def causadb_ocb_load_partition(
    ledger_path: str,
    partition_id: str,
    resolve_blobs: bool = True,
) -> str:
    """Load a specific OCB partition, resolving ``$blob`` refs on demand.

    Args:
        ledger_path: absolute path to the ledger file.
        partition_id: name of the partition (e.g. ``OCB_PARTITION_<ns>.log``).
        resolve_blobs: if True (default) ``$blob`` payloads are resolved
            against the BlobStore; if False they are returned as
            ``{"resolved": False, "$blob": hash}`` (metadata only, no read).

    Returns:
        JSON string list of event dicts. When the list exceeds 1000 events
        (BIT-CHR.35 P3), a dict ``{"events": [...], "truncated": True,
        "count": N}`` is returned instead. Cuando el cap de bytes
        (``MAX_RESPONSE_BYTES``, env ``CAUSADB_MAX_RESPONSE_BYTES``) se
        dispara, el dict incluye además ``message`` explicando el corte.
        Nonexistent partition → ``[]``.

    Raises:
        ValueError: on ANY error (Fall-Closed, Article VIII).
    """
    from causadb._ocb_manager import OCB
    try:
        ocb = OCB.for_ledger(ledger_path)
        events = ocb.load_partition_by_id(
            partition_id, resolve_blobs=resolve_blobs
        )
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"ocb_load_partition failed: {e}")

    total = len(events)
    if total > 1000:
        events = events[:1000]
    max_bytes = _max_response_bytes()
    kept, cap_info = _apply_byte_cap(events, max_bytes)
    if cap_info["truncated"]:
        return json.dumps({
            "events": kept,
            "truncated": True,
            "count": total,
            "message": _truncation_message(cap_info),
        }, default=str, sort_keys=True)
    if total > 1000:
        return json.dumps({
            "events": kept,
            "truncated": True,
            "count": total,
        }, default=str, sort_keys=True)
    return json.dumps(kept, default=str, sort_keys=True)


# F.14 — Shared Documents (Coordinación Multi-Agente)
# Thin wrappers (Article II): delegan en `_shared_docs.py` — no lógica nueva.
def causadb_shared_document_read(
    name: str,
    ledger_path: str
) -> str:
    """Lee anotador fijo de coordinación multi-agente.

    Args:
        name: "AUDIT_REPORT" o "ACTION_PLAN".
        ledger_path: absolute path to the ledger file.

    Returns:
        JSON string with the document content.

    Raises:
        ValueError: if name is not allowed.
    """
    from causadb._shared_docs import read_shared_doc, ALLOWED_NAMES
    if name not in ALLOWED_NAMES:
        raise ValueError(f"Nombre no permitido: {name}. Permitidos: {sorted(ALLOWED_NAMES)}")
    doc = read_shared_doc(ledger_path, name)
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True)


def causadb_shared_document_write(
    name: str,
    content: str,
    ledger_path: str
) -> str:
    """Escribe anotador fijo de coordinación multi-agente.

    Args:
        name: "AUDIT_REPORT" o "ACTION_PLAN".
        content: JSON string with the document content.
        ledger_path: absolute path to the ledger file.

    Returns:
        JSON string with {"status": "ok", "name": name}.

    Raises:
        ValueError: if name is not allowed or content is invalid JSON.
    """
    from causadb._shared_docs import write_shared_doc, ALLOWED_NAMES
    if name not in ("AUDIT_REPORT", "ACTION_PLAN"):
        raise ValueError(f"Nombre no permitido: {name}. Permitidos: AUDIT_REPORT, ACTION_PLAN")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON inválido: {e}")
    write_shared_doc(ledger_path, name, data)
    return json.dumps({"status": "ok", "name": name})
