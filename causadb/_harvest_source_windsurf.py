"""HarvestSource — puntita Windsurf/Devin Desktop (Fase 15.2-bis).

Lee las sesiones de Windsurf/Devin desde su store SQLite real
(``~/.local/share/devin/cli/sessions.db``) y las convierte en eventos
canónicos vía el motor universal ``_agent_transcript``.

Schema real (verificado sobre el db real, sesión ``plume-grease``):

  - ``sessions(id, working_directory, model, agent_mode, created_at, title, ...)``
  - ``message_nodes(row_id INTEGER PK AUTOINCREMENT, session_id, node_id,
    parent_node_id, chat_message TEXT (JSON), created_at, metadata)``
  - ``tool_call_state(session_id, tool_call_id, tool_call_json TEXT (JSON),
    tool_call_update_json TEXT (JSON))``

El campo ``chat_message`` es JSON:
  ``{"message_id":"...", "role":"system"|"user"|"assistant"|"tool",
    "content":"texto del mensaje"|[...]|null, ...}``

El campo ``tool_call_json`` de tool_call_state es:
  ``{"toolCallId":"...", "title":"Ran command"|"Wrote file"|...,
    "kind":"execute"|"edit"|..., "content":[...], "rawInput":{...}, ...}``

Mapeo de la puntita (una fila ``message_nodes`` → cero o más raw dicts):

  - ``role='user'`` → no genera evento; se recuerda su ``content`` como
    ``last_user_content`` (prompt para el LLM_INVOKED siguiente).
  - ``role='system'`` → ignorar (no genera eventos).
  - ``role='assistant'`` → LLM_INVOKED (model de la sesión). Windsurf no
    expone thoughts → ``thoughts=[]``. Tokens no disponibles → 0.
  - ``role='tool'`` → TOOL_CALLED. Se busca en ``tool_call_state`` por
    ``(session_id, tool_call_id)`` para obtener ``tool_name`` (``kind`` o
    ``title``) y ``arguments`` (``rawInput``). El ``result`` es el
    ``content`` del mensaje tool.

Cursor: ``{"max_rowid": int}`` — barrido con ``LIMIT`` + batching sobre la
tabla ``message_nodes`` (modo read-only). Solo avanza sobre eventos
efectivamente escritos (atomicidad, Artículo I; el ``_harvester`` pasa el
prefijo escrito a ``advance_cursor``).

Conexión: ``sqlite3.connect("file:...?mode=ro", uri=True)`` — NUNCA
``immutable=1`` (el db real tiene WAL activo; ``ro`` necesita el ``-shm``).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Iterator, Optional

from causadb._agent_transcript import _compute_duration_ms
from causadb._harvest_source import HarvestSource

BATCH_SIZE = 500


def _derive_default_db_path() -> str:
    """Store de Windsurf/Devin: env override o el path por defecto."""
    env_path = os.environ.get("CAUSADB_WINDSURF_DB_PATH")
    if env_path:
        return env_path
    return os.path.join(
        os.path.expanduser("~"), ".local", "share", "devin", "cli", "sessions.db"
    )


def _s_to_iso(ts: int) -> str:
    """Segundos (epoch UTC, INTEGER en Windsurf) → ISO 8601 con Z."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class WindsurfHarvestSource(HarvestSource):
    """Fuente de harvest para las sesiones de Windsurf/Devin Desktop.

    Args:
        ledger_path: Ruta absoluta al ledger (requerido por la clase base).
        db_path: Ruta al store SQLite de Windsurf. Default:
            ``CAUSADB_WINDSURF_DB_PATH`` o
            ``~/.local/share/devin/cli/sessions.db`` (override para tests).
    """

    def __init__(self, ledger_path: str, db_path: Optional[str] = None):
        super().__init__(ledger_path)
        self.db_path = db_path or _derive_default_db_path()

    def source_type(self) -> str:
        return "windsurf"

    def cursor_key(self) -> str:
        return "harvest.windsurf"

    def detect(self) -> bool:
        if not os.path.isfile(self.db_path):
            return False
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            cnt = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            con.close()
            return cnt > 0
        except sqlite3.Error:
            return False

    def harvest(self, cursor: dict | None = None) -> Iterator[dict]:
        cursor = cursor or {}
        max_rowid = int(cursor.get("max_rowid", 0))

        # Cache de model por sesión (Windsurf guarda model en sessions).
        session_models: dict[str, str | None] = {}

        # Estado entre mensajes del barrido, POR-SESIÓN.
        session_last_user_content: dict[str, str] = {}
        session_last_user_ts: dict[str, str] = {}

        # Cache de tool_call_state → (tool_name, arguments).
        tool_call_cache: dict[tuple[str, str], dict] = {}

        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            # Pre-cargar tool_call_state para búsqueda rápida.
            tc_rows = con.execute(
                "SELECT session_id, tool_call_id, tool_call_json "
                "FROM tool_call_state"
            ).fetchall()
            for tc_session_id, tc_id, tc_json in tc_rows:
                try:
                    tc_data = json.loads(tc_json)
                except (json.JSONDecodeError, TypeError):
                    tc_data = {}
                tool_call_cache[(tc_session_id, tc_id)] = tc_data

            query = (
                "SELECT m.rowid AS r, m.session_id, m.chat_message, m.created_at, "
                "s.model AS session_model "
                "FROM message_nodes m "
                "LEFT JOIN sessions s ON s.id = m.session_id "
                "WHERE m.rowid > ? ORDER BY m.rowid LIMIT ?"
            )
            while True:
                rows = con.execute(query, (max_rowid, BATCH_SIZE)).fetchall()
                if not rows:
                    break
                for r, session_id, chat_message_json, created_at, session_model in rows:
                    try:
                        cm = json.loads(chat_message_json)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    role = cm.get("role", "")
                    ts_iso = _s_to_iso(created_at)

                    # -- model por sesión (cache) --------------------------
                    if session_id not in session_models:
                        session_models[session_id] = session_model
                    model = session_models.get(session_id)

                    if role == "user":
                        content = cm.get("content", "")
                        if isinstance(content, str):
                            session_last_user_content[session_id] = content
                        session_last_user_ts[session_id] = ts_iso
                        continue

                    if role == "system":
                        continue

                    if role == "tool":
                        tc_id = cm.get("tool_call_id")
                        content = cm.get("content", "")
                        if tc_id:
                            tc_data = tool_call_cache.get(
                                (session_id, tc_id), {}
                            )
                            tool_name = (
                                tc_data.get("kind")
                                or tc_data.get("title")
                                or "unknown_tool"
                            )
                            arguments = tc_data.get("rawInput") or {}
                            raw = {
                                "type": "TOOL_CALLED",
                                "timestamp": ts_iso,
                                "tool_name": tool_name,
                                "arguments": arguments,
                                "result": content or "",
                                "tool_call_id": tc_id,
                                "agent": self.source_type(),
                            }
                            raw["__harvest_rowid"] = r
                            raw["__harvest_session_id"] = session_id or "unknown"
                            raw["__harvest_locator"] = self.db_path
                            yield raw
                        continue

                    if role == "assistant":
                        if model:
                            content = cm.get("content", "")
                            if not isinstance(content, str):
                                content = ""
                            prompt = session_last_user_content.get(
                                session_id, ""
                            )
                            prev_ts = session_last_user_ts.get(session_id)
                            raw = {
                                "type": "LLM_INVOKED",
                                "timestamp": ts_iso,
                                "model": model,
                                "prompt": prompt,
                                "response_tokens": 0,
                                "duration_ms": _compute_duration_ms(
                                    prev_ts, ts_iso
                                ),
                                "response_content": content,
                                "agent": self.source_type(),
                            }
                            raw["__harvest_rowid"] = r
                            raw["__harvest_session_id"] = session_id or "unknown"
                            raw["__harvest_locator"] = self.db_path
                            yield raw

                max_rowid = rows[-1][0]
        finally:
            con.close()

    def advance_cursor(
        self, cursor: dict | None, harvested_raw_events: list[dict]
    ) -> dict:
        cursor = cursor or {}
        new_max = int(cursor.get("max_rowid", 0))
        for ev in harvested_raw_events:
            rid = ev.get("__harvest_rowid")
            if rid is not None:
                new_max = max(new_max, int(rid))
        return {"max_rowid": new_max}
