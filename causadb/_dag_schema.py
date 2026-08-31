"""F.13.1.1 — Schema del DAG cache persistente.

Define la estructura JSON del cache DAG que se materializa en
``dag.json`` (fases posteriores F.13.1.2 writer, F.13.1.3 reader,
F.13.1.4 incremental, F.13.1.5 integración).

Estructura del cache (especificada en roadmap Fase 13.1.1):

.. code-block:: json

    {
      "schema_version": int,
      "last_event_id": str,
      "last_seq": int,
      "last_offset": int,
      "last_hash": str,
      "covered_archives": [str, ...],
      "writer_history": {file_path: [[event_id, chain_position], ...]},
      "history_positions": {file_path: [int, ...]},
      "event_writes": {event_id: [file_path, ...]},
      "event_reads": {event_id: [file_path, ...]},
      "event_meta": {event_id: {"event_type": str, "timestamp": str}},
      "event_ids": [event_id, ...],
      "built_at": "ISO timestamp"
    }

Decisiones de diseño (documentadas para fases posteriores):

1. **El DAG es un ``dict`` plain, no un dataclass.** Se serializa a
   JSON directo (precedente: ``ledger.log.index.json``). Un dataclass
   añadiría una capa de traducción sin beneficio — el cache se lee/escribe
   como JSON en disco.

2. **``writer_history`` usa listas ``[event_id, chain_position]``, no
   tuplas.** JSON no tiene tuplas; ``json.dumps((a, b))`` produce
   ``"[a, b]"`` y al deserializar vuelve como lista. Para que el roundtrip
   sea exacto (``dag_from_dict(dag_to_dict(dag)) == dag``), el DAG en
   memoria TAMBIÉN usa listas. El consumidor (``_causal_cone.py``) usa
   tuplas internamente — la conversión ocurre en el writer (F.13.1.2).

3. **``schema_version`` es ``int`` (3), no ``str`` ("2").** Difiere del
   ``schema_version`` de eventos (que es ``"0.1.0"``) porque el DAG cache
   es un artefacto interno, no un evento del ledger. Versionado entero
   simple: bump + invalidación automática en ``dag_from_dict``. La v2
   cambió la semántica de ``chain_position`` (posición en orden de
   ledger, no en la cadena de parents — BIT-CHR.110 TD-#3a). La v3
   (BIT-CHR.117) agrega los campos que permiten a ``trace_upstream`` /
   ``trace_downstream`` operar SIN materializar el ledger
   (``event_reads``, ``event_meta``, ``event_ids``), los campos de
   metadata para el tail-read incremental y el guard de truncamiento
   (``last_offset``, ``last_hash``, ``covered_archives``), y ELIMINA
   ``event_parents`` (peso muerto — nadie lo consume en producción).

4. **``validate_dag`` es sustantivo (Artículo IX).** Verifica tipos
   anidados (no solo top-level): ``writer_history`` entries son
   ``[str, int]``, ``event_writes`` values son ``list[str]``,
   ``event_reads`` values son ``list[str]``, ``event_meta`` values son
   ``{"event_type": str, "timestamp": str}``, ``event_ids`` es
   ``list[str]``, etc. Un ``return True`` trivial sería detectado por el
   test anti-teatro.

5. **``built_at`` es ``str`` ISO.** No se valida el formato ISO exacto
   (flexibilidad para el writer elegir ``datetime.utcnow().isoformat() +
   "Z"`` u otros). Solo se valida que sea ``str``.
"""
from datetime import datetime
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

#: Versión del schema del DAG cache. Bump + invalidación automática en
#: ``dag_from_dict`` cuando la estructura cambie estructuralmente.
#:
#: v2 (BIT-CHR.110 TD-#3a): ``chain_position`` ya NO es la posición en
#: la cadena ``parent_event_id`` (rota en el ledger real — parents None
#: en ~99.99% de los eventos) sino la posición en ORDEN DE LEDGER
#: (0-based, coincide con ``sequence_number``).
#:
#: v3 (BIT-CHR.117): agrega ``last_offset``/``last_hash``/
#: ``covered_archives`` (tail-read incremental + guard de truncamiento),
#: ``event_reads``/``event_meta``/``event_ids`` (trace sin materializar
#: el ledger), y elimina ``event_parents`` (peso muerto). El bump
#: invalida caches v2 (``read_dag`` retorna None → rebuild).
DAG_SCHEMA_VERSION: int = 3

#: Campos requeridos en todo DAG cache válido (orden estable para docs).
DAG_REQUIRED_FIELDS: tuple = (
    "schema_version",
    "last_event_id",
    "last_seq",
    "last_offset",
    "last_hash",
    "covered_archives",
    "writer_history",
    "history_positions",
    "event_writes",
    "event_reads",
    "event_meta",
    "event_ids",
    "built_at",
)


# ---------------------------------------------------------------------------
# Constructores
# ---------------------------------------------------------------------------

def make_empty_dag() -> dict:
    """Retorna un DAG vacío válido.

    Un DAG vacío representa el estado antes de cachear cualquier evento:
    ``last_event_id`` es ``""`` (no hay último evento), ``last_seq`` es
    ``0`` (no se ha cacheado ningún evento), y los dicts anidados están
    vacíos. ``built_at`` se setea al momento de construcción.

    Los campos de metadata del tail-read (``last_offset``, ``last_hash``,
    ``covered_archives``) arrancan con defaults inertes (0, "", []) — el
    caller (``_load_or_build_dag``) los sobreescribe con valores reales
    antes de ``write_dag``.
    """
    return {
        "schema_version": DAG_SCHEMA_VERSION,
        "last_event_id": "",
        "last_seq": 0,
        "last_offset": 0,
        "last_hash": "",
        "covered_archives": [],
        "writer_history": {},
        "history_positions": {},
        "event_writes": {},
        "event_reads": {},
        "event_meta": {},
        "event_ids": [],
        "built_at": datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# Serialización / Deserialización
# ---------------------------------------------------------------------------

def dag_to_dict(dag: dict) -> dict:
    """Serializa un DAG a un dict JSON-serializable.

    El DAG ya es un ``dict`` plain con tipos JSON-nativos (listas, no
    tuplas), así que esta función es esencialmente una validación +
    copia defensiva. Retorna un NUEVO dict (no muta el input) para que
    el caller pueda modificarlo sin afectar el original.

    Raises:
        ValueError: si el DAG no pasa ``validate_dag``.
    """
    if not validate_dag(dag):
        raise ValueError(
            "dag_to_dict: el DAG no pasa validate_dag — no se puede "
            "serializar un DAG inválido."
        )
    # Copia profunda defensiva: el caller puede mutar el resultado sin
    # afectar el input, y viceversa. Usamos json roundtrip porque los
    # tipos ya son JSON-nativos (más rápido que copy.deepcopy para
    # estructuras puras de dict/list/str/int).
    import json
    return json.loads(json.dumps(dag))


def dag_from_dict(data: dict) -> dict:
    """Deserializa un dict (típicamente de ``json.load``) a un DAG válido.

    Valida ``schema_version`` primero — si no matchea
    ``DAG_SCHEMA_VERSION``, levanta ``ValueError`` (el caller debe
    descartar el cache y rebuild on-the-fly, ver F.13.1.3 staleness).

    Args:
        data: dict con la estructura del DAG cache.

    Returns:
        Un NUEVO dict (copia defensiva) que pasa ``validate_dag``.

    Raises:
        ValueError: si ``schema_version`` no matchea ``DAG_SCHEMA_VERSION``
            o si el dict no pasa ``validate_dag``.
    """
    if not isinstance(data, dict):
        raise ValueError(
            f"dag_from_dict: se esperaba dict, se obtuvo {type(data).__name__}"
        )
    sv = data.get("schema_version")
    if sv != DAG_SCHEMA_VERSION:
        raise ValueError(
            f"dag_from_dict: schema_version mismatch — cache tiene {sv!r}, "
            f"se esperaba {DAG_SCHEMA_VERSION!r}. El cache es de una versión "
            f"incompatible y debe ser descartado (rebuild on-the-fly)."
        )
    if not validate_dag(data):
        raise ValueError(
            "dag_from_dict: el dict no pasa validate_dag — estructura "
            "corrupta o incompleta."
        )
    # Copia defensiva (mismo rationale que dag_to_dict).
    import json
    return json.loads(json.dumps(data))


# ---------------------------------------------------------------------------
# Validación
# ---------------------------------------------------------------------------

def validate_dag(dag: dict) -> bool:
    """Valida que *dag* tenga todos los campos requeridos con tipos correctos.

    Verificación sustantiva (Artículo IX — anti-teatro):
    - Campos top-level presentes y con tipo correcto.
    - ``writer_history`` values son ``list`` de ``[str, int]`` (2 elementos).
    - ``history_positions`` values son ``list`` de ``int``.
    - ``event_writes`` values son ``list`` de ``str``.
    - ``event_reads`` values son ``list`` de ``str``.
    - ``event_meta`` values son ``{"event_type": str, "timestamp": str}``.
    - ``event_ids`` es ``list`` de ``str``.
    - ``last_offset`` es ``int`` (no bool).
    - ``last_hash`` es ``str``.
    - ``covered_archives`` es ``list`` de ``str``.

    Args:
        dag: el dict a validar.

    Returns:
        ``True`` si el DAG es válido, ``False`` en caso contrario.
        Nunca levanta excepciones — los callers usan el bool para
        decidir (degradación suave en F.13.1.3).
    """
    if not isinstance(dag, dict):
        return False

    # 1. Campos top-level presentes.
    for field in DAG_REQUIRED_FIELDS:
        if field not in dag:
            return False

    # 2. schema_version: int (no bool — bool es subtipo de int en Python).
    if not isinstance(dag["schema_version"], int) or isinstance(
        dag["schema_version"], bool
    ):
        return False

    # 3. last_event_id: str.
    if not isinstance(dag["last_event_id"], str):
        return False

    # 4. last_seq: int (no bool, no float).
    if not isinstance(dag["last_seq"], int) or isinstance(dag["last_seq"], bool):
        return False

    # 5. last_offset: int (no bool) — byte offset del ledger donde
    #    termina el cache (para el tail-read incremental).
    if not isinstance(dag["last_offset"], int) or isinstance(
        dag["last_offset"], bool
    ):
        return False

    # 6. last_hash: str — hash del ledger.log.last_hash.json al momento
    #    del build ("" si ausente).
    if not isinstance(dag["last_hash"], str):
        return False

    # 7. covered_archives: list[str] — archivos .gz en <ledger_dir>/archive/.
    ca = dag["covered_archives"]
    if not isinstance(ca, list):
        return False
    for archive_name in ca:
        if not isinstance(archive_name, str):
            return False

    # 8. writer_history: dict[str, list[list[str, int]]].
    wh = dag["writer_history"]
    if not isinstance(wh, dict):
        return False
    for file_path, entries in wh.items():
        if not isinstance(file_path, str):
            return False
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2:
                return False
            event_id, chain_position = entry[0], entry[1]
            if not isinstance(event_id, str):
                return False
            if not isinstance(chain_position, int) or isinstance(
                chain_position, bool
            ):
                return False

    # 9. history_positions: dict[str, list[int]].
    hp = dag["history_positions"]
    if not isinstance(hp, dict):
        return False
    for file_path, positions in hp.items():
        if not isinstance(file_path, str):
            return False
        if not isinstance(positions, list):
            return False
        for pos in positions:
            if not isinstance(pos, int) or isinstance(pos, bool):
                return False

    # 10. event_writes: dict[str, list[str]].
    ew = dag["event_writes"]
    if not isinstance(ew, dict):
        return False
    for event_id, writes in ew.items():
        if not isinstance(event_id, str):
            return False
        if not isinstance(writes, list):
            return False
        for w in writes:
            if not isinstance(w, str):
                return False

    # 11. event_reads: dict[str, list[str]].
    er = dag["event_reads"]
    if not isinstance(er, dict):
        return False
    for event_id, reads in er.items():
        if not isinstance(event_id, str):
            return False
        if not isinstance(reads, list):
            return False
        for r in reads:
            if not isinstance(r, str):
                return False

    # 12. event_meta: dict[str, {"event_type": str, "timestamp": str}].
    em = dag["event_meta"]
    if not isinstance(em, dict):
        return False
    for event_id, meta in em.items():
        if not isinstance(event_id, str):
            return False
        if not isinstance(meta, dict):
            return False
        if not isinstance(meta.get("event_type"), str):
            return False
        if not isinstance(meta.get("timestamp"), str):
            return False

    # 13. event_ids: list[str] (orden de ledger).
    ei = dag["event_ids"]
    if not isinstance(ei, list):
        return False
    for eid in ei:
        if not isinstance(eid, str):
            return False

    # 14. built_at: str.
    if not isinstance(dag["built_at"], str):
        return False

    return True