"""StoryBoard persistente — Fase 12 (ver Chronicle; ver docs/design_index.md).

Función pura (sin clases, sin estado, sin I/O) que agrupa raw events de
una sesión de agente y produce un dict con el detalle completo de la
sesión: turns (prompt, assistant_response, reasoning), tool_calls,
files_touched, decisions, errors, tokens_used y duration_s.

El Harvester persiste el dict como archivo
``<storyboard_path>/<tool>/<session_id>.json`` (artículo I: archivo de
detalle en disco, NO un evento del ledger — toda escritura al ledger
sigue pasando por LedgerWriter).

Artículo VIII: sin abstracciones con 0 implementaciones — una función pura.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _parse_timestamp(ts) -> datetime:
    """Parse ISO 8601 timestamp a datetime UTC. Retorna epoch si falla."""
    try:
        ts_str = str(ts).replace("Z", "+00:00")
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def sanitize_session_id(session_id) -> str:
    """Sanitiza un session_id para usarlo como nombre de archivo.

    Neutraliza separadores de ruta (``/``, ``\\``), secuencias ``..``
    (path traversal) y caracteres peligrosos; retorna un componente de
    nombre de archivo seguro (artículo IX — seguridad real, no teatro).
    """
    if not session_id:
        return "unknown"
    s = str(session_id).replace("/", "_").replace("\\", "_").replace("..", "__")
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in s)
    safe = safe.strip("._")
    return safe or "unknown"


def build_storyboard(raw_events: list[dict], tool: str = "unknown") -> dict | None:
    """Construye el storyboard de una sesión desde raw events normalizados.

    Args:
        raw_events: Lista de raw dicts cosechados de una fuente de agente.
            Cada dict debe tener al menos ``type`` y ``timestamp``.
        tool: Identificador de la herramienta/agente (ej. "gemini", "opencode").

    Returns:
        dict con el detalle de la sesión, o None si no hay eventos que
        detallar (0 raw_events o 0 LLM_INVOKED).
    """
    if not raw_events:
        return None

    # 1. Extraer session_id del primer raw event
    session_id = raw_events[0].get("__harvest_session_id", "unknown")

    # 2. Agrupar raw events por tipo
    llm_invoked: list[dict] = []
    tool_called: list[dict] = []
    reasoning_steps: list[dict] = []
    file_modified: list[dict] = []

    for raw in raw_events:
        t = raw.get("type", "")
        if t == "LLM_INVOKED":
            llm_invoked.append(raw)
        elif t == "TOOL_CALLED":
            tool_called.append(raw)
        elif t == "REASONING_STEP":
            reasoning_steps.append(raw)
        elif t == "FILE_MODIFIED":
            file_modified.append(raw)

    # Si no hay LLM_INVOKED, no hay turnos que detallar
    if not llm_invoked:
        return None

    turn_count = len(llm_invoked)

    # 3. turns: uno por LLM_INVOKED, en orden de aparición. Cada turno
    #    consume la ventana de eventos anterior: el user_prompt más
    #    reciente (REASONING_STEP step_type="user_prompt") y el reasoning
    #    (pasos no-user_prompt de la ventana). Fallback de prompt: el
    #    campo ``prompt`` del propio LLM_INVOKED.
    turns: list[dict] = []
    pending_prompt = ""
    pending_reasoning: list[str] = []
    for raw in raw_events:
        t = raw.get("type", "")
        if t == "REASONING_STEP":
            if raw.get("step_type") == "user_prompt":
                # Asignar siempre (incluso vacío) para que un user_prompt
                # con description vacía no herede el prompt del turno previo
                pending_prompt = raw.get("description", "") or ""
            else:
                desc = raw.get("description", "") or raw.get("reasoning", "")
                if desc:
                    pending_reasoning.append(desc)
        elif t == "LLM_INVOKED":
            turns.append({
                "prompt": pending_prompt or raw.get("prompt", "") or "",
                "assistant_response": raw.get("response_content", "") or "",
                "reasoning": list(pending_reasoning),
                "timestamp": raw.get("timestamp", ""),
            })
            pending_prompt = ""
            pending_reasoning = []

    # 4. tool_calls: detalle completo en orden de aparición
    tool_calls: list[dict] = []
    for tc in tool_called:
        entry = {
            "tool_name": tc.get("tool_name", "unknown"),
            "input": tc.get("input") or tc.get("arguments") or "",
            "result": tc.get("result", ""),
            "timestamp": tc.get("timestamp", ""),
        }
        err = tc.get("error", "")
        if err:
            entry["error"] = err
        tool_calls.append(entry)

    # 5. files_touched: paths únicos de FILE_MODIFIED
    files_touched: list[str] = []
    seen_paths: set[str] = set()
    for fm in file_modified:
        path = fm.get("path", "")
        if path and path not in seen_paths:
            seen_paths.add(path)
            files_touched.append(path)

    # 6. decisions: REASONING_STEP donde step_type contiene "decision"
    decisions: list[dict] = []
    for rs in reasoning_steps:
        st = rs.get("step_type", "")
        if "decision" in st:
            decisions.append({
                "step_type": st,
                "reasoning": rs.get("reasoning", rs.get("description", "")),
            })

    # 7. errors: TOOL_CALLED donde error no está vacío
    errors: list[dict] = []
    for tc in tool_called:
        err = tc.get("error", "")
        if err:
            errors.append({
                "tool_name": tc.get("tool_name", "unknown"),
                "error": err,
            })

    # 8. tokens_used: suma de response_tokens de LLM_INVOKED
    tokens_used = 0
    for llm in llm_invoked:
        rt = llm.get("response_tokens")
        if isinstance(rt, (int, float)):
            tokens_used += int(rt)

    # 9. duration_s: diff entre primer y último timestamp
    timestamps = [raw.get("timestamp", "") for raw in raw_events if raw.get("timestamp")]
    duration_s = 0
    if len(timestamps) >= 2:
        try:
            first = _parse_timestamp(timestamps[0])
            last = _parse_timestamp(timestamps[-1])
            duration_s = round((last - first).total_seconds())
        except Exception:
            duration_s = 0

    # 10. created_at: momento de construcción (ISO 8601 UTC con Z)
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "tool": tool,
        "session_id": session_id,
        "created_at": created_at,
        "turn_count": turn_count,
        "turns": turns,
        "tool_calls": tool_calls,
        "files_touched": files_touched,
        "decisions": decisions,
        "errors": errors,
        "tokens_used": tokens_used,
        "duration_s": duration_s,
    }
