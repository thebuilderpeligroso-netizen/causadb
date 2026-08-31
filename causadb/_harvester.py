"""Harvester — core del sedimenter.

Orquesta fuentes de harvest (ver ``_harvest_source.py``), mapea los raw
dicts a ``CanonicalEvent`` y los escribe al ledger a través de
``LedgerWriter`` (artículo I — toda escritura al ledger pasa por
LedgerWriter, nunca directa).

Flujo por fuente registrada (``harvest_all``):
    1. ``_load_cursors()``  — lee ``{config_dir}/.harvester_cursors.json``
    2. Para cada fuente:
       ``detect()`` → ``harvest(cursor)`` (retorna lista o generador —
       duck typing; el harvester itera el resultado en batches) →
       ``_event_from_raw()`` → ``write_events()`` → actualizar cursor
    3. ``_save_cursors()``  — persiste cursores al mismo archivo

Deduplicación: el cursor (``{"index": N}``) actúa como marca de agua
secuencial. En la siguiente corrida, la fuente cosecha solo desde
``index`` hacia adelante, evitando reescribir eventos ya sedimentados.
"""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Optional

from causadb._harvest_source import HarvestSource
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._config import CausaDBConfig
from causadb._decision_distill import candidate_decision_events, load_existing_decision_parents
# Conjunto de tipos de evento considerados "esenciales" para el OCB (caché L1).
# Los demás (TOOL_CALLED, COMMAND_RUN, REASONING_STEP) van al ledger pero no
# deberían saturar el OCB, que debe contener solo la señal de sesión compacta.
ESSENTIAL_EVENT_TYPES = {
    EventType.FILE_MODIFIED,
    EventType.GOVERNANCE_DECISION,
    EventType.PROJECT_SNAPSHOT,
}
# Registra los 6 EventTypes de Hermes ANTES de que el harvester los emita,
# para que un raw con type custom no se degrade a OBSERVATION (H0.2 — Bloqueante #4).
from causadb import _hermes_event_types  # noqa: F401

# ---------------------------------------------------------------------------
# Fuentes de agente (generan SESSION_SUMMARY al cosechar)
# ---------------------------------------------------------------------------
_AGENT_SOURCES = frozenset({"gemini", "opencode", "claude", "grok", "hermes", "openjarvis", "codex", "cursor", "windsurf"})

# ---------------------------------------------------------------------------
# Markers de cursor (fuentes con cursores no-secuenciales)
# ---------------------------------------------------------------------------
# Las fuentes de agente (gemini/opencode) incrustan en cada raw dict la
# información necesaria para que ``advance_cursor`` sepa HASTA DÓNDE avanzó
# (qué línea/rowid consumió el evento). Estas claves son reservadas: no
# viajan al payload del evento (ver ``_event_from_raw``).
_CURSOR_MARKER_KEYS = frozenset({
    "__harvest_file",
    "__harvest_offset",
    "__harvest_mtime",
    "__harvest_rowid",
    "__harvest_message_id",
    "__harvest_session_id",
    "__harvest_locator",
    "__conversation_ref",
})

# ---------------------------------------------------------------------------
# Timestamp normalization
# ---------------------------------------------------------------------------

_NON_ISO_DT_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
]


def normalize_timestamp(ts: str | int | float) -> str:
    """Normaliza cualquier timestamp soportado a ISO 8601 (UTC con Z).

    Acepta:
    - ``str`` ISO 8601 con/sin Z/offset  (ej. ``"2024-01-01T12:00:00Z"``)
    - ``str`` SQL ``"YYYY-MM-DD HH:MM:SS"``
    - ``int`` / ``float`` Unix timestamp (segundos desde epoch)

    Retorna:
        str en formato ``YYYY-MM-DDTHH:MM:SS.ffffffZ``.
    """
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError, OSError):
            return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    ts_str = str(ts).strip()

    # 1 — ISO 8601 nativo (Python 3.11+ soporta fromisoformat con Z)
    try:
        normalized = ts_str.replace("Z", "+00:00").replace("z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        return dt.isoformat() + "Z"
    except (ValueError, TypeError):
        pass

    # 2 — Formatos no-ISO comunes
    for fmt in _NON_ISO_DT_FORMATS:
        try:
            dt = datetime.strptime(ts_str, fmt)
            return dt.isoformat() + "Z"
        except (ValueError, TypeError):
            continue

    # 3 — Fallback: devolver literal (puede ser ISO 8601 válido de todos modos)
    return ts_str


# ---------------------------------------------------------------------------
# Harvester
# ---------------------------------------------------------------------------

class Harvester:
    """Orquesta fuentes de harvest y escribe eventos al ledger.

    Args:
        ledger_path: Ruta absoluta al ledger.
        config_path: (opcional) Ruta al archivo JSON de cursores.
            Por defecto ``<ledger_dir>/.harvester_cursors.json``.
    """

    def __init__(self, ledger_path: str, config_path: Optional[str] = None):
        if not os.path.isabs(ledger_path):
            raise ValueError(f"ledger_path must be absolute, got: {ledger_path}")

        self.ledger_path = ledger_path
        self.config_path = config_path or os.path.join(
            os.path.dirname(ledger_path), ".harvester_cursors.json"
        )
        self._sources: dict[str, HarvestSource] = {}
        self._config = CausaDBConfig(ledger_path=ledger_path)
        self._writer = LedgerWriter(ledger_path, config=self._config)
        self._conversation_ref_sessions: set[str] = set()

    # -- Public API ----------------------------------------------------------

    def register_source(self, source: HarvestSource):
        """Registra una fuente de harvest."""
        self._sources[source.source_type()] = source

    def harvest_all(
        self,
        dry_run: bool = False,
        stop_event: Optional[threading.Event] = None,
    ) -> dict[str, int] | dict[str, list]:
        """Cosecha de todas las fuentes registradas.

        Args:
            dry_run: Si True, no escribe al ledger ni avanza cursores.
                Retorna ``{source_type: [raw_events]}`` en lugar de
                ``{source_type: count}``.
            stop_event: Si se provee, el backfill se aborta cooperativamente
                al terminar la fuente en curso cuando el evento está seteado
                (H-OPS.1). Permite que ``HarvesterDaemon.stop()`` retorne en
                el tiempo de UNA fuente en vez de esperar el backfill
                completo — sin sacrificar la garantía D.3 (el append en
                curso termina, el shutdown event queda último).

        Returns:
            ``{source_type: events_count}`` (dry_run=False) o
            ``{source_type: [raw_events]}`` (dry_run=True).
        """
        results: dict[str, int] | dict[str, list] = {}
        cursors = self._load_cursors()

        for source_type, source in self._sources.items():
            # Auditoría I.2 (BIT-CHR.41; ver docs/design_index.md): aislamiento por
            # fuente. Una excepción en la COSECHA de una fuente (ej. sqlite
            # corrupto en opencode) NO debe abortar las demás ni perder el
            # guardado de cursores (evita re-harvest en cascada). El fallo
            # de ESCRITURA ya lo maneja `write_events` (fail-fast + prefijo).
            if stop_event is not None and stop_event.is_set():
                break
            try:
                outcome = self._harvest_one(source, cursors, dry_run=dry_run)
            except Exception:
                import logging
                logging.exception(f"Harvest failed for source {source_type}; continuing with others")
                outcome = 0 if not dry_run else []
            results[source_type] = outcome

        if not dry_run:
            self._save_cursors(cursors)
        return results

    def harvest_source(self, source_type: str) -> int:
        """Cosecha de una fuente específica por su identificador."""
        source = self._sources.get(source_type)
        if source is None:
            return 0

        cursors = self._load_cursors()
        count = self._harvest_one(source, cursors)
        self._save_cursors(cursors)
        return count

    def write_events(self, events: list) -> int:
        """Escribe una lista de ``CanonicalEvent`` al ledger vía LedgerWriter.

        Centraliza la escritura al ledger para que tanto el flujo de
        harvest por fuente como cualquier caller externo usen el mismo
        camino (artículo I — toda escritura pasa por LedgerWriter).

        Semántica fail-fast con conteo de prefijo (auditoría I.2 /
        BIT-CHR.41 Auditoría I.2): si un evento falla al escribirse, la
        escritura se aborta en ese punto y se retorna cuántos eventos
        anteriores SÍ fueron escritos. El caller interpreta el retorno
        como "se escribieron los primeros N" — los no escritos se
        re-cosechan en la siguiente corrida (el cursor solo avanza sobre
        lo escrito, atomicidad por fuente).

        Args:
            events: lista de instancias de ``CanonicalEvent``.

        Returns:
            Número de eventos efectivamente escritos (prefijo de la lista).
        """
        count = 0
        for event in events:
            try:
                self._writer.append(event)
                count += 1
            except Exception:
                import logging
                logging.exception(
                    f"Failed to write event {event.event_id} to ledger; "
                    f"aborting batch at {count} written"
                )
                break
        return count

    # -- Internal ------------------------------------------------------------

    def _harvest_one(self, source: HarvestSource, cursors: dict, dry_run: bool = False) -> int | list:
        import logging as _harv_log

        source_type = source.source_type()
        if not source.detect():
            return 0 if not dry_run else []

        cursor = cursors.get(source.cursor_key())
        # C.2.3 — Generaliza la carga del dedup de conversation_ref a TODAS
        # las fuentes agente (antes solo opencode). Cualquier fuente que
        # emita __conversation_ref debe rehidratar el set de sesiones ya
        # referenciadas para no duplicar la ref al reiniciar el harvester.
        if source_type in _AGENT_SOURCES and isinstance(cursor, dict):
            self._conversation_ref_sessions.update(
                str(session_id)
                for session_id in cursor.get("conversation_ref_sessions", [])
                if session_id
            )
        harvest_result = source.harvest(cursor)

        if dry_run:
            if isinstance(harvest_result, list):
                return harvest_result
            return list(harvest_result)

        batch_size = int(os.environ.get("CAUSADB_HARVEST_BATCH_SIZE", 500))

        # Cap anti-OOM: si una sesión cosechada supera este número de
        # eventos, se skipea el summarizer/storyboard con warning (mejor
        # que OOM de 24GB). El ledger se escribe igual; solo se difiere
        # la generación del resumen/storyboard para sesiones grandes.
        summary_max_events = int(os.environ.get("CAUSADB_SUMMARY_MAX_EVENTS", 5000))

        _harv_log.info(
            "harvest_one [%s] starting",
            source_type,
        )

        is_agent = source_type in _AGENT_SOURCES

        total_count = 0
        batch = []
        # Estructura ligera: una sola lista de raw events (antes había
        # dos listas idénticas: all_raw_for_summarizer y
        # all_raw_for_storyboard). Solo se acumula para agent sources
        # (NIT H8: ambas ramas eran idénticas). Se llena con cap: si
        # supera ``summary_max_events``, se deja de acumular y se marca
        # ``too_large`` para skipear summarizer/storyboard.
        raw_events_buffer: list = []
        too_large = False
        # FIX.2 (Fase 1 — OCB feed por batch) — el OCB se alimenta en vivo
        # dentro de _flush_batch, batch por batch, en cuanto los eventos
        # se escriben al ledger. NO se acumulan CanonicalEvent en una lista
        # ``ocb_events`` al final (eso causaba un pico de RAM dominante
        # para 81K eventos opencode). El OCB particiona, rota y descarta
        # solo (caché volátil de corto plazo, Art. V); NO se llama
        # close_session() — un harvest abarca varias sesiones. El ledger
        # sigue siendo la fuente de verdad (Art. I); el OCB es una
        # proyección volátil para resume/detalle granular. Un evento → un
        # append al OCB (sin duplicar). ``ocb_failed`` skipea el feed
        # para el resto del harvest tras el primer fallo (evita log spam).
        ocb = None
        ocb_failed = False
        # FIX.GOV-AUTO-1 — Parents de GOVERNANCE_DECISION existentes para
        # dedup. Lazy: se carga UNA vez por _harvest_one, solo si hay
        # eventos escritos (evita una pasada completa del ledger para
        # fuentes sin actividad).
        existing_decision_parents: Optional[set] = None
        conversation_ref_sessions = self._conversation_ref_sessions

        def _flush_batch():
            nonlocal total_count, cursor, too_large, ocb, ocb_failed, existing_decision_parents
            if not batch:
                return True
            projected_batch = []
            batch_sessions: set[str] = set()
            for raw in batch:
                session_id = raw.get("__harvest_session_id")
                if session_id in conversation_ref_sessions or session_id in batch_sessions:
                    raw = dict(raw)
                    raw.pop("__conversation_ref", None)
                elif session_id is not None:
                    batch_sessions.add(session_id)
                projected_batch.append(raw)
            events = [self._event_from_raw(source_type, raw) for raw in projected_batch]
            written = self.write_events(events)
            conversation_ref_sessions.update(
                raw.get("__harvest_session_id")
                for raw in projected_batch[:written]
                if raw.get("__conversation_ref") is not None
            )
            if is_agent and written > 0 and not ocb_failed:
                try:
                    if ocb is None:
                        from causadb._ocb_manager import OCB
                        ocb = OCB.for_ledger(self.ledger_path, actor_id=source_type)
                    # F3 — solo append eventos esenciales al OCB para evitar
                    # saturar con TOOL_CALLED (68K), COMMAND_RUN (52K) y
                    # REASONING_STEP (30K). Los no esenciales siguen al ledger
                    # vía write_events pero no al OCB.
                    essential_events = [ev for ev in events[:written] if ev.event_type in ESSENTIAL_EVENT_TYPES]
                    for ev in essential_events:
                        ocb.append(ev)
                except Exception:
                    import logging
                    logging.exception(
                        "OCB feed failed for source %s; continuing with harvest",
                        source_type,
                    )
                    ocb_failed = True
            if is_agent and not too_large:
                # Acumular solo si no superamos el cap. Una vez superado,
                # no se recupera (skipeo conservador).
                remaining = summary_max_events - len(raw_events_buffer)
                if remaining <= 0:
                    too_large = True
                else:
                    raw_events_buffer.extend(batch[:written])
                    if len(raw_events_buffer) >= summary_max_events:
                        too_large = True
            total_count += written
            if written > 0:
                next_cursor = source.advance_cursor(cursor, batch[:written])
                # C.2.3 — Generaliza la persistencia del dedup a cualquier fuente
                # agente (antes solo opencode). Se persiste SIEMPRE que el
                # set global tenga algo que guardar (no condicionado a que el
                # batch actual emita ref): si un batch es 100% deduped, no
                # re-guardar la clave la pierde del cursor y la corrida
                # siguiente vuelve a duplicar la ref (regresión detectada por
                # Checker 2026-08-12).
                if is_agent and self._conversation_ref_sessions:
                    next_cursor["conversation_ref_sessions"] = sorted(
                        self._conversation_ref_sessions
                    )
                cursors[source.cursor_key()] = next_cursor
                cursor = cursors[source.cursor_key()]
                # FIX.GOV-AUTO-1 (C1) — Auto-distill de decisiones: derivar
                # DENTRO de _flush_batch sobre events[:written] (los padres
                # ya están en el ledger → orden causal para
                # _by_parent_event_id). Cubre TODAS las fuentes (agente +
                # shell/n8n), sin depender de raw_events_buffer ni del cap
                # de 5000. Dedup por parent contra decisiones existentes,
                # cargado una vez por _harvest_one.
                if existing_decision_parents is None:
                    existing_decision_parents = load_existing_decision_parents(self.ledger_path)
                gov_events = candidate_decision_events(
                    events[:written],
                    self._config,
                    ctx_id=f"harvester:{source_type}",
                )
                gov_events = [
                    g for g in gov_events
                    if g.parent_event_id not in existing_decision_parents
                ]
                if gov_events:
                    self.write_events(gov_events)
                    for g in gov_events:
                        if g.parent_event_id:
                            existing_decision_parents.add(g.parent_event_id)
            batch.clear()
            return written > 0

        for raw in harvest_result:
            batch.append(raw)
            if len(batch) >= batch_size:
                if not _flush_batch():
                    break

        _flush_batch()

        count = total_count

        # FIX.OCB-FLUSH — cada corrida con eventos archiva su ACTIVE como
        # PARTITION (evita que _init_workspace lo orphane en la próxima
        # instanciación, congelando revive/resume en el último rebuild).
        if ocb is not None and not ocb_failed:
            try:
                flushed = ocb.flush_active_to_partition()
                _harv_log.info(
                    "harvest_one [%s] ocb flush_active_to_partition=%s",
                    source_type, flushed,
                )
            except Exception:
                import logging
                logging.exception(
                    "OCB flush failed for source %s; continuing with harvest",
                    source_type,
                )

        if count > 0 and is_agent:
            if too_large:
                _harv_log.warning(
                    "harvest_one [%s] session too large for summary "
                    "(> %d events); summary skipped",
                    source_type, summary_max_events,
                )
            else:
                try:
                    from causadb._session_summarizer import summarize_session
                    summary_event = summarize_session(raw_events_buffer, tool=source_type)
                    if summary_event is not None:
                        self.write_events([summary_event])
                except Exception:
                    import logging
                    logging.exception("Session summary generation failed; continuing with harvest")

        if count > 0 and is_agent:
            if too_large:
                _harv_log.warning(
                    "harvest_one [%s] session too large for storyboard "
                    "(> %d events); storyboard skipped",
                    source_type, summary_max_events,
                )
            else:
                try:
                    from causadb._storyboard import build_storyboard, sanitize_session_id
                    sessions: dict[str, list] = {}
                    for raw in raw_events_buffer:
                        sid = raw.get("__harvest_session_id") or "unknown"
                        sessions.setdefault(sid, []).append(raw)
                    for session_id, session_raws in sessions.items():
                        board = build_storyboard(session_raws, tool=source_type)
                        if board is None:
                            continue
                        safe_tool = sanitize_session_id(source_type)
                        safe_session = sanitize_session_id(session_id)
                        story_dir = os.path.join(self._config.storyboard_path, safe_tool)
                        os.makedirs(story_dir, exist_ok=True)
                        tmp_file = os.path.join(story_dir, f".{safe_session}.tmp")
                        story_file = os.path.join(story_dir, f"{safe_session}.json")
                        with open(tmp_file, "w", encoding="utf-8") as f:
                            json.dump(board, f, indent=2)
                        os.replace(tmp_file, story_file)
                except Exception:
                    import logging
                    logging.exception("Storyboard generation failed; continuing with harvest")

        if count > 0 and is_agent:
            try:
                # El distill post-harvest corre si se cosechó al menos
                # una sesión (independiente del cap de summarizer/storyboard).
                sessions_harvested = len({
                    raw.get("__harvest_session_id")
                    for raw in raw_events_buffer
                    if raw.get("__harvest_session_id")
                })
                if sessions_harvested >= 1:
                    from causadb._skill_registry import distill_post_harvest
                    distill_post_harvest(self.ledger_path, source_type=source_type)
            except Exception:
                import logging
                logging.exception("Post-harvest distill failed; continuing with harvest")

        return count

    def _load_cursors(self) -> dict:
        """Carga cursores desde el archivo de configuración."""
        if not os.path.exists(self.config_path):
            return {}
        try:
            with open(self.config_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _save_cursors(self, cursors: dict):
        """Persiste cursores en el archivo de configuración."""
        cfg_dir = os.path.dirname(self.config_path)
        if cfg_dir:
            os.makedirs(cfg_dir, exist_ok=True)
        with open(self.config_path, "w") as f:
            json.dump(cursors, f, indent=2)

    def _event_from_raw(self, source_type: str, raw: dict) -> CanonicalEvent:
        """Mapea un raw dict de una fuente a un CanonicalEvent.

        Extrae los campos reservados ``type``, ``timestamp`` del dict;
        el resto pasa a ``payload``. El event_id se deriva
        determinísticamente del contenido vía SHA-256.

        ``__harvest_session_id`` es un cursor marker (no viaja al payload
        automáticamente), pero se proyecta explícitamente al campo
        ``session_id`` del payload para que summarizer/storyboard/OCB
        puedan recuperar la sesión de un evento ya sedimentado en el
        ledger sin necesidad del raw dict original.
        """
        event_type_str = raw.get("type", "OBSERVATION")
        timestamp_raw = raw.get(
            "timestamp", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        timestamp = normalize_timestamp(timestamp_raw)
        ctx_id = f"harvester:{source_type}"

        # Campos reservados → no pasan a payload
        reserved = {
            "type", "timestamp", "ctx_id", "source",
            "source_type", "event_id",
        } | _CURSOR_MARKER_KEYS
        payload = {k: v for k, v in raw.items() if k not in reserved}

        # Proyectar __harvest_session_id → session_id en el payload.
        # El marker sigue siendo reservado (no viaja con su nombre
        # original), pero su valor se expone como ``session_id`` para
        # que el ledger sea self-describing respecto a la sesión.
        session_id = raw.get("__harvest_session_id")
        if session_id is not None:
            payload["session_id"] = session_id
            conversation_ref = raw.get("__conversation_ref")
            if conversation_ref is not None:
                payload["conversation_ref"] = conversation_ref

        # Proyectar __harvest_locator → session_locator (ruta al JSONL crudo
        # que originó la sesión). Permite localizar/reabrir la conversación
        # original a partir de un evento ya sedimentado (C1.3).
        locator = raw.get("__harvest_locator")
        if locator is not None:
            payload["session_locator"] = locator

        # event_id determinístico — SHA-256 del contenido del evento, SIN
        # los campos reservados ni los markers de cursor (Artículo VI: el
        # mismo contenido lógico debe producir el mismo event_id aunque la
        # posición de cursor cambie; los markers son bookkeeping, no
        # contenido causal). ``session_id`` SÍ entra al hash porque es
        # contenido causal (identifica la sesión a la que pertenece el
        # evento).
        raw_for_hash = dict(payload)
        raw_json = json.dumps(raw_for_hash, sort_keys=True)
        event_id = hashlib.sha256(raw_json.encode()).hexdigest()[:32]

        # Convertir event_type string a EventType
        try:
            et = EventType(event_type_str)
        except ValueError:
            et = EventType("OBSERVATION")

        return CanonicalEvent(
            event_type=et,
            ctx_id=ctx_id,
            source=f"harvester:{source_type}",
            source_type="agent",
            timestamp=timestamp,
            payload=MappingProxyType(payload),
            event_id=event_id,
        )
