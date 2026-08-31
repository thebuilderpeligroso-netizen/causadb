"""HarvestSource — puntita codex (Fase 15.2-ter).

Lee las sesiones de Codex CLI desde su store JSONL
(``~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl``) y las convierte en
eventos canónicos vía el motor universal ``_agent_transcript``.

Formato real del rollout (auditoría 15.1-A, verificado sobre sesiones reales):

  - ``session_meta``: ``{"session_id": "thread_id", "sources": [...],
    "model_provider": "openai"}`` → se extrae session_id.
  - ``event_msg``: ``{"type": "task_started"|"task_complete"|"user_message"}``
    → marcadores de inicio/fin de sesión.
  - ``response_item``: ``{"type": "message", "role": "developer"|"user",
    "content": [{"type": "input_text", "text": "..."}]}``
    → ``role=developer`` = mensaje del agente (asistente).
    → ``role=user`` = mensaje del usuario.
  - ``turn_context``: ``{"selected_model": "gpt-5.6-sol", ...}``
    → provee el modelo para el LLM_INVOKED.
  - ``world_state``: ignorado.

Mapeo:

  1. Para cada línea ``response_item``:
     - Extraer role. Si ``developer`` → asistente; si ``user`` → usuario.
     - Extraer texto de ``content[0].text``.
     - Buscar el ``turn_context`` más reciente para obtener ``selected_model``.
     - Construir mensaje normalizado y pasarlo por ``agent_message_to_raw``.
  2. Codex NO expone thoughts ni tool_calls en el payload → se pasan vacíos.
  3. ``tokens`` se pasa como ``None`` (Codex no expone conteo de tokens).

Cursor: ``{"files": {relpath: {"offset": int}}}`` — patrón gemini, por
archivos con offset en bytes. Relpath es relativo a ``~/.codex/sessions/``.

Env override: ``CAUSADB_CODEX_DIR`` (opcional, default ``~/.codex``).
"""

from __future__ import annotations

import json
import os
from glob import glob
from typing import Iterator, Optional

from causadb._agent_transcript import agent_message_to_raw
from causadb._harvest_source import HarvestSource


def _derive_default_codex_dir() -> str:
    """Directorio base de Codex: env override o ``~/.codex``."""
    env_dir = os.environ.get("CAUSADB_CODEX_DIR")
    if env_dir:
        return env_dir
    return os.path.join(os.path.expanduser("~"), ".codex")


def _derive_sessions_dir(codex_dir: str) -> str:
    return os.path.join(codex_dir, "sessions")


class CodexHarvestSource(HarvestSource):
    """Fuente de harvest para sesiones de Codex CLI.

    Args:
        ledger_path: Ruta absoluta al ledger (requerido por la clase base).
        codex_dir: Ruta al directorio ``~/.codex``. Default:
            ``CAUSADB_CODEX_DIR`` o ``~/.codex``.
        sessions_dir: Override directo del dir de sesiones (para tests).
    """

    def __init__(
        self,
        ledger_path: str,
        codex_dir: Optional[str] = None,
        sessions_dir: Optional[str] = None,
    ):
        super().__init__(ledger_path)
        self.codex_dir = codex_dir or _derive_default_codex_dir()
        self.sessions_dir = sessions_dir or _derive_sessions_dir(self.codex_dir)

    def source_type(self) -> str:
        return "codex"

    def cursor_key(self) -> str:
        return "harvest.codex"

    def detect(self) -> bool:
        if not os.path.isdir(self.sessions_dir):
            return False
        # Buscar cualquier rollout-*.jsonl recursivamente
        pattern = os.path.join(self.sessions_dir, "*", "*", "*", "rollout-*.jsonl")
        return any(glob(pattern))

    def harvest(self, cursor: dict | None = None) -> Iterator[dict]:
        cursor = cursor or {}
        files_cursor = cursor.get("files", {})
        for relpath, fpath in self._iter_sessions_by_mtime():
            entry = files_cursor.get(relpath, {})
            yield from self._harvest_file(relpath, fpath, entry)

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
            files[relpath] = entry
        return {"files": files}

    # -- Internal ----------------------------------------------------------

    def _iter_sessions_by_mtime(self):
        """Itera los archivos rollout-*.jsonl ordenados por mtime."""
        pattern = os.path.join(
            self.sessions_dir, "*", "*", "*", "rollout-*.jsonl"
        )
        files = sorted(glob(pattern), key=os.path.getmtime)
        for fpath in files:
            # relpath: relativo a sessions_dir
            relpath = os.path.relpath(fpath, self.sessions_dir)
            yield relpath, fpath

    def _harvest_file(
        self, relpath: str, fpath: str, entry: dict
    ) -> list[dict]:
        """Parsea un archivo JSONL de Codex desde el offset indicado.

        Estrategia:
          1. Leer todas las líneas desde el offset.
          2. Extraer ``session_meta`` → session_id.
          3. Para cada ``response_item``: construir mensaje normalizado y
             pasarlo por ``agent_message_to_raw``.
          4. ``turn_context`` provee el modelo para el LLM_INVOKED.
          5. ``world_state`` y ``event_msg`` se ignoran (no generan eventos).
        """
        offset = int(entry.get("offset", 0))
        size = os.path.getsize(fpath)
        if offset > size:
            offset = 0  # archivo truncado/reescrito → releer desde 0

        # -- 1. parse de líneas desde el offset ----------------------------
        session_id: Optional[str] = None
        current_model: Optional[str] = None
        response_items: list[dict] = []  # (line_end, obj)

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
                    # Línea parcial: no avanzar.
                    break
                pos = line_end
                if not isinstance(obj, dict):
                    continue

                t = obj.get("type")
                p = obj.get("payload") or {}

                if t == "session_meta":
                    session_id = p.get("session_id") or session_id
                elif t == "turn_context":
                    # Older rollouts used selected_model; current Codex
                    # rollouts expose the same value as model.
                    current_model = (
                        p.get("selected_model") or p.get("model") or current_model
                    )
                elif t == "response_item":
                    response_items.append((line_end, obj, current_model))

        if not response_items:
            return []

        # -- 2. normalización + motor --------------------------------------
        raws: list[dict] = []
        last_user_content: Optional[str] = None
        prev_timestamp: Optional[str] = None

        for line_end, obj, model_at_time in response_items:
            p = obj.get("payload") or {}
            role = p.get("role", "")
            content = p.get("content") or []
            timestamp = obj.get("timestamp") or ""

            # Extraer texto del primer content item
            text = ""
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict):
                    text = first.get("text") or ""

            if role in ("developer", "assistant"):
                # Mensaje del asistente (agente)
                normalized = {
                    "kind": "assistant",
                    "model": current_model,
                    "timestamp": timestamp,
                    "content": text,
                    "thoughts": [],
                    "tool_calls": [],
                    "tokens": None,
                }
            elif role == "user":
                # Mensaje del usuario
                normalized = {
                    "kind": "user",
                    "model": None,
                    "timestamp": timestamp,
                    "content": text,
                    "thoughts": [],
                    "tool_calls": [],
                    "tokens": None,
                }
            else:
                continue

            msg_raws = agent_message_to_raw(
                "codex",
                normalized,
                last_user_content=last_user_content,
                prev_timestamp=prev_timestamp,
            )
            for r in msg_raws:
                r["__harvest_file"] = relpath
                r["__harvest_offset"] = line_end
                r["__harvest_session_id"] = session_id or relpath
                r["__harvest_locator"] = relpath
            raws.extend(msg_raws)

            # Estado entre mensajes
            if normalized["kind"] == "user":
                last_user_content = normalized["content"]
                prev_timestamp = normalized["timestamp"]

        return raws
