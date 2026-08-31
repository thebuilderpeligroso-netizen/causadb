"""F.12.4 — Causal cone (downstream / impact).

``trace_downstream(event_id, ledger_path)`` walks FORWARD from the source
event to HEAD and returns the list of events that depend on it
transitively — the "blast radius" of reverting the source event.

Algorithm (from Causari ``impact.rs``, reimplemented in Python):

1. Walk forward from the source event to HEAD.
2. For each subsequent event: compute ``effective_writes(event)`` (the
   set of file paths the event wrote, from its payload).
3. If any file written by the source event is read by a subsequent
   event → that downstream event is "tainted".
4. For every tainted event: its writes ALSO propagate (transitive taint).
5. Return the list of tainted events (each as a plain dict, in ledger
   order) with the dependency chain encoded by the ``tainted_by`` field
   (the set of event_ids whose writes caused this event's taint).

Payload contract (agent-declared file dependencies):
  - ``writes``: list[str] of file paths the event mutated.
  - ``reads``:  list[str] of file paths the event read.

NOTE: F.12.3 will later add ``trace_upstream`` to this same module.
The downstream helpers below are deliberately self-contained so the
upstream implementation can reuse the iteration primitives without
coupling to the downstream taint logic.

BIT-CHR.117 — DAG cache activado en producción:
  - ``_load_or_build_dag`` (antes ``_load_dag_if_fresh``) ahora puede
    ESCRIBIR el cache: build completo (cold), rebuild (truncado/
    archives cambiados) o tail-read incremental (eventos nuevos).
  - Con el cache fresh, ``trace_upstream`` y ``trace_downstream``
    operan SIN materializar el ledger (schema v3: ``event_reads``,
    ``event_meta``, ``event_ids``, ``event_writes``).
  - Normalización de paths (BIT-CHR.110 TD-#3d): ``causadb/causadb/X``
    → ``causadb/X`` en todos los puntos de recolección.
"""
import json
import os
from bisect import bisect_left
from collections import deque
from typing import Dict, Iterator, List, Optional, Set, Tuple

from causadb._file_index import _normalize_rel_path
from causadb._ledger_reader import LedgerReader


# ---------------------------------------------------------------------------
# Iteration primitives (shared with F.12.3 trace_upstream)
# ---------------------------------------------------------------------------

def _iter_events_from(ledger_path: str) -> Iterator[dict]:
    """Yield every event in the ledger as a plain dict (ledger order).

    Uses ``LedgerReader.read_all_entries`` so archives (gz) are included
    transparently. Each yielded dict is the raw ledger entry's ``event``
    field (a ``CanonicalEvent.to_dict()`` payload) — NOT a
    ``CanonicalEvent`` instance, so callers can mutate / index it
    cheaply without rebuilding the frozen dataclass.

    BIT-CHR.117: lee en modo ``tolerant`` — una línea final truncada
    (crash a mitad de escritura) se saltea en vez de propagar el error.
    Esto permite que el rebuild del DAG cache funcione sobre un ledger
    truncado (el escenario exacto del guard de truncamiento por tamaño).
    """
    reader = LedgerReader(ledger_path)
    for entry in reader.read_all_entries(tolerant=True):
        ev = entry.get("event")
        if ev is None:
            continue
        yield ev


def _effective_writes_for(events: List[dict], event_id: str) -> Set[str]:
    """Return the set of file paths written by the event with ``event_id``.

    Looks up the event in *events* (a list of dicts already materialised
    from the ledger) and reads its ``payload.writes`` list. Returns an
    empty set if the event is not found or has no ``writes`` key.

    BIT-CHR.117: normaliza los paths (``causadb/causadb/X`` →
    ``causadb/X``) para consistencia con el DAG cache.
    """
    for ev in events:
        if ev.get("event_id") == event_id:
            payload = ev.get("payload") or {}
            writes = payload.get("writes") or []
            return {_normalize_rel_path(w) for w in writes}
    return set()


def _effective_reads(ev: dict) -> Set[str]:
    """Return the set of file paths read by a single event dict.

    BIT-CHR.117: normaliza los paths (``causadb/causadb/X`` →
    ``causadb/X``) para consistencia con el DAG cache.
    """
    payload = ev.get("payload") or {}
    reads = payload.get("reads") or []
    return {_normalize_rel_path(r) for r in reads}


def _effective_writes(ev: dict) -> Set[str]:
    """Return the set of file paths written by a single event dict.

    BIT-CHR.117: normaliza los paths (``causadb/causadb/X`` →
    ``causadb/X``) para consistencia con el DAG cache.
    """
    payload = ev.get("payload") or {}
    writes = payload.get("writes") or []
    return {_normalize_rel_path(w) for w in writes}


# ---------------------------------------------------------------------------
# F.13.1.5 — DAG cache integration helpers
# ---------------------------------------------------------------------------
#
# ``trace_upstream`` y ``trace_downstream`` usan el DAG cache
# (``dag.json``) para evitar materializar el ledger y rebuild
# ``writer_history`` cada vez. El cache se valida con ``read_dag`` (que
# verifica hash + schema) y ``is_dag_stale`` (que compara ``last_seq``
# con el último ``sequence_number`` del ledger + guard de truncamiento
# por tamaño + comparación de ``last_hash``).
#
# BIT-CHR.117 — ``_load_or_build_dag`` (antes ``_load_dag_if_fresh``)
# ahora puede ESCRIBIR el cache: si el cache no existe, está corrupto,
# truncado, o los archives cambiaron → build completo; si está stale
# por eventos nuevos → tail-read incremental + ``update_dag``. El
# resultado se persiste con ``write_dag`` para que la PRÓXIMA llamada
# sea un cache HIT puro (sin materializar el ledger).
#
# Umbral configurable: ``CausaDBConfig.dag_cache_min_events`` (default
# 100). Por debajo de ese umbral, no vale la pena usar el cache (el
# overhead de leer + validar el JSON supera al savings de saltear el
# rebuild). En ese caso se build on-the-fly como antes.
#
# Degradación suave (artículo V): si el cache está stale, corrupto, o
# no se puede leer, se cae al path on-the-fly original. Nunca se
# propaga un cache potencialmente inconsistente como si fuera válido.

def _dag_path_for(ledger_path: str) -> str:
    """Retorna el path del DAG cache asociado a *ledger_path*.

    BIT-CHR.117: el DAG vive en el MISMO directorio que el ledger
    (``dag.json``), consistente con ``file_index.json`` (BIT-CHR.115).
    La convención vieja ``.causadb/dag.json`` producía un dir anidado
    inexistente (``.causadb/.causadb/dag.json``) cuando el ledger ya
    vive en ``.causadb/`` — el cache NUNCA se escribía.
    """
    return os.path.join(os.path.dirname(os.path.abspath(ledger_path)), "dag.json")


def _current_archives(ledger_path: str) -> List[str]:
    """Lista sorted de archivos ``.gz`` en ``<ledger_dir>/archive/`` (o []).

    Se compara contra ``dag["covered_archives"]`` para detectar cambios
    de archives (un archive nuevo invalida el cache — los eventos
    archivados ya no están en el ledger activo).
    """
    archive_dir = os.path.join(os.path.dirname(ledger_path), "archive")
    if not os.path.isdir(archive_dir):
        return []
    try:
        return sorted(f for f in os.listdir(archive_dir) if f.endswith(".gz"))
    except OSError:
        return []


def _read_last_hash_file(ledger_path: str) -> str:
    """Lee ``<ledger>.last_hash.json`` → str, o "" si no existe/ilegible."""
    last_hash_path = ledger_path + ".last_hash.json"
    if not os.path.exists(last_hash_path):
        return ""
    try:
        with open(last_hash_path) as f:
            return json.load(f).get("last_hash", "")
    except (json.JSONDecodeError, OSError, KeyError):
        return ""


def _last_complete_line_end(ledger_path: str) -> int:
    """Byte offset justo después de la última línea JSON completa del ledger.

    Se usa para setear ``dag["last_offset"]`` tras un build completo:
    el tail-read incremental de la próxima llamada arranca desde acá.
    Una línea final truncada (crash) se ignora — el writer la reescribe.

    Algoritmo: leer el último bloque (~8192 bytes); si ninguna línea
    parsea, ampliar hacia atrás (caso extremo: todo el archivo).

    Returns:
        Offset (int) después de la última línea válida, o 0 si el
        ledger no existe / está vacío / no tiene líneas válidas.
    """
    if not os.path.exists(ledger_path):
        return 0
    try:
        size = os.path.getsize(ledger_path)
    except OSError:
        return 0
    if size == 0:
        return 0

    block = 8192
    while True:
        start = max(0, size - block)
        try:
            with open(ledger_path, "rb") as f:
                f.seek(start)
                data = f.read(size - start)
        except OSError:
            return 0
        text = data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        # Caminar las líneas trackeando offsets de bytes.
        pos = start
        last_valid_end = None
        for line in lines:
            stripped = line.strip()
            if stripped:
                try:
                    json.loads(stripped)
                    last_valid_end = pos + len(line.encode("utf-8", errors="replace"))
                except (json.JSONDecodeError, ValueError):
                    pass
            pos += len(line.encode("utf-8", errors="replace")) + 1
        if last_valid_end is not None:
            return last_valid_end
        if start == 0:
            return 0
        block *= 2


def _read_tail_events(
    ledger_path: str, offset: int
) -> Optional[Tuple[List[dict], int]]:
    """Lee eventos nuevos desde *offset* hasta el final del ledger.

    Helper del tail-read incremental (BIT-CHR.117): lee líneas desde un
    byte offset dado, parsea cada línea y resuelve los payloads de esas
    líneas (BlobStore del dir ``<dirname>/blobs``).

    Args:
        ledger_path: path absoluto del ledger.
        offset: byte offset desde donde leer (``dag["last_offset"]``).

    Returns:
        ``(new_events, new_offset)`` donde ``new_events`` es la lista de
        dicts de eventos (payloads resueltos) y ``new_offset`` es la
        posición post-última-línea-parseada. O ``None`` si la PRIMERA
        línea en *offset* no parsea (corrupción → el caller debe rebuild
        completo). Si una línea NO-primera no parsea → cortar en la
        última buena y devolver lo leído hasta ahí.
    """
    if not os.path.exists(ledger_path):
        return [], offset
    try:
        size = os.path.getsize(ledger_path)
    except OSError:
        return [], offset
    if offset >= size:
        return [], offset

    from causadb._blob_store import BlobStore, resolve_payload

    blob_store = BlobStore(os.path.join(os.path.dirname(ledger_path), "blobs"))
    new_events: List[dict] = []
    current_offset = offset
    first = True
    try:
        with open(ledger_path, "r") as f:
            f.seek(offset)
            while True:
                line_start = f.tell()
                line = f.readline()
                if not line:
                    break
                line_end = f.tell()
                stripped = line.strip()
                if not stripped:
                    current_offset = line_end
                    continue
                try:
                    entry = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    if first:
                        # Primera línea corrupta → el caller debe rebuild.
                        return None
                    # Línea posterior corrupta → cortar en la última buena.
                    break
                ev = entry.get("event")
                if ev is not None:
                    ev["payload"] = resolve_payload(ev.get("payload") or {}, blob_store)
                    new_events.append(ev)
                current_offset = line_end
                first = False
    except OSError:
        return None
    return new_events, current_offset


def _load_or_build_dag(ledger_path: str, min_events: int) -> Optional[dict]:
    """Carga el DAG cache si está fresh; si no, lo construye/actualiza y lo ESCRIBE.

    BIT-CHR.117 — renombrado desde ``_load_dag_if_fresh``: ahora puede
    ESCRIBIR el cache (antes solo leía). Flujo:

    1. ``ledger_last_seq = get_last_ledger_seq(ledger_path)``; si
       ``ledger_last_seq + 1 < min_events`` → return None (umbral).
    2. ``dag_path = _dag_path_for(ledger_path)``; ``dag = read_dag(dag_path)``.
    3. Si ``dag`` no es None, ``not is_dag_stale(dag, ledger_path)`` y
       archives actuales == ``dag["covered_archives"]`` → return dag (fresh).
    4. Sino, TRES ramas:
       - ``dag is None`` (cold/corrupto) → build completo:
         ``events = list(_iter_events_from(ledger_path))``;
         ``dag = build_dag(events)``.
       - ``dag`` existe pero ``os.path.getsize(ledger_path) <
         dag.get("last_offset", 0)`` o archives cambiaron → REBUILD
         completo (igual que arriba).
       - ``dag`` existe, size >= last_offset, pero stale (eventos
         nuevos) → tail-read: leer líneas nuevas desde
         ``dag["last_offset"]`` con ``resolve_blobs=True``. Primera
         línea nueva corrupta → rebuild completo. Línea posterior
         corrupta → parar ahí sin avanzar. Si ``new_events`` vacío y
         sigue stale → rebuild. ``dag = update_dag(dag, new_events)``.
    5. Después del build o update: setear ``dag["last_offset"]`` =
       posición post-última-línea-parseada (NO ``os.path.getsize()`` —
       carrera con append concurrente); ``dag["last_hash"]`` = leer
       ``ledger.log.last_hash.json`` (o "" si no existe);
       ``dag["covered_archives"]`` = sorted list de
       ``<ledger_dir>/archive/`` o [].
    6. Verificación post-update: si ``get_last_ledger_seq(ledger_path) >
       dag["last_seq"]`` → rebuild completo (retry acotado, una sola vez).
    7. ``write_dag(dag, dag_path)``; envolver TODO (build/read/write) en
       try/except → degradación suave return None si falla (no propagar
       errores al trace).

    Args:
        ledger_path: path absoluto del ledger.
        min_events: umbral mínimo de eventos para justificar el cache.

    Returns:
        El dict DAG fresh (leído o recién construido/actualizado), o
        ``None`` (degradación suave — el caller cae al path on-the-fly).
    """
    # Import diferido para evitar import circular:
    # _dag_cache importa _causal_cone (para _writer_history y
    # _effective_writes), así que no podemos importarlo al top-level
    # del módulo — lo hacemos aquí, en el primer uso.
    from causadb._dag_cache import (
        build_dag,
        get_last_ledger_seq,
        is_dag_stale,
        read_dag,
        update_dag,
        write_dag,
    )

    try:
        # 1. Umbral: si el ledger tiene menos eventos que el mínimo, no
        # vale la pena el cache. Usamos last_seq como proxy del número
        # de eventos (en una cadena lineal con seqs 0-indexed,
        # last_seq == número de eventos - 1).
        ledger_last_seq = get_last_ledger_seq(ledger_path)
        if ledger_last_seq + 1 < min_events:
            return None

        dag_path = _dag_path_for(ledger_path)
        dag = read_dag(dag_path)

        # 3. Cache fresh → usarlo tal cual.
        if (
            dag is not None
            and not is_dag_stale(dag, ledger_path)
            and _current_archives(ledger_path) == (dag.get("covered_archives") or [])
        ):
            return dag

        # 4. Cache no usable → construir/actualizar.
        if dag is None:
            # Rama A: cold/corrupto → build completo.
            events = list(_iter_events_from(ledger_path))
            dag = build_dag(events)
        elif (
            os.path.getsize(ledger_path) < int(dag.get("last_offset") or 0)
            or _current_archives(ledger_path) != (dag.get("covered_archives") or [])
        ):
            # Rama B: ledger truncado o archives cambiaron → rebuild completo.
            events = list(_iter_events_from(ledger_path))
            dag = build_dag(events)
        else:
            # Rama C: stale por eventos nuevos → tail-read incremental.
            tail = _read_tail_events(ledger_path, int(dag.get("last_offset") or 0))
            if tail is None:
                # Primera línea nueva corrupta → rebuild completo.
                events = list(_iter_events_from(ledger_path))
                dag = build_dag(events)
            else:
                new_events, new_offset = tail
                if not new_events:
                    # Sigue stale pero no hay eventos nuevos parseables →
                    # rebuild (el ledger cambió de contenido sin crecer).
                    events = list(_iter_events_from(ledger_path))
                    dag = build_dag(events)
                else:
                    dag = update_dag(dag, new_events)
                    dag["last_offset"] = new_offset

        # 5. Setear metadata real (offset post-última-línea, last_hash,
        # covered_archives). Para el caso update, last_offset ya vino del
        # tail-read; para el caso build, lo computamos acá.
        if dag.get("last_offset") is None or dag.get("last_offset") == 0:
            dag["last_offset"] = _last_complete_line_end(ledger_path)
        dag["last_hash"] = _read_last_hash_file(ledger_path)
        dag["covered_archives"] = _current_archives(ledger_path)

        # 6. Verificación post-update: si el ledger tiene más eventos que
        # el DAG cubre (append concurrente durante el build/update) →
        # rebuild completo (retry acotado, una sola vez).
        if get_last_ledger_seq(ledger_path) > int(dag.get("last_seq") or 0):
            events = list(_iter_events_from(ledger_path))
            dag = build_dag(events)
            dag["last_offset"] = _last_complete_line_end(ledger_path)
            dag["last_hash"] = _read_last_hash_file(ledger_path)
            dag["covered_archives"] = _current_archives(ledger_path)

        # 7. Persistir el cache para la próxima llamada.
        write_dag(dag, dag_path)
        return dag
    except Exception:
        # Degradación suave: no propagar errores al trace. El caller cae
        # al path on-the-fly (materialización completa).
        return None


def _dag_history_to_cone(
    dag: dict,
) -> Tuple[Dict[str, List[Tuple[str, int]]], Dict[str, List[int]]]:
    """Convierte ``writer_history``/``history_positions`` del DAG (listas
    JSON-nativas) a las tuplas que usa el BFS de ``trace_upstream``.

    El DAG cache almacena ``writer_history`` como
    ``{file: [[event_id, chain_position], ...]}`` (listas, porque JSON
    no preserva tuplas — contrato F.13.1.1). El BFS de ``trace_upstream``
    usa tuplas ``(event_id, chain_position)`` internamente. Esta función
    hace la conversión en el boundary cache→cone.

    Args:
        dag: el dict DAG cache (de ``read_dag``).

    Returns:
        ``(history, history_positions)`` donde:
          - ``history``: ``Dict[str, List[Tuple[str, int]]]`` (tuplas).
          - ``history_positions``: ``Dict[str, List[int]]`` (paralelo).
    """
    wh = dag.get("writer_history") or {}
    hp = dag.get("history_positions") or {}

    history: Dict[str, List[Tuple[str, int]]] = {}
    for file_path, entries in wh.items():
        # Convertir listas [event_id, chain_position] → tuplas.
        history[file_path] = [(entry[0], entry[1]) for entry in entries]

    # history_positions ya es list[int] en el cache, copiar defensivamente.
    positions: Dict[str, List[int]] = {
        file_path: list(positions_list)
        for file_path, positions_list in hp.items()
    }
    return history, positions


def _dag_position_of(dag: dict) -> Dict[str, int]:
    """Reconstruye ``position_of`` (event_id → chain_position) del DAG cache.

    El DAG cache no almacena ``position_of`` directamente (sería
    redundante con ``writer_history``), pero lo almacena implícitamente:
    cada entry de ``writer_history[file]`` es ``[event_id, chain_position]``.
    Esta función itera todas las entries y construye el mapeo.

    Args:
        dag: el dict DAG cache (de ``read_dag``).

    Returns:
        ``Dict[str, int]`` mapeando event_id → chain_position.
    """
    position_of: Dict[str, int] = {}
    wh = dag.get("writer_history") or {}
    for entries in wh.values():
        for event_id, chain_position in entries:
            # Si un evento escribe múltiples files, aparece en múltiples
            # entries pero todas con la misma chain_position (un evento
            # tiene una sola posición en la cadena). No hay conflicto.
            position_of[event_id] = chain_position
    return position_of


# ---------------------------------------------------------------------------
# Downstream cone (F.12.4)
# ---------------------------------------------------------------------------

def trace_downstream(event_id: str, ledger_path: str) -> List[dict]:
    """Return the list of events transitively tainted by *event_id*.

    Walks forward from *event_id* to HEAD. An event is tainted iff it
    reads a file that was written by *event_id* OR by any previously
    tainted event (transitive closure). The source event itself is
    never included in the result.

    Each result entry is a dict with at least:
      - ``event_id``: str
      - ``event_type``: str
      - ``timestamp``: str
      - ``tainted_by``: list[str] — event_ids whose writes caused this
        event's taint (the immediate parents in the dependency chain).

    The result is in ledger order (chronological).

    Raises ``ValueError`` if *event_id* is not found in the ledger.

    BIT-CHR.117: con el DAG cache fresh, itera ``dag["event_ids"]`` y
    usa ``event_reads``/``event_writes``/``event_meta`` — NO materializa
    el ledger. El resultado es idéntico al path on-the-fly (degradación
    cuando el cache no está disponible).
    """
    from causadb._config import CausaDBConfig
    min_events = CausaDBConfig(ledger_path=ledger_path).dag_cache_min_events
    dag = _load_or_build_dag(ledger_path, min_events)

    if dag is not None:
        # --- Cache HIT: operar desde el DAG sin materializar el ledger. ---
        event_ids = dag.get("event_ids") or []
        try:
            source_idx = event_ids.index(event_id)
        except ValueError:
            raise ValueError(f"event_id not found in ledger: {event_id}")

        def _reads_of(eid: str) -> Set[str]:
            return set((dag.get("event_reads") or {}).get(eid) or [])

        def _writes_of(eid: str) -> Set[str]:
            return set((dag.get("event_writes") or {}).get(eid) or [])

        event_meta = dag.get("event_meta") or {}

        source_writes = _writes_of(event_id)
        propagated_writes: Set[str] = set(source_writes)

        tainted: List[dict] = []
        tainted_ids: Set[str] = set()

        for eid in event_ids[source_idx + 1:]:
            reads = _reads_of(eid)
            if not reads:
                continue
            # Which tainted-source writes does this event read?
            caused_by = reads & propagated_writes
            if not caused_by:
                continue
            # Taint this event.
            meta = event_meta.get(eid) or {}
            tainted_entry = {
                "event_id": eid,
                "event_type": meta.get("event_type"),
                "timestamp": meta.get("timestamp"),
                "tainted_by": sorted(
                    {tid for tid in tainted_ids
                     if _writes_of(tid) & reads}
                    | ({event_id} if source_writes & reads else set())
                ),
            }
            tainted.append(tainted_entry)
            tainted_ids.add(eid)
            # Propagate this event's writes for transitive taint.
            propagated_writes |= _writes_of(eid)

        return tainted

    # --- Degradación: path on-the-fly (materializar el ledger). ---
    events = list(_iter_events_from(ledger_path))

    # Locate the source event and its writes.
    source_writes = _effective_writes_for(events, event_id)
    source_index: Optional[int] = None
    for i, ev in enumerate(events):
        if ev.get("event_id") == event_id:
            source_index = i
            break
    if source_index is None:
        raise ValueError(f"event_id not found in ledger: {event_id}")

    # Propagated writes: starts as the source's writes; grows as events
    # are tainted (their writes also propagate).
    propagated_writes: Set[str] = set(source_writes)

    tainted: List[dict] = []
    tainted_ids: Set[str] = set()

    for ev in events[source_index + 1:]:
        reads = _effective_reads(ev)
        if not reads:
            continue
        # Which tainted-source writes does this event read?
        caused_by = reads & propagated_writes
        if not caused_by:
            continue
        # Taint this event.
        tainted_entry = {
            "event_id": ev.get("event_id"),
            "event_type": ev.get("event_type"),
            "timestamp": ev.get("timestamp"),
            "tainted_by": sorted(
                {tid for tid in tainted_ids
                 if _effective_writes_for(events, tid) & reads}
                | ({event_id} if source_writes & reads else set())
            ),
        }
        tainted.append(tainted_entry)
        tainted_ids.add(ev.get("event_id"))
        # Propagate this event's writes for transitive taint.
        propagated_writes |= _effective_writes(ev)

    return tainted


# ---------------------------------------------------------------------------
# Upstream cone (F.12.3 — trace_upstream)
# ---------------------------------------------------------------------------

def _writer_history(events: List[dict]) -> Dict[str, List[Tuple[str, int]]]:
    """Build ``file → [(event_id, chain_position)]`` sorted by position.

    *chain_position* is the INDEX OF THE EVENT IN LEDGER ORDER (0-based,
    as yielded by ``read_all``) — NOT a position in the
    ``parent_event_id`` chain (BIT-CHR.110 TD-#3a: the real ledger
    leaves parents unset on ~99.99% of events, so the parent chain
    collapses to HEAD and would lose every previous writer). Ledger
    order is a total order present for every event, with or without
    parent links, and matches ``sequence_number`` order.

    The returned lists are sorted ascending by position so they can be
    binary-searched with ``bisect`` to find the last writer strictly
    before a given position.

    Only events that actually WRITE a file (per ``payload.writes``) appear
    as entries; files that are only ever read are absent from the dict.

    BIT-CHR.117: los paths se normalizan vía ``_effective_writes``
    (``causadb/causadb/X`` → ``causadb/X``).
    """
    # chain_position = position in ledger order (enumerate). Every event
    # with an event_id gets a position — nobody is excluded (TD-#3a).
    position_of: Dict[str, int] = {}
    for pos, ev in enumerate(events):
        eid = ev.get("event_id")
        if eid is not None:
            position_of[eid] = pos

    history: Dict[str, List[Tuple[str, int]]] = {}
    for ev in events:
        eid = ev.get("event_id")
        if eid is None or eid not in position_of:
            continue
        for f in _effective_writes(ev):
            history.setdefault(f, []).append((eid, position_of[eid]))

    # Sort each file's list by position (ascending) for bisect.
    for f in history:
        history[f].sort(key=lambda pair: pair[1])
    return history


def trace_upstream(file_path: str, line_number: int, ledger_path: str) -> dict:
    """Return the upstream causal cone of line *line_number* of *file_path*.

    Finds the writer event W that introduced the line (via
    ``attribute_line`` from F.12.2), then BFS upstream: for each event E
    in the cone, compute ``effective_reads(E)`` (declared reads + own
    writes), and for each file F read by E, binary-search ``writer_history``
    for the last writer with chain_position STRICTLY LESS than E's. If
    found, add edge ``(F, prior_event_id)`` to E's upstream and enqueue
    the prior event if not already in the cone.

    Args:
        file_path: relative path of the file within the workspace snapshot.
        line_number: 1-based line number to trace upstream from.
        ledger_path: absolute path to the ledger file.

    Returns:
        A dict with:
          - ``writer_event``: dict (the introducer event's attribution
            fields) or ``None`` if the line was never introduced.
          - ``cone``: dict ``event_id → {"reason": str, "upstream":
            list[(file, prior_event_id)]}``.
          - ``visited``: set of event_ids in the cone (includes the writer).
          - ``depth``: int — the longest chain length from the writer to
            a root in the cone (0 if the writer has no upstream).

    Raises:
        ValueError: if *file_path* was never touched in the ledger
            (propagated from ``attribute_line``).

    BIT-CHR.117: con el DAG cache fresh, el BFS opera SIN materializar
    el ledger (``event_reads``/``event_writes`` del cache). El resultado
    es idéntico al path on-the-fly (degradación cuando el cache no está
    disponible).
    """
    from causadb._causal_attrib import attribute_line

    # 1. Find the writer event W that introduced the line.
    introducer = attribute_line(file_path, line_number, ledger_path)
    if introducer is None:
        return {
            "writer_event": None,
            "cone": {},
            "visited": set(),
            "depth": 0,
        }
    w_id = introducer["event_id"]

    from causadb._config import CausaDBConfig
    min_events = CausaDBConfig(ledger_path=ledger_path).dag_cache_min_events
    dag = _load_or_build_dag(ledger_path, min_events)

    if dag is not None:
        # --- Cache HIT: BFS desde el DAG sin materializar el ledger. ---
        # ``_reads_of``/``_writes_of`` reemplazan a ``by_id`` +
        # ``_effective_reads``/``_effective_writes``: los reads/writes
        # normalizados ya están en el cache (schema v3).
        history, history_positions = _dag_history_to_cone(dag)
        position_of = _dag_position_of(dag)

        def _reads_of(eid: str) -> Set[str]:
            return set((dag.get("event_reads") or {}).get(eid) or [])

        def _writes_of(eid: str) -> Set[str]:
            return set((dag.get("event_writes") or {}).get(eid) or [])

        cone: Dict[str, dict] = {
            w_id: {"reason": "writer", "upstream": []},
        }
        visited: Set[str] = {w_id}
        depth_of: Dict[str, int] = {w_id: 0}
        queue: deque = deque([w_id])

        while queue:
            e_id = queue.popleft()
            e_pos = position_of.get(e_id)
            if e_pos is None:
                # Event not on the HEAD chain — skip reads exploration.
                continue
            # effective_reads(E) = declared reads + writes of E itself
            # (misma semántica que _effective_reads | _effective_writes).
            eff_reads = _reads_of(e_id) | _writes_of(e_id)
            for f in eff_reads:
                writers = history.get(f)
                if not writers:
                    continue
                # Binary search: find the last writer with chain_position
                # STRICTLY LESS than e_pos. writers is sorted ascending by
                # chain_position. bisect_left on the positions gives the
                # insertion point; the element just before it is the last
                # writer with position < e_pos.
                positions = history_positions.get(f)
                if positions is None:
                    continue
                idx = bisect_left(positions, e_pos)
                if idx == 0:
                    # No writer strictly before E for this file.
                    continue
                prior_event_id, _ = writers[idx - 1]
                if prior_event_id == e_id:
                    # Don't self-reference: a writer reading its own write
                    # is not an upstream dependency.
                    if idx - 1 == 0:
                        continue
                    prior_event_id, _ = writers[idx - 2]
                    if prior_event_id == e_id:
                        continue
                # Add edge (f, prior_event_id) to E's upstream.
                cone[e_id]["upstream"].append((f, prior_event_id))
                if prior_event_id not in visited:
                    visited.add(prior_event_id)
                    cone[prior_event_id] = {
                        "reason": f"read by {e_id}",
                        "upstream": [],
                    }
                    depth_of[prior_event_id] = depth_of[e_id] + 1
                    queue.append(prior_event_id)

        depth = max(depth_of.values()) if depth_of else 0
        return {
            "writer_event": introducer,
            "cone": cone,
            "visited": visited,
            "depth": depth,
        }

    # --- Degradación: path on-the-fly (materializar el ledger). ---
    # 2. Materialise events (necesario para el BFS — los reads no están
    # en el cache cuando el cache no está disponible).
    events = list(_iter_events_from(ledger_path))
    by_id: Dict[str, dict] = {}
    for ev in events:
        eid = ev.get("event_id")
        if eid is not None:
            by_id[eid] = ev

    # chain_position = index in LEDGER ORDER (enumerate), NOT the
    # parent chain (TD-#3a: parents are None in the real ledger).
    position_of: Dict[str, int] = {}
    for pos, ev in enumerate(events):
        eid = ev.get("event_id")
        if eid is not None:
            position_of[eid] = pos

    # 3. Build writer_history: file → [(event_id, chain_position)] sorted.
    history: Dict[str, List[Tuple[str, int]]] = {}
    for ev in events:
        eid = ev.get("event_id")
        if eid is None or eid not in position_of:
            continue
        for f in _effective_writes(ev):
            history.setdefault(f, []).append((eid, position_of[eid]))
    for f in history:
        history[f].sort(key=lambda pair: pair[1])
    # Precompute the parallel positions list for each file so the BFS
    # binary search is O(log n) per lookup (no per-iteration list rebuild).
    history_positions: Dict[str, List[int]] = {
        f: [pair[1] for pair in pairs] for f, pairs in history.items()
    }

    # 4. BFS from W. cone[event_id] = {"reason", "upstream": [(file, prior)]}.
    cone: Dict[str, dict] = {
        w_id: {"reason": "writer", "upstream": []},
    }
    visited: Set[str] = {w_id}
    # depth_of[event_id] = number of hops from the writer.
    depth_of: Dict[str, int] = {w_id: 0}
    queue: deque = deque([w_id])

    while queue:
        e_id = queue.popleft()
        e_ev = by_id.get(e_id)
        if e_ev is None:
            continue
        e_pos = position_of.get(e_id)
        if e_pos is None:
            # Event not on the HEAD chain — skip reads exploration.
            continue
        # effective_reads(E) = declared reads + writes of E itself (per plan).
        eff_reads = _effective_reads(e_ev) | _effective_writes(e_ev)
        for f in eff_reads:
            writers = history.get(f)
            if not writers:
                continue
            # Binary search: find the last writer with chain_position
            # STRICTLY LESS than e_pos. writers is sorted ascending by
            # chain_position. bisect_left on the positions gives the
            # insertion point; the element just before it is the last
            # writer with position < e_pos.
            positions = history_positions.get(f)
            if positions is None:
                continue
            idx = bisect_left(positions, e_pos)
            if idx == 0:
                # No writer strictly before E for this file.
                continue
            prior_event_id, _ = writers[idx - 1]
            if prior_event_id == e_id:
                # Don't self-reference: a writer reading its own write
                # is not an upstream dependency.
                if idx - 1 == 0:
                    continue
                prior_event_id, _ = writers[idx - 2]
                if prior_event_id == e_id:
                    continue
            # Add edge (f, prior_event_id) to E's upstream.
            cone[e_id]["upstream"].append((f, prior_event_id))
            if prior_event_id not in visited:
                visited.add(prior_event_id)
                cone[prior_event_id] = {
                    "reason": f"read by {e_id}",
                    "upstream": [],
                }
                depth_of[prior_event_id] = depth_of[e_id] + 1
                queue.append(prior_event_id)

    depth = max(depth_of.values()) if depth_of else 0
    return {
        "writer_event": introducer,
        "cone": cone,
        "visited": visited,
        "depth": depth,
    }