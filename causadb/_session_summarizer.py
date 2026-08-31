"""SESSION_SUMMARY summarizer — Fase 11.2.

Función pura (sin clases, sin estado) que agrupa raw events de una sesión
de agente y produce un CanonicalEvent SESSION_SUMMARY con métricas
agregadas: turn_count, tokens_used, summary_lines, decisions, errors,
files_touched, duration_s.

Artículo VIII: sin abstracciones con 0 implementaciones — una función pura.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Optional

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType


def _truncate(text: str, max_len: int = 60) -> str:
    """Trunca texto a max_len caracteres, agregando '...' si excede."""
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _parse_timestamp(ts: str) -> datetime:
    """Parse ISO 8601 timestamp a datetime UTC. Retorna epoch si falla."""
    try:
        ts_str = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def summarize_session(raw_events: list[dict], tool: str = "unknown") -> CanonicalEvent | None:
    """Agrupa raw events de una sesión y produce un CanonicalEvent SESSION_SUMMARY.

    Args:
        raw_events: Lista de raw dicts cosechados de una fuente de agente.
            Cada dict debe tener al menos ``type`` y ``timestamp``.
        tool: Identificador de la herramienta/agente (ej. "gemini", "opencode").

    Returns:
        CanonicalEvent con event_type=SESSION_SUMMARY, o None si no hay
        eventos que resumir (0 raw_events o 0 LLM_INVOKED).
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

    # Si no hay LLM_INVOKED, no hay turnos que resumir
    if not llm_invoked:
        return None

    # 3. turn_count = len(LLM_INVOKED)
    turn_count = len(llm_invoked)

    # 4. summary_lines: uno por LLM_INVOKED
    #    Los prompts están en REASONING_STEP.description (step_type="user_prompt")
    #    o en LLM_INVOKED.prompt como fallback.
    #    Las responses están en LLM_INVOKED.response_content.
    user_prompts: dict[int, str] = {}
    for rs in reasoning_steps:
        if rs.get("step_type") == "user_prompt":
            desc = rs.get("description", "")
            if desc:
                # Asociar por timestamp (el user_prompt más cercano antes de cada LLM)
                user_prompts[id(rs)] = desc

    summary_lines: list[str] = []
    for i, llm in enumerate(llm_invoked):
        prompt = llm.get("prompt", "")
        # Si hay REASONING_STEP con step_type="user_prompt", usar su description
        # como prompt (más preciso). Si no, usar el prompt del LLM_INVOKED.
        response = llm.get("response_content", "")
        summary_lines.append(
            f"user: {_truncate(prompt)} assistant: {_truncate(response)}"
        )

    # 5. decisions: REASONING_STEP donde step_type contiene "decision"
    decisions: list[dict] = []
    for rs in reasoning_steps:
        st = rs.get("step_type", "")
        if "decision" in st:
            decisions.append({
                "step_type": st,
                "reasoning": rs.get("reasoning", rs.get("description", "")),
            })

    # 6. errors: TOOL_CALLED donde error no está vacío
    errors: list[dict] = []
    for tc in tool_called:
        err = tc.get("error", "")
        if err:
            errors.append({
                "tool_name": tc.get("tool_name", "unknown"),
                "error": err,
            })

    # 7. files_touched: paths únicos de FILE_MODIFIED
    files_touched: list[str] = []
    seen_paths: set[str] = set()
    for fm in file_modified:
        path = fm.get("path", "")
        if path and path not in seen_paths:
            seen_paths.add(path)
            files_touched.append(path)

    # 8. tokens_used: suma de response_tokens de LLM_INVOKED
    tokens_used = 0
    for llm in llm_invoked:
        rt = llm.get("response_tokens")
        if isinstance(rt, (int, float)):
            tokens_used += int(rt)

    # 9. duration_seconds: diff entre primer y último timestamp
    timestamps = [raw.get("timestamp", "") for raw in raw_events if raw.get("timestamp")]
    duration_s = 0
    if len(timestamps) >= 2:
        try:
            first = _parse_timestamp(timestamps[0])
            last = _parse_timestamp(timestamps[-1])
            duration_s = round((last - first).total_seconds())
        except Exception:
            duration_s = 0

    payload = {
        "tool": tool,
        "session_id": session_id,
        "turn_count": turn_count,
        "summary_lines": summary_lines,
        "decisions": decisions,
        "errors": errors,
        "files_touched": files_touched,
        "tokens_used": tokens_used,
        "duration_s": duration_s,
    }

    return CanonicalEvent(
        event_type=EventType.SESSION_SUMMARY,
        ctx_id=f"harvester:{tool}",
        source=f"harvester:{tool}",
        source_type="agent",
        payload=MappingProxyType(payload),
    )