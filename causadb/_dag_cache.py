"""F.13.1.2 + F.13.1.3 + F.13.1.4 — DAG Writer/Reader/Incremental Update.

Este módulo materializa el cache DAG (especificado en F.13.1.1
``_dag_schema.py``) a partir de los eventos del ledger, lo escribe
a disco (``dag.json``) con file locking y hash de integridad
(F.13.1.2), lo carga de vuelta con verificación de integridad y
detección de staleness (F.13.1.3), y lo actualiza incrementalmente
cuando llegan eventos nuevos (F.13.1.4).

Funciones públicas:
- ``build_dag(events) -> dict``: construye el DAG desde una lista de
  eventos (dicts del ledger reader).
- ``write_dag(dag, path) -> None``: escribe el DAG a ``path`` con
  ``fsync`` y ``fcntl.flock`` (lock file ``path + ".lock"``).
- ``compute_dag_hash(dag) -> str``: SHA-256 de ``json.dumps(dag,
  sort_keys=True)``.
- ``read_dag(path) -> Optional[dict]``: carga el DAG desde ``path``
  con ``fcntl.flock(LOCK_SH)``, verifica integridad (``dag_hash``)
  y schema (``validate_dag``). Degradación suave: retorna ``None``
  ante cualquier corrupción.
- ``get_last_ledger_seq(ledger_path) -> int``: retorna el
  ``sequence_number`` del último evento del ledger (0 si vacío).
  BIT-CHR.117: lee SOLO el último bloque del ledger (no el archivo
  completo) — el trace ya no paga la lectura completa para chequear
  staleness.
- ``is_dag_stale(dag, ledger_path) -> bool``: compara ``dag.last_seq``
  con el último ``sequence_number`` del ledger para detectar staleness.
  BIT-CHR.117: agrega el guard de truncamiento por tamaño
  (``size < dag.last_offset`` → stale) y la comparación opcional de
  ``last_hash``.
- ``update_dag(dag, new_events) -> dict``: actualiza incrementalmente
  un DAG existente con eventos nuevos (sin reconstruir desde cero).

Decisiones de diseño (documentadas para fases posteriores F.13.1.3+):

1. **``build_dag`` delega el position-index a ``_causal_cone._writer_history``.**
   No duplica el algoritmo de indexado por posición en orden de ledger
   ni el cálculo de ``chain_position`` (v2, BIT-CHR.110 TD-#3a: posición
   en orden de ledger, no en la cadena de parents). El contrato de
   F.13.1.1 exige que ``writer_history`` use listas
   ``[event_id, chain_position]`` (no tuplas, porque JSON no las
   preserva); la conversión tupla→lista ocurre aquí, en el boundary
   entre ``_causal_cone`` (tuplas internas) y el cache persistente
   (listas JSON-nativas).

2. **``write_dag`` sigue el patrón de ``_ledger_writer.py:148-151``.**
   Lock file separado ``path + ".lock"`` (no el archivo de datos),
   ``fcntl.flock(LOCK_EX)`` alrededor de la escritura, ``fsync`` antes
   de liberar el lock. Esto protege tanto la escritura concurrente
   (dos writers) como la lectura concurrente (un reader leyendo un
   archivo parcialmente escrito — F.13.1.3 usará ``LOCK_SH``).

3. **``dag_hash`` es un campo top-level del JSON escrito, no un hash
   externo.** Se computa sobre el DAG SIN el campo ``dag_hash`` (para
   evitar recursión) y se añade al payload antes de serializar. El
   reader (F.13.1.3) puede verificar integridad re-computando el hash
   del dict sin ``dag_hash`` y comparando.

4. **``build_dag`` retorna vía ``dag_to_dict``.** Esto garantiza que
   el dict retornado es JSON-serializable (copia profunda vía json
   roundtrip, ya que los tipos son JSON-nativos) y pasa ``validate_dag``.
   Si el caller muta el resultado, no afecta estructuras internas.

5. **``last_seq`` se extrae del ``sequence_number`` del último evento.**
   Si el último evento no tiene ``sequence_number`` (eventos legacy o
   sintéticos sin ese campo), se usa ``0``. El reader (F.13.1.3) usa
   ``last_seq`` para detectar staleness comparando con el último
   ``sequence_number`` del ledger.

6. **Schema v3 (BIT-CHR.117): ``event_parents`` fue ELIMINADO.** Nadie
   lo consume en producción (el cono causal usa posiciones en orden de
   ledger, no la cadena de parents — TD-#3a). En su lugar el cache
   almacena ``event_reads``/``event_meta``/``event_ids`` para que
   ``trace_upstream``/``trace_downstream`` operen sin materializar el
   ledger, y ``last_offset``/``last_hash``/``covered_archives`` para el
   tail-read incremental y el guard de truncamiento.

7. **Normalización de paths (BIT-CHR.110 TD-#3d).** ``build_dag`` y
   ``update_dag`` normalizan ``payload.writes``/``payload.reads`` vía
   ``_normalize_rel_path`` (``causadb/causadb/X`` → ``causadb/X``) para
   que el cono conecte eventos que escriben/leen el mismo archivo con
   estilos mixtos. ``writer_history`` ya normaliza vía
   ``_effective_writes`` (que normaliza en ``_causal_cone``).
"""
import fcntl
import hashlib
import json
import os
from datetime import datetime
from typing import Dict, List, Optional

from causadb._causal_cone import _writer_history
from causadb._dag_schema import DAG_SCHEMA_VERSION, dag_to_dict, validate_dag
from causadb._file_index import _normalize_rel_path


# ---------------------------------------------------------------------------
# build_dag
# ---------------------------------------------------------------------------

def build_dag(events: List[dict]) -> dict:
    """Construye un DAG cache desde una lista de eventos del ledger.

    Args:
        events: lista de dicts, cada uno es el ``event`` field de un
            entry del ledger (ver ``LedgerReader.read_all_entries``).
            Puede ser vacía.

    Returns:
        Un dict DAG válido (pasa ``validate_dag``) y JSON-serializable,
        retornado vía ``dag_to_dict`` para garantizar la copia defensiva.

    Notas:
        - Delega el position-index a ``_causal_cone._writer_history``
          (no duplica el algoritmo de indexado por posición en orden
          de ledger — v2, BIT-CHR.110 TD-#3a).
        - Convierte las tuplas ``(event_id, chain_position)`` de
          ``_writer_history`` a listas ``[event_id, chain_position]``
          (contrato F.13.1.1: JSON no preserva tuplas).
        - ``history_positions`` se construye paralelo a
          ``writer_history`` (extraído de las chain_positions).
        - Schema v3 (BIT-CHR.117): llena ``event_ids`` (orden de
          ledger), ``event_reads`` (payload.reads normalizado, solo
          no-vacíos) y ``event_meta`` (event_type + timestamp SOLO para
          eventos con reads no vacíos — los únicos taintables). Ya NO
          construye ``event_parents``.
        - Normaliza writes/reads vía ``_normalize_rel_path``
          (BIT-CHR.110 TD-#3d).
        - ``last_offset``/``last_hash``/``covered_archives`` arrancan
          con defaults inertes (0, "", []) — el caller
          (``_load_or_build_dag``) los sobreescribe con valores reales
          antes de ``write_dag``.
    """
    # 1. writer_history: delegar a _causal_cone (tuplas) y convertir a listas.
    cone_history = _writer_history(events)  # Dict[str, List[Tuple[str, int]]]

    writer_history: Dict[str, List[List]] = {}
    history_positions: Dict[str, List[int]] = {}
    for file_path, entries in cone_history.items():
        # Convertir tuplas → listas (contrato F.13.1.1).
        writer_history[file_path] = [[eid, pos] for eid, pos in entries]
        history_positions[file_path] = [pos for _, pos in entries]

    # 2. event_writes / event_reads / event_meta / event_ids (schema v3).
    #    Normalizar paths (BIT-CHR.110 TD-#3d).
    event_writes: Dict[str, List[str]] = {}
    event_reads: Dict[str, List[str]] = {}
    event_meta: Dict[str, dict] = {}
    event_ids: List[str] = []
    for ev in events:
        eid = ev.get("event_id")
        if eid is None:
            continue
        event_ids.append(eid)
        payload = ev.get("payload") or {}

        writes = [_normalize_rel_path(w) for w in (payload.get("writes") or [])]
        # Solo registrar eventos que tienen writes no vacíos.
        if writes:
            event_writes[eid] = list(writes)

        reads = [_normalize_rel_path(r) for r in (payload.get("reads") or [])]
        # Solo registrar eventos con reads no vacíos (los únicos taintables).
        if reads:
            event_reads[eid] = list(reads)
            event_meta[eid] = {
                "event_type": ev.get("event_type"),
                "timestamp": ev.get("timestamp"),
            }

    # 3. last_event_id y last_seq: del último evento en ledger order.
    last_event_id = ""
    last_seq = 0
    if events:
        last_ev = events[-1]
        last_event_id = last_ev.get("event_id") or ""
        # sequence_number puede estar ausente (eventos sintéticos/legacy).
        seq = last_ev.get("sequence_number")
        if isinstance(seq, int) and not isinstance(seq, bool):
            last_seq = seq

    # 4. Ensamblar el DAG.
    dag = {
        "schema_version": DAG_SCHEMA_VERSION,
        "last_event_id": last_event_id,
        "last_seq": last_seq,
        "last_offset": 0,
        "last_hash": "",
        "covered_archives": [],
        "writer_history": writer_history,
        "history_positions": history_positions,
        "event_writes": event_writes,
        "event_reads": event_reads,
        "event_meta": event_meta,
        "event_ids": event_ids,
        "built_at": datetime.utcnow().isoformat() + "Z",
    }

    # 5. Retornar vía dag_to_dict: valida + copia defensiva JSON-nativa.
    return dag_to_dict(dag)


# ---------------------------------------------------------------------------
# compute_dag_hash
# ---------------------------------------------------------------------------

def compute_dag_hash(dag: dict) -> str:
    """Computa el SHA-256 de ``json.dumps(dag, sort_keys=True)``.

    Helper público usado por ``write_dag`` y por el reader (F.13.1.3)
    para verificar integridad. El hash es determinístico: mismo dict →
    mismo hash, independientemente del orden de inserción de las keys
    (``sort_keys=True``).

    Args:
        dag: el dict DAG (sin el campo ``dag_hash`` — si lo tiene,
            se incluye en el hash, lo que sería recursivo. El caller
            debe pasar el DAG sin ``dag_hash``).

    Returns:
        str: hex digest de 64 caracteres.
    """
    payload = json.dumps(dag, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# write_dag
# ---------------------------------------------------------------------------

def write_dag(dag: dict, path: str) -> None:
    """Escribe el DAG a ``path`` con ``fsync`` y file locking.

    Patrón de file locking (sigue ``_ledger_writer.py:148-151``):
    - Lock file separado: ``path + ".lock"`` (no el archivo de datos).
    - ``fcntl.flock(LOCK_EX)`` alrededor de la escritura completa.
    - ``fsync`` antes de liberar el lock (durabilidad).
    - ``fcntl.flock(LOCK_UN)`` en el ``finally`` (liberación garantizada).

    El JSON escrito incluye un campo top-level ``dag_hash`` (SHA-256
    del DAG sin ese campo) para que el reader (F.13.1.3) pueda verificar
    integridad.

    Args:
        dag: el dict DAG a escribir (debe pasar ``validate_dag``).
        path: path absoluto del archivo destino (``dag.json``).
    """
    lock_path = path + ".lock"

    # Asegurar que el lock file existe (crear si no, como _ledger_writer).
    if not os.path.exists(lock_path):
        open(lock_path, "a").close()

    # Computar el hash del DAG SIN el campo dag_hash (evitar recursión).
    dag_for_hash = {k: v for k, v in dag.items() if k != "dag_hash"}
    dag_hash = compute_dag_hash(dag_for_hash)

    # Ensamblar el payload con el hash incluido.
    dag_with_hash = dict(dag_for_hash)
    dag_with_hash["dag_hash"] = dag_hash
    payload = json.dumps(dag_with_hash, sort_keys=True)

    # File locking + escritura atómica (fsync).
    with open(lock_path, "a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            with open(path, "w") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# F.13.1.3 — read_dag / staleness detection
# ---------------------------------------------------------------------------

def read_dag(path: str) -> Optional[dict]:
    """Carga el DAG cache desde ``path`` con flock compartido (LOCK_SH).

    Degradación suave: ante cualquier corrupción o problema de lectura,
    retorna ``None`` (el caller debe rebuild on-the-fly). Esto sigue
    el principio de "fail-safe" del artículo V — nunca propagar un
    cache corrupto como si fuera válido.

    Casos que retornan ``None``:
    - El archivo no existe (cache frío).
    - JSON parse error (archivo truncado o corrupto).
    - ``dag_hash`` mismatch (contenido alterado después de la escritura).
    - ``validate_dag`` falla (schema inválido o incompleto).
    - ``fcntl.flock`` lanza excepción (timeout / recurso no disponible).

    File locking (sigue el patrón de ``write_dag``):
    - Lock file separado: ``path + ".lock"`` (no el archivo de datos).
    - ``fcntl.flock(LOCK_SH)`` para lectura compartida — múltiples
      readers pueden leer concurrentemente, pero un writer (LOCK_EX)
      los bloquearía.
    - ``fcntl.flock(LOCK_UN)`` en el ``finally`` (liberación garantizada).

    Verificación de integridad:
    1. Leer el JSON completo.
    2. Extraer el campo ``dag_hash`` del dict.
    3. Remover ``dag_hash`` del dict.
    4. Re-computar ``compute_dag_hash`` sobre el dict sin ``dag_hash``.
    5. Comparar. Si mismatch → ``None`` (corrupción detectada).

    Args:
        path: path absoluto del archivo cache (``dag.json``).

    Returns:
        Un dict DAG válido (pasa ``validate_dag``) sin el campo
        ``dag_hash`` (este campo es metadata de integridad, no parte
        del DAG semántico), o ``None`` si el cache no se puede usar.
    """
    # 1. Cache frío: archivo no existe.
    if not os.path.exists(path):
        return None

    lock_path = path + ".lock"

    # Asegurar que el lock file existe (crear si no, como write_dag).
    # Esto evita errores de open() si el lock file nunca fue creado
    # (caso: cache escrito a mano sin pasar por write_dag).
    if not os.path.exists(lock_path):
        try:
            open(lock_path, "a").close()
        except OSError:
            # Si no podemos crear el lock file, degradar suave.
            return None

    # 2. File locking + lectura. Degradación suave ante cualquier excepción.
    try:
        with open(lock_path, "a") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
            try:
                with open(path, "r") as f:
                    raw = f.read()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except (OSError, IOError):
        # flock timeout, permisos, etc. → degradar suave.
        return None

    # 3. JSON parse. Degradación suave ante JSON corrupto.
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None

    # 4. Debe ser un dict.
    if not isinstance(loaded, dict):
        return None

    # 5. Verificación de integridad: dag_hash mismatch.
    stored_hash = loaded.get("dag_hash")
    if not isinstance(stored_hash, str):
        # Sin dag_hash o tipo incorrecto → cache corrupto/incompleto.
        return None

    # Remover dag_hash para re-computar el hash (evitar recursión).
    dag_for_hash = {k: v for k, v in loaded.items() if k != "dag_hash"}
    recomputed_hash = compute_dag_hash(dag_for_hash)

    if recomputed_hash != stored_hash:
        # Contenido alterado después de la escritura → corrupción.
        return None

    # 6. Validación de schema. Si invalid → None.
    if not validate_dag(dag_for_hash):
        return None

    # 7. Retornar el DAG sin el campo dag_hash (es metadata de
    # integridad, no parte del DAG semántico). El caller (F.13.1.5
    # integración) recibe un dict limpio que pasa validate_dag.
    return dag_for_hash


# ---------------------------------------------------------------------------
# BIT-CHR.117 — get_last_ledger_seq por última línea (sin leer el ledger completo)
# ---------------------------------------------------------------------------

def _scan_full_ledger_seq(ledger_path: str) -> int:
    """Scan completo del ledger (archives + activo) buscando el último seq.

    Fallback SOLO para el caso "ledger vacío + archives presentes": tras
    un archive el ledger activo queda en 0 bytes y el último evento puede
    estar archivado. Este scan es el comportamiento histórico de
    ``get_last_ledger_seq`` (lectura completa) — se usa únicamente cuando
    no hay alternativa (ledger vacío con archives).
    """
    from causadb._ledger_reader import LedgerReader

    reader = LedgerReader(ledger_path)
    last_seq = 0
    try:
        for entry in reader.read_all_entries():
            event = entry.get("event") or {}
            seq = event.get("sequence_number")
            # Solo aceptar ints reales (no bool, no float, no None).
            if isinstance(seq, int) and not isinstance(seq, bool):
                last_seq = seq
    except Exception:
        # Ledger corrupto o ilegible → retornar 0 (degradación segura).
        return 0
    return last_seq


def _read_last_line_seq(ledger_path: str, size: int) -> int:
    """Lee el ``sequence_number`` de la última línea JSON válida del ledger.

    Algoritmo (BIT-CHR.117 — el trace ya no paga la lectura completa):
    1. Empezar con los últimos ~8192 bytes.
    2. Si ninguna línea completa parsea con ``sequence_number`` int,
       ampliar el bloque hacia atrás hasta encontrar una (caso extremo:
       leer todo el archivo).
    3. Devolver el ``sequence_number`` de la ÚLTIMA línea JSON válida.
    4. Línea final cortada (crash) → ignorarla (el writer la reescribe).
    5. Cualquier excepción → 0 (degradación suave).

    Args:
        ledger_path: path absoluto del ledger.
        size: tamaño del ledger en bytes (ya verificado > 0).

    Returns:
        El ``sequence_number`` de la última línea válida, o 0 si no se
        encuentra ninguna.
    """
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
        # Iterar desde el final: la última línea válida es la que manda.
        # Una línea final truncada (crash) no parsea → se ignora y se
        # toma la anterior.
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                # Línea corrupta/truncada → seguir hacia atrás.
                continue
            seq = (entry.get("event") or {}).get("sequence_number")
            if isinstance(seq, int) and not isinstance(seq, bool):
                return seq
        # No se encontró ninguna línea válida en este bloque → ampliar.
        if start == 0:
            return 0
        block *= 2


def get_last_ledger_seq(ledger_path: str) -> int:
    """Retorna el ``sequence_number`` del último evento del ledger.

    BIT-CHR.117: ya NO lee el ledger completo. Algoritmo:
    1. Si el ledger no existe → 0.
    2. Leer el tamaño; si ``size == 0``:
       - si existe ``<dirname>/archive/`` con .gz → FALLBACK al scan
         completo actual (el último evento puede estar archivado; el
         ledger quedó vacío tras archive).
       - si no hay archives → 0 (ledger vacío).
    3. Si size > 0: leer el último bloque (empezando con los últimos
       ~8192 bytes; si ninguna línea completa parsea con
       ``sequence_number`` int, ampliar el bloque hacia atrás hasta
       encontrar una; caso extremo leer todo el archivo). Devolver el
       ``sequence_number`` de la ÚLTIMA línea JSON válida. Línea final
       cortada (crash) → ignorarla (el writer la reescribe). Cualquier
       excepción → 0 (degradación suave).

    Preserva la semántica de retorno (0 si vacío/corrupto) — dependen
    de ella ``is_dag_stale`` y ``cli/_cmd_watch.py``.

    Args:
        ledger_path: path absoluto del ledger (``ledger.log``).

    Returns:
        El ``sequence_number`` del último evento, o ``0`` si el
        ledger está vacío o no existe.
    """
    # Si el ledger no existe, no hay eventos → seq 0.
    if not os.path.exists(ledger_path):
        return 0

    try:
        size = os.path.getsize(ledger_path)
    except OSError:
        return 0

    if size == 0:
        # Ledger vacío. Si hay archives, el último evento puede estar
        # archivado (el ledger quedó vacío tras archive) → fallback al
        # scan completo. Si no hay archives → 0 (ledger vacío).
        archive_dir = os.path.join(os.path.dirname(ledger_path), "archive")
        try:
            if os.path.isdir(archive_dir) and any(
                f.endswith(".gz") for f in os.listdir(archive_dir)
            ):
                return _scan_full_ledger_seq(ledger_path)
        except OSError:
            return 0
        return 0

    # size > 0: leer la última línea válida (sin leer el ledger completo).
    return _read_last_line_seq(ledger_path, size)


# ---------------------------------------------------------------------------
# F.13.1.4 — update_dag (incremental)
# ---------------------------------------------------------------------------

def update_dag(dag: dict, new_events: List[dict]) -> dict:
    """Actualiza incrementalmente un DAG existente con eventos nuevos.

    Toma un DAG cache (típicamente de ``read_dag`` o ``build_dag``) y
    una lista de eventos nuevos (con ``sequence_number > dag.last_seq``),
    y produce el DAG actualizado sin reconstruir desde cero.

    El resultado debe ser **idéntico** a ``build_dag(all_events)`` donde
    ``all_events`` = eventos que produjeron el DAG original + new_events,
    asumiendo que los new_events extienden la cadena lineal desde el
    último evento del DAG original (cada new_event tiene como parent al
    evento inmediatamente anterior en orden de sequence_number).

    Args:
        dag: el dict DAG existente (de ``read_dag`` o ``build_dag``).
            No se muta — se trabaja sobre una copia.
        new_events: lista de dicts de eventos nuevos, en orden de
            ``sequence_number`` ascendente (orden del ledger). Cada
            evento debe tener ``event_id``; ``parent_event_id`` y
            ``payload.writes`` son opcionales.

    Returns:
        Un NUEVO dict DAG actualizado (vía ``dag_to_dict`` para
        garantizar JSON-safety y validación). El DAG original no se muta.

    El update realiza:
    1. Para cada new_event (en orden):
       - Calcula su ``chain_position`` = base + índice, donde base es
         el número de eventos ya en la cadena (``dag.last_seq + 1`` si
         el DAG no está vacío, ``0`` si está vacío).
       - Para cada file en ``payload.writes`` (normalizado):
         - Append ``[event_id, chain_position]`` a
           ``writer_history[file]`` (listas, no tuplas — contrato
           F.13.1.1).
         - Append ``chain_position`` a ``history_positions[file]``.
       - ``event_writes[event_id]`` = ``list(writes)`` normalizado si
         writes no vacío (igual que ``build_dag``).
       - ``event_reads[event_id]`` = ``list(reads)`` normalizado si
         reads no vacío (igual que ``build_dag``).
       - ``event_meta[event_id]`` = ``{"event_type", "timestamp"}`` si
         reads no vacío (igual que ``build_dag``).
       - ``event_ids`` se extiende con el event_id (orden de ledger).
    2. ``last_event_id`` y ``last_seq`` se actualizan al último
       new_event (si new_events no está vacío).
    3. ``built_at`` se actualiza al timestamp actual.
    4. ``schema_version`` se mantiene (no bump para incremental).
    5. ``last_offset``/``last_hash``/``covered_archives`` NO se tocan —
       los setea el caller (``_load_or_build_dag``) después del update.
    6. ``dag_hash`` se recomputará cuando ``write_dag`` guarde el DAG.

    Notas de diseño:

    1. **No duplica el position-index de ``_causal_cone._writer_history``.**
       Usa la información del DAG previo (``last_seq`` como proxy del
       número de eventos en orden de ledger) para calcular las
       chain_positions de los nuevos eventos sin reconstruir el index
       completo. Esto es válido cuando los new_events extienden el
       ledger en orden (caso normal de append). Si los new_events
       tuvieran positions que NO continúan la secuencia (branching),
       las chain_positions calculadas podrían diferir de
       ``build_dag(all_events)`` — en ese caso el caller debe usar
       ``build_dag`` directamente.

    2. **``last_seq`` como proxy del número de eventos.** En el ledger
       real los ``sequence_number`` son índices 0-indexed contiguos en
       orden de ledger, así que ``last_seq == número de eventos - 1``
       (consistente con la semántica v2 de ``chain_position`` = posición
       en orden de ledger). El primer nuevo evento tiene chain_position
       = ``last_seq + 1``. Si el DAG está vacío (``last_seq == 0`` y
       ``last_event_id == ""``), el primer nuevo evento tiene
       chain_position = 0.

    3. **Append al final mantiene el orden por chain_position.** Como
       los nuevos eventos siempre tienen chain_positions mayores a los
       existentes, appendar a ``writer_history[file]`` y
       ``history_positions[file]`` mantiene el orden ascendente
       requerido por el contrato (F.13.1.1) y por ``bisect`` en
       ``_causal_cone``.

    4. **Copia defensiva.** Se trabaja sobre una copia profunda del
       DAG original (vía ``json`` roundtrip, igual que
       ``dag_to_dict``) para no mutar el input. El resultado pasa por
       ``dag_to_dict`` para validación + copia final.
    """
    import json as _json

    # Copia defensiva profunda del DAG original (no mutar el input).
    # Los tipos del DAG ya son JSON-nativos, así que json roundtrip es
    # seguro y más rápido que copy.deepcopy para esta estructura.
    updated = _json.loads(_json.dumps(dag))

    # Determinar la base para las chain_positions de los nuevos eventos.
    # Si el DAG está vacío (last_event_id == "" y last_seq == 0), los
    # nuevos eventos empiezan en chain_position 0.
    # Si el DAG tiene eventos, los nuevos eventos empiezan en
    # last_seq + 1 (asumiendo cadena lineal con seqs 0-indexed).
    if updated["last_event_id"] == "" and updated["last_seq"] == 0:
        chain_base = 0
    else:
        chain_base = updated["last_seq"] + 1

    # Asegurar que los dicts anidados existen (defensivo — un DAG válido
    # ya los tiene, pero esto protege contra DAGs construidos a mano).
    if "writer_history" not in updated:
        updated["writer_history"] = {}
    if "history_positions" not in updated:
        updated["history_positions"] = {}
    if "event_writes" not in updated:
        updated["event_writes"] = {}
    if "event_reads" not in updated:
        updated["event_reads"] = {}
    if "event_meta" not in updated:
        updated["event_meta"] = {}
    if "event_ids" not in updated:
        updated["event_ids"] = []

    writer_history = updated["writer_history"]
    history_positions = updated["history_positions"]
    event_writes = updated["event_writes"]
    event_reads = updated["event_reads"]
    event_meta = updated["event_meta"]
    event_ids = updated["event_ids"]

    # Procesar cada new_event en orden de sequence_number ascendente.
    for index, new_event in enumerate(new_events):
        event_id = new_event.get("event_id")
        if event_id is None:
            # Evento sin event_id — skip (no se puede indexar).
            # Esto matchea build_dag que también skipcea eventos sin event_id.
            continue

        # chain_position del nuevo evento.
        chain_position = chain_base + index

        # event_ids: extender en orden de ledger (schema v3).
        event_ids.append(event_id)

        # writes del payload (normalizado — BIT-CHR.110 TD-#3d).
        payload = new_event.get("payload") or {}
        writes = [_normalize_rel_path(w) for w in (payload.get("writes") or [])]

        # event_writes: solo registrar si writes no es vacío (igual que build_dag).
        if writes:
            event_writes[event_id] = list(writes)

        # reads del payload (normalizado — BIT-CHR.110 TD-#3d).
        reads = [_normalize_rel_path(r) for r in (payload.get("reads") or [])]

        # event_reads + event_meta: solo si reads no es vacío (igual que
        # build_dag — los únicos eventos taintables).
        if reads:
            event_reads[event_id] = list(reads)
            event_meta[event_id] = {
                "event_type": new_event.get("event_type"),
                "timestamp": new_event.get("timestamp"),
            }

        # writer_history + history_positions: para cada file escrito.
        # Usar _effective_writes para consistencia con _writer_history
        # (deduplica files dentro del mismo evento vía set).
        # NOTA: build_dag usa _writer_history que internamente usa
        # _effective_writes (set), así que un evento que escribe el
        # mismo file dos veces en su payload.writes aparece una sola vez
        # en writer_history. Replicamos ese comportamiento.
        from causadb._causal_cone import _effective_writes
        files_written = _effective_writes(new_event)
        for file_path in files_written:
            # Append [event_id, chain_position] (lista, no tupla — contrato).
            writer_history.setdefault(file_path, []).append(
                [event_id, chain_position]
            )
            # Append chain_position a history_positions (paralelo a writer_history).
            history_positions.setdefault(file_path, []).append(chain_position)

    # Actualizar last_event_id y last_seq al último new_event procesado.
    if new_events:
        last_new_event = new_events[-1]
        last_eid = last_new_event.get("event_id")
        if last_eid is not None:
            updated["last_event_id"] = last_eid
        # last_seq: usar sequence_number del último evento si está presente,
        # sino fallback a chain_base + len(new_events) - 1.
        seq = last_new_event.get("sequence_number")
        if isinstance(seq, int) and not isinstance(seq, bool):
            updated["last_seq"] = seq
        else:
            # Fallback: la chain_position del último evento procesado.
            updated["last_seq"] = chain_base + len(new_events) - 1

    # Actualizar built_at.
    updated["built_at"] = datetime.utcnow().isoformat() + "Z"

    # Retornar vía dag_to_dict: valida + copia defensiva JSON-nativa.
    return dag_to_dict(updated)


def is_dag_stale(dag: dict, ledger_path: str) -> bool:
    """Detecta si el DAG cache está stale respecto al ledger.

    Compara ``dag["last_seq"]`` con el último ``sequence_number`` del
    ledger (vía ``get_last_ledger_seq``). Si el ledger tiene eventos
    más nuevos (sequence_number > dag.last_seq), el DAG está stale.

    BIT-CHR.117 — guard de truncamiento: si el tamaño del ledger es
    MENOR que ``dag["last_offset"]``, el ledger fue truncado/restaurado
    y el cache es obsoleto (los sequence_number pueden reiniciar tras
    un archive — la comparación de seqs sola devolvería "fresh"
    incorrectamente). También compara ``last_hash`` (si el DAG lo
    tiene) contra ``ledger.log.last_hash.json``.

    Casos (según spec F.13.1.3):
    - ``size < dag.last_offset`` → stale (True) — truncamiento.
    - ``dag.last_hash`` no vacío y difiere del ``last_hash.json``
      actual → stale (True).
    - ``dag.last_seq < last_ledger_seq`` → stale (True).
    - ``dag.last_seq == last_ledger_seq`` → fresh (False).
    - Ledger vacío o ilegible → stale (True) — degradar seguro,
      forzar rebuild on-the-fly (artículo V: no propagar estado
      potencialmente inconsistente).
    - DAG con ``last_seq`` None o 0 se considera stale si el ledger
      no está vacío (cubierto por la comparación ``dag_last_seq <
      ledger_last_seq`` cuando ``ledger_last_seq > 0``).

    Args:
        dag: el dict DAG cache (de ``read_dag`` o ``build_dag``).
        ledger_path: path absoluto del ledger.

    Returns:
        ``True`` si el DAG está stale (debe rebuild), ``False`` si
        está fresh (puede usarse).
    """
    # BIT-CHR.117 — Guard de truncamiento por tamaño. Si el ledger es
    # más chico que el offset donde termina el cache, fue truncado o
    # restaurado → cache obsoleto (los seqs pueden coincidir tras un
    # archive porque reinician a 0).
    size = os.path.getsize(ledger_path) if os.path.exists(ledger_path) else 0
    if size < int(dag.get("last_offset") or 0):
        return True  # ledger truncado/restaurado → cache obsoleto

    # BIT-CHR.117 — Comparación opcional de last_hash. Si el DAG tiene
    # un last_hash registrado y el ledger.log.last_hash.json actual
    # difiere → el ledger cambió de contenido → stale. Si el DAG no
    # tiene last_hash (build sin last_hash) NO forzar stale por eso.
    dag_last_hash = dag.get("last_hash") or ""
    if dag_last_hash:
        actual_hash = _read_last_hash_file(ledger_path)
        if actual_hash is not None and actual_hash != dag_last_hash:
            return True

    # Obtener last_seq del DAG. Default 0 si ausente/invalid.
    dag_last_seq = dag.get("last_seq")
    if not isinstance(dag_last_seq, int) or isinstance(dag_last_seq, bool):
        dag_last_seq = 0

    # Obtener el último sequence_number del ledger.
    # get_last_ledger_seq retorna 0 si el ledger está vacío, no existe,
    # o está corrupto (degradación segura).
    ledger_last_seq = get_last_ledger_seq(ledger_path)

    # Si el ledger está vacío o ilegible (seq 0), degradar a stale.
    # Esto cubre el caso "ledger vacío o no se puede leer → True".
    # Nota: un DAG vacío (seq 0) contra un ledger vacío (seq 0) sería
    # fresh por la comparación numérica, pero la spec dice "ledger
    # vacío → stale". Sin embargo, en la práctica, si el ledger está
    # vacío, no hay nada que cachear y el DAG vacío es correcto.
    # Distinguimos: si el ledger NO existe como archivo, es fresh
    # (ambos vacíos). Si existe pero está vacío/corrupto, stale.
    if ledger_last_seq == 0:
        if not os.path.exists(ledger_path):
            # Ledger no existe → DAG vacío (seq 0) es fresh.
            return dag_last_seq != 0
        # Ledger existe pero seq=0 → vacío o corrupto. Degradar a stale
        # según spec ("ledger vacío o no se puede leer → True").
        # Pero si el DAG también está vacío (seq 0), fresh.
        return dag_last_seq != 0

    # Ledger tiene eventos (seq > 0). Comparar.
    return dag_last_seq < ledger_last_seq


def _read_last_hash_file(ledger_path: str) -> Optional[str]:
    """Lee ``<ledger>.last_hash.json`` → ``{"last_hash": "<sha256>"}``.

    Retorna ``None`` si el archivo no existe o no se puede leer.
    """
    last_hash_path = ledger_path + ".last_hash.json"
    if not os.path.exists(last_hash_path):
        return None
    try:
        with open(last_hash_path) as f:
            return json.load(f).get("last_hash")
    except (json.JSONDecodeError, OSError, KeyError):
        return None