"""HarvestSource — puntita gemini-cli (ver docs/design_index.md).

Lee las sesiones de gemini-cli desde el store del proyecto activo
(``~/.gemini/tmp/<proyecto>/chats/session-*.jsonl``) y las convierte en
eventos canónicos vía el motor universal ``_agent_transcript``.

Formato real del oplog (auditoría E del plan, verificado sobre sesiones
reales de ``~/.gemini/tmp/cortex-agents/chats/``):

  - línea metadata ``{"kind": "main", ...}``        → se SALTA
  - línea ``{"$set": {"messages": [...]}}``         → reescribe el array
    ``messages`` COMPLETO (contar sus mensajes anidados duplicaría todo)
    → se SALTA
  - líneas ``{"$set": {"lastUpdated": ...}}``       → se SALTA
  - línea de mensaje top-level ``{id, timestamp, type: user|gemini,
    content, thoughts, tokens, model, toolCalls}`` → mensaje real

Detalles reales descubiertos en la implementación:

  1. **Re-emisión de mensajes con toolCalls**: el oplog emite cada mensaje
     gemini con toolCalls DOS veces — la primera SIN ``toolCalls`` (snapshot
     en-progreso) y la segunda CON el resultado. El parser deduplica por
     ``message id`` quedándose con la última emisión. Si la primera emisión
     ya fue cosechada en una corrida anterior (cursor ``last_message_id``),
     la re-emisión solo produce los ``TOOL_CALLED`` que faltaban — el
     razonamiento/LLM_INVOKED ya fueron escritos (no se duplican).
  2. **Mensajes user de tipo ``functionResponse``**: el resultado de una
     tool llega también como mensaje ``type: user`` con content
     ``[{"functionResponse": ...}]``. NO es un prompt: se salta por
     completo (no genera eventos ni actualiza el prompt del user).
  3. **Línea parcial**: gemini-cli escribe incrementalmente. Si la última
     línea no es JSON válido, se tolera y el cursor NO avanza más allá de
     la última línea completa parseable.

Cursor: ``{"files": {relpath: {"mtime": float, "offset": int,
"last_message_id": str | None}}}`` — solo avanza sobre eventos
efectivamente escritos (atomicidad, Artículo I).
"""

from __future__ import annotations

import json
import os
from glob import glob
from typing import Iterator, Optional

from causadb._agent_transcript import agent_message_to_raw
from causadb._harvest_source import HarvestSource, migrate_legacy_cursor
from causadb._store_discovery import discover_chats_dirs


def _normalize_tool_result(result) -> object:
    """Normaliza el result de gemini (lista de functionResponse) al dict
    interno con la respuesta efectiva de la tool."""
    if isinstance(result, list) and result:
        fr = result[0].get("functionResponse") if isinstance(result[0], dict) else None
        if isinstance(fr, dict):
            return fr.get("response") or fr
    return result


def _normalize_message(obj: dict) -> dict | None:
    """Normaliza una línea de mensaje top-level del oplog de gemini-cli al
    shape del motor universal. Retorna None para mensajes que no generan
    eventos (user functionResponse, tipos desconocidos)."""
    msg_type = obj.get("type")
    timestamp = obj.get("timestamp")

    if msg_type == "user":
        content = obj.get("content") or []
        texts: list[str] = []
        has_function_response = False
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("functionResponse"):
                        has_function_response = True
                    elif part.get("text") is not None:
                        texts.append(str(part["text"]))
                elif isinstance(part, str):
                    texts.append(part)
        else:
            texts.append(str(content))
        if has_function_response and not texts:
            # Resultado de tool entregado como mensaje user: no es prompt.
            return None
        return {
            "kind": "user",
            "model": None,
            "timestamp": timestamp,
            "content": "\n".join(texts),
            "thoughts": [],
            "tool_calls": [],
            "tokens": None,
        }

    if msg_type == "gemini":
        content = obj.get("content") or ""
        if isinstance(content, list):
            content = "\n".join(
                str(p.get("text", ""))
                for p in content
                if isinstance(p, dict) and p.get("text") is not None
            )
        thoughts = [
            {"subject": th.get("subject"), "description": th.get("description", "")}
            for th in (obj.get("thoughts") or [])
            if isinstance(th, dict)
        ]
        tool_calls = []
        for tc in obj.get("toolCalls") or []:
            if not isinstance(tc, dict):
                continue
            tool_calls.append({
                "name": tc.get("name"),
                "arguments": tc.get("args") or {},
                "result": _normalize_tool_result(tc.get("result")),
                "timestamp": tc.get("timestamp") or timestamp,
                "id": tc.get("id"),
            })
        return {
            "kind": "assistant",
            "model": obj.get("model"),
            "timestamp": timestamp,
            "content": str(content),
            "thoughts": thoughts,
            "tool_calls": tool_calls,
            "tokens": obj.get("tokens"),
        }

    return None


class GeminiHarvestSource(HarvestSource):
    """Fuente de harvest para sesiones de gemini-cli (GAP-01).

    Modo **single-store** (backward-compat): ``project_dir``/``chats_dir``
    explícito o env ``CAUSADB_GEMINI_PROJECT_DIR`` (o workspace del ledger).
    Las claves de cursor son basenames.

    Modo **multi-store** (GAP-01): auto-discovery desde el ``projects.json``
    real de gemini-cli (``~/.gemini/projects.json`` → ``~/.gemini/tmp/<slug>/
    chats``). Las claves de cursor son ``<slug>/<basename>`` (un basename ya
    no identifica de forma única entre stores). Las claves legacy del cursor
    se migran re-encuadradas por ``os.path.exists`` (ver
    ``migrate_legacy_cursor``) → 0 duplicados en la transición.

    Args:
        ledger_path: Ruta absoluta al ledger (requerido por la clase base).
        project_dir: Ruta al project dir de gemini-cli (contiene ``chats/``).
            Si se pasa → modo single-store (backward-compat).
        chats_dir: Override directo del dir de chats (para tests).
    """

    def __init__(
        self,
        ledger_path: str,
        project_dir: Optional[str] = None,
        chats_dir: Optional[str] = None,
    ):
        super().__init__(ledger_path)
        self.project_dir = project_dir
        self.chats_dir = chats_dir
        if project_dir is not None or chats_dir is not None:
            # single-store explícito (backward-compat: claves basename)
            self._mode = "single"
            self.chats_dirs = [
                chats_dir or os.path.join(project_dir, "chats")
            ]
            return
        # Auto-discovery (GAP-01), NUNCA CWD:
        #   1. env CAUSADB_GEMINI_PROJECT_DIR → single-store (override
        #      explícito del operador: gana sobre el discovery)
        #   2. projects.json → multi-store
        #   3. workspace del ledger (dirname(ledger)/chats o
        #      dirname(dirname(ledger))/chats) → single-store
        #   4. nada → detect() False
        env_dir = os.environ.get("CAUSADB_GEMINI_PROJECT_DIR")
        if env_dir:
            self._mode = "single"
            self.chats_dirs = [os.path.join(env_dir, "chats")]
            return
        dirs = discover_chats_dirs()
        if dirs:
            self._mode = "multi"
            self.chats_dirs = dirs
            return
        ledger_dir = os.path.dirname(os.path.abspath(ledger_path))
        workspace_candidates = [
            os.path.join(ledger_dir, "chats"),
            os.path.join(os.path.dirname(ledger_dir), "chats"),
        ]
        found = [c for c in workspace_candidates if os.path.isdir(c)]
        self._mode = "single"
        self.chats_dirs = found

    def source_type(self) -> str:
        # SIN colon (fix de namespace — ver plan §3): con "agent:gemini"
        # el source "harvester:agent:gemini" sería inválido.
        return "gemini"

    def cursor_key(self) -> str:
        return "agent:gemini"

    def _cursor_key_for(self, chats_dir: str, basename: str) -> str:
        """Clave de cursor: basename (single-store) o ``slug/basename``
        (multi-store — el basename no es único entre stores)."""
        if self._mode == "multi":
            slug = os.path.basename(os.path.dirname(chats_dir)) or "default"
            return f"{slug}/{basename}"
        return basename

    def detect(self) -> bool:
        for chats_dir in self.chats_dirs:
            if any(glob(os.path.join(chats_dir, "session-*.jsonl"))):
                return True
        return False

    def harvest(self, cursor: dict | None = None) -> Iterator[dict]:
        cursor = cursor or {}
        if self._mode == "multi":
            # Migración de claves legacy (basename) → multi-store
            # (re-encuadre por os.path.exists; colisiones/ghosts preservados).
            migrate_legacy_cursor(cursor, self.chats_dirs)
        files_cursor = cursor.get("files", {})
        for chats_dir in self.chats_dirs:
            for fpath in self._iter_sessions_by_mtime(chats_dir):
                basename = os.path.basename(fpath)
                relpath = self._cursor_key_for(chats_dir, basename)
                entry = files_cursor.get(relpath, {})
                yield from self._harvest_file(relpath, fpath, entry)

    # -- Internal ----------------------------------------------------------

    def _iter_sessions_by_mtime(self, chats_dir: str):
        files = sorted(
            glob(os.path.join(chats_dir, "session-*.jsonl")),
            key=os.path.getmtime,
        )
        return files

    def _harvest_file(
        self, relpath: str, fpath: str, entry: dict
    ) -> list[dict]:
        offset = int(entry.get("offset", 0))
        mtime = os.path.getmtime(fpath)
        size = os.path.getsize(fpath)
        if offset > size:
            offset = 0  # archivo truncado/reescrito → releer desde 0
        last_message_id = entry.get("last_message_id")

        # -- 1. parse de líneas desde el offset -----------------------------
        # ``by_id``: última emisión por message id (la completa, con
        # toolCalls). ``order``: orden de primer avistamiento.
        by_id: dict[str, dict] = {}
        order: list[str] = []
        line_end_by_id: dict[str, int] = {}
        canonical_session_id: Optional[str] = None
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
                    # Línea parcial (gemini-cli escribiendo): no avanzar.
                    break
                pos = line_end
                if not isinstance(obj, dict):
                    continue
                if "$set" in obj:
                    continue  # reescribe mensajes completos → duplicaría
                if obj.get("kind") == "main":
                    # Metadata de sesión: captura la identidad canónica
                    # (sessionId UUID de gemini-cli) para C1.2. No genera
                    # eventos.
                    if canonical_session_id is None and obj.get("sessionId"):
                        canonical_session_id = str(obj.get("sessionId"))
                    continue
                mid = obj.get("id")
                if not mid:
                    continue
                line_end_by_id[mid] = line_end
                if mid in by_id:
                    by_id[mid] = obj  # última emisión gana
                else:
                    by_id[mid] = obj
                    order.append(mid)

        if not order:
            return []

        # ── 2. normalización + motor ────────────────────────────────────────
        # Re-emisión entre corridas: la primera emisión (sin toolCalls) ya
        # fue cosechada en una corrida anterior → solo emitir TOOL_CALLED.
        first_id = order[0]
        re_emission = bool(last_message_id) and first_id == last_message_id

        raws: list[dict] = []
        last_user_content: Optional[str] = None
        prev_timestamp: Optional[str] = None
        session_id = canonical_session_id or relpath
        for mid in order:
            obj = by_id[mid]
            normalized = _normalize_message(obj)
            if normalized is None:
                continue
            msg_raws = agent_message_to_raw(
                "gemini",
                normalized,
                last_user_content=last_user_content,
                prev_timestamp=prev_timestamp,
            )
            if re_emission and mid == first_id:
                # Solo los tool calls que faltaban (reasoning/LLM ya escritos)
                msg_raws = [r for r in msg_raws if r["type"] == "TOOL_CALLED"]
            for r in msg_raws:
                r["__harvest_file"] = relpath
                r["__harvest_offset"] = line_end_by_id.get(mid, 0)
                r["__harvest_mtime"] = mtime
                r["__harvest_message_id"] = mid
                r["__harvest_session_id"] = session_id
                # Locator: archivo JSONL crudo que originó la sesión
                # (identidad de despliegue, distinta del UUID canónico).
                # Siempre el basename — el relpath del cursor lleva prefijo
                # de store en modo multi (slug/basename).
                r["__harvest_locator"] = os.path.basename(relpath)
            raws.extend(msg_raws)
            # Estado entre mensajes (motor puro — el estado vive acá)
            if normalized["kind"] == "user":
                last_user_content = normalized["content"]
                prev_timestamp = normalized["timestamp"]

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
