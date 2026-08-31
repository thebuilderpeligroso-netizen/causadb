"""HarvestSource — puntita Hermes Agent (BIT-HM.1; ver docs/design_index.md).

Lee las sesiones de Hermes Agent (NousResearch/hermes-agent) desde su store
SQLite real (``~/.hermes/state.db``) y las convierte en eventos canónicos vía
el motor universal ``_agent_transcript``.

Schema real (verificado contra datos reales generados con Hermes v0.19.1 +
Ollama local el 2026-08-02; DDL de ``hermes_state_common.py`` del repo):

  - ``sessions(id, source, model, started_at, ended_at, message_count,
    tool_call_count, input_tokens, output_tokens, cwd, billing_provider, ...)``
  - ``messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id, role,
    content, tool_call_id, tool_calls, tool_name, timestamp REAL, token_count,
    finish_reason, reasoning, reasoning_content, ...)``
  - ``session_model_usage`` — agregado por ``(session_id, model, task)``.

Hallazgos reales del store (documentados para el reporte, patrón opencode):
  - ``messages.token_count`` es NULL en la práctica → el ``response_tokens``
    del LLM_INVOKED (per-message) es el token count REAL del mensaje si es
    int > 0; si es NULL/0 → ``0`` honesto (patrón windsurf/grok/openjarvis).
    El agregado por-sesión (``sessions.input_tokens/output_tokens``) vive
    SOLO en ``API_ATTEMPT`` (fallback H2.2) y ``COST_ACCOUNTED`` — nunca se
    repite por assistant (contrato H2.3: no repetir el agregado por mensaje).
  - ``sessions.model`` puede traer prefijo provider (``local:...``,
    ``custom:...``) o desnudo → la puntita lo normaliza (strip del prefijo).
  - ``timestamp`` es REAL epoch (segundos) → ``_s_to_iso``.
  - ``reasoning`` y ``reasoning_content`` se escriben idénticos → se usa
    ``reasoning_content`` para los thoughts.

Mapeo de la puntita (una fila ``messages`` → cero o varios raw dicts):
  - ``role='user'`` → no genera evento; se recuerda su ``content`` como
    ``last_user_content`` POR-SESIÓN (prompt para el LLM_INVOKED siguiente de
    ESA sesión — nunca cross-session; ver hallazgo 2.1 de la auditoría).
  - ``role='assistant'`` con ``reasoning_content`` no vacío → REASONING_STEP
    (subject sintetizado con las primeras 8 palabras, patrón opencode).
  - ``role='assistant'`` con ``tool_calls`` JSON array → TOOL_CALLED por cada
    entrada (``function.name`` → tool_name, ``function.arguments`` →
    arguments, ``id`` → tool_call_id, para el pairing).
  - ``role='tool'`` → no genera evento; su ``content`` se resuelve como
    ``result`` del TOOL_CALLED vía lookup SQL anticipado en la fila
    assistant (FIX.HERMES, antes pairing post-hoc sobre la lista).
  - ``role='assistant'`` con model por-sesión → LLM_INVOKED (incluye el
    assistant de tool_calls con ``finish_reason='tool_calls'`` y
    ``response_content`` vacío; el contrato del motor solo exige
    kind=assistant + model, ``_agent_transcript.py:168``).

Cursor: ``{"max_rowid": int}`` — barrido con ``LIMIT`` + batching sobre la
tabla ``messages`` (modo read-only). Solo avanza sobre eventos efectivamente
escritos (atomicidad, Artículo I; el ``_harvester`` pasa el prefijo escrito a
``advance_cursor``).

Conexión: ``sqlite3.connect("file:...?mode=ro", uri=True)`` — NUNCA
``immutable=1`` (Hermes usa WAL; ``ro`` necesita el ``-shm``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Iterator, Optional

# ... existing code ...

_LOG_COMPLETED_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \w+ \[([0-9]{8}_[0-9]{6}_[a-f0-9]{6})\] "
    r"agent\.conversation_loop: API call #\d+: "
    r"model=(\S+) provider=(\S+) in=(\d+) out=(\d+) total=\d+ latency=([\d.]+)s$"
)
_LOG_FAILED_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \w+ \[([0-9]{8}_[0-9]{6}_[a-f0-9]{6})\] "
    r"agent\.conversation_loop: API call failed \(attempt \d+/\d+\) "
    r"error_type=(\S+) thread=\S+ provider=(\S*) base_url=(\S+) model=(\S*) summary=(.+)$"
)

from datetime import datetime, timezone
from causadb._config import CausaDBConfig
from causadb._agent_transcript import _compute_duration_ms, infer_step_type
from causadb._harvest_source import HarvestSource
from causadb._redactor import _redact_url_credentials, redact_payload
from causadb._store_discovery import normalize_store_path

BATCH_SIZE = 500

_PROVIDER_PREFIXES = ("local:", "custom:", "ollama:")

def _parse_log_events(log_path: str, session_billing_data: dict, ledger_path: str) -> list[dict]:
    """Parsea el log y extrae los eventos API_ATTEMPT con correcciones."""
    events = []
    if not os.path.exists(log_path):
        return events
    
    config = CausaDBConfig(ledger_path=ledger_path)
    session_counters = {}

    with open(log_path, "r") as f:
        for line in f:
            line = line.strip()
            
            m_comp = _LOG_COMPLETED_RE.match(line)
            if m_comp:
                ts_str, session_id, model, provider, tokens_in, tokens_out, latency = m_comp.groups()
                session_counters[session_id] = session_counters.get(session_id, 0) + 1
                ordinal = session_counters[session_id]
                
                billing = session_billing_data.get(session_id, {})
                
                # Timestamp to UTC ISO
                dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
                ts_iso = dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
                
                event = {
                    "type": "API_ATTEMPT",
                    "timestamp": ts_iso,
                    "hermes_session_id": session_id,
                    "session_id": session_id,
                    "status": "completed",
                    "model": model,
                    "provider": provider,
                    "mode": billing.get("billing_mode") or "unknown",
                    "tokens_in": int(tokens_in),
                    "tokens_out": int(tokens_out),
                    "latency_ms": int(float(latency) * 1000),
                    "request_ref": f"{session_id}#{ts_str}#call{ordinal}",
                    "billing_base_url": _redact_url_credentials(billing.get("billing_base_url", "")),
                    "session_model_usage": {
                        "provider": billing.get("billing_provider"),
                        "base_url": _redact_url_credentials(billing.get("billing_base_url", "")),
"mode": billing.get("billing_mode") or "unknown",
                        "api_call_count": billing.get("api_call_count", 0)
                    }
                }

                # -- H2.2: correlación granular por session_id ---------------
                # session_model_usage (store) complementa el evento per-request
                # con cache/reasoning/coste/task. El guard de columnas ya se
                # aplicó en _harvest: si la columna no existe (schema legacy)
                # o la sesión no tiene fila de billing, la key no está en
                # `billing` y el campo queda AUSENTE (default seguro, sin
                # crash — Art. V: no inventar datos).
                #
                # Prioridad de coste: actual_cost_usd > estimated_cost_usd —
                # la factura real del proveedor (actual) gana sobre la
                # estimación del pricing cache (estimated); si el store no
                # computó coste (ambos ausentes o 0, el default del DDL =
                # "costo no computado"), cost_usd se OMITE: fabricar $0 sería
                # inventar un dato que el store no afirma.
                for src_key, dst_key in (
                    ("cache_read_tokens", "cache_read"),
                    ("cache_write_tokens", "cache_write"),
                    ("reasoning_tokens", "reasoning_tokens"),
                ):
                    val = billing.get(src_key)
                    if val is not None:
                        event[dst_key] = int(val)

                task_val = billing.get("task")
                if task_val:
                    event["task"] = task_val

                actual_cost = billing.get("actual_cost_usd")
                estimated_cost = billing.get("estimated_cost_usd")
                if actual_cost:
                    event["cost_usd"] = float(actual_cost)
                elif estimated_cost:
                    event["cost_usd"] = float(estimated_cost)

                # cost_status / cost_source: metadatos honestos del store;
                # solo si tienen valor (None/"" se omiten, no viajan al payload).
                for meta_key in ("cost_status", "cost_source"):
                    meta_val = billing.get(meta_key)
                    if meta_val not in (None, ""):
                        event[meta_key] = meta_val

                # Fallback de tokens: si el log reporta 0/0 (campo omitido o
                # truncado en la línea), usar los agregados por-sesión del
                # store (input_tokens/output_tokens).
                if int(tokens_in) == 0 and billing.get("input_tokens") is not None:
                    event["tokens_in"] = int(billing["input_tokens"])
                if int(tokens_out) == 0 and billing.get("output_tokens") is not None:
                    event["tokens_out"] = int(billing["output_tokens"])

                events.append(redact_payload(event, config))
                continue

            m_fail = _LOG_FAILED_RE.match(line)
            if m_fail:
                ts_str, session_id, err_type, provider, base_url, model, summary = m_fail.groups()
                session_counters[session_id] = session_counters.get(session_id, 0) + 1
                ordinal = session_counters[session_id]
                
                dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
                ts_iso = dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
                
                event = {
                    "type": "API_ATTEMPT",
                    "timestamp": ts_iso,
                    "hermes_session_id": session_id,
                    "session_id": session_id,
                    "status": "failed",
                    "error": summary,
                    "provider": provider or "unknown",
                    "base_url": _redact_url_credentials(base_url),
                    "model": model or "unknown",
                    "mode": "unknown",
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "request_ref": f"{session_id}#{ts_str}#call{ordinal}"
                }
                events.append(redact_payload(event, config))
    return events


def _derive_default_db_path() -> str:
    """Store de Hermes: env override > config del agente > path default.

    GAP-01 (forward-compat): la config de Hermes (``~/.hermes/config.json``)
    puede declarar el store con la key ``data``. El env override SIEMPRE
    gana (el operador manda).
    """
    return normalize_store_path(
        "CAUSADB_HERMES_DB_PATH",
        os.path.join(os.path.expanduser("~"), ".hermes", "config.json"),
        os.path.join(os.path.expanduser("~"), ".hermes", "state.db"),
    )


def _s_to_iso(ts: float) -> str:
    """Segundos (epoch UTC, REAL en Hermes) → ISO 8601 con Z (formato
    canónico del ledger). Determinístico y puro."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_model(model: str | None) -> str | None:
    """Estripea el/los prefijo(s) provider del model name si los trae
    (``local:...``, ``custom:...``, ``ollama:...``, incluso anidados como
    ``custom:ollama:qwen3.5:4b``). Un Ollama tag (``qwen3.5:4b``) se
    preserva. Devuelve None si no hay model o si tras el strip no queda
    nombre (incluye prefijo desnudo tipo ``local:``). Set cerrado de
    prefijos (``_PROVIDER_PREFIXES``); sensible a mayúsculas (Hermes
    persiste los prefijos en minúsculas)."""
    if not model:
        return None
    m = model.strip()
    changed = True
    while changed:
        changed = False
        for prefix in _PROVIDER_PREFIXES:
            if m.startswith(prefix) and len(m) >= len(prefix):
                m = m[len(prefix):]
                changed = True
                break
    return m or None


def _synthesize_subject(text: str) -> str:
    """Hermes no trae subject en los reasoning → la puntita sintetiza uno
    determinístico con las primeras 8 palabras del texto."""
    return " ".join(text.split()[:8])


def _hermes_conversation_ref(session_id: str) -> dict:
    """Metadata-only locator for the Hermes SQLite transcript."""
    return {
        "provider": "hermes",
        "native_id": session_id,
        "locator_kind": "sqlite",
        "locator": "hermes_default",
        "resolver": "hermes",
        "confidence": "verified",
        "content_class": "transcript_complete",
        "privacy_class": "raw_sensitive",
    }


def _parse_tool_calls(tool_calls_json: str | None) -> list[dict]:
    """Parse del JSON array de ``tool_calls`` de un mensaje assistant.
    Devuelve lista de dicts ``{"id", "name", "arguments"}`` o [] si no hay."""
    if not tool_calls_json:
        return []
    try:
        parsed = json.loads(tool_calls_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    result = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        fn = entry.get("function") or {}
        if not isinstance(fn, dict):
            continue
        result.append({
            "id": entry.get("id") or entry.get("call_id"),
            "name": fn.get("name") or "unknown_tool",
            "arguments": fn.get("arguments") or "",
        })
    return result


def _resolve_tool_result(con: sqlite3.Connection, session_id: str,
                         tool_call_id: str | None, tool_name: str,
                         tc_rowid: int, stmt_by_id: str,
                         stmt_by_name: str) -> str:
    """Result de un tool_call vía lookup anticipado (FIX.HERMES).

    El TOOL_CALLED se emite en la fila assistant; su ``result`` se resuelve
    aquí consultando la fila ``role='tool'`` que lo completa (la fila tool
    tiene rowid MAYOR y ya existe en el store aunque el barrido aún no la
    alcance). Reemplaza el pairing post-hoc sobre la lista (eliminado).

    - Primario: por ``tool_call_id`` de la fila tool (caso real — la fila
      tool SIEMPRE lleva tool_call_id, confirmado con datos reales).
    - Fallback: por ``tool_name`` de una fila tool sin id que siga a este
      tool_call (caso defensivo de datos viejos).

    Los statements se preparan UNA vez en ``harvest()`` antes del while y se
    reutilizan por tool_call (deuda de performance auditada, hallazgo #4:
    no re-preparar por iteración). Devuelve el content o "" si no hay
    result (sesión cortada).
    """
    if tool_call_id:
        row = con.execute(
            stmt_by_id, (session_id, tool_call_id, tc_rowid)
        ).fetchone()
        if row:
            return row[0] or ""
    row = con.execute(
        stmt_by_name, (session_id, tool_name, tc_rowid)
    ).fetchone()
    return row[0] if row else ""


class HermesHarvestSource(HarvestSource):
    """Fuente de harvest para las sesiones de Hermes Agent.

    Args:
        ledger_path: Ruta absoluta al ledger (requerido por la clase base).
        db_path: Ruta al store SQLite de Hermes. Default:
            ``CAUSADB_HERMES_DB_PATH`` o ``~/.hermes/state.db`` (override para
            tests).
    """

    def __init__(self, ledger_path: str, db_path: Optional[str] = None):
        super().__init__(ledger_path)
        self.db_path = db_path or _derive_default_db_path()

    def source_type(self) -> str:
        # SIN colon (fix de namespace — ver plan §3)
        return "hermes"

    def cursor_key(self) -> str:
        return "agent:hermes"

    def detect(self) -> bool:
        return os.path.isfile(self.db_path)

    def harvest(self, cursor: dict | None = None) -> Iterator[dict]:
        return self._harvest(cursor)

    def harvest_session(self, session_id: str) -> Iterator[dict]:
        """Read only one session without scanning unrelated messages."""
        return self._harvest({}, session_id=session_id)

    def _harvest(
        self, cursor: dict | None = None, session_id: str | None = None
    ) -> Iterator[dict]:
        target_session_id = session_id
        cursor = cursor or {}
        max_rowid = int(cursor.get("max_rowid", 0))

        # Dedup de ciclo de vida persistido en el cursor
        started_emitted = set(cursor.get("session_started_emitted", []))
        ended_emitted = set(cursor.get("session_ended_emitted", []))
        api_attempt_emitted = set(cursor.get("api_attempt_emitted", []))

        # Cache de model por sesión (Hermes no guarda model por-mensaje).
        session_models: dict[str, str | None] = {}

        # Estado entre mensajes del barrido, POR-SESIÓN: el barrido intercala
        # filas de todas las sesiones por rowid, así que un estado global
        # contaminaría prompts entre sesiones (auditoría, hallazgo 2.1).
        session_last_user_content: dict[str, str] = {}
        session_last_user_ts: dict[str, str] = {}

        # -- 1. conexión read-only (nunca immutable: WAL real) -------------
        # FIX.HERMES: harvest es un generador (yield por raw, sin materializar
        # lista); el finally cierra la conexión cuando se extenúa o se cierra.
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            # --- Guard de tablas requeridas (BIT-CHR.105) -------------------
            # Stores corruptos/vacíos pueden no tener `messages` o
            # `sessions`. Los PRAGMA table_info siguientes asumen que existen
            # y lanzan sqlite3.OperationalError si no. Aunque harvest_all
            # aísla la falla por fuente, llena el log de errores. Fail-open
            # silencioso: si falta una tabla requerida, loguear warning y
            # retornar temprano (generador vacío).
            existing_tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            required = {'messages', 'sessions'}
            missing = required - existing_tables
            if missing:
                logging.warning(
                    "Hermes store %s missing required tables %s — "
                    "skipping harvest for this source",
                    self.db_path, sorted(missing),
                )
                return

            # Obtener datos de facturación de session_model_usage
            billing_data = {}
            try:
                # Guard de columnas (patrón del archivo, H2.2): stores legacy
                # pueden no tener la tabla o las columnas granulares
                # (cache/reasoning/coste/task). PRAGMA table_info determina qué
                # columnas existen y se seleccionan SOLO esas.
                usage_cols = [c[1] for c in con.execute(
                    "PRAGMA table_info(session_model_usage)"
                ).fetchall()]
                if "session_id" not in usage_cols:
                    # Tabla sin session_id → no correlacionable por sesión
                    raise sqlite3.OperationalError(
                        "session_model_usage sin session_id"
                    )
                # Columnas base (schema v22) + granulares H2.2 si existen.
                select_cols = [
                    c for c in (
                        "session_id", "billing_provider", "billing_base_url",
                        "billing_mode", "api_call_count", "task",
                        "cache_read_tokens", "cache_write_tokens",
                        "reasoning_tokens", "estimated_cost_usd",
                        "actual_cost_usd", "cost_status", "cost_source",
                        "input_tokens", "output_tokens",
                    ) if c in usage_cols
                ]
                billing_rows = con.execute(
                    f"SELECT {', '.join(select_cols)} FROM session_model_usage"
                ).fetchall()
                for row in billing_rows:
                    rec = dict(zip(select_cols, row))
                    sid = rec.pop("session_id", None)
                    if not sid:
                        continue
                    billing_data[sid] = rec
            except sqlite3.OperationalError:
                pass # Tabla podría no existir / schema legacy no usable

            # Parsear log pasando billing_data y ledger_path
            log_path = os.path.join(os.path.dirname(self.db_path), "logs", "agent.log")
            all_api_attempts = _parse_log_events(log_path, billing_data, self.ledger_path)
            new_api_attempts = [e for e in all_api_attempts if e["request_ref"] not in api_attempt_emitted]

            # Comprobar si existen las columnas extendidas antes de hacer el SELECT
            s_cols = con.execute("PRAGMA table_info(sessions)").fetchall()
            s_col_names = [c[1] for c in s_cols]
            
            m_cols = con.execute("PRAGMA table_info(messages)").fetchall()
            m_col_names = [c[1] for c in m_cols]
            
            has_s_ext = all(col in s_col_names for col in ['started_at', 'ended_at', 'end_reason', 'parent_session_id'])
            session_cols = ", s.started_at AS sess_started, s.ended_at AS sess_ended, s.end_reason AS sess_end_reason, s.parent_session_id AS sess_parent" if has_s_ext else ", NULL AS sess_started, NULL AS sess_ended, NULL AS sess_end_reason, NULL AS sess_parent"
            
            # Columnas de mensajes para el lossless
            msg_cols_to_add = [
                'id', 'effect_disposition', 'token_count', 'reasoning_details', 
                'codex_reasoning_items', 'codex_message_items', 
                'platform_message_id', 'observed', 'active', 'compacted', 'api_content'
            ]
            msg_selects = []
            for col in msg_cols_to_add:
                if col in m_col_names:
                    msg_selects.append(f"m.{col}")
                else:
                    msg_selects.append(f"NULL AS {col}")
            
            msg_select_str = ", " + ", ".join(msg_selects)

            session_clause = " AND m.session_id = ?" if target_session_id else ""
            query = (
                "SELECT m.rowid AS r, m.session_id, m.role, m.content, "
                "m.tool_call_id, m.tool_calls, m.tool_name, m.timestamp, "
                "m.reasoning_content, m.finish_reason, "
                "s.model AS session_model, "
                "s.input_tokens AS sess_input, s.output_tokens AS sess_output"
                + session_cols + msg_select_str +
                " FROM messages m LEFT JOIN sessions s ON s.id = m.session_id "
                f"WHERE m.rowid > ?{session_clause} ORDER BY m.rowid LIMIT ?"
            )
            # Statements del lookup anticipado de tool results (FIX.HERMES):
            # preparados UNA vez antes del while y reutilizados por tool_call
            # (deuda de performance auditada, hallazgo #4: no re-preparar
            # por iteración).
            stmt_tool_by_id = (
                "SELECT content FROM messages WHERE session_id=? AND role='tool' "
                "AND tool_call_id=? AND rowid > ? ORDER BY rowid LIMIT 1"
            )
            stmt_tool_by_name = (
                "SELECT content FROM messages WHERE session_id=? AND role='tool' "
                "AND tool_name=? AND tool_call_id IS NULL AND rowid > ? "
                "ORDER BY rowid LIMIT 1"
            )
            while True:
                params = (max_rowid, target_session_id, BATCH_SIZE) if target_session_id else (
                    max_rowid, BATCH_SIZE
                )
                rows = con.execute(query, params).fetchall()
                if not rows:
                    break
                for (r, row_session_id, role, content, tool_call_id, tool_calls,
                     tool_name, timestamp, reasoning_content, finish_reason,
                     session_model, sess_input, sess_output,
                     sess_started, sess_ended, sess_end_reason, sess_parent,
                     m_id, m_effect, m_tokens, m_reasoning_det, m_codex_reasoning,
                     m_codex_message, m_platform_id, m_observed, m_active,
                     m_compacted, m_api_content) in rows:
                    # Timestamp ISO una vez por fila (segundos epoch).
                    ts_iso = _s_to_iso(timestamp)

                    # -- SESSION_STARTED (una vez por sesión) --------------
                    if row_session_id and row_session_id not in started_emitted:
                        raw_started = {
                            "type": "SESSION_STARTED",
                            "timestamp": ts_iso,
                            "session_id": row_session_id,
                            "agent": self.source_type(),
                        }
                        if sess_started:
                            raw_started["started_at"] = _s_to_iso(sess_started)
                        if sess_parent:
                            raw_started["parent_session_id"] = sess_parent
                        
                        raw_started["__harvest_rowid"] = r
                        raw_started["__harvest_session_id"] = row_session_id
                        raw_started["__harvest_locator"] = self.db_path
                        if row_session_id:
                            raw_started["__conversation_ref"] = _hermes_conversation_ref(
                                row_session_id
                            )
                        yield raw_started
                        started_emitted.add(row_session_id)

                    # -- model por sesión (cache) --------------------------
                    if row_session_id not in session_models:
                        session_models[row_session_id] = _normalize_model(session_model)
                    model = session_models.get(row_session_id)

                    if role == "user":
                        session_last_user_content[row_session_id] = content
                        session_last_user_ts[row_session_id] = ts_iso
                        # El primer mensaje puede cerrar la sesión? Poco probable pero posible.
                    elif role == "assistant":
                        # -- thoughts → REASONING_STEP ----------------------
                        if reasoning_content:
                            subject = _synthesize_subject(reasoning_content)
                            raw = {
                                "type": "REASONING_STEP",
                                "timestamp": ts_iso,
                                "step_type": infer_step_type(subject),
                                "step_hash": hashlib.sha256(
                                    reasoning_content.encode("utf-8")
                                ).hexdigest(),
                                "subject": subject,
                                "description": reasoning_content,
                                "agent": self.source_type(),
                                "message_id": m_id,
                                "message_role": role,
                            }
                            if finish_reason: raw["message_finish_reason"] = finish_reason
                            if m_effect: raw["message_effect_disposition"] = m_effect
                            if m_tokens: raw["message_token_count"] = m_tokens
                            if m_reasoning_det: raw["message_reasoning_details"] = m_reasoning_det
                            if m_codex_reasoning: raw["message_codex_reasoning_items"] = m_codex_reasoning
                            if m_codex_message: raw["message_codex_message_items"] = m_codex_message
                            if m_platform_id: raw["message_platform_message_id"] = m_platform_id
                            if m_api_content: raw["message_api_content"] = m_api_content
                            raw["message_observed"] = m_observed
                            raw["message_active"] = m_active
                            raw["message_compacted"] = m_compacted
                            raw["__harvest_rowid"] = r
                            raw["__harvest_session_id"] = row_session_id or "unknown"
                            raw["__harvest_locator"] = self.db_path
                            if row_session_id:
                                raw["__conversation_ref"] = _hermes_conversation_ref(
                                    row_session_id
                                )
                            yield raw

                        # -- tool_calls → TOOL_CALLED -----------------------
                        # El result se resuelve ANTES de emitir (lookup
                        # anticipado, FIX.HERMES): la fila role='tool' ya
                        # existe en el store aunque el barrido no la alcance.
                        for tc in _parse_tool_calls(tool_calls):
                            result = _resolve_tool_result(
                                con, row_session_id, tc["id"], tc["name"], r,
                                stmt_tool_by_id, stmt_tool_by_name,
                            )
                            raw = {
                                "type": "TOOL_CALLED",
                                "timestamp": ts_iso,
                                "tool_name": tc["name"],
                                "arguments": tc["arguments"],
                                "result": result,
                                "tool_call_id": tc["id"],
                                "agent": self.source_type(),
                                "message_id": m_id,
                                "message_role": role,
                            }
                            if finish_reason: raw["message_finish_reason"] = finish_reason
                            if m_effect: raw["message_effect_disposition"] = m_effect
                            if m_tokens: raw["message_token_count"] = m_tokens
                            if m_reasoning_det: raw["message_reasoning_details"] = m_reasoning_det
                            if m_codex_reasoning: raw["message_codex_reasoning_items"] = m_codex_reasoning
                            if m_codex_message: raw["message_codex_message_items"] = m_codex_message
                            if m_platform_id: raw["message_platform_message_id"] = m_platform_id
                            if m_api_content: raw["message_api_content"] = m_api_content
                            raw["message_observed"] = m_observed
                            raw["message_active"] = m_active
                            raw["message_compacted"] = m_compacted
                            raw["__harvest_rowid"] = r
                            raw["__harvest_session_id"] = row_session_id or "unknown"
                            raw["__harvest_locator"] = self.db_path
                            if row_session_id:
                                raw["__conversation_ref"] = _hermes_conversation_ref(
                                    row_session_id
                                )
                            yield raw

                        # -- assistant + model → LLM_INVOKED ----------------
                        if model:
                            prompt = session_last_user_content.get(row_session_id, "")
                            prev_ts = session_last_user_ts.get(row_session_id)
                            raw = {
                                "type": "LLM_INVOKED",
                                "timestamp": ts_iso,
                                "model": model,
                                "prompt": prompt,
                                # H2.3 — response_tokens per-message honesto:
                                # token count REAL del mensaje (messages.token_count,
                                # int > 0) o 0 si es NULL/0. NUNCA el agregado
                                # sessions.output_tokens (vivía solo en
                                # API_ATTEMPT/COST_ACCOUNTED; repetirlo por
                                # assistant duplicaba el total N veces).
                                "response_tokens": (
                                    int(m_tokens)
                                    if (isinstance(m_tokens, int) and m_tokens > 0)
                                    else 0
                                ),
                                "duration_ms": _compute_duration_ms(prev_ts, ts_iso),
                                "response_content": content or "",
                                "agent": self.source_type(),
                                "message_id": m_id,
                                "message_role": role,
                            }
                            if finish_reason: raw["message_finish_reason"] = finish_reason
                            if m_effect: raw["message_effect_disposition"] = m_effect
                            if m_tokens: raw["message_token_count"] = m_tokens
                            if m_reasoning_det: raw["message_reasoning_details"] = m_reasoning_det
                            if m_codex_reasoning: raw["message_codex_reasoning_items"] = m_codex_reasoning
                            if m_codex_message: raw["message_codex_message_items"] = m_codex_message
                            if m_platform_id: raw["message_platform_message_id"] = m_platform_id
                            if m_api_content: raw["message_api_content"] = m_api_content
                            raw["message_observed"] = m_observed
                            raw["message_active"] = m_active
                            raw["message_compacted"] = m_compacted
                            raw["__harvest_rowid"] = r
                            raw["__harvest_session_id"] = row_session_id or "unknown"
                            raw["__harvest_locator"] = self.db_path
                            if row_session_id:
                                raw["__conversation_ref"] = _hermes_conversation_ref(
                                    row_session_id
                                )
                            yield raw

                    # -- SESSION_ENDED (una vez por sesión, al cerrarse) ----
                    if row_session_id and sess_ended and row_session_id not in ended_emitted:
                        raw_ended = {
                            "type": "SESSION_ENDED",
                            "timestamp": _s_to_iso(sess_ended),
                            "session_id": row_session_id,
                            "ended_at": _s_to_iso(sess_ended),
                            "agent": self.source_type(),
                        }
                        if sess_end_reason:
                            raw_ended["end_reason"] = sess_end_reason
                        if sess_parent:
                            raw_ended["parent_session_id"] = sess_parent

                        raw_ended["__harvest_rowid"] = r
                        raw_ended["__harvest_session_id"] = row_session_id
                        raw_ended["__harvest_locator"] = self.db_path
                        yield raw_ended
                        ended_emitted.add(row_session_id)

                max_rowid = rows[-1][0]  # avance dentro del barrido
        finally:
            con.close()
        
        # Emitir API_ATTEMPT al final del barrido
        for api_attempt in new_api_attempts:
            yield api_attempt

    def advance_cursor(
        self, cursor: dict | None, harvested_raw_events: list[dict]
    ) -> dict:
        cursor = cursor or {}
        new_max = int(cursor.get("max_rowid", 0))
        started = set(cursor.get("session_started_emitted", []))
        ended = set(cursor.get("session_ended_emitted", []))
        api_attempts = set(cursor.get("api_attempt_emitted", []))

        for ev in harvested_raw_events:
            rid = ev.get("__harvest_rowid")
            if rid is not None:
                new_max = max(new_max, int(rid))
            
            sid = ev.get("__harvest_session_id")
            etype = ev.get("type")
            if sid and sid != "unknown":
                if etype == "SESSION_STARTED":
                    started.add(sid)
                elif etype == "SESSION_ENDED":
                    ended.add(sid)
            
            if etype == "API_ATTEMPT":
                ref = ev.get("request_ref")
                if ref:
                    api_attempts.add(ref)

        return {
            "max_rowid": new_max,
            "session_started_emitted": sorted(list(started)),
            "session_ended_emitted": sorted(list(ended)),
            "api_attempt_emitted": sorted(list(api_attempts)),
        }
