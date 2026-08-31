"""HarvestSource — puntita OpenJarvis (BIT-OJ.1; ver docs/design_index.md).

Lee los traces de OpenJarvis (``~/.openjarvis/traces.db``, store SQLite
real) y los convierte en eventos canónicos vía el motor universal
``_agent_transcript``.

Schema real (verificado con ``PRAGMA table_info`` sobre el db real el
2026-08-02):

  - ``traces(id INTEGER PK, trace_id TEXT, query TEXT, agent TEXT,
    model TEXT, engine TEXT, result TEXT, outcome TEXT, feedback REAL,
    started_at REAL, ended_at REAL, total_tokens INTEGER,
    total_latency_seconds REAL, metadata TEXT, messages TEXT)``
  - ``trace_steps(id INTEGER PK, trace_id TEXT, step_index INTEGER,
    step_type TEXT, timestamp REAL, duration_seconds REAL, input TEXT,
    output TEXT, metadata TEXT)``

Hallazgos reales del store (2026-08-02 — 7 traces + 7 trace_steps):
  - ``step_type`` real = ``'respond'`` (7/7 filas); el enum documentado
    (``route/retrieve/generate/tool_call/respond``) NO aparece en los datos
    reales — el mapeo lo cubre por contrato igual (plan §4).
  - ``trace_steps.timestamp`` es REAL epoch en **SEGUNDOS** (10 dígitos,
    ej. ``1783107851.9548256``) → ``_s_to_iso``. **DESVIACIÓN del plan
    línea 86** (decía ``_ms_to_iso``; verificado contra datos reales).
  - El JOIN ``traces``↔``trace_steps`` es por ``trace_id`` (TEXT), NO
    ``tr.id = ts.trace_id`` como decía el plan línea 202 (``traces.id`` es
    INTEGER 1-7 mientras ``trace_steps.trace_id`` es el hash TEXT
    ``6ee7c1284aec41c6`` — el JOIN del plan no matchearía nada).
  - ``traces.total_tokens`` = **0 en todos los traces reales** →
    ``response_tokens`` honesto = 0 (documentado, no inventado).
  - ``trace_steps.input`` = ``'{}'`` (vacío en los reales) → el prompt del
    LLM_INVOKED se toma de ``traces.query`` (dato real del store).
  - ``trace_steps.duration_seconds`` REAL > 0 → ``duration_ms`` =
    ``int(duration_seconds * 1000)``. Decisión de mapeo: el store guarda la
    duración REAL del step, más honesta que ``_compute_duration_ms`` (que
    sin un user previo por-step devuelve 0 — no hay user en el store de
    OpenJarvis); ``_compute_duration_ms`` queda como fallback si la duración
    no está disponible (<= 0).
  - ``output`` = JSON ``{"content": "..."}`` → ``response_content`` = el
    ``content`` parseado; si no parsea o no hay content → el output crudo.

Mapeo de la puntita (una fila ``trace_steps`` → cero o un raw dict):
  - ``step_type='respond'`` | ``'generate'`` → **LLM_INVOKED**
    (``model``=``traces.model`` desnudo — OpenJarvis no guarda prefijos
    provider; ``prompt``=``traces.query``; ``response_tokens``=
    ``traces.total_tokens``; ``duration_ms`` desde ``duration_seconds``;
    ``timestamp`` con ``_s_to_iso``).
  - ``step_type='tool_call'`` → **TOOL_CALLED** (``input`` JSON →
    ``arguments``, ``output`` JSON → ``result``, ``tool_name`` derivado del
    input).
  - ``step_type='route'`` | ``'retrieve'`` → **REASONING_STEP** (subject
    sintetizado desde ``input``, ``step_type`` por heurística del motor
    ``infer_step_type``).
  - Cualquier otro ``step_type`` → **NO romper, saltar** (degradación
    suave, ``continue``).

Cursor: ``{"max_rowid": int}`` — barrido sobre ``trace_steps.id`` (INTEGER
PRIMARY KEY → id = rowid) con batching (``BATCH_SIZE = 500``) y
``WHERE ts.id > ? ORDER BY ts.id LIMIT ?``. Solo avanza sobre eventos
efectivamente escritos (atomicidad, Artículo I; el ``_harvester`` pasa el
prefijo escrito a ``advance_cursor``).

Conexión: ``sqlite3.connect("file:...?mode=ro", uri=True)`` — NUNCA
``immutable=1`` (el store real tiene WAL activo; ``ro`` necesita el
``-shm``).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Iterator, Optional

from causadb._agent_transcript import _compute_duration_ms, infer_step_type
from causadb._harvest_source import HarvestSource

BATCH_SIZE = 500


def _derive_default_db_path() -> str:
    """Store de OpenJarvis: env override o el path por defecto del usuario."""
    env_path = os.environ.get("CAUSADB_OPENJARVIS_DB_PATH")
    if env_path:
        return env_path
    return os.path.join(os.path.expanduser("~"), ".openjarvis", "traces.db")


def _s_to_iso(ts: float) -> str:
    """Segundos (epoch UTC, REAL en OpenJarvis) → ISO 8601 con Z (formato
    canónico del ledger). Determinístico y puro."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_json_or_raw(text: str):
    """Parse JSON si se puede; si no, devuelve el string crudo intacto."""
    if not text:
        return text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _output_content(output: str) -> str:
    """Extrae ``content`` del JSON de output de un step
    (``{"content": "..."}``). Si no parsea o no hay content → output crudo."""
    parsed = _parse_json_or_raw(output)
    if isinstance(parsed, dict):
        content = parsed.get("content")
        if isinstance(content, str):
            return content
    return output


def _step_duration_ms(duration_seconds: float | None, ts_iso: str) -> int:
    """duration_ms del step: ``duration_seconds`` REAL del store (el dato
    real y honesto). Si no está disponible (<= 0), fallback al motor
    universal (sin user previo por-step devuelve 0 — documentado)."""
    if duration_seconds and duration_seconds > 0:
        return int(duration_seconds * 1000)
    return _compute_duration_ms(None, ts_iso)


def _synthesize_subject(text: str) -> str:
    """OpenJarvis no trae subject en los steps → la puntita sintetiza uno
    determinístico con las primeras 8 palabras del texto."""
    return " ".join(text.split()[:8])


def _step_to_raw(step: dict, trace: dict) -> Optional[dict]:
    """Mapea UNA fila ``trace_steps`` (con su ``trace`` JOIN-ada) a un raw
    dict canónico, o None si el step no genera eventos (step_type
    desconocido — degradación suave).

    Args:
        step: dict con ``step_type``, ``timestamp`` (REAL epoch segundos),
            ``duration_seconds``, ``input``, ``output``.
        trace: dict con ``model``, ``query``, ``total_tokens`` (del JOIN).
    """
    stype = step.get("step_type") or ""
    ts_iso = _s_to_iso(step["timestamp"])

    if stype in ("respond", "generate"):
        # LLM_INVOKED: el model lo provee el trace (no el step).
        # El input real es '{}' → el prompt se toma de traces.query.
        return {
            "type": "LLM_INVOKED",
            "timestamp": ts_iso,
            "model": trace.get("model") or "",
            "prompt": trace.get("query") or "",
            # tokens reales del store (0 en los datos reales — honesto).
            "response_tokens": int(trace.get("total_tokens") or 0),
            "duration_ms": _step_duration_ms(step.get("duration_seconds"), ts_iso),
            "response_content": _output_content(step.get("output") or ""),
            "agent": "openjarvis",
        }

    if stype == "tool_call":
        # TOOL_CALLED: input → arguments, output → result (JSON cuando parsea).
        parsed_input = _parse_json_or_raw(step.get("input") or "")
        if isinstance(parsed_input, dict):
            tool_name = (
                parsed_input.get("name")
                or parsed_input.get("tool")
                or parsed_input.get("tool_name")
                or "unknown_tool"
            )
        else:
            tool_name = "unknown_tool"
        return {
            "type": "TOOL_CALLED",
            "timestamp": ts_iso,
            "tool_name": tool_name,
            "arguments": parsed_input,
            "result": _parse_json_or_raw(step.get("output") or ""),
            "agent": "openjarvis",
        }

    if stype in ("route", "retrieve"):
        # REASONING_STEP: subject sintetizado desde input, step_type por la
        # heurística del motor universal.
        input_text = step.get("input") or ""
        subject = _synthesize_subject(input_text)
        return {
            "type": "REASONING_STEP",
            "timestamp": ts_iso,
            "step_type": infer_step_type(subject),
            "step_hash": hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
            "subject": subject,
            "description": input_text,
            "agent": "openjarvis",
        }

    # step_type desconocido → sin evento, sin romper (degradación suave).
    return None


class OpenJarvisHarvestSource(HarvestSource):
    """Fuente de harvest para los traces de OpenJarvis.

    Args:
        ledger_path: Ruta absoluta al ledger (requerido por la clase base).
        db_path: Ruta al store SQLite de OpenJarvis. Default:
            ``CAUSADB_OPENJARVIS_DB_PATH`` o ``~/.openjarvis/traces.db``
            (override para tests).
    """

    def __init__(self, ledger_path: str, db_path: Optional[str] = None):
        super().__init__(ledger_path)
        self.db_path = db_path or _derive_default_db_path()

    def source_type(self) -> str:
        # SIN colon (fix de namespace — ver plan §3)
        return "openjarvis"

    def cursor_key(self) -> str:
        return "agent:openjarvis"

    def detect(self) -> bool:
        return os.path.isfile(self.db_path)

    def harvest(self, cursor: dict | None = None) -> Iterator[dict]:
        cursor = cursor or {}
        max_rowid = int(cursor.get("max_rowid", 0))

        # -- 1. conexión read-only (nunca immutable: WAL real) -------------
        # FIX.GEN-A: harvest es un generador (yield por raw, sin materializar
        # lista); el finally cierra la conexión cuando se extenúa o se cierra.
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            # Model/query/tokens viven en traces; los steps se unen por
            # trace_id (TEXT hash) — ver desviación del plan en el docstring.
            query = (
                "SELECT ts.id AS sid, ts.trace_id, ts.step_type, ts.timestamp, "
                "ts.duration_seconds, ts.input, ts.output, "
                "tr.model AS model, tr.query AS query, "
                "tr.total_tokens AS total_tokens "
                "FROM trace_steps ts "
                "LEFT JOIN traces tr ON tr.trace_id = ts.trace_id "
                "WHERE ts.id > ? ORDER BY ts.id LIMIT ?"
            )
            while True:
                rows = con.execute(query, (max_rowid, BATCH_SIZE)).fetchall()
                if not rows:
                    break
                for (sid, trace_id, step_type, timestamp, duration_seconds,
                     input_text, output, model, query_text,
                     total_tokens) in rows:
                    raw = _step_to_raw(
                        {
                            "step_type": step_type,
                            "timestamp": timestamp,
                            "duration_seconds": duration_seconds,
                            "input": input_text,
                            "output": output,
                        },
                        {
                            "model": model,
                            "query": query_text,
                            "total_tokens": total_tokens,
                        },
                    )
                    if raw is None:
                        continue  # step_type desconocido: no romper
                    raw["__harvest_rowid"] = sid
                    raw["__harvest_session_id"] = trace_id or "unknown"
                    raw["__harvest_locator"] = self.db_path
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
