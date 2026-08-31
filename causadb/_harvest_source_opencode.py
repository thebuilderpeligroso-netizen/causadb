"""HarvestSource — puntita opencode (ver docs/design_index.md).

Lee las sesiones de opencode desde su store SQLite real
(``~/.local/share/opencode/opencode.db``) y las convierte en eventos
canónicos vía el motor universal ``_agent_transcript``.

Schema real (auditoría de schema §5.3, verificada sobre el db real):

  - ``session(id, slug, directory, title, agent, model, time_created, ...)``
  - ``message(id, session_id, time_created, time_updated, data)`` — data JSON
    con ``role``, ``agent``, ``model``, ``tokens``, ``time``, ``finish``.
  - ``part(id, message_id, session_id, time_created, time_updated, data)`` —
    data JSON tipado por ``$.type``: ``reasoning`` (text + time.start/end),
    ``tool`` (tool + callID + state.input/output), ``text`` (el prompt),
    ``step-start`` / ``step-finish``, ``patch``, ``compaction``, ``file``.

Desviaciones del plan (documentadas para el reporte):
  - los parts de razonamiento son ``type="reasoning"`` (el plan decía
    "thinking");
  - los tool parts son ``type="tool"`` (el plan decía "tool_use");
  - ``part.id`` es texto (``prt_...``) → el cursor es por ``rowid``
    (existe: la tabla no es WITHOUT ROWID).

Mapeo de la puntita (una fila ``part`` → cero o un raw dict):
  - ``type="reasoning"`` → REASONING_STEP. opencode NO trae subject → la
    puntita sintetiza uno determinístico (primeras 8 palabras del texto) y
    el ``step_type`` se infiere con la heurística del motor universal
    (``infer_step_type``). Timestamp: ``data.time.start`` (ms) o
    ``part.time_created``.
  - ``type="tool"`` → TOOL_CALLED con ``tool_name=data.tool``,
    ``arguments=state.input``, ``result=state.output`` (íntegro: el peso lo
    absorbe el BlobStore), ``tool_call_id=callID``. Timestamp:
    ``state.time.start`` (ms) o ``part.time_created``.
  - Los parts ``text`` / ``step-start`` / ``step-finish`` / ``patch`` /
    ``compaction`` / ``file`` NO generan eventos en este MVP (el plan solo
    mapea reasoning y tool).

Cursor: ``{"max_rowid": int}`` — barrido con ``LIMIT`` + batching sobre la
tabla ``part`` (modo read-only; ~100K parts de ~100KB en el store real).
Solo avanza sobre eventos efectivamente escritos (atomicidad, Artículo I;
el `_harvester` pasa el prefijo escrito a ``advance_cursor``).

Conexión: ``sqlite3.connect("file:...?mode=ro", uri=True)`` — NUNCA
``immutable=1`` (el db real tiene WAL activo; ``ro`` necesita el ``-shm``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Iterator, Optional

from causadb._agent_transcript import infer_step_type
from causadb._harvest_source import HarvestSource
from causadb._store_discovery import normalize_store_path

BATCH_SIZE = 500


def _derive_default_db_path() -> str:
    """Store de opencode: env override > config del agente > path default.

    GAP-01 (forward-compat): la config de opencode
    (``~/.config/opencode/opencode.json``) puede declarar el store con la
    key ``data``. El env override SIEMPRE gana (el operador manda).
    """
    return normalize_store_path(
        "CAUSADB_OPENCODE_DB_PATH",
        os.path.join(os.path.expanduser("~"), ".config", "opencode", "opencode.json"),
        os.path.join(os.path.expanduser("~"), ".local", "share", "opencode", "opencode.db"),
    )


def _ms_to_iso(ms: int) -> str:
    """Milisegundos (epoch UTC) → ISO 8601 con Z (formato canónico del
    ledger). Determinístico y puro."""
    return datetime.fromtimestamp(
        ms / 1000, tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")


def _synthesize_subject(text: str) -> str:
    """opencode no trae subject en los reasoning → la puntita sintetiza uno
    determinístico con las primeras 8 palabras del texto."""
    return " ".join(text.split()[:8])


def _part_to_raw(part: dict, agent: Optional[str]) -> Optional[dict]:
    """Mapea UNA fila ``part`` (con su data JSON ya parseado) a un raw dict
    canónico, o None si el part no genera eventos."""
    d = part["data"]
    ptype = d.get("type")

    if ptype == "reasoning":
        text = d.get("text") or ""
        ts_ms = (d.get("time") or {}).get("start") or part["time_created"]
        subject = _synthesize_subject(text)
        return {
            "type": "REASONING_STEP",
            "timestamp": _ms_to_iso(int(ts_ms)),
            "step_type": infer_step_type(subject),
            "step_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "subject": subject,
            "description": text,
            "agent": agent or "opencode",
        }

    if ptype == "tool":
        state = d.get("state") or {}
        ts_ms = (state.get("time") or {}).get("start") or part["time_created"]
        return {
            "type": "TOOL_CALLED",
            "timestamp": _ms_to_iso(int(ts_ms)),
            "tool_name": d.get("tool") or "unknown_tool",
            "arguments": state.get("input") or {},
            "result": state.get("output") or "",
            "tool_call_id": d.get("callID"),
            "agent": agent or "opencode",
        }

    # text / step-start / step-finish / patch / compaction / file → no eventos
    return None


def _opencode_conversation_ref(session_id: str) -> dict:
    """Metadata-only locator for the OpenCode SQLite transcript."""
    return {
        "provider": "opencode",
        "native_id": session_id,
        "locator_kind": "sqlite",
        "locator": "opencode_default",
        "resolver": "opencode",
        "confidence": "verified",
        "content_class": "transcript_complete",
        "privacy_class": "raw_sensitive",
    }


class OpenCodeHarvestSource(HarvestSource):
    """Fuente de harvest para las sesiones de opencode.

    Args:
        ledger_path: Ruta absoluta al ledger (requerido por la clase base).
        db_path: Ruta al store SQLite de opencode. Default:
            ``CAUSADB_OPENCODE_DB_PATH`` o
            ``~/.local/share/opencode/opencode.db`` (override para tests).
    """

    def __init__(self, ledger_path: str, db_path: Optional[str] = None):
        super().__init__(ledger_path)
        self.db_path = db_path or _derive_default_db_path()

    def source_type(self) -> str:
        # SIN colon (fix de namespace — ver plan §3)
        return "opencode"

    def cursor_key(self) -> str:
        return "agent:opencode"

    def detect(self) -> bool:
        return os.path.isfile(self.db_path)

    def harvest(self, cursor: dict | None = None) -> Iterator[dict]:
        cursor = cursor or {}
        max_rowid = int(cursor.get("max_rowid", 0))

        # -- 1. conexión read-only (nunca immutable: WAL real) -------------
        # La conexión es por-llamada; se cierra al terminar (y el -shm del
        # store real lo administra el proceso de opencode, no nosotros).
        # FIX.GEN-A: harvest es un generador (yield por raw, sin materializar
        # lista); el finally cierra la conexión cuando se extenúa o se cierra.
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            # --- CLAMP_GAP_OCB (BIT-CHR.105): reconciliar cursor adelantado.
            # opencode compacta la tabla `part` (VACUUM/re-create) que resetea
            # rowids. Si cursor.max_rowid > MAX(rowid) actual → el WHERE
            # retornaba 0 filas silenciosamente, abandonando el backfill.
            # Clamp: si detectamos drift, clampeamos al MAX(rowid) actual
            # (no reescribimos lo ya cosechado —Artículo I— pero permitimos
            # que el harvest continúe desde la "nueva frontera" del DB).
            # NO emitimos evento al ledger: el schema de OBSERVATION exige
            # file_path/line_number/severity que no aplican a bookkeeping
            # operacional del harvester (este BIT-CHR.105 corrige el
            # OBSERVATION inválido inicial que rompía `causadb validate`/
            # `replay` con `ValueError: invalid severity None`).
            # Logging.warning basta para trazabilidad operacional sin
            # contaminar el ledger.
            db_max_rowid = con.execute("SELECT MAX(rowid) FROM part").fetchone()[0]
            if db_max_rowid is not None and max_rowid > db_max_rowid:
                logging.warning(
                    "OpenCode harvest cursor drifted ahead of DB "
                    "(saved=%d > actual=%d, delta=%d). Likely DB compactación "
                    "reseteó rowids. Cursor clampeado a DB MAX(rowid) para "
                    "reanudar backfill.",
                    max_rowid, db_max_rowid, max_rowid - int(db_max_rowid),
                )
                max_rowid = int(db_max_rowid)

            query = (
                "SELECT p.rowid AS r, p.id, p.time_created, p.data, s.agent, s.id AS session_id "
                "FROM part p LEFT JOIN session s ON s.id = p.session_id "
                "WHERE p.rowid > ? ORDER BY p.rowid LIMIT ?"
            )
            while True:
                rows = con.execute(query, (max_rowid, BATCH_SIZE)).fetchall()
                if not rows:
                    break
                for r, part_id, time_created, data_json, agent, session_id in rows:
                    try:
                        data = json.loads(data_json)
                    except (json.JSONDecodeError, TypeError):
                        continue  # data corrupto: no romper el barrido
                    raw = _part_to_raw(
                        {"id": part_id, "time_created": time_created, "data": data},
                        agent,
                    )
                    if raw is None:
                        continue
                    raw["__harvest_rowid"] = r
                    raw["__harvest_session_id"] = session_id
                    if session_id:
                        raw["__conversation_ref"] = _opencode_conversation_ref(session_id)
                    yield raw
                max_rowid = rows[-1][0]  # avance dentro del barrido
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
