import os
import json
import time
import logging
import threading
from collections import Counter
from typing import Optional

from causadb._event_schema import CanonicalEvent
from causadb._blob_store import BlobStore


logger = logging.getLogger(__name__)


class OCB:
    def __init__(self, actor_id: str, base_path: str, threshold_events: int = 80,
                 partition_minutes: int = 15, retention_days: Optional[int] = None,
                 max_rewind_partitions: int = 5,
                 blob_store: Optional[BlobStore] = None,
                 blob_store_threshold: int = 1024,
                 max_size_mb: int = 20, max_partitions: int = 500,
                 fail_loud: bool = False):
        self.actor_id = actor_id
        self.base_path = base_path
        self.threshold_events = threshold_events
        self.partition_minutes = partition_minutes
        self.retention_days = retention_days
        self.max_rewind_partitions = max_rewind_partitions
        # Fase 0 (deuda #13) — BlobStore opcional. Con él, los payloads
        # que superan (estrictamente) ``blob_store_threshold`` se
        # externalizan a refs ``$blob`` (formato uniforme con el ledger);
        # sin él → comportamiento inline histórico (cero regresión).
        self.blob_store = blob_store
        self.blob_store_threshold = blob_store_threshold
        # F2 (M1) — LRU purge configurable.
        self.max_size_mb = max_size_mb
        self.max_partitions = max_partitions
        # F2 (M1) — fail_loud solo para tests: fuerza RuntimeError si la
        # derivación del blob_store falla. ``for_ledger`` NO lo pasa.
        self.fail_loud = fail_loud
        self.observers = []
        self._lock = threading.Lock()
        self._closed = False
        self._last_purge_sweep = 0.0
        self._active_path = os.path.join(base_path, "OCB_ACTIVE.log")
        self._summary_path = os.path.join(base_path, "OCB_SUMMARY.json")
        self._manifest_path = os.path.join(base_path, "OCB_MANIFEST.json")
        self._init_workspace()
        # F2 (M1) — fail_loud: solo para tests. Si fail_loud=True y
        # blob_store is None (no derivado), forzar RuntimeError con
        # diagnóstico. ``for_ledger`` NO pasa fail_loud (preserva callers).
        if self.fail_loud and self.blob_store is None:
            raise RuntimeError(
                "fail_loud=True: OCB degradado a inline (blob_store is None)"
            )

    @classmethod
    def for_ledger(cls, ledger_path: str, actor_id: str = "cli") -> "OCB":
        """Build an OCB instance from a ledger path.

        Fase 0 — el BlobStore se deriva de ``CausaDBConfig``: si
        ``blob_store_enabled`` es True, el OCB comparte el BlobStore del
        ledger (mismo ``blob_store_path``) → content-addressing: mismo
        contenido → mismo hash → el OCB reutiliza los blobs que ya escribe
        el LedgerWriter (NO duplica contenido, Art. V).

        F2 (M1) — si la derivación falla, loggea WARNING + emite un evento
        ``OBSERVATION`` al ledger via ``LedgerWriter.append`` (Art. I — el
        único write al ledger en M1). El OCB inline sigue siendo válido.
        """
        if not ledger_path:
            raise ValueError("ledger_path is required")
        base_path = os.path.join(os.path.dirname(ledger_path), "ocb")
        os.makedirs(base_path, exist_ok=True)
        blob_store = None
        threshold_events = 80
        blob_store_threshold = 1024
        max_size_mb = 64
        max_partitions = 500
        retention_days = None
        try:
            from causadb._config import CausaDBConfig
            config = CausaDBConfig.from_env_with_overrides(ledger_path=ledger_path)
            threshold_events = config.ocb_threshold_events
            blob_store_threshold = config.blob_store_threshold
            max_size_mb = config.ocb_max_size_mb
            max_partitions = config.ocb_max_partitions
            retention_days = config.ocb_retention_days
            if config.blob_store_enabled:
                blob_store = BlobStore(config.blob_store_path)
        except Exception as err:
            # F2 (M1) — Observabilidad durable de la degradación.
            logger.warning("OCB.for_ledger degradado a inline: %s", err)
            cls._emit_degradation_observation(ledger_path, err)
            blob_store = None
        return cls(
            actor_id, base_path,
            blob_store=blob_store,
            blob_store_threshold=blob_store_threshold,
            max_size_mb=max_size_mb,
            max_partitions=max_partitions,
            threshold_events=threshold_events,
            retention_days=retention_days,
        )

    @staticmethod
    def _emit_degradation_observation(ledger_path: str, err: Exception):
        """F2 (M1) — emite un evento ``OBSERVATION`` al ledger via
        ``LedgerWriter.append`` (Art. I). Si la escritura falla, loggea
        doble-fallo y continúa (no propaga — el OCB inline sigue válido).
        """
        try:
            from causadb._ledger_writer import LedgerWriter
            from causadb._event_types import EventType
            from types import MappingProxyType
            writer = LedgerWriter(ledger_path)
            event = CanonicalEvent(
                event_type=EventType.OBSERVATION,
                ctx_id="ocb",
                source="opencode:agent",
                source_type="agent",
                payload=MappingProxyType({
                    "file_path": "causadb/_ocb_manager.py",
                    "line_number": 0,
                    "description": f"OCB.for_ledger degraded to inline: {err}",
                    "severity": "minor",
                    "resolved": False,
                }),
            )
            writer.append(event)
        except Exception as double_err:
            logger.warning(
                "OCB.for_ledger doble-fallo al emitir OBSERVATION: %s",
                double_err,
            )

    def _init_workspace(self):
        if os.path.exists(self._active_path) and not os.path.exists(self._summary_path):
            if os.path.getsize(self._active_path) > 0:
                self._rotate()
            else:
                # F1 — OCB_ORPHAN_*.log: creado cuando el ACTIVE tiene contenido
                # pero no hay SUMMARY.json. Este archivo "huérfano" (active sin
                # close_session) es evidencia de un cierre abrupto (crash detection).
                # Su presencia (vacío o con contenido) distingue:
                #   - orphan con contenido → sesión previa con actividad → NOT first_run
                #   - orphan vacío    → evidencia residual de crash → aún abrupt_close
                #   - sin orphan       → first_run si no hay partitions/summary activa
                ts = int(time.time())
                orphan = os.path.join(self.base_path, f"OCB_ORPHAN_{ts}.log")
                os.rename(self._active_path, orphan)

    def append(self, event: CanonicalEvent):
        """Appenda un ``CanonicalEvent`` a la partición activa del OCB.

        Fase 0 — si el OCB tiene ``blob_store`` y el payload serializado
        supera (estrictamente) ``blob_store_threshold`` bytes, el payload
        se persiste en el BlobStore y la línea de partición guarda
        ``event.to_dict()`` completo (12 campos) con ``payload``
        reemplazado por ``{"$blob": hash}`` — formato uniforme con el
        ledger (Art. I), sin duplicar contenido (Art. V). Caso contrario
        se escribe inline (comportamiento histórico).
        """
        with self._lock:
            self._append_locked(event)
        self._auto_purge()

    def _serialize_event(self, event: CanonicalEvent) -> str:
        """Serializa un evento a línea de partición (JSON, sort_keys).

        Externaliza payloads grandes al BlobStore con refs ``$blob``
        cuando hay blob_store y ``len(payload_bytes) > threshold``
        (estrictamente mayor; ``== threshold`` → inline).
        """
        event_dict = event.to_dict()
        payload = dict(event.payload)
        if self.blob_store is not None:
            payload_bytes = json.dumps(payload, sort_keys=True).encode()
            if len(payload_bytes) > self.blob_store_threshold:
                content_hash = self.blob_store.put(payload)
                payload = {"$blob": content_hash}
        event_dict["payload"] = payload
        return json.dumps(event_dict, sort_keys=True)

    def _append_locked(self, event):
        count = self._count_active()
        if count >= self.threshold_events:
            self._rotate()
        if os.path.exists(self._active_path):
            mtime = os.path.getmtime(self._active_path)
            elapsed = time.time() - mtime
            if elapsed >= self.partition_minutes * 60:
                self._rotate()
        with open(self._active_path, "a") as f:
            f.write(self._serialize_event(event) + "\n")
            f.flush()
            os.fsync(f.fileno())
        for obs in self.observers:
            obs({"event_id": event.event_id, "event_type": event.event_type})

    def _resolve_payload(self, payload: dict, resolve_blobs: bool = True) -> dict:
        """Resolución fall-closed PROPIA de un payload ``$blob`` (ajuste 4).

        NO depende de ``resolve_payload()`` del BlobStore (BIT-CHR.35 F2
        va a cambiar esa función a fail-fast; acá el OCB define su propia
        semántica):
        - con ``blob_store`` y ``resolve_blobs=True``: ``blob_store.get(hash)``
          bajo demanda; si el blob no existe (``FileNotFoundError``) →
          ``{"resolved": False, "$blob": hash}`` (no crashea).
        - sin ``blob_store`` o ``resolve_blobs=False``: metadata + flag
          ``{"resolved": False, "$blob": hash}`` (no intenta leer).
        """
        content_hash = payload["$blob"]
        if not resolve_blobs or self.blob_store is None:
            return {"resolved": False, "$blob": content_hash}
        try:
            return self.blob_store.get(content_hash)
        except FileNotFoundError:
            return {"resolved": False, "$blob": content_hash}

    def _parse_partition_lines(self, text: str, resolve_blobs: bool = True) -> list:
        """Parsea las líneas de una partición en eventos dict.

        Aplica resolución de payloads ``$blob`` por línea (bajo demanda,
        ``_resolve_payload``). Con ``resolve_blobs=False`` los blobs solo
        se marcan con el flag ``resolved`` sin leer contenido (default
        para particiones precargadas — el detalle granular se carga a
        pedido con ``load_partition_by_id``, Art. V).
        """
        events = []
        for raw_line in text.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload")
            if isinstance(payload, dict) and "$blob" in payload:
                event["payload"] = self._resolve_payload(
                    payload, resolve_blobs=resolve_blobs
                )
            events.append(event)
        return events

    def _auto_purge(self):
        now = time.time()
        if now - self._last_purge_sweep < 60:
            return
        self._last_purge_sweep = now
        self._purge_lru()

    def _total_size(self) -> int:
        """F2 (M1) — suma de bytes de todas las partition files."""
        total = 0
        for fname in os.listdir(self.base_path):
            if not (fname.startswith("OCB_PARTITION_") and fname.endswith(".log")):
                continue
            path = os.path.join(self.base_path, fname)
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
        return total

    def _purge_lru(self):
        """F2 (M1) — LRU purge con 3 modos en serie (idea 5 operador):
        1. Size cap (``max_size_mb``): borra las más viejas hasta < cap.
           ``max_size_mb=0`` → disabled (no purge por size).
        2. Quantity cap (``max_partitions``): borra las más viejas hasta
           ``len(partitions) <= max_partitions``.
        3. Legacy mtime (si ``retention_days is not None``): borra las con
           mtime > cutoff.

        LIFO: las más viejas se desplazan cuando entra contenido nuevo.
        Sort por nombre = time_ns = cronológico.
        """
        base = self.base_path
        partitions = sorted(
            [f for f in os.listdir(base) if f.startswith("OCB_PARTITION_")],
            key=lambda x: x,
        )

        # 1. Tope por tamaño
        if self.max_size_mb > 0:
            size_cap_bytes = self.max_size_mb * 1024 * 1024
            while partitions:
                total_bytes = sum(
                    os.path.getsize(os.path.join(base, pf))
                    for pf in partitions
                    if os.path.exists(os.path.join(base, pf))
                )
                if total_bytes <= size_cap_bytes:
                    break
                oldest = partitions.pop(0)
                try:
                    os.remove(os.path.join(base, oldest))
                except OSError:
                    pass

        # 2. Tope de seguridad por cantidad
        while len(partitions) > self.max_partitions:
            oldest = partitions.pop(0)
            try:
                os.remove(os.path.join(base, oldest))
            except OSError:
                pass

        # 3. Legacy mtime (opcional)
        if self.retention_days is not None:
            cutoff = time.time() - self.retention_days * 86400
            for pf in partitions[:]:
                path = os.path.join(base, pf)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        partitions.remove(pf)
                except OSError:
                    pass

    def _count_active(self):
        if not os.path.exists(self._active_path):
            return 0
        with open(self._active_path) as f:
            return sum(1 for _ in f)

    def _rotate(self):
        if os.path.exists(self._active_path) and os.path.getsize(self._active_path) > 0:
            ns = time.time_ns()
            archive = os.path.join(self.base_path, f"OCB_PARTITION_{ns}.log")
            os.rename(self._active_path, archive)

    def flush_active_to_partition(self) -> bool:
        """Rota el ACTIVE a una PARTITION si tiene contenido.

        El harvester crea un OCB por corrida y nunca cierra sesión (FIX.2):
        sin este flush, el ACTIVE residual quedaría orphaneado por la
        siguiente instanciación (_init_workspace) antes de alcanzar
        threshold_events/partition_minutes. Retorna True si se rotó.
        """
        with self._lock:
            if not os.path.exists(self._active_path):
                return False
            if os.path.getsize(self._active_path) <= 0:
                return False
            self._rotate()
            return True

    def close_session(self, summary):
        with self._lock:
            if self._closed:
                return
            if not os.path.exists(self._active_path):
                return
            self._closed = True
            ts = int(time.time())
            session_file = f"OCB_SESSION_{ts}.log"
            os.rename(self._active_path, os.path.join(self.base_path, session_file))
            full_summary = {"timestamp": ts, "sedimentada": True, **summary}
            with open(self._summary_path, "w") as f:
                json.dump(full_summary, f)
                f.flush()
                os.fsync(f.fileno())
            with open(self._manifest_path, "w") as f:
                json.dump({"sedimentada": True, "session_file": session_file}, f)
                f.flush()
                os.fsync(f.fileno())

    def load_session_context(self):
        entries = os.listdir(self.base_path)

        # 1. Identificar artefactos
        has_summary = os.path.exists(self._summary_path)
        has_active = os.path.exists(self._active_path)
        # Partitions
        partition_files = sorted(
            [f for f in entries if f.startswith("OCB_PARTITION_") and f.endswith(".log")],
            key=lambda x: x,
        )
        # Orphans (todos — el has_orphan gobierna la detección de first_run;
        # el preload es selectivo: solo con contenido).
        orphan_files = sorted([f for f in entries if f.startswith("OCB_ORPHAN_")], key=lambda x: x)
        has_orphan = bool(orphan_files)
        recent_orphan = None
        for orphan in reversed(orphan_files):
            if os.path.getsize(os.path.join(self.base_path, orphan)) > 0:
                recent_orphan = orphan
                break

        # F1 — first run: sin evidencia alguna de sesión previa. Preserva la
        # semántica original: un orphan vacío (crash detection residual)
        # sigue siendo evidencia → NO es first_run → abrupt_close.
        # El chequeo de first_run debe considerar has_orphan (cualquier orphan,
        # vacío o no), separándolo del preload (que solo incluye orphans con
        # contenido). Un orphan vacío residual (crash detection) es evidencia
        # de una sesión previa → session_type ``abrupt_close`` (NO ``first_run``).
        if not has_active and not has_summary and not partition_files and not has_orphan:
            return {"session_type": "first_run", "summary": {}, "preloaded_partitions": [], "total_partitions": 0}

        # 2. Candidatos para preload (lo reciente: particiones + orphan
        # con contenido + ACTIVE). El detalle granular NO se resuelve por
        # defecto (Art. V — el detalle se carga a pedido con
        # ``load_partition_by_id``).
        candidates = []
        for pf in partition_files[-2:]:
            candidates.append({"id": pf, "type": "partition"})
        if recent_orphan:
            candidates.append({"id": recent_orphan, "type": "orphan"})
        if has_active and os.path.getsize(self._active_path) > 0:
            candidates.append({"id": "OCB_ACTIVE.log", "type": "active"})

        # 3. Read content, parse, get first timestamp (para orden cronológico).
        # Ordenar por filename mezclaría rangos (prefijos distintos entre
        # particiones/orphan/ACTIVE) → ordenar por timestamp de la primera
        # línea válida de cada entrada.
        preloaded_data = []
        for item in candidates:
            path = os.path.join(self.base_path, item["id"])
            with open(path) as f:
                content = f.read()
            lines = self._parse_partition_lines(content, resolve_blobs=False)
            first_ts = ""
            if lines and isinstance(lines[0].get("timestamp"), str):
                first_ts = lines[0].get("timestamp")
            preloaded_data.append({
                "id": item["id"],
                "content": content,
                "lines": lines,
                "first_ts": first_ts,
            })
        preloaded_data.sort(key=lambda x: x["first_ts"])

        preloaded = [
            {"id": item["id"], "content": item["content"], "lines": item["lines"]}
            for item in preloaded_data
        ]

        # 4. Summary y session type.
        summary = {}
        if has_summary:
            with open(self._summary_path) as f:
                summary = json.load(f)
        has_session = any(f.startswith("OCB_SESSION_") for f in entries)
        # F2 (M1) — gap residual 4: ``normal_close`` requiere evidencia de
        # cierre limpio (session file o active + particiones). Si hay
        # summary pero NO hay active, partitions ni session file → el
        # summary está huérfano → ``abrupt_close`` (no ``normal_close``
        # falso).
        if has_summary and (has_session or has_active or bool(partition_files)):
            session_type = "normal_close"
        else:
            session_type = "abrupt_close"

        return {
            "session_type": session_type,
            "summary": summary,
            "preloaded_partitions": preloaded,
            # R.4.0 — magnitud de la historia no cargada (el agente ve
            # cuántas particiones existen además de las preloaded). Art.
            # V: solo cuenta archivos, no resuelve contenido.
            "total_partitions": len(partition_files),
        }

    def load_older_partition(self, partition_id, distance=1):
        partitions = sorted(
            [f for f in os.listdir(self.base_path) if f.startswith("OCB_PARTITION_")],
            key=lambda x: x,
        )
        try:
            idx = partitions.index(partition_id)
        except ValueError:
            return ""
        target = idx - distance
        if target < 0 or distance >= self.max_rewind_partitions:
            return ""
        path = os.path.join(self.base_path, partitions[target])
        if not os.path.exists(path):
            return ""
        with open(path) as f:
            return f.read()

    def _purge_old_partitions(self):
        """F2 (M1) — legacy wrapper: delega en ``_purge_lru``. Mantiene
        compatibilidad con tests existentes que llaman
        ``_purge_old_partitions()`` directamente."""
        self._purge_lru()

    def purge(self, keep_last=None, older_than_days=None):
        with self._lock:
            partitions = sorted(
                [f for f in os.listdir(self.base_path) if f.startswith("OCB_PARTITION_")],
                key=lambda x: x,
            )
            if older_than_days is not None:
                cutoff = time.time() - older_than_days * 86400
                for pf in partitions:
                    path = os.path.join(self.base_path, pf)
                    try:
                        if os.path.getmtime(path) < cutoff:
                            os.remove(path)
                    except OSError:
                        pass
            elif keep_last is not None and keep_last < len(partitions):
                for pf in partitions[:-keep_last]:
                    path = os.path.join(self.base_path, pf)
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            else:
                for pf in partitions:
                    path = os.path.join(self.base_path, pf)
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    def load_context(self, include_metadata: bool = False):
        """F0 (M1) — contexto del OCB. Con ``include_metadata=True``
        devuelve además ``partition_metadata`` (lista de dicts via
        ``_partition_metadata``). Default False para no romper callers
        previos que solo querían los IDs."""
        partitions = []
        for f in os.listdir(self.base_path):
            if f.startswith("OCB_PARTITION_") and f.endswith(".log"):
                partitions.append(f)
        active_count = self._count_active()
        result = {
            "summary": {},
            "partition_ids": partitions,
            "count": len(partitions),
        }
        if include_metadata:
            result["partition_metadata"] = [
                self._partition_metadata(p) for p in partitions
            ]
        return result

    def _partition_metadata(self, partition_id: str) -> dict:
        """F0 (M1) — metadata de una partición leyendo primera+última
        línea. Costo: 2 reads. No resuelve blobs (metadata only).

        Devuelve dict con: ``id``, ``first_timestamp``, ``last_timestamp``,
        ``session_ids`` (set), ``sources`` (set), ``event_types`` (Counter),
        ``event_count``.
        """
        path = os.path.join(self.base_path, partition_id)
        session_ids = set()
        sources = set()
        event_types = Counter()
        first_timestamp = None
        last_timestamp = None
        event_count = 0
        if os.path.exists(path):
            with open(path) as f:
                lines = f.readlines()
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event_count += 1
                ts = ev.get("timestamp")
                if ts is not None:
                    if first_timestamp is None:
                        first_timestamp = ts
                    last_timestamp = ts
                et = ev.get("event_type")
                if et is not None:
                    event_types[et] += 1
                ctx = ev.get("ctx_id")
                if ctx is not None:
                    session_ids.add(ctx)
                src = ev.get("source")
                if src is not None:
                    sources.add(src)
        return {
            "id": partition_id,
            "first_timestamp": first_timestamp,
            "last_timestamp": last_timestamp,
            "session_ids": session_ids,
            "sources": sources,
            "event_types": event_types,
            "event_count": event_count,
        }

    def load_partition_by_id(self, partition_id, resolve_blobs: bool = True):
        """Carga una partición completa, resolviendo payloads ``$blob``
        bajo demanda (API de detalle granular, ajuste 6).

        F0 (M1) — kwarg ``resolve_blobs`` (default True — no rompe callers
        CLI). Con ``resolve_blobs=False`` los payloads ``$blob`` se marcan
        con flag ``resolved: False`` sin leer contenido (metadata only).

        Retorna lista de eventos dict; cada payload ``$blob`` se resuelve
        contra el BlobStore (fall-closed: ``resolved: False`` si el blob
        falta o no hay store). Partición inexistente → ``[]``.
        """
        path = os.path.join(self.base_path, partition_id)
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return self._parse_partition_lines(f.read(), resolve_blobs=resolve_blobs)

    def save_summary(self, summary_dict):
        with open(self._summary_path, "w") as f:
            json.dump(summary_dict, f)
            f.flush()
            os.fsync(f.fileno())
        with open(self._manifest_path, "w") as f:
            json.dump({"sedimentada": True}, f)
            f.flush()
            os.fsync(f.fileno())

    # ===================================================================
    # F0 (M1) — rebuild: backfill OCB desde ledger
    # ===================================================================

    @classmethod
    def rebuild(cls, ledger_path: str, batch_callback=None) -> int:
        """F0 (M1) — backfill del OCB desde el ledger.

        Modo fresh explícito:
        1. Instancia ``OCB.for_ledger(ledger_path)`` (corre
           ``_init_workspace`` que podría crear orphan).
        2. Borra ``OCB_ACTIVE.log``, ``OCB_PARTITION_*.log``,
           ``OCB_ORPHAN_*.log``, ``OCB_SESSION_*.log``, ``OCB_SUMMARY.json``,
           ``OCB_MANIFEST.json`` (preserva ``RESUME.md`` por cosmético).
        3. Lee TODOS los eventos del ledger via
           ``LedgerReader(ledger_path).read_all_entries(resolve_blobs=False)``
           — CRÍTICO: ``resolve_blobs=False`` preserva refs ``$blob``
           intactas; así ``_serialize_event`` las traspasa sin
           reexternalizar al BlobStore.
        4. Append con ``ocb.append(event)`` por cada uno. El OCB rota solo
           (200/partición).
        5. ``batch_callback(count)`` cada 1000 eventos.
        6. Verificación final (Art. IX): si ``total_lines !=
           eventos_leidos`` → ``raise RuntimeError("rebuild incompleto:
           leídos N, escritos M")``.

        Retorna total appendeado.
        """
        ocb = cls.for_ledger(ledger_path)
        base_path = ocb.base_path

        # 2. Modo fresh: borrar artefactos (preserva RESUME.md)
        artifacts_to_clear = [
            "OCB_ACTIVE.log", "OCB_SUMMARY.json", "OCB_MANIFEST.json",
        ]
        for fname in list(os.listdir(base_path)):
            if (
                fname.startswith("OCB_PARTITION_")
                or fname.startswith("OCB_ORPHAN_")
                or fname.startswith("OCB_SESSION_")
            ):
                artifacts_to_clear.append(fname)
        for fname in artifacts_to_clear:
            path = os.path.join(base_path, fname)
            try:
                os.remove(path)
            except OSError:
                pass

        # 3. Leer eventos del ledger (resolve_blobs=False — preserva $blob)
        from causadb._ledger_reader import LedgerReader
        reader = LedgerReader(ledger_path)

        count = 0
        written = 0
        for entry in reader.read_all_entries(resolve_blobs=False):
            event = CanonicalEvent.from_dict(entry["event"])
            try:
                ocb.append(event)
                written += 1
            except Exception:
                # F0 (M1) — Art. IX: si un append falla, contamos el
                # evento como leído pero no escrito. La verificación
                # final detecta la discrepancia y raise con diagnóstico.
                pass
            count += 1
            if batch_callback is not None and count % 1000 == 0:
                batch_callback(count)

        # 6. Verificación final (Art. IX — anti-teatro)
        #
        # FIX.OCB-FLUSH — se compara ``written`` (appends EXITOSOS) contra
        # ``count`` (eventos leídos), NO total_lines en disco. Durante un
        # rebuild largo (>60s, ej. 140K eventos) el LRU purge (_auto_purge)
        # recorta particiones viejas mientras el rebuild appenda: el OCB es
        # caché volátil (Art. V) y el ledger es la fuente de verdad (Art. I),
        # por lo que total_lines < count es comportamiento LEGÍTIMO, no una
        # falla de escritura. ``written`` es el único contador que detecta
        # appends que realmente fallaron.
        if written != count:
            raise RuntimeError(
                f"rebuild incompleto: leídos {count}, escritos {written}"
            )

        return count
