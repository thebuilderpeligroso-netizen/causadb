import json
import logging
import os
import time
from typing import Optional, List, Dict, Any
from causadb._blob_store import BlobStore, resolve_payload
from causadb._ledger_reader import LedgerReader
from causadb._ledger_writer import LedgerWriter


# BIT-CHR.35 P3 — Cap de outputs MCP query/replay (anti-gigantismo).
#
# ``DEFAULT_QUERY_LIMIT`` es el cap aplicado cuando el caller NO pasa
# ``limit`` explícito. Elegimos 1000 (extremo superior del rango
# 500-1000 del plan) para minimizar breaking surface en ledgers
# pequeños: la mayoría de queries filtradas devuelven < 1000 eventos
# y no se ven afectadas. Solo ``query()`` sin filtros sobre ledgers
# grandes (>1000 eventos) se trunca — que es exactamente el caso
# patológico que motivó esta fase (TOOL_CALLED = 21.799 eventos /
# 87MB resueltos).
#
# ``MAX_QUERY_LIMIT`` es el cap duro: un caller que pasa ``limit``
# explícito mayor que este valor se clampea (no error). Previene
# que un agente o cliente mal configurado pida "todo" vía limit=10**9.
DEFAULT_QUERY_LIMIT = 1000
MAX_QUERY_LIMIT = 1000


# Claves de payload que se conservan incluso con ``include_payloads=False``.
# Son ligeras y preservan trazabilidad (content_hash para harvest
# filesystem, $blob para blobs externalizados, path/ctx_id para
# identificación). El resto (content, result, arguments, reasoning,
# etc.) se descarta para cortar ~90% de bytes.
_PAYLOAD_TRACEABILITY_KEYS = frozenset({
    "path", "action", "content_hash", "$blob",
    "event_id", "parent_event_id", "ctx_id",
})


def _slim_payload(payload: Any) -> Any:
    """Reducir un payload a sus claves de trazabilidad.

    Si el payload es un dict con ``$blob``, se conserva tal cual (el
    marcador ``$blob`` ya es ligero y permite al caller resolver on-demand
    si lo desea). Si es un dict inline, se conservan solo las claves en
    ``_PAYLOAD_TRACEABILITY_KEYS``. Si no es dict, se devuelve ``{}``.
    """
    if not isinstance(payload, dict):
        return {}
    # $blob refs se conservan íntegras (ya son ligeras y son la forma
    # de trazabilidad para payloads externalizados).
    if "$blob" in payload:
        return {"$blob": payload["$blob"]}
    # Payload inline: conservar solo claves de trazabilidad.
    return {k: v for k, v in payload.items() if k in _PAYLOAD_TRACEABILITY_KEYS}


class LedgerIndex:
    def __init__(self, ledger_path: str, index_path: Optional[str] = None):
        if not ledger_path:
            raise ValueError("ledger_path is required")
        self.ledger_path = ledger_path
        self.index_path = index_path or (ledger_path + ".index.json")
        self.last_hash = None
        self.event_ids = {}
        self._by_event_type = {}
        self._by_ctx_id = {}
        self._by_parent_event_id = {}
        self._by_source = {}
        # BIT-CHR.99 Gap #1 — hint opcional seteado por query() cuando el
        # cap oculta eventos recientes. None cuando no aplica (caller
        # explícito, ledger < cap, o sin resultados). Los callers que no
        # lo inspeccionan (rest_api, mcp/server resource) lo ignoran sin
        # comportamiento visible — el reset on-entry en query() previene
        # stale state en LedgerIndex compartido.
        self.last_query_hint: Optional[Dict[str, Any]] = None
        # Caché del seq máximo del ledger (para detectar recientes ocultos).
        # Se computa en rebuild() o lazily en _maybe_set_recent_hint si el
        # index.json legacy no tiene el campo (back-compat).
        self._max_seq: Optional[int] = None
        self._load_index()

    def _load_index(self):
        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, "r") as f:
                    data = json.load(f)
                    self.event_ids = data.get("event_ids", {})
                    self._by_event_type = data.get("by_event_type", {})
                    self._by_ctx_id = data.get("by_ctx_id", {})
                    self._by_parent_event_id = data.get("by_parent_event_id", {})
                    self._by_source = data.get("by_source", {})
                    self.last_hash = data.get("last_hash")
                    # BIT-CHR.99 Gap #1 — back-compat: index.json legacy
                    # sin el campo max_seq → None (se computa lazily en
                    # _maybe_set_recent_hint). index.json nuevos (post-
                    # rebuild) lo incluyen.
                    self._max_seq = data.get("max_seq")
            except (json.JSONDecodeError, KeyError):
                pass

    def _validate_cache(self):
        last_hash_path = self.ledger_path + ".last_hash.json"
        if os.path.exists(last_hash_path):
            try:
                with open(last_hash_path) as f:
                    data = json.load(f)
                actual = data.get("last_hash")
                if actual is None:
                    logging.warning(
                        "%s exists but has no last_hash key; skipping cache validation",
                        last_hash_path,
                    )
                elif actual != self.last_hash:
                    self.rebuild()
            except (json.JSONDecodeError, OSError) as exc:
                logging.debug(
                    "Cache validation failed for %s: %s; using cache as-is",
                    last_hash_path, exc,
                )

    def rebuild(self):
        self.event_ids = {}
        self._by_event_type = {}
        self._by_ctx_id = {}
        self._by_parent_event_id = {}
        self._by_source = {}
        if not os.path.exists(self.ledger_path):
            self.last_hash = "GENESIS"
            return

        last_hash = "GENESIS"
        with open(self.ledger_path, "r") as f:
            while True:
                pos = f.tell()
                line = f.readline()
                if not line:
                    break
                try:
                    entry = json.loads(line.strip())
                    event = entry["event"]
                    eid = event["event_id"]
                    seq = event.get("sequence_number", 0)
                    self.event_ids[eid] = (pos, seq)

                    etype = event.get("event_type")
                    if etype:
                        self._by_event_type.setdefault(etype, []).append(seq)

                    ctx = event.get("ctx_id")
                    if ctx:
                        self._by_ctx_id.setdefault(ctx, []).append(seq)

                    parent = event.get("parent_event_id")
                    if parent:
                        self._by_parent_event_id.setdefault(parent, []).append(seq)

                    src = event.get("source")
                    if src:
                        self._by_source.setdefault(src, []).append(seq)

                    last_hash = entry["hash"]
                except (json.JSONDecodeError, KeyError):
                    continue
        
        self.last_hash = last_hash
        # BIT-CHR.99 Gap #1 — cachear el seq máximo del ledger para
        # detectar recientes ocultos en query() sin re-escanear.
        self._max_seq = max((v[1] for v in self.event_ids.values()), default=None)
        
        with open(self.index_path, "w") as f:
            json.dump({
                "event_ids": self.event_ids,
                "by_event_type": self._by_event_type,
                "by_ctx_id": self._by_ctx_id,
                "by_parent_event_id": self._by_parent_event_id,
                "by_source": self._by_source,
                "last_hash": self.last_hash,
                "max_seq": self._max_seq,
            }, f)
            f.flush()
            os.fsync(f.fileno())

    def get_offset(self, event_id: str) -> Optional[int]:
        self._validate_cache()
        if not self.event_ids:
            self.rebuild()
        
        if event_id not in self.event_ids:
            self.rebuild()
            
        val = self.event_ids.get(event_id)
        if val is not None:
            return val[0]
        return None

    def query(
        self,
        event_type: Optional[str] = None,
        ctx_id: Optional[str] = None,
        parent_event_id: Optional[str] = None,
        source: Optional[str] = None,
        from_time: Optional[str] = None,
        to_time: Optional[str] = None,
        text: Optional[str] = None,
        limit: Optional[int] = None,
        include_payloads: bool = True,
        intent_only: bool = True,
        include_excerpts: bool = False,
    ) -> List[Dict[str, Any]]:
        """Query ledger entries by filters (AND-combined).

        Args:
            event_type: filter by event type.
            ctx_id: filter by context ID.
            parent_event_id: filter by parent event ID.
            source: filter by source string.
            from_time: ISO 8601 inclusive lower bound.
            to_time: ISO 8601 inclusive upper bound.
            text: case-insensitive substring search in payload.
            limit: máximo número de entradas a devolver. Si es ``None``
                se aplica ``DEFAULT_QUERY_LIMIT`` (cap anti-gigantismo).
                Si es mayor que ``MAX_QUERY_LIMIT`` se clampea a ese
                valor (no error). El cap se aplica ANTES de resolver
                blobs — los eventos truncados no tocan disco de blobs.
            include_payloads: si ``True`` (default) los payloads se
                resuelven completos (incluye ``$blob`` → contenido en
                disco). Si ``False``, los payloads se reducen a sus
                claves de trazabilidad (``content_hash``, ``$blob``,
                ``path``, etc.) sin resolver blobs — corta ~90% de bytes.

        Returns:
            Lista de entradas del ledger (event + hash + prev_hash),
            ordenada por ``sequence_number``, truncada a ``limit``.
        """
        # BIT-CHR.99 Gap #1 — reset on-entry: previene stale hint en
        # LedgerIndex compartido (ej: MCP server que reusa el mismo
        # index para múltiples queries). El hint se setea solo si esta
        # call específica oculta recientes.
        self.last_query_hint = None
        # Resolver el limit efectivo: None → default; > MAX → clamp.
        if limit is None:
            effective_limit = DEFAULT_QUERY_LIMIT
        else:
            effective_limit = min(limit, MAX_QUERY_LIMIT)
            if effective_limit < 0:
                effective_limit = 0

        # If text/time filters are requested, delegate to query_engine
        # (the index doesn't index timestamps or text payloads).
        if text is not None or from_time is not None or to_time is not None:
            from causadb._query_engine import query_events, _extract_excerpt
            results = query_events(
                self.ledger_path,
                event_type=event_type,
                ctx_id=ctx_id,
                parent_event_id=parent_event_id,
                source=source,
                from_time=from_time,
                to_time=to_time,
                text=text,
                intent_only=intent_only,
                include_excerpts=include_excerpts,
            )
            # query_events returns event dicts; LedgerIndex.query returns
            # full ledger entries (event + hash + prev_hash). Re-read the
            # ledger to build full entries for matching event_ids.
            # BIT-CHR.114 — correlacionar por sequence_number (único), NO por
            # event_id: el ledger real tiene event_ids duplicados (el mismo
            # id en miles de seq). Correlacionar por event_id traería TODAS
            # las ocurrencias y el cap truncaría las MÁS VIEJAS (fuera del
            # rango temporal pedido).
            matching_seqs = {e.get("sequence_number") for e in results}
            excerpts = {
                e["event_id"]: e.get("excerpt")
                for e in results if e.get("excerpt") is not None
            }
            full_entries = []
            with open(self.ledger_path, "r") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line.strip())
                        if entry.get("event", {}).get("sequence_number") in matching_seqs:
                            full_entries.append(entry)
                    except json.JSONDecodeError:
                        continue
            full_entries.sort(
                key=lambda x: x.get("event", {}).get("sequence_number", 0)
            )
            # BIT-CHR.35 P3: aplicar cap ANTES de resolver blobs.
            full_entries = full_entries[:effective_limit]
            if include_payloads:
                blob_dir = os.path.join(os.path.dirname(self.ledger_path), "blobs")
                for entry in full_entries:
                    event = entry.get("event")
                    if isinstance(event, dict):
                        event["payload"] = resolve_payload(event.get("payload", {}), BlobStore(blob_dir))
            else:
                for entry in full_entries:
                    event = entry.get("event")
                    if isinstance(event, dict):
                        event["payload"] = _slim_payload(event.get("payload", {}))
            # Q.2 — heredar el excerpt calculado por query_events (que re-leer el
            # ledger desde raw lines lo descartaría). Si no vino (excerpts off),
            # recomputar sobre el payload resuelto para el path MCP.
            for entry in full_entries:
                event = entry.get("event")
                if not isinstance(event, dict):
                    continue
                eid = event.get("event_id")
                if eid in excerpts and excerpts[eid] is not None:
                    event["excerpt"] = excerpts[eid]
                elif include_excerpts and text is not None:
                    payload = event.get("payload", {})
                    try:
                        orig = json.dumps(payload, sort_keys=True)
                        event["excerpt"] = _extract_excerpt(
                            orig, text.lower(), orig.lower()
                        )
                    except Exception:
                        pass
            # BIT-CHR.99 Gap #1 — notificar si el cap oculta recientes.
            # filters_used=True aquí (text/from_time/to_time activaron el
            # branch delegado), así que el helper es no-op — pero lo
            # llamamos por simetría y para que el reset on-entry sea el
            # único lugar que toca last_query_hint.
            self._maybe_set_recent_hint(
                full_entries, effective_limit,
                filters_used=(
                    text is not None
                    or from_time is not None
                    or to_time is not None
                    or limit is not None
                ),
            )
            return full_entries

        self._validate_cache()
        if not self._by_event_type:
            self.rebuild()
        
        result_seqs = None
        
        if event_type is not None:
            seqs = set(self._by_event_type.get(event_type, []))
            result_seqs = seqs if result_seqs is None else result_seqs & seqs
        
        if ctx_id is not None:
            seqs = set(self._by_ctx_id.get(ctx_id, []))
            result_seqs = seqs if result_seqs is None else result_seqs & seqs
        
        if parent_event_id is not None:
            seqs = set(self._by_parent_event_id.get(parent_event_id, []))
            result_seqs = seqs if result_seqs is None else result_seqs & seqs
        
        if source is not None:
            seqs = set(self._by_source.get(source, []))
            result_seqs = seqs if result_seqs is None else result_seqs & seqs
        
        if result_seqs is None:
            result_seqs = {v[1] for v in self.event_ids.values()}
            if not result_seqs:
                return []
        
        # Re-read ledger to get event entries for matching sequence numbers
        seq_to_eid = {v[1]: k for k, v in self.event_ids.items()}
        results = []
        with open(self.ledger_path, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line.strip())
                    seq = entry.get("event", {}).get("sequence_number", 0)
                    if seq in result_seqs:
                        results.append(entry)
                except json.JSONDecodeError:
                    continue
        
        # Sort by sequence_number to maintain order
        results.sort(key=lambda r: r.get("event", {}).get("sequence_number", 0))
        # BIT-CHR.35 P3: aplicar cap ANTES de resolver blobs. Esto es
        # crítico para el ahorro de memoria — si resolviéramos blobs
        # primero y truncáramos después, el pico de memoria sería el
        # mismo que sin cap (los blobs resueltos ya están en RAM).
        results = results[:effective_limit]
        if include_payloads:
            # The fast path reads raw lines (json.loads) not via LedgerReader —
            # $blob entries must still be resolved here (preserves ledger-level tests
            # that assert payload.reasoning through this code path).
            blob_dir = os.path.join(os.path.dirname(self.ledger_path), "blobs")
            for entry in results:
                event = entry.get("event")
                if isinstance(event, dict):
                    event["payload"] = resolve_payload(event.get("payload", {}), BlobStore(blob_dir))
        else:
            # Sin resolución de blobs: el payload se reduce a claves de
            # trazabilidad. Los ``$blob`` refs se conservan como marcador
            # (ligero) — el caller puede resolver on-demand si lo necesita.
            for entry in results:
                event = entry.get("event")
                if isinstance(event, dict):
                    event["payload"] = _slim_payload(event.get("payload", {}))
        # BIT-CHR.99 Gap #1 — notificar si el cap oculta recientes.
        # Aquí filters_used incluye text/from_time/to_time (este branch
        # es el indexado, sin esos filtros) Y limit explícito — el
        # caller que pide limit=N sabe que está truncando, no necesita
        # que le avisemos. event_type/ctx_id/parent/source NO cuentan
        # como "filtro de recencia" — el caller puede pedir
        # event_type=FILE_MODIFIED sin saber que hay recientes ocultos.
        self._maybe_set_recent_hint(
            results, effective_limit,
            filters_used=(limit is not None),
        )
        return results

    def _maybe_set_recent_hint(
        self,
        results: List[Dict[str, Any]],
        effective_limit: int,
        filters_used: bool,
    ) -> None:
        """Setear ``self.last_query_hint`` cuando el cap oculta recientes.

        BIT-CHR.99 Gap #1 — la tool ``causadb_query`` sin
        ``from_time``/``to_time``/``text`` devuelve eventos del Génesis
        (seq bajos) en vez de los últimos. En un ledger de 158k eventos
        esto engaña a operadores/agentes. Este helper detecta el caso
        (cap alcanzado Y hay eventos con seq mayor al último devuelto)
        y setea un hint que los callers (MCP, CLI) pueden propagar.

        No setea el hint cuando:
        - ``filters_used``: caller explícito (from_time/to_time/text) ya
          sabe qué quiere, no notificamos.
        - ``not results``: sin resultados, nada que notificar.
        - ``len(results) < effective_limit``: no se alcanzó el cap →
          no hay recientes ocultos.
        - ``self._max_seq is None``: ledger vacío o index corrupto.

        Anti-abstracción (Art. VIII): helper private al módulo (prefijo
        ``_``), no exportado — es un refactor interno de ``query()``,
        no una nueva abstracción con 0 o 1 implementaciones concretas.
        """
        if filters_used:
            return
        if not results:
            return
        if len(results) < effective_limit:
            return
        # Cachear _max_seq si no fue computed (index.json legacy sin el
        # campo — back-compat con índices pre-BIT-CHR.99).
        if getattr(self, "_max_seq", None) is None:
            self._max_seq = max(
                (v[1] for v in self.event_ids.values()), default=None
            )
        if self._max_seq is None:
            return
        last_seq_returned = (
            results[-1].get("event", {}).get("sequence_number", 0)
            if results else 0
        )
        if self._max_seq > last_seq_returned:
            self.last_query_hint = {
                "hint": "result_capped_recents_hidden",
                "max_seq_in_ledger": self._max_seq,
                "last_seq_returned": last_seq_returned,
                "use_from_time_to_get_recent": True,
            }
