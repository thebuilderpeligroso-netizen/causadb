"""Motor universal de transcripción de agentes (ver docs/design_index.md).

Funciones puras + dicts — sin clases (Artículo VIII). Es un helper
compartido que las "puntitas" de harvest de agentes (gemini, opencode,
claude, ...) usan para convertir mensajes normalizados en raw dicts de
eventos canónicos (``REASONING_STEP``, ``TOOL_CALLED``, ``LLM_INVOKED``).

Shape normalizado de mensaje (lo que cada puntita produce):

    {
        "kind": "user" | "assistant",
        "model": str | None,
        "timestamp": str,
        "content": str,                                  # texto del mensaje
        "thoughts": [{"subject": str, "description": str}],   # razonamiento
        "tool_calls": [{"name": str, "arguments": dict|str,
                        "result": dict|str, "timestamp": str}],
        "tokens": {"input": int, "output": int, "thoughts": int,
                   "cached": int, "tool": int, "total": int} | None,
    }

El estado entre mensajes (``last_user_content`` = prompt del user más
reciente, ``prev_timestamp`` = timestamp del user más reciente) se pasa
como argumentos → la función sigue siendo pura (auditoría del plan).

Decisión del operador (2026-07-31): el contenido COMPLETO
(razonamiento/prompts/results) se preserva — NO se recorta a 2000 chars.
El ``LedgerWriter`` (``_ledger_writer.py:177-182``) lo blob-ifica vía
``BlobStore`` (``{"$blob": hash}``) cuando está habilitado y el payload
supera el umbral; el ledger queda liviano y el contenido íntegro vive en
``blobs/`` (carga a demanda). Este módulo nunca trunca.
"""

import hashlib
import re
from datetime import datetime

from causadb._harvester import normalize_timestamp

# ---------------------------------------------------------------------------
# step_type heurística (enum cerrado de REASONING_STEP en _schema_validator:
# plan / analysis / decision / reflection)
# ---------------------------------------------------------------------------

_PLAN_RE = re.compile(
    r"\b(plan|plans|planned|planning|strategy|strategic|roadmap|approach|"
    r"sequenc|todo list|next steps?)\b",
    re.IGNORECASE,
)
_DECISION_RE = re.compile(
    r"\b(decid\w*|choices?|select\w*|determin\w*|opt(?:ions?|ing)?|"
    r"weigh(?:ing)?|trade-?off)\b",
    re.IGNORECASE,
)
_REFLECTION_RE = re.compile(
    r"\b(reflect\w*|re-examin\w*|reconsider\w*|review\w*|assess\w*|"
    r"evaluat\w*|retrospect\w*|lessons?|revisiting)\b",
    re.IGNORECASE,
)


def infer_step_type(subject: str | None) -> str:
    """Infiera el ``step_type`` de un thought por heurística del subject.

    Fallback determinístico: ``"analysis"`` cuando no hay subject o no
    matchea ninguna heurística. El orden de chequeo (plan → decision →
    reflection → analysis) está fijado para que el mapping sea
    determinístico.
    """
    if not subject:
        return "analysis"
    if _PLAN_RE.search(subject):
        return "plan"
    if _DECISION_RE.search(subject):
        return "decision"
    if _REFLECTION_RE.search(subject):
        return "reflection"
    return "analysis"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compute_duration_ms(prev_timestamp: str | None, current_timestamp: str | None) -> int:
    """Milisegundos entre dos timestamps ISO. Determinístico: 0 si alguno
    no es parseable o no hay previo."""
    if not prev_timestamp or not current_timestamp:
        return 0
    try:
        t0 = datetime.fromisoformat(normalize_timestamp(prev_timestamp).replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(normalize_timestamp(current_timestamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0
    delta_ms = int((t1 - t0).total_seconds() * 1000)
    return max(0, delta_ms)


def _response_tokens(tokens: dict | None) -> int:
    if not tokens:
        return 0
    try:
        return int(tokens.get("output") or tokens.get("total") or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Motor
# ---------------------------------------------------------------------------

def agent_message_to_raw(
    tool_id: str,
    msg: dict,
    last_user_content: str | None = None,
    prev_timestamp: str | None = None,
) -> list[dict]:
    """Mapea UN mensaje normalizado a raw dicts de eventos canónicos.

    Args:
        tool_id: Identificador de la herramienta de agente ("gemini",
            "opencode", ...) — se atribuye en el payload como ``agent``.
        msg: Mensaje en el shape normalizado.
        last_user_content: Contenido del mensaje de user más reciente
            (el "prompt" para LLM_INVOKED). Estado pasado por el caller
            para preservar la pureza.
        prev_timestamp: Timestamp del user más reciente (para
            ``duration_ms``).

    Returns:
        Lista de raw dicts con ``type``, ``timestamp`` y campos payload.
        Orden por mensaje: REASONING_STEP(s) → TOOL_CALLED(s) →
        LLM_INVOKED (si assistant + model).
    """
    raws: list[dict] = []
    ts = msg.get("timestamp") or ""
    kind = msg.get("kind")
    model = msg.get("model")

    # -- thoughts → REASONING_STEP --
    for thought in msg.get("thoughts") or []:
        description = thought.get("description") or ""
        subject = thought.get("subject")
        raws.append({
            "type": "REASONING_STEP",
            "timestamp": ts,
            "step_type": infer_step_type(subject),
            "step_hash": _sha256(description),
            "subject": subject,
            "description": description,
            "agent": tool_id,
        })

    # -- tool_calls → TOOL_CALLED --
    for tc in msg.get("tool_calls") or []:
        tc_ts = tc.get("timestamp") or ts
        raws.append({
            "type": "TOOL_CALLED",
            "timestamp": tc_ts,
            "tool_name": tc.get("name") or "unknown_tool",
            "arguments": tc.get("arguments") or {},
            "result": tc.get("result") or "",
            "tool_call_id": tc.get("id"),
            "agent": tool_id,
        })

    # -- assistant + model → LLM_INVOKED --
    if kind == "assistant" and model:
        raws.append({
            "type": "LLM_INVOKED",
            "timestamp": ts,
            "model": model,
            "prompt": last_user_content or "",
            "response_tokens": _response_tokens(msg.get("tokens")),
            "duration_ms": _compute_duration_ms(prev_timestamp, ts),
            "response_content": msg.get("content") or "",
            "agent": tool_id,
        })

    return raws
