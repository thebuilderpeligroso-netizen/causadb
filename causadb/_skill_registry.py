"""F.13.4.3 — Skill Registry (ledger-based cache).

Cache reconstruible en disco para skills. **Ledger-first**: los skills
se persisten como eventos ``SKILL_CREATED`` / ``SKILL_PRUNED`` en el
ledger (única fuente de verdad histórica — Artículo I). El cache en
disco (``.causadb/skills/cache.json``) es RECONSTRUIBLE: se puede
borrar y regenerar via ``reconstruct_state()``.

Funciones públicas:
- ``register_skill(ledger_path, skill_dict, config=None) -> str``:
  Loggea ``SKILL_CREATED`` al ledger via ``LedgerWriter``. Genera
  ``skill_id`` (UUID) si falta. Retorna ``skill_id``.
- ``load_skills(ledger_path, types=None, config=None) -> list``:
  Replay el ledger via ``ReplayEngine``, retorna ``state["skills"]``.
  Si ``types``, filtra por ``skill_type``.
- ``prune_skills(ledger_path, max_tokens, config=None) -> list``:
  Carga skills via ``load_skills``. Si ``sum(token_count) > max_tokens``,
  recorre skills ordenados por ``confidence`` ASCENDING (menor primero)
  y loggea ``SKILL_PRUNED`` hasta que total <= max_tokens. Retorna
  lista de ``skill_ids`` pruneados.
- ``write_skills_cache(skills, cache_path) -> None``: Cache disk con
  ``flock`` (igual que ``_dag_cache.write_dag``).
- ``read_skills_cache(cache_path) -> Optional[list]``: Lee cache disk,
  ``None`` si corrupto (degradación suave — Artículo V).

Decisiones de diseño:

1. **Ledger-first, cache-second.** Toda mutación de skills pasa por
   ``LedgerWriter.append`` (Artículo I: Ledger Monism). El cache en
   disco es una optimización de lectura, no storage primario. Borrarlo
   no pierde datos — se reconstruye desde el ledger.

2. ``register_skill`` acepta un ``skill_dict`` con keys canónicas
   (``skill_type``, ``skill_name``, ``content``, ``token_count``,
   ``confidence``, ``source_session``, opcional ``skill_id``). Estas
   keys se mapean 1:1 al payload del evento ``SKILL_CREATED`` (que es
   lo que el handler en ``_replay_engine.py:273-284`` lee). Si
   ``skill_id`` falta, se genera un UUID.

3. ``load_skills`` SIEMPRE replay el ledger (via ``ReplayEngine``).
   No lee del cache por defecto — el cache es opt-in para callers que
   quieren optimizar (e.g. ``_cmd_watch``). Esto garantiza
   consistencia: el estado retornado es el estado ledger-real, no
   un snapshot potencialmente stale. Anti-teatro (Artículo IX): un
   test que mute ``load_skills`` para retornar ``[]`` sin replay debe
   fallar.

4. ``prune_skills`` ordena por ``confidence`` ASCENDING (menor
   confianza primero). Esto preserva los skills de mayor confianza
   cuando hay que recortar. Loggea ``SKILL_PRUNED`` por cada skill
   removido — el handler en ``_replay_engine.py:286-288`` los remueve
   de ``state["skills"]`` en replay. Anti-teatro: un test que mute
   ``prune_skills`` para selección random debe fallar (podría podar el
   de mayor confianza).

5. ``write_skills_cache`` / ``read_skills_cache`` siguen el patrón de
   ``_dag_cache.write_dag`` / ``read_dag``: lock file separado
   (``path + ".lock"``), ``fcntl.flock``, ``fsync``, hash de
   integridad (``cache_hash``). Degradación suave en el reader:
   retorna ``None`` ante cualquier corrupción.

6. ``SKILL_PRUNED`` payload lleva ``skill_id`` + ``reason`` (string
   humano). El handler de replay solo usa ``skill_id`` para filtrar;
   ``reason`` es metadata para auditoría.
"""

import fcntl
import hashlib
import json
import os
import uuid
from types import MappingProxyType
from typing import Any, Dict, List, Optional

from causadb._config import CausaDBConfig
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._replay_engine import ReplayEngine


# ---------------------------------------------------------------------------
# register_skill
# ---------------------------------------------------------------------------

def register_skill(
    ledger_path: str,
    skill_dict: Dict[str, Any],
    config: Optional[CausaDBConfig] = None,
) -> str:
    """Loggea un ``SKILL_CREATED`` event al ledger via LedgerWriter.

    Args:
        ledger_path: path absoluto del ledger.
        skill_dict: dict con keys canónicas:
            - ``skill_type`` (str, requerido)
            - ``skill_name`` (str, requerido)
            - ``content`` (str, requerido)
            - ``token_count`` (int, requerido)
            - ``confidence`` (float, requerido)
            - ``source_session`` (str, opcional)
            - ``skill_id`` (str, opcional — se genera UUID si falta)
        config: config opcional (si None, se crea default).

    Returns:
        El ``skill_id`` (UUID generado o el provisto).

    Raises:
        ValueError: si faltan keys requeridas en ``skill_dict``.

    Notes:
        - Ledger-first (Artículo I): la persistencia ES el evento en
          el ledger. No se escribe a ningún cache aquí — el cache es
          opt-in y se regenera desde el ledger.
        - El ``skill_id`` se genera aquí (no en el LedgerWriter) para
          que el caller pueda referenciarlo inmediatamente.
    """
    # Validar keys requeridas.
    required = ["skill_type", "skill_name", "content", "token_count", "confidence"]
    missing = [k for k in required if k not in skill_dict]
    if missing:
        raise ValueError(f"skill_dict missing required keys: {missing}")

    # Generar skill_id si falta.
    skill_id = skill_dict.get("skill_id") or str(uuid.uuid4())

    # Upsert por skill_name (BIT-CHR.103): si existe un skill vivo con el
    # mismo skill_name, emitir SKILL_PRUNED del existente ANTES del
    # SKILL_CREATED del nuevo. Coexistencia si skill_name es distinto
    # (Opcion A — agnosticismo tool estricto). No usar LedgerIndex (no
    # indexa por skill_name); replay parcial via load_skills.
    skill_name = skill_dict["skill_name"]
    existing = load_skills(ledger_path, config=config)
    collision = [s for s in existing if s.get("skill_name") == skill_name]
    if collision:
        writer_prune = LedgerWriter(ledger_path, config)
        for old in collision:
            old_id = old.get("skill_id")
            if not old_id:
                continue
            prune_payload = {
                "skill_id": old_id,
                "reason": f"replaced by new skill with same skill_name={skill_name!r}",
            }
            prune_event = CanonicalEvent(
                event_type=EventType.SKILL_PRUNED,
                ctx_id="skills",
                source="causadb:skill_registry",
                source_type="agent",
                payload=MappingProxyType(prune_payload),
                metadata=EventMetadata(
                    trace_id=old_id,
                    session_id="skills",
                ),
            )
            writer_prune.append(prune_event)

    # Ensamblar el payload del evento SKILL_CREATED. Las keys del
    # payload matchean 1:1 lo que el handler en _replay_engine.py lee
    # (skill_id, skill_type, skill_name, content, token_count,
    # confidence, source_session).
    payload = {
        "skill_id": skill_id,
        "skill_type": skill_dict["skill_type"],
        "skill_name": skill_dict["skill_name"],
        "content": skill_dict["content"],
        "token_count": skill_dict["token_count"],
        "confidence": skill_dict["confidence"],
        "source_session": skill_dict.get("source_session"),
    }

    writer = LedgerWriter(ledger_path, config)
    event = CanonicalEvent(
        event_type=EventType.SKILL_CREATED,
        ctx_id="skills",
        source="causadb:skill_registry",
        source_type="agent",
        payload=MappingProxyType(payload),
        metadata=EventMetadata(
            trace_id=skill_id,
            session_id=skill_dict.get("source_session") or "skills",
        ),
    )
    writer.append(event)

    return skill_id


# ---------------------------------------------------------------------------
# load_skills
# ---------------------------------------------------------------------------

def load_skills(
    ledger_path: str,
    types: Optional[List[str]] = None,
    config: Optional[CausaDBConfig] = None,
    limit: Optional[int] = None,
    order: str = "desc",
) -> List[Dict[str, Any]]:
    """Replay el ledger via ReplayEngine y retorna ``state["skills"]``.

    Args:
        ledger_path: path absoluto del ledger.
        types: si no None, filtra skills por ``skill_type`` (lista de
            tipos a incluir). Si None, retorna todos los skills.
        config: config opcional.
        limit: si no None, retorna solo los primeros ``limit`` skills
            despues de ordenar (paginacion). Si None, retorna todos.
        order: orden por ``timestamp`` — ``"desc"`` (default, mas
            recientes primero) o ``"asc"`` (mas antiguos primero).

    Returns:
        Lista de skill dicts (como los produce el handler de
        ``SKILL_CREATED`` en ``_replay_engine.py``: cada dict tiene
        ``skill_id``, ``skill_type``, ``skill_name``, ``content``,
        ``token_count``, ``confidence``, ``source_session``,
        ``timestamp``, ``event_id``).

    Notes:
        - SIEMPRE replay el ledger (no lee cache). Ledger-first.
        - Anti-teatro (Artículo IX): un test que mute esta función
          para retornar ``[]`` sin replay debe fallar (test #5 y #12).
    """
    engine = ReplayEngine(ledger_path, config)
    state = engine.reconstruct_state()
    skills = state.get("skills", [])

    if types is not None:
        # Filtrar por skill_type.
        types_set = set(types)
        skills = [s for s in skills if s.get("skill_type") in types_set]

    # Ordenar por timestamp (BIT-CHR.103). desc = mas recientes primero.
    # NOTA P4-A2: este contrato de orden está DUPLICADO en
    # ``cli/_cmd_revive.py::_run_revive`` (skills_precomputed desde
    # replay_state, que ya no pasa por acá para evitar un re-play extra).
    # Si cambiás el orden acá, sincronizá el sort de _run_revive.
    reverse = (order == "desc")
    skills = sorted(skills, key=lambda s: s.get("timestamp", ""), reverse=reverse)

    if limit is not None:
        skills = skills[:limit]

    return skills


# ---------------------------------------------------------------------------
# prune_skills
# ---------------------------------------------------------------------------

def prune_skills(
    ledger_path: str,
    max_tokens: int,
    config: Optional[CausaDBConfig] = None,
) -> List[str]:
    """Prunea skills de menor confidence hasta que total <= max_tokens.

    Args:
        ledger_path: path absoluto del ledger.
        max_tokens: presupuesto máximo de tokens. Si la suma de
            ``token_count`` de todos los skills excede este valor,
            se prunean skills empezando por el de menor ``confidence``.
        config: config opcional.

    Returns:
        Lista de ``skill_id`` pruneados (en el orden en que se
        prunearon: menor confidence primero).

    Notes:
        - Ledger-first: cada prune se loggea como ``SKILL_PRUNED``
          event. El handler en ``_replay_engine.py:286-288`` remueve
          el skill de ``state["skills"]`` en replay.
        - Orden: ``confidence`` ASCENDING (menor primero). Si hay
          empates, se preserva el orden de aparición (stable sort).
        - Anti-teatro (Artículo IX): un test que mute esta función
          para selección random debe fallar (test #7 y #11) — podría
          podar el de mayor confianza.
        - Si total <= max_tokens, no se prune nada y se retorna ``[]``.

    Nota post-BIT-CHR.103: el orden de empate de ``confidence`` cambió
    sutilmente — ``load_skills`` ahora devuelve DESC por default, lo que
    afecta el orden de empate en ``sorted(skills, key=confidence)`` de
    prune_skills. No introduce bug, pero el orden de prune en empates es
    distinto a pre-fix.
    """
    skills = load_skills(ledger_path, config=config)

    total_tokens = sum(s.get("token_count", 0) for s in skills)
    if total_tokens <= max_tokens:
        return []

    # Ordenar por confidence ASCENDING (menor primero). Stable sort
    # preserva orden de aparición en empates.
    sorted_skills = sorted(skills, key=lambda s: s.get("confidence", 0.0))

    pruned_ids: List[str] = []
    current_total = total_tokens

    writer = LedgerWriter(ledger_path, config)

    for skill in sorted_skills:
        if current_total <= max_tokens:
            break
        skill_id = skill.get("skill_id")
        if not skill_id:
            # Skill sin skill_id — no se puede prunear (no se puede
            # referenciar en el evento SKILL_PRUNED). Skip defensivo.
            continue
        token_count = skill.get("token_count", 0)

        payload = {
            "skill_id": skill_id,
            "reason": f"pruned: token budget exceeded (max={max_tokens})",
        }
        event = CanonicalEvent(
            event_type=EventType.SKILL_PRUNED,
            ctx_id="skills",
            source="causadb:skill_registry",
            source_type="agent",
            payload=MappingProxyType(payload),
            metadata=EventMetadata(
                trace_id=skill_id,
                session_id="skills",
            ),
        )
        writer.append(event)

        pruned_ids.append(skill_id)
        current_total -= token_count

    return pruned_ids


# ---------------------------------------------------------------------------
# write_skills_cache / read_skills_cache
# ---------------------------------------------------------------------------

def _compute_cache_hash(skills: list) -> str:
    """SHA-256 de ``json.dumps(skills, sort_keys=True)``.

    Helper para integridad del cache (igual que ``compute_dag_hash``).
    """
    payload = json.dumps(skills, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def write_skills_cache(skills: list, cache_path: str) -> None:
    """Escribe el cache de skills a disco con flock + fsync.

    Sigue el patrón de ``_dag_cache.write_dag``:
    - Lock file separado: ``cache_path + ".lock"``.
    - ``fcntl.flock(LOCK_EX)`` alrededor de la escritura.
    - ``fsync`` antes de liberar el lock.
    - ``cache_hash`` (SHA-256) incluido en el JSON para verificación
      de integridad en el reader.

    Args:
        skills: lista de skill dicts (JSON-serializable).
        cache_path: path absoluto del archivo cache.
    """
    lock_path = cache_path + ".lock"
    if not os.path.exists(lock_path):
        open(lock_path, "a").close()

    # Computar hash SIN el campo cache_hash (evitar recursión).
    cache_hash = _compute_cache_hash(skills)

    payload_dict = {
        "skills": skills,
        "cache_hash": cache_hash,
    }
    payload = json.dumps(payload_dict, sort_keys=True)

    with open(lock_path, "a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            # Asegurar que el directorio padre existe.
            parent = os.path.dirname(cache_path)
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            with open(cache_path, "w") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_skills_cache(cache_path: str) -> Optional[list]:
    """Lee el cache de skills de disco con flock compartido.

    Degradación suave (Artículo V): ante cualquier corrupción o
    problema de lectura, retorna ``None``. El caller debe rebuild
    on-the-fly desde el ledger.

    Casos que retornan ``None``:
    - El archivo no existe (cache frío).
    - JSON parse error.
    - ``cache_hash`` mismatch (contenido alterado).
    - Estructura inválida (no es dict, o ``skills`` no es lista).

    Args:
        cache_path: path absoluto del archivo cache.

    Returns:
        Lista de skill dicts, o ``None`` si el cache no se puede usar.
    """
    if not os.path.exists(cache_path):
        return None

    lock_path = cache_path + ".lock"
    if not os.path.exists(lock_path):
        try:
            open(lock_path, "a").close()
        except OSError:
            return None

    try:
        with open(lock_path, "a") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
            try:
                with open(cache_path, "r") as f:
                    raw = f.read()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except (OSError, IOError):
        return None

    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(loaded, dict):
        return None

    stored_hash = loaded.get("cache_hash")
    if not isinstance(stored_hash, str):
        return None

    skills = loaded.get("skills")
    if not isinstance(skills, list):
        return None

    # Verificación de integridad: re-computar hash sobre skills.
    recomputed = _compute_cache_hash(skills)
    if recomputed != stored_hash:
        return None

    return skills


# ---------------------------------------------------------------------------
# distill_post_harvest (Fase 14.1)
# ---------------------------------------------------------------------------

def distill_post_harvest(
    ledger_path: str,
    source_type: str,
    config: Optional[CausaDBConfig] = None,
) -> dict:
    """Run distill on the ledger and register resulting skills.

    Filters:
        - Includes governance: el skill ``governance`` (decisiones
          origin='distill'+'agent' desde GOVERNANCE_DECISION events) se
          registra como cualquier otro skill (FIX.GOV-AUTO-3 — hoy el
          harvest deriva decisiones automáticamente, el skill las refleja).
        - Exclude skills with confidence <= 0 (e.g., conventions placeholder).
        - Before registering a skill, prunes ALL previous skills of the same
          type to prevent accumulation (one active version per type).

    Key mapping: distill() outputs {type, name, content, token_count, confidence}
    → register_skill() expects {skill_type, skill_name, ...}
    Follow the exact pattern from _cmd_watch.py:_auto_distill (lines 254-262).

    Returns:
        dict: {"status": "ok", "skills_registered": int, "skill_ids": list}
               or {"status": "skipped", "reason": str}
    """
    from causadb._distill import distill

    config = config or CausaDBConfig(ledger_path=ledger_path)

    try:
        result = distill(ledger_path, config)
        skills = result.get("skills", [])
    except Exception as e:
        return {"status": "skipped", "reason": f"distill failed: {e}"}

    registered_ids = []
    for skill in skills:
        skill_type = skill.get("type")
        confidence = skill.get("confidence", 0.0)

        # Skip low-confidence (placeholder, conventions = 0.0)
        if confidence <= 0:
            continue

        # BIT-CHR.103: NO prune por tipo. register_skill hace upsert por
        # skill_name (reemplaza si coincide, coexiste si no). Agnosticismo
        # tool estricto: un harvester no destruye skills de otro.
        # Key mapping: follow _auto_distill pattern
        payload = {
            "skill_type": skill.get("type"),
            "skill_name": skill.get("name"),
            "content": skill.get("content"),
            "token_count": skill.get("token_count"),
            "confidence": skill.get("confidence"),
            "source_session": f"harvest:{source_type}",
        }
        skill_id = register_skill(ledger_path, payload, config)
        registered_ids.append(skill_id)

    return {
        "status": "ok",
        "skills_registered": len(registered_ids),
        "skill_ids": registered_ids,
    }
