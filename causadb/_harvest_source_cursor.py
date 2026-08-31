"""HarvestSource — puntita cursor (Fase 15.2).

Lee las sesiones de Cursor desde su store JSONL
(``~/.cursor/projects/<project-slug>/agent-transcripts/<session-uuid>/
<session-uuid>.jsonl``) y las convierte en eventos canónicos vía el motor
universal ``_agent_transcript``.

Formato real del transcript (auditoría sobre sesiones reales de
``~/.cursor/projects/empty-window/agent-transcripts/``):

  - Cada línea es un objeto JSON con ``role`` y ``message``.
  - ``message.content`` es un array de bloques:
    - ``{"type": "text", "text": "..."}`` → texto del mensaje.
    - ``{"type": "tool_use", "name": "...", "input": {...}}`` → tool call.
    - ``{"type": "tool_result", ...}`` → resultado de tool (no usado
      directamente; los tool_use se emiten sin result en el mismo mensaje).
  - ``role=user`` → mensaje del usuario (actualiza ``last_user_content``).
  - ``role=assistant`` → mensaje del asistente (genera eventos).
  - ``type=turn_ended`` → marcador de fin de turno (ignorado).

Mapeo vía ``agent_message_to_raw`` (motor universal):

  1. Para cada línea con ``role=assistant``:
     - Extraer texto de todos los ``content`` items con ``type=text``.
     - Extraer tool calls de los items ``type=tool_use``.
     - Construir mensaje normalizado y pasarlo por ``agent_message_to_raw``.
  2. Para cada línea con ``role=user``:
     - Extraer texto y actualizar ``last_user_content`` (no genera eventos).
  3. Cursor NO expone ``model`` → ``"model": "cursor"`` hardcodeado para
     que ``agent_message_to_raw`` genere ``LLM_INVOKED``.
  4. Cursor NO expone ``thoughts`` ni ``tokens`` → se pasan como ``[]`` y
     ``None``.

Cursor: ``{"files": {relpath: {"offset": int}}}`` — patrón gemini/codex,
por archivos con offset en bytes. Relpath es relativo a
``~/.cursor/projects/``.

Env override: ``CAUSADB_CURSOR_PROJECTS_DIR`` (opcional, default
``~/.cursor/projects``).
"""

from __future__ import annotations

import json
import os
from glob import glob
from typing import Iterator, Optional

from causadb._agent_transcript import agent_message_to_raw
from causadb._harvest_source import HarvestSource


def _derive_default_projects_dir() -> str:
    """Directorio base de proyectos Cursor: env override o ``~/.cursor/projects``."""
    env_dir = os.environ.get("CAUSADB_CURSOR_PROJECTS_DIR")
    if env_dir:
        return env_dir
    return os.path.join(os.path.expanduser("~"), ".cursor", "projects")


class CursorHarvestSource(HarvestSource):
    """Fuente de harvest para sesiones de Cursor.

    Args:
        ledger_path: Ruta absoluta al ledger (requerido por la clase base).
        projects_dir: Ruta al directorio ``~/.cursor/projects``. Default:
            ``CAUSADB_CURSOR_PROJECTS_DIR`` o ``~/.cursor/projects``.
    """

    def __init__(
        self,
        ledger_path: str,
        projects_dir: Optional[str] = None,
    ):
        super().__init__(ledger_path)
        self.projects_dir = projects_dir or _derive_default_projects_dir()

    def source_type(self) -> str:
        return "cursor"

    def cursor_key(self) -> str:
        return "harvest.cursor"

    def detect(self) -> bool:
        if not os.path.isdir(self.projects_dir):
            return False
        # Buscar cualquier <session-uuid>.jsonl recursivamente
        pattern = os.path.join(
            self.projects_dir, "*", "agent-transcripts", "*", "*.jsonl"
        )
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
        """Itera los archivos <session-uuid>.jsonl ordenados por mtime."""
        pattern = os.path.join(
            self.projects_dir, "*", "agent-transcripts", "*", "*.jsonl"
        )
        files = sorted(glob(pattern), key=os.path.getmtime)
        for fpath in files:
            # relpath: relativo a projects_dir
            relpath = os.path.relpath(fpath, self.projects_dir)
            yield relpath, fpath

    def _harvest_file(
        self, relpath: str, fpath: str, entry: dict
    ) -> list[dict]:
        """Parsea un archivo JSONL de Cursor desde el offset indicado.

        Estrategia:
          1. Leer todas las líneas desde el offset.
          2. Extraer ``session_id`` del nombre del directorio padre.
          3. Para cada línea con ``role=assistant``: construir mensaje
             normalizado y pasarlo por ``agent_message_to_raw``.
          4. ``role=user``: actualizar ``last_user_content`` (no genera eventos).
          5. ``type=turn_ended``: ignorado.
        """
        offset = int(entry.get("offset", 0))
        size = os.path.getsize(fpath)
        if offset > size:
            offset = 0  # archivo truncado/reescrito → releer desde 0

        # session_id = nombre del directorio padre (el UUID de sesión)
        session_id = os.path.basename(os.path.dirname(fpath))

        # -- 1. parse de líneas desde el offset ----------------------------
        raws: list[dict] = []
        last_user_content: Optional[str] = None
        prev_timestamp: Optional[str] = None

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

                role = obj.get("role")
                msg = obj.get("message") or {}
                content = msg.get("content") or []

                # Extraer texto y tool_uses del array content
                texts: list[str] = []
                tool_calls: list[dict] = []
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    ct = c.get("type")
                    if ct == "text":
                        t = c.get("text") or ""
                        if t:
                            texts.append(t)
                    elif ct == "tool_use":
                        tool_calls.append({
                            "name": c.get("name") or "unknown_tool",
                            "arguments": c.get("input") or {},
                            "result": "",  # Cursor no incluye result en el mismo mensaje
                            "timestamp": None,
                            "id": c.get("id"),
                        })

                if role == "assistant":
                    normalized = {
                        "kind": "assistant",
                        "model": "cursor",  # hardcodeado: Cursor no expone model
                        "timestamp": "",   # Cursor no expone timestamp por mensaje
                        "content": "\n".join(texts),
                        "thoughts": [],
                        "tool_calls": tool_calls,
                        "tokens": None,
                    }
                elif role == "user":
                    normalized = {
                        "kind": "user",
                        "model": None,
                        "timestamp": "",
                        "content": "\n".join(texts),
                        "thoughts": [],
                        "tool_calls": [],
                        "tokens": None,
                    }
                else:
                    # turn_ended u otros → ignorar
                    continue

                msg_raws = agent_message_to_raw(
                    "cursor",
                    normalized,
                    last_user_content=last_user_content,
                    prev_timestamp=prev_timestamp,
                )
                for r in msg_raws:
                    r["__harvest_file"] = relpath
                    r["__harvest_offset"] = line_end
                    r["__harvest_session_id"] = session_id
                    r["__harvest_locator"] = relpath
                raws.extend(msg_raws)

                # Estado entre mensajes
                if normalized["kind"] == "user":
                    last_user_content = normalized["content"]
                    prev_timestamp = normalized["timestamp"]

        return raws
