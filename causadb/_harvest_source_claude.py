"""HarvestSource — puntita Claude Code (BIT-CL.1; ver docs/design_index.md).

Lee las sesiones de Claude Code (``~/.claude/projects/<slug>/<session>.jsonl``,
oplog JSONL) y las convierte en eventos canónicos vía el motor universal
``_agent_transcript``.

Formato real del store (verificado sobre sesiones reales el 2026-08-02 —
las 2 únicas sesiones de ``~/.claude/projects/``, ambas del proyecto
open-design, 9 líneas cada una):

  - ``{"type": "queue-operation", "operation": "enqueue"|"dequeue"}`` → SALTA
  - ``{"type": "attachment", "attachment": {...}}`` (hook_success
    SessionStart, deferred_tools_delta, agent_listing_delta, skill_listing)
    → SALTA (no es un mensaje de conversación)
  - ``{"type": "user", "message": {"role": "user", "content": [...]},
    "timestamp": "2026-07-20T15:40:15.523Z"}`` → mensaje user real.
    El ``content`` es una LISTA de bloques (``{"type": "text", "text": ...}``
    o ``{"type": "tool_result", ...}``). NO trae ``message.id``.
  - ``{"type": "assistant", "message": {"id": "<uuid>", "role": "assistant",
    "model": "<synthetic>", "usage": {...}, "content": [...]},
    "error": "authentication_failed", "isApiErrorMessage": true}`` → SALTA
    (auth fallida — dato real de AMBAS sesiones)
  - ``{"type": "last-prompt", "lastPrompt": ..., "leafUuid": ...}`` → SALTA
    (sin ``timestamp`` top-level siquiera)

Hallazgos reales del store (documentados para el reporte de auditoría):
  - **Las 2 sesiones reales son de auth fallida**: el único assistant trae
    ``isApiErrorMessage: true`` + ``error: "authentication_failed"`` y model
    ``<synthetic>`` → el harvest real de la fixture produce **0 eventos**
    (todo es salteable; solo hay un user message, que no genera evento).
    Esto es lo honesto (Artículo IX: no inventar datos de happy-path que no
    existen localmente).
  - **El mapping de happy-path (thinking/tool_use/tool_result/text/usage) se
    cubre con un unit test SINTÉTICO explícitamente no-fixture** (mismo
    precedente que ``test_step_type_mapping_unit`` de openjarvis) — NO hay
    datos reales de assistant exitoso en la máquina.
  - Los mensajes user NO traen ``message.id`` → el dedup por message id es
    exclusivo de los assistant (``message.id``); los user lines se re-parsean
    de forma idempotente (no emiten eventos).
  - El ``usage`` real trae ``input_tokens``/``output_tokens`` y los
    ``cache_creation_input_tokens``/``cache_read_input_tokens`` (→
    ``tokens.cached``), además de campos nulos de telemetría
    (``service_tier``, ``iterations``, ``speed``) que se ignoran.
  - Los timestamps son ISO 8601 UTC con ``Z`` (``2026-07-20T15:40:15.523Z``)
    → se pasan tal cual (el motor ``_harvester.normalize_timestamp`` los
    normaliza al ledger; mismo helper/patrón que gemini).
  - Claude Code NO re-emite mensajes como gemini-cli (los assistant ya van
    completos con tool_use+text+usage) → el dedup por ``message.id`` es
    defensivo vía ``last_message_id`` del cursor, no re-emisión.

Mapeo de la puntita (una línea top-level → cero o varios raw dicts):
  - ``type='user'`` con bloques ``text`` → NO genera evento; se recuerda
    ``last_user_content`` (join del texto de los bloques) como prompt para el
    LLM_INVOKED siguiente y se actualiza ``prev_timestamp``.
  - ``type='user'`` con bloques ``tool_result`` → completa el ``result`` del
    TOOL_CALLED pendiente cuyo ``tool_call_id`` matchea ``tool_use_id``
    (pairing explícito por id, patrón tool_use/tool_result de Claude). NO
    actualiza ``last_user_content`` ni ``prev_timestamp`` (un tool_result no
    es un prompt; mismo contrato que el functionResponse de gemini).
  - ``type='assistant'`` con ``message`` y SIN ``isApiErrorMessage``/``error``:
    bloques ``thinking`` → REASONING_STEP (subject sintetizado primeras 8
    palabras, ``step_type`` por heurística del motor), bloques ``tool_use`` →
    TOOL_CALLED (``name``→tool_name, ``input``→arguments, ``id``→tool_call_id,
    ``result``="" hasta que el tool_result lo complete), bloques ``text`` →
    ``response_content`` del LLM_INVOKED, ``usage`` → ``response_tokens`` via
    ``_response_tokens`` del motor + ``duration_ms`` via ``_compute_duration_ms``
    desde el user previo. LLM_INVOKED con ``model = message.model`` y
    ``prompt = last_user_content``.
  - ``type='assistant'`` con ``isApiErrorMessage: true`` o ``error`` → SALTA
    (dato real verificado).
  - ``type='tool_result'`` top-level (si apareciera) → completa resultados.
  - Cualquier otro ``type`` (``queue-operation``, ``attachment``,
    ``last-prompt``, ``summary``, ``progress``, ...) o línea sin
    ``message``/sin ``content`` → SALTA sin romper.
  - Línea corrupta/incompleta (JSON parse fail) → **tolerar y seguir** con el
    patrón gemini: el parse se detiene en esa línea (no se avanza el offset
    más allá), sin crashear; cuando la línea se complete, la siguiente corrida
    la retoma.

Nota de diseño (deviation del plan, documentada): el marker de dedup es
``__harvest_message_id`` — YA reservado en
``_harvester._CURSOR_MARKER_KEYS`` (no viaja al payload del evento, lo
filtra ``_event_from_raw``) — y no un ``__claude_msg_id`` nuevo: un marker
inédito SÍ viajaría al payload (el frozenset de markers está cerrado en el
núcleo, que Artículo II/§9 del plan prohíbe tocar).

Limitación conocida (documentada): el pairing tool_use↔tool_result es
IN-RUN. Si el assistant con ``tool_use`` se cosecha en una corrida y el user
con su ``tool_result`` recién aparece en la siguiente (escritura incremental
entre corridas), el TOOL_CALLED ya escrito conserva ``result=""`` (ledger
append-only; el resultado llega después). El barrido completo del archivo
(offset 0, el caso normal de la fixture y de la primera corrida) pareja todo
en la misma corrida.

Limitación del cursor por offset (mismo contrato que gemini): el estado
entre mensajes (``last_user_content``/``prev_timestamp``) solo se reconstruye
dentro de la ventana parseada. Si una corrida arranca a mitad de archivo
(cursor offset ya pasado el user line), el LLM_INVOKED siguiente lleva
``prompt=""`` y ``duration_ms=0`` — honesto, no inventado.

Cursor: ``{"files": {relpath: {"mtime": float, "offset": int,
"last_message_id": str | None}}}`` (patrón gemini — es un oplog JSONL).
Solo avanza sobre eventos efectivamente escritos (atomicidad, Artículo I; el
``_harvester`` pasa el prefijo escrito a ``advance_cursor``).
"""

from __future__ import annotations

import json
import os
from glob import glob
from typing import Iterator, Optional

from causadb._agent_transcript import agent_message_to_raw
from causadb._harvest_source import HarvestSource


def _derive_default_projects_dir() -> str:
    """Store de Claude Code: env override o ``~/.claude/projects``."""
    env_dir = os.environ.get("CAUSADB_CLAUDE_PROJECTS_DIR")
    if env_dir:
        return env_dir
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def _synthesize_subject(text: str) -> str:
    """Claude Code no trae subject en los ``thinking`` → la puntita
    sintetiza uno determinístico con las primeras 8 palabras del texto
    (patrón opencode/hermes/openjarvis)."""
    return " ".join(text.split()[:8])


def _join_text_blocks(content) -> str:
    """Join del texto de los bloques ``text`` de un ``content`` de mensaje
    (lista de bloques Claude). Si ``content`` ya es string plano (desviación
    de OpenClaude, Fase 5), se devuelve tal cual — defensivo, no rompe."""
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                if part.get("text") is not None:
                    texts.append(str(part["text"]))
        return "\n".join(texts)
    if isinstance(content, str):
        return content
    return ""


def _extract_tool_results(content) -> list[dict]:
    """Bloques ``tool_result`` de un ``content`` de mensaje user."""
    if not isinstance(content, list):
        return []
    return [
        part for part in content
        if isinstance(part, dict) and part.get("type") == "tool_result"
    ]


def _tool_result_content(block: dict) -> str:
    """Normaliza el ``content`` de un bloque ``tool_result`` (string o lista
    de bloques text) a un string único para el ``result`` del TOOL_CALLED."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(str(part.get("text") or ""))
        joined = "\n".join(t for t in texts if t)
        if joined:
            return joined
    if content is not None:
        return str(content)
    return ""


def _normalize_usage(usage) -> dict | None:
    """``message.usage`` de Claude → shape ``tokens`` del motor universal:
    ``{"input", "output", "cached"}`` (``cached`` = cache_creation +
    cache_read, plan §Fase 3). Los campos nulos de telemetría se ignoran."""
    if not isinstance(usage, dict):
        return None

    def _n(v) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    return {
        "input": _n(usage.get("input_tokens")),
        "output": _n(usage.get("output_tokens")),
        "cached": (
            _n(usage.get("cache_creation_input_tokens"))
            + _n(usage.get("cache_read_input_tokens"))
        ),
    }


def _normalize_assistant(obj: dict, message: dict) -> dict:
    """Normaliza una línea ``assistant`` (sin error) al shape del motor
    universal: ``content[]`` → thoughts (``thinking``) / tool_calls
    (``tool_use``) / content (``text``), ``usage`` → tokens."""
    blocks = message.get("content") or []
    thoughts: list[dict] = []
    tool_calls: list[dict] = []
    texts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "thinking":
            thinking = block.get("thinking") or ""
            thoughts.append({
                "subject": _synthesize_subject(thinking),
                "description": thinking,
            })
        elif block_type == "tool_use":
            tool_calls.append({
                "name": block.get("name"),
                "arguments": block.get("input") or {},
                # el result lo completa el tool_result del user siguiente
                "result": "",
                "timestamp": obj.get("timestamp"),
                "id": block.get("id"),
            })
        elif block_type == "text":
            texts.append(str(block.get("text") or ""))
    return {
        "kind": "assistant",
        "model": message.get("model"),
        "timestamp": obj.get("timestamp"),
        "content": "\n".join(texts),
        "thoughts": thoughts,
        "tool_calls": tool_calls,
        "tokens": _normalize_usage(message.get("usage")),
    }


def _complete_tool_results(raws: list[dict], tool_results: list[dict]) -> None:
    """Completa el ``result`` de los TOOL_CALLED pendientes cuyos
    ``tool_call_id`` matchean los ``tool_use_id`` de los ``tool_result``
    (pairing explícito por id, patrón tool_use/tool_result de Claude)."""
    for tr in tool_results:
        tool_use_id = tr.get("tool_use_id")
        if not tool_use_id:
            continue
        result = _tool_result_content(tr)
        for raw in reversed(raws):
            if (
                raw.get("type") == "TOOL_CALLED"
                and raw.get("tool_call_id") == tool_use_id
                and not raw.get("result")
            ):
                raw["result"] = result
                break


class ClaudeHarvestSource(HarvestSource):
    """Fuente de harvest para las sesiones de Claude Code.

    Args:
        ledger_path: Ruta absoluta al ledger (requerido por la clase base).
        projects_dir: Ruta al dir ``projects/`` de Claude Code (contiene
            ``<slug>/<session>.jsonl``). Default:
            ``CAUSADB_CLAUDE_PROJECTS_DIR`` o ``~/.claude/projects``
            (override para tests).
    """

    def __init__(self, ledger_path: str, projects_dir: Optional[str] = None):
        super().__init__(ledger_path)
        self.projects_dir = projects_dir or _derive_default_projects_dir()

    def source_type(self) -> str:
        # SIN colon (fix de namespace — ver plan §3)
        return "claude"

    def cursor_key(self) -> str:
        return "agent:claude"

    def detect(self) -> bool:
        if not os.path.isdir(self.projects_dir):
            return False
        return any(
            glob(os.path.join(self.projects_dir, "**", "*.jsonl"), recursive=True)
        )

    def harvest(self, cursor: dict | None = None) -> Iterator[dict]:
        cursor = cursor or {}
        files_cursor = cursor.get("files", {})
        # FIX.GEN-B: generador por archivo; _harvest_file intacto (preserva pairing)
        for relpath, fpath in self._iter_sessions_by_mtime():
            entry = files_cursor.get(relpath, {})
            yield from self._harvest_file(relpath, fpath, entry)

    # -- Internal ----------------------------------------------------------

    def _iter_sessions_by_mtime(self):
        files = sorted(
            glob(os.path.join(self.projects_dir, "**", "*.jsonl"), recursive=True),
            key=os.path.getmtime,
        )
        for fpath in files:
            yield os.path.relpath(fpath, self.projects_dir), fpath

    def _harvest_file(
        self, relpath: str, fpath: str, entry: dict
    ) -> list[dict]:
        offset = int(entry.get("offset", 0))
        mtime = os.path.getmtime(fpath)
        size = os.path.getsize(fpath)
        if offset > size:
            offset = 0  # archivo truncado/reescrito → releer desde 0
        last_message_id = entry.get("last_message_id")

        raws: list[dict] = []
        last_user_content: Optional[str] = None
        prev_timestamp: Optional[str] = None
        # Dedup defensivo por message id (Claude no re-emite, pero si el
        # offset quedó antes de un assistant ya procesado, no re-emitir).
        seen_ids: set[str] = set()
        if last_message_id:
            seen_ids.add(last_message_id)

        with open(fpath, "rb") as f:
            f.seek(offset)
            pos = offset
            for raw_line in f:
                line_end = pos + len(raw_line)
                stripped = raw_line.strip()
                if not stripped:
                    pos = line_end
                    continue
                try:
                    obj = json.loads(stripped.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Línea parcial/corrupta (patrón gemini): no romper, no
                    # avanzar más allá de la última línea válida.
                    break
                pos = line_end
                if not isinstance(obj, dict):
                    continue
                line_type = obj.get("type")

                if line_type == "user":
                    message = obj.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    tool_results = _extract_tool_results(content)
                    if tool_results:
                        _complete_tool_results(raws, tool_results)
                    text = _join_text_blocks(content)
                    if text:
                        # Prompt real → estado para el LLM_INVOKED siguiente.
                        last_user_content = text
                        prev_timestamp = obj.get("timestamp")
                    continue

                if line_type == "assistant":
                    if obj.get("isApiErrorMessage") or obj.get("error"):
                        # Auth fallida / error de API (dato real verificado).
                        continue
                    message = obj.get("message")
                    if not isinstance(message, dict):
                        continue
                    mid = message.get("id")
                    if not mid or mid in seen_ids:
                        continue  # dedup por message id
                    normalized = _normalize_assistant(obj, message)
                    msg_raws = agent_message_to_raw(
                        "claude",
                        normalized,
                        last_user_content=last_user_content,
                        prev_timestamp=prev_timestamp,
                    )
                    for raw in msg_raws:
                        raw["__harvest_file"] = relpath
                        raw["__harvest_offset"] = line_end
                        raw["__harvest_mtime"] = mtime
                        raw["__harvest_message_id"] = mid
                        # Fix A2 (Fase 13): relpath = <slug>/<session>.jsonl.
                        # El session_id es el basename (<session>.jsonl) — dos
                        # sesiones del mismo slug deben tener ids DISTINTOS.
                        raw["__harvest_session_id"] = os.path.basename(relpath)
                    raws.extend(msg_raws)
                    seen_ids.add(mid)
                    continue

                if line_type == "tool_result":
                    # Top-level (si apareciera): completa resultados igual.
                    _complete_tool_results(raws, [obj])
                    continue

                # Cualquier otro type (queue-operation, attachment,
                # last-prompt, summary, progress, ...) → saltar sin romper.
                continue

        return raws

    def advance_cursor(
        self, cursor: dict | None, harvested_raw_events: list[dict]
    ) -> dict:
        cursor = cursor or {}
        files = dict(cursor.get("files", {}))
        for ev in harvested_raw_events:
            relpath = ev.get("__harvest_file")
            if not relpath:
                continue
            entry = dict(files.get(relpath, {}))
            entry["offset"] = max(
                int(entry.get("offset", 0)), int(ev.get("__harvest_offset", 0))
            )
            entry["mtime"] = float(
                ev.get("__harvest_mtime", entry.get("mtime", 0.0))
            )
            mid = ev.get("__harvest_message_id")
            if mid:
                entry["last_message_id"] = mid
            files[relpath] = entry
        return {"files": files}
