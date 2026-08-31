"""HarvestSource — puntita Grok Build (BIT-GK.1; ver docs/design_index.md).

Lee las sesiones de Grok Build (xai-org/grok-build, ``~/.grok/sessions/
<enc-cwd>/<session-id>/``) y las convierte en eventos canónicos vía el
motor universal ``_agent_transcript``.

Formato real del store (verificado el 2026-08-02 — auditoría del operador
sobre la única sesión: ``%2Fhome%2Fjuliussb/019f492c-79d3-75b3-9651-95142b28c3c6``,
3 líneas / 1.619 bytes):

  - **``updates.jsonl`` (3 líneas) = fuente primaria (DECISIÓN del
    operador, ver desviación abajo).** Es JSON-RPC ``session/update``:
    ``{"timestamp": <epoch SEGUNDOS>, "method": "session/update" |
    "_x.ai/session/update", "params": {"sessionId", "update":
    {"sessionUpdate": <str>, ..., "_meta": {"eventId", "modelId?"}}}}``.
    Los 3 ``sessionUpdate`` reales (orden exacto):
      - ``retry_state`` (``type:"failed"``, ``error_type:"api"``,
        ``message`` = error 403 auth) → SALTAR (fallo/error).
      - ``user_message_chunk`` (``content:{type:"text", text:"."}``,
        ``_meta.modelId:"grok-4.5"``) → acumula el prompt del user
        (``last_user_content``), captura ``_meta.modelId`` como model de
        sesión, NO emite evento.
      - ``turn_completed`` (``stop_reason:"error"``, ``agent_result`` =
        texto del error, ``prompt_id``) → SALTAR (turno fallido); si
        ``stop_reason`` fuera normal y hubiera ``agent_result`` →
        LLM_INVOKED (ver mapeo abajo).
  - El ``_meta.modelId`` aparece SOLO en ``user_message_chunk`` (no en
    ``turn_completed``) → el model de la sesión se resuelve del PRIMER
    ``_meta.modelId`` del stream; si no apareciera, de
    ``summary.json.current_model_id`` del mismo dir si el archivo existe;
    si tampoco → NO emitir LLM_INVOKED (Art. IX: no inventar model).
  - ``summary.json``: metadata (``current_model_id:"grok-4.5"``,
    ``created_at/updated_at`` ISO, ``num_messages``). Fuente de model
    (fallback), NO emite eventos.
  - ``chat_history.jsonl`` (5 líneas) = NO fuente primaria (DECISIÓN del
    operador): las 5 líneas son ``system`` (system prompt) + 4 ``user``
    (1 user_info + 2 synthetic ``system_reminder`` + ``<user_query>
    .</user_query>``); NO tienen timestamp ni id y NO hay línea
    ``assistant`` (la sesión es un turno fallido). Contradice el sesgo del
    plan ("chat_history primaria"): el user guide oficial (17-sessions.md)
    define ``updates.jsonl`` como "the authoritative conversation log"
    (conversation + tool calls) que impulsa ``/resume`` → updates.jsonl
    es la primaria. DESVIACIÓN del plan documentada.
  - ``events.jsonl`` (9 líneas): ciclo de vida (MCP, ``turn_started`` turn
    0, ``turn_ended`` outcome:"error") → NO primaria (duplicado de ciclo,
    sin contenido).
  - ``signals.json``: NO EXISTE en esta sesión → sin token usage real →
    ``response_tokens`` no disponible (no inventar; el motor usa
    ``_response_tokens`` que con tokens None da 0).
  - ``rewind_points.jsonl``: snapshots de archivos → no harvestable.
  - ``session_search.sqlite``: índice FTS5 de ``grok sessions search`` →
    NO fuente.
  - ``prompt_history.jsonl``: 1 línea ``{timestamp, session_id,
    prompt:"."}`` → complemento opcional (el prompt real).

Consecuencia honesta (Artículo IX): la ÚNICA sesión real es un turno
FALLIDO (auth 403) → el harvest real de la fixture produce **0 eventos**.
El happy-path (user_message_chunk + turn_completed ``stop_reason`` normal
+ ``agent_result``) se cubre con un unit test SINTÉTICO explícitamente
no-fixture (precedente ``test_synthetic_happy_path_mapping`` de claude)
sobre la forma VERIFICADA — NO inventar tipos de ``sessionUpdate`` no
verificados: si un stream saludable trae tipos adicionales (ej. chunks de
assistant/tool calls del ACP), se SALTAN sin romper y sin emitir hasta
verificar con datos reales.

Mapeo de la puntita (una línea top-level → cero o un raw dict):
  - ``sessionUpdate='retry_state'`` → SALTA (fallo/error).
  - ``sessionUpdate='user_message_chunk'`` → NO emite evento; si el
    ``content.text`` no es vacío acumula ``last_user_content`` (append con
    join) y actualiza ``prev_timestamp``; si trae ``_meta.modelId`` y no
    hay ``session_model`` aún lo captura (primer modelId del stream).
    Un ``content`` que no sea ``{type:"text", text:...}`` se ignora
    defensivamente.
  - ``sessionUpdate='turn_completed'`` → si ``stop_reason == "error"`` o
    no hay ``agent_result`` → SALTA (turno fallido, dato real). Si
    ``stop_reason`` normal + ``agent_result`` + model resuelto
    (stream → summary.json) → LLM_INVOKED vía el motor con
    ``prompt=last_user_content``, ``duration_ms`` vía
    ``_compute_duration_ms(prev_timestamp, ts)`` (dentro del motor).
  - Cualquier otro ``sessionUpdate`` (tipos no verificados con datos
    reales) → SALTA sin romper; NO se emiten hasta verificar (Art. IX).
  - Línea corrupta/incompleta (JSON parse fail) → **tolerar y seguir** con
    el patrón gemini/claude: el parse se detiene en esa línea (no se
    avanza el offset más allá), sin crashear; cuando la línea se complete,
    la siguiente corrida la retoma.

Nota de diseño: los markers anexados a cada raw son ``__harvest_file`` /
``__harvest_offset`` / ``__harvest_mtime`` — los 3 YA reservados en
``_harvester._CURSOR_MARKER_KEYS`` (no viajan al payload del evento, lo
filtra ``_event_from_raw``; ver nota de ``__harvest_message_id`` en
claude). NO hay message ids en este formato → NO se anexa
``__harvest_message_id`` ni markers inéditos (el frozenset de markers del
núcleo está cerrado, y un marker inédito SÍ viajaría al payload).

Limitación del cursor por offset (mismo contrato que gemini/claude): el
estado entre mensajes (``last_user_content``/``prev_timestamp``/
``session_model``) solo se reconstruye dentro de la ventana parseada. Si
una corrida arranca a mitad de archivo (cursor offset ya pasó el
``user_message_chunk``), el LLM_INVOKED siguiente lleva ``prompt=""`` y
``duration_ms=0`` — honesto, no inventado (el model igual se resuelve del
summary.json si el stream no aportó modelId en la ventana).

Cursor: ``{"files": {relpath: {"mtime": float, "offset": int}}}`` (patrón
claude/gemini — es un oplog JSONL, sin ``last_message_id`` porque no hay
message ids en este formato). Solo avanza sobre eventos efectivamente
escritos (atomicidad, Artículo I; el ``_harvester`` pasa el prefijo
escrito a ``advance_cursor``).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from glob import glob
from typing import Iterator, Optional

from causadb._agent_transcript import agent_message_to_raw
from causadb._harvest_source import HarvestSource


def _derive_default_sessions_dir() -> str:
    """Store de Grok Build: env override o ``~/.grok/sessions``."""
    env_dir = os.environ.get("CAUSADB_GROK_SESSIONS_DIR")
    if env_dir:
        return env_dir
    return os.path.join(os.path.expanduser("~"), ".grok", "sessions")


def _s_to_iso(ts: float) -> str:
    """Segundos (epoch UTC, entero/REAL en updates.jsonl) → ISO 8601 con Z
    (formato canónico del ledger). Determinístico y puro."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _read_summary_model(fpath: str) -> Optional[str]:
    """Fallback de model: ``summary.json.current_model_id`` del mismo dir
    de la sesión (dato real del store). None si el archivo no existe, no
    parsea o no trae el campo."""
    summary_path = os.path.join(os.path.dirname(fpath), "summary.json")
    if not os.path.isfile(summary_path):
        return None
    try:
        with open(summary_path, encoding="utf-8") as f:
            summary = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    model = summary.get("current_model_id")
    if isinstance(model, str) and model:
        return model
    return None


class GrokHarvestSource(HarvestSource):
    """Fuente de harvest para las sesiones de Grok Build.

    Args:
        ledger_path: Ruta absoluta al ledger (requerido por la clase base).
        sessions_dir: Ruta al dir ``sessions/`` de Grok Build (contiene
            ``<enc-cwd>/<session-id>/updates.jsonl``). Default:
            ``CAUSADB_GROK_SESSIONS_DIR`` o ``~/.grok/sessions``
            (override para tests).
    """

    def __init__(self, ledger_path: str, sessions_dir: Optional[str] = None):
        super().__init__(ledger_path)
        self.sessions_dir = sessions_dir or _derive_default_sessions_dir()

    def source_type(self) -> str:
        # SIN colon (fix de namespace — ver plan §3)
        return "grok"

    def cursor_key(self) -> str:
        return "agent:grok"

    def detect(self) -> bool:
        if not os.path.isdir(self.sessions_dir):
            return False
        return any(
            glob(
                os.path.join(self.sessions_dir, "**", "updates.jsonl"),
                recursive=True,
            )
        )

    def harvest(self, cursor: dict | None = None) -> Iterator[dict]:
        cursor = cursor or {}
        files_cursor = cursor.get("files", {})
        for relpath, fpath in self._iter_sessions_by_mtime():
            entry = files_cursor.get(relpath, {})
            yield from self._harvest_file(relpath, fpath, entry)

    # -- Internal ----------------------------------------------------------

    def _iter_sessions_by_mtime(self):
        files = sorted(
            glob(
                os.path.join(self.sessions_dir, "**", "updates.jsonl"),
                recursive=True,
            ),
            key=os.path.getmtime,
        )
        for fpath in files:
            yield os.path.relpath(fpath, self.sessions_dir), fpath

    def _harvest_file(
        self, relpath: str, fpath: str, entry: dict
    ) -> list[dict]:
        offset = int(entry.get("offset", 0))
        mtime = os.path.getmtime(fpath)
        size = os.path.getsize(fpath)
        if offset > size:
            offset = 0  # archivo truncado/reescrito → releer desde 0

        raws: list[dict] = []
        # Estado entre mensajes (solo dentro de la ventana parseada — ver
        # limitación del cursor por offset en el docstring).
        last_user_content: Optional[str] = None
        prev_timestamp: Optional[str] = None
        # Model de la sesión: primer _meta.modelId del stream; si no
        # apareciera, fallback a summary.json.current_model_id (per-file).
        session_model: Optional[str] = None
        assistant_thoughts: list[dict] = []
        assistant_content: list[str] = []

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
                    # Línea parcial/corrupta (patrón gemini/claude): no
                    # romper, no avanzar más allá de la última línea válida.
                    break
                pos = line_end
                if not isinstance(obj, dict):
                    continue

                params = obj.get("params")
                update = params.get("update") if isinstance(params, dict) else None
                if not isinstance(update, dict):
                    continue  # sin sessionUpdate → saltear sin romper
                session_update = update.get("sessionUpdate")
                ts_iso = _s_to_iso(obj.get("timestamp", 0))

                if session_update == "retry_state":
                    # Fallo/error (dato real: auth 403) → SALTA.
                    continue

                if session_update == "user_message_chunk":
                    # Prompt del user (acumulado por chunks) + model del
                    # stream. NO emite evento.
                    content = update.get("content")
                    if isinstance(content, dict) and content.get("type") == "text":
                        text = content.get("text")
                        if text:
                            # Append con join (chunks parciales del prompt).
                            if last_user_content is None:
                                last_user_content = str(text)
                            else:
                                last_user_content = last_user_content + "\n" + str(text)
                            prev_timestamp = ts_iso
                    meta = update.get("_meta")
                    if (
                        session_model is None
                        and isinstance(meta, dict)
                        and meta.get("modelId")
                    ):
                        session_model = meta.get("modelId")
                    continue

                if session_update in ("agent_thought_chunk", "agent_message_chunk"):
                    content = update.get("content")
                    text = content.get("text") if isinstance(content, dict) else None
                    if text:
                        if session_update == "agent_thought_chunk":
                            assistant_thoughts.append({
                                "subject": None,
                                "description": str(text),
                            })
                        else:
                            assistant_content.append(str(text))
                    continue

                if session_update == "turn_completed":
                    stop_reason = update.get("stop_reason")
                    agent_result = update.get("agent_result")
                    if stop_reason == "error":
                        # Turno fallido (dato real) / sin respuesta → SALTA.
                        continue
                    # Current Grok streams carry the assistant response in
                    # agent_message_chunk and finish with end_turn; older
                    # streams put it directly in agent_result.
                    response_content = agent_result or "".join(assistant_content)
                    if not response_content:
                        continue
                    model = session_model
                    if not model:
                        # Sin modelId en el stream → summary.json del mismo
                        # dir (fallback); si tampoco → no inventar model.
                        model = _read_summary_model(fpath)
                    if not model:
                        continue
                    normalized = {
                        "kind": "assistant",
                        "model": model,
                        "timestamp": ts_iso,
                        "content": response_content,
                        "thoughts": assistant_thoughts,
                        "tool_calls": [],
                        "tokens": None,
                    }
                    msg_raws = agent_message_to_raw(
                        "grok",
                        normalized,
                        last_user_content=last_user_content,
                        prev_timestamp=prev_timestamp,
                    )
                    for raw in msg_raws:
                        raw["__harvest_file"] = relpath
                        raw["__harvest_offset"] = line_end
                        raw["__harvest_mtime"] = mtime
                        raw["__harvest_session_id"] = os.path.basename(os.path.dirname(fpath))
                        raw["__harvest_locator"] = relpath
                    raws.extend(msg_raws)
                    assistant_thoughts = []
                    assistant_content = []
                    continue

                # Cualquier otro sessionUpdate (tipos no verificados — ej.
                # chunks de assistant/tool calls del ACP) → SALTAR sin
                # romper; NO emitir hasta verificar con datos reales
                # (Artículo IX, documentado en el docstring).
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
            files[relpath] = entry
        return {"files": files}
