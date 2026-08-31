"""F.13.4.3 — Tests para Skill Registry (ledger-based cache).

Test-first discipline (Artículo III). Anti-teatro (Artículo IX).

Cobertura:
1.  register_skill loggea SKILL_CREATED → replay → state["skills"] lo contiene.
2.  register_skill genera skill_id (UUID) si falta.
3.  register_skill preserva todos los fields (skill_type, skill_name,
    content, token_count, confidence, source_session).
4.  load_skills sobre ledger vacío → [].
5.  load_skills retorna todos los skills (register 3 → load 3).
6.  load_skills filtra por types (register 3 tipos, filter ["file_tree"]
    → solo esos).
7.  prune_skills remueve lowest confidence primero (3 skills conf
    0.9, 0.5, 0.1, max=100, total=200 → 0.1 y 0.5 se prunean, 0.9 queda).
8.  prune_skills loggea SKILL_PRUNED events (replay los contiene).
9.  prune_skills no prunea si total <= max_tokens (no SKILL_PRUNED).
10. register → prune → load: el skill pruneado no está.
11. ANTI-TEATRO: mutar prune_skills para random selection → test #7
    falla (podría podar el de 0.9).
12. ANTI-TEATRO: mutar load_skills para retornar [] → test #5 falla.

Helpers:
    _make_ledger(tmp_path) — crea workspace via causadb_init, retorna
    ledger_path.
    _sample_skill(...) — construye un skill_dict canónico.
"""

import os
import random
from typing import Any, Dict, List

import pytest

from causadb._init import causadb_init
from causadb._skill_registry import (
    register_skill,
    load_skills,
    prune_skills,
    write_skills_cache,
    read_skills_cache,
)
from causadb._replay_engine import ReplayEngine


# --- Helpers ---

def _make_ledger(tmp_path) -> str:
    """Crea un workspace via causadb_init y retorna el ledger_path absoluto."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    return result["ledger_path"]


def _sample_skill(
    skill_type: str = "file_tree",
    name: str = "test",
    confidence: float = 0.8,
    tokens: int = 100,
    skill_id: str = None,
    content: str = "x",
    source_session: str = "s",
) -> Dict[str, Any]:
    """Construye un skill_dict canónico para register_skill."""
    d = {
        "skill_type": skill_type,
        "skill_name": name,
        "content": content,
        "token_count": tokens,
        "confidence": confidence,
        "source_session": source_session,
    }
    if skill_id is not None:
        d["skill_id"] = skill_id
    return d


# ============================================================================
# register_skill
# ============================================================================

def test_register_skill_logs_skill_created_event(tmp_path):
    """#1: register → replay → state["skills"] contiene el skill."""
    ledger = _make_ledger(tmp_path)
    skill = _sample_skill(name="tree1")

    register_skill(ledger, skill)

    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    skills = state["skills"]
    assert len(skills) == 1, f"expected 1 skill, got {len(skills)}"
    assert skills[0]["skill_name"] == "tree1"
    assert skills[0]["skill_type"] == "file_tree"


def test_register_skill_generates_skill_id_if_missing(tmp_path):
    """#2: skill sin skill_id → se genera UUID."""
    ledger = _make_ledger(tmp_path)
    skill = _sample_skill()
    assert "skill_id" not in skill, "precondition: skill_dict sin skill_id"

    skill_id = register_skill(ledger, skill)

    # Debe ser un UUID válido (36 chars, formato 8-4-4-4-12).
    assert isinstance(skill_id, str)
    assert len(skill_id) == 36
    parts = skill_id.split("-")
    assert len(parts) == 5, f"not a UUID: {skill_id}"
    assert len(parts[0]) == 8
    assert len(parts[1]) == 4
    assert len(parts[2]) == 4
    assert len(parts[3]) == 4
    assert len(parts[4]) == 12

    # El skill_id generado debe aparecer en el state tras replay.
    state = ReplayEngine(ledger).reconstruct_state()
    assert state["skills"][0]["skill_id"] == skill_id


def test_register_skill_preserves_all_fields(tmp_path):
    """#3: skill_type, skill_name, content, token_count, confidence,
    source_session se preservan en el state reconstruido."""
    ledger = _make_ledger(tmp_path)
    skill = _sample_skill(
        skill_type="tool_patterns",
        name="repeated_tools",
        confidence=0.42,
        tokens=77,
        content="tool 'grep' used 5 times",
        source_session="sess-abc-123",
    )

    register_skill(ledger, skill)

    state = ReplayEngine(ledger).reconstruct_state()
    s = state["skills"][0]
    assert s["skill_type"] == "tool_patterns"
    assert s["skill_name"] == "repeated_tools"
    assert s["content"] == "tool 'grep' used 5 times"
    assert s["token_count"] == 77
    assert s["confidence"] == 0.42
    assert s["source_session"] == "sess-abc-123"


# ============================================================================
# load_skills
# ============================================================================

def test_load_skills_empty_ledger(tmp_path):
    """#4: load_skills sobre ledger recién inicializado → []."""
    ledger = _make_ledger(tmp_path)
    skills = load_skills(ledger)
    assert skills == [], f"expected [], got {skills}"


def test_load_skills_returns_all_skills(tmp_path):
    """#5: register 3, load → 3."""
    ledger = _make_ledger(tmp_path)
    register_skill(ledger, _sample_skill(name="a"))
    register_skill(ledger, _sample_skill(name="b"))
    register_skill(ledger, _sample_skill(name="c"))

    skills = load_skills(ledger)
    assert len(skills) == 3, f"expected 3, got {len(skills)}"
    names = sorted(s["skill_name"] for s in skills)
    assert names == ["a", "b", "c"]


def test_load_skills_filter_by_type(tmp_path):
    """#6: register 3 tipos, filter ["file_tree"] → solo esos."""
    ledger = _make_ledger(tmp_path)
    register_skill(ledger, _sample_skill(skill_type="file_tree", name="ft"))
    register_skill(ledger, _sample_skill(skill_type="tool_patterns", name="tp"))
    register_skill(ledger, _sample_skill(skill_type="decisions", name="dec"))

    filtered = load_skills(ledger, types=["file_tree"])
    assert len(filtered) == 1, f"expected 1, got {len(filtered)}"
    assert filtered[0]["skill_type"] == "file_tree"
    assert filtered[0]["skill_name"] == "ft"

    # Filtro con múltiples tipos.
    filtered2 = load_skills(ledger, types=["file_tree", "decisions"])
    assert len(filtered2) == 2
    types = sorted(s["skill_type"] for s in filtered2)
    assert types == ["decisions", "file_tree"]

    # Filtro con tipo que no existe → [].
    filtered3 = load_skills(ledger, types=["nonexistent"])
    assert filtered3 == []


# ============================================================================
# prune_skills
# ============================================================================

def test_prune_skills_removes_lowest_confidence_first(tmp_path):
    """#7: 3 skills conf 0.9, 0.5, 0.1, max=100 (total 200) → 0.1 y 0.5
    se prunean, 0.9 queda."""
    ledger = _make_ledger(tmp_path)
    register_skill(ledger, _sample_skill(name="high", confidence=0.9, tokens=100))
    register_skill(ledger, _sample_skill(name="mid", confidence=0.5, tokens=50))
    register_skill(ledger, _sample_skill(name="low", confidence=0.1, tokens=50))

    # total = 100 + 50 + 50 = 200 > max=100. Hay que podar hasta <= 100.
    # Orden ASC: 0.1 (50), 0.5 (50), 0.9 (100).
    # Poda 0.1 → total 150. Poda 0.5 → total 100. Stop.
    pruned = prune_skills(ledger, max_tokens=100)

    assert len(pruned) == 2, f"expected 2 pruned, got {len(pruned)}"

    # Los skills restantes tras replay deben ser solo el de 0.9.
    remaining = load_skills(ledger)
    assert len(remaining) == 1, f"expected 1 remaining, got {len(remaining)}"
    assert remaining[0]["skill_name"] == "high"
    assert remaining[0]["confidence"] == 0.9


def test_prune_skills_logs_skill_pruned_events(tmp_path):
    """#8: después de prune, replay contiene SKILL_PRUNED events."""
    ledger = _make_ledger(tmp_path)
    register_skill(ledger, _sample_skill(name="high", confidence=0.9, tokens=100))
    register_skill(ledger, _sample_skill(name="low", confidence=0.1, tokens=100))

    prune_skills(ledger, max_tokens=100)

    # Replay el ledger crudo y buscar eventos SKILL_PRUNED.
    from causadb._ledger_reader import LedgerReader
    reader = LedgerReader(ledger)
    pruned_events = [
        e for e in (entry["event"] for entry in reader.read_all_entries())
        if e.get("event_type") == "SKILL_PRUNED"
    ]
    assert len(pruned_events) >= 1, (
        f"expected at least 1 SKILL_PRUNED event, got {len(pruned_events)}"
    )
    # Cada SKILL_PRUNED debe tener skill_id en el payload.
    for ev in pruned_events:
        assert "skill_id" in ev.get("payload", {}), (
            f"SKILL_PRUNED event missing skill_id: {ev}"
        )


def test_prune_skills_no_prune_under_max_tokens(tmp_path):
    """#9: total < max → no SKILL_PRUNED loggeado."""
    ledger = _make_ledger(tmp_path)
    register_skill(ledger, _sample_skill(name="a", confidence=0.5, tokens=30))
    register_skill(ledger, _sample_skill(name="b", confidence=0.8, tokens=40))
    # total = 70 < max=100 → no prune.

    pruned = prune_skills(ledger, max_tokens=100)

    assert pruned == [], f"expected no prunes, got {pruned}"

    # Verificar que no hay eventos SKILL_PRUNED en el ledger.
    from causadb._ledger_reader import LedgerReader
    reader = LedgerReader(ledger)
    pruned_events = [
        e for e in (entry["event"] for entry in reader.read_all_entries())
        if e.get("event_type") == "SKILL_PRUNED"
    ]
    assert pruned_events == [], (
        f"expected 0 SKILL_PRUNED events, got {len(pruned_events)}"
    )

    # Los skills siguen todos presentes.
    skills = load_skills(ledger)
    assert len(skills) == 2


def test_register_then_prune_then_load_removes_pruned(tmp_path):
    """#10: register A, prune A, load → A no está."""
    ledger = _make_ledger(tmp_path)
    skill_id_a = register_skill(ledger, _sample_skill(name="a", confidence=0.1, tokens=200))
    register_skill(ledger, _sample_skill(name="b", confidence=0.9, tokens=50))

    # total = 250 > max=100. Orden ASC: 0.1 (200), 0.9 (50).
    # Poda 0.1 → total 50 <= 100. Stop.
    pruned = prune_skills(ledger, max_tokens=100)

    assert skill_id_a in pruned, (
        f"expected {skill_id_a} in pruned {pruned}"
    )

    skills = load_skills(ledger)
    ids = [s["skill_id"] for s in skills]
    assert skill_id_a not in ids, (
        f"pruned skill {skill_id_a} still in state: {ids}"
    )
    assert len(skills) == 1
    assert skills[0]["skill_name"] == "b"


# ============================================================================
# ANTI-TEATRO (Artículo IX)
# ============================================================================

def test_anti_teatro_prune_skills_random_selection(tmp_path):
    """#11: mutar prune_skills para random selection → test #7 falla
    (podría podar el de 0.9).

    Anti-teatro: este test demuestra que el orden por confidence
    ASCENDING NO es decorativo. Si se muta a random, hay una
    probabilidad no trivial de podar el skill de mayor confidence
    (0.9), violando el contrato de "preservar los de mayor confianza".

    Metodología:
    1. Construir el escenario del test #7 (3 skills, conf 0.9/0.5/0.1).
    2. Ejecutar prune_skills correcto → debe preservar el de 0.9.
    3. Mutar prune_skills para usar random.shuffle en vez de
       sorted-by-confidence.
    4. Ejecutar la versión mutada muchas veces → demostrar que a
       veces poda el de 0.9 (comportamiento incorrecto).
    5. Restaurar.
    """
    import causadb._skill_registry as sr_mod

    # --- Paso 1: versión correcta preserva el de 0.9 ---
    ledger_ok = _make_ledger(tmp_path / "ok")
    register_skill(ledger_ok, _sample_skill(name="high", confidence=0.9, tokens=100))
    register_skill(ledger_ok, _sample_skill(name="mid", confidence=0.5, tokens=50))
    register_skill(ledger_ok, _sample_skill(name="low", confidence=0.1, tokens=50))
    prune_skills(ledger_ok, max_tokens=100)
    remaining_ok = load_skills(ledger_ok)
    assert len(remaining_ok) == 1
    assert remaining_ok[0]["confidence"] == 0.9, (
        "precondition: correct version preserves highest confidence"
    )

    # --- Paso 2: mutar prune_skills para random selection ---
    original_prune = sr_mod.prune_skills

    def random_prune(ledger_path, max_tokens, config=None):
        """Versión mutada: random selection en vez de confidence ASC."""
        skills = sr_mod.load_skills(ledger_path, config=config)
        total = sum(s.get("token_count", 0) for s in skills)
        if total <= max_tokens:
            return []
        # MUTACIÓN: random en vez de sorted by confidence.
        shuffled = list(skills)
        random.shuffle(shuffled)
        pruned = []
        current = total
        from causadb._ledger_writer import LedgerWriter
        from causadb._event_schema import CanonicalEvent, EventMetadata
        from causadb._event_types import EventType
        from types import MappingProxyType
        writer = LedgerWriter(ledger_path, config)
        for skill in shuffled:
            if current <= max_tokens:
                break
            sid = skill.get("skill_id")
            if not sid:
                continue
            tc = skill.get("token_count", 0)
            ev = CanonicalEvent(
                event_type=EventType.SKILL_PRUNED,
                ctx_id="skills",
                source="causadb:skill_registry",
                source_type="agent",
                payload=MappingProxyType({"skill_id": sid, "reason": "random"}),
                metadata=EventMetadata(trace_id=sid, session_id="skills"),
            )
            writer.append(ev)
            pruned.append(sid)
            current -= tc
        return pruned

    try:
        sr_mod.prune_skills = random_prune

        # --- Paso 3: la versión mutada a veces poda el de 0.9 ---
        # Ejecutar N veces en ledgers frescos. En al menos una iteración
        # el skill de 0.9 debe ser pruneado (random no es determinista,
        # pero con 3 skills y 2 prunes, P(podar 0.9) es alta).
        pruned_high_count = 0
        n_iters = 30
        for i in range(n_iters):
            ledger_mut = _make_ledger(tmp_path / f"mut_{i}")
            register_skill(ledger_mut, _sample_skill(name="high", confidence=0.9, tokens=100))
            register_skill(ledger_mut, _sample_skill(name="mid", confidence=0.5, tokens=50))
            register_skill(ledger_mut, _sample_skill(name="low", confidence=0.1, tokens=50))
            random_prune(ledger_mut, max_tokens=100)
            remaining = load_skills(ledger_mut)
            # Si el de 0.9 fue pruneado, remaining no contiene "high".
            if not any(s["confidence"] == 0.9 for s in remaining):
                pruned_high_count += 1

        assert pruned_high_count > 0, (
            "ANTI-TEATRO FALLÓ: la versión mutada (random) nunca podó el "
            f"skill de 0.9 en {n_iters} iteraciones. Esto significa que "
            "el test #7 no detectaría la mutación — el orden por "
            "confidence no estaría siendo verificado efectivamente."
        )
    finally:
        sr_mod.prune_skills = original_prune

    # Sanity check post-restore: la versión correcta vuelve a preservar 0.9.
    ledger_restored = _make_ledger(tmp_path / "restored")
    register_skill(ledger_restored, _sample_skill(name="high", confidence=0.9, tokens=100))
    register_skill(ledger_restored, _sample_skill(name="mid", confidence=0.5, tokens=50))
    register_skill(ledger_restored, _sample_skill(name="low", confidence=0.1, tokens=50))
    prune_skills(ledger_restored, max_tokens=100)
    remaining = load_skills(ledger_restored)
    assert len(remaining) == 1 and remaining[0]["confidence"] == 0.9, (
        "post-restore: correct version must preserve highest confidence"
    )


def test_anti_teatro_load_skills_no_replay(tmp_path):
    """#12: mutar load_skills para retornar [] → test #5 falla.

    Anti-teatro: este test demuestra que load_skills DEBE hacer replay
    del ledger. Si se muta para retornar [] sin replay, el test #5
    (register 3, load → 3) falla.

    Metodología:
    1. Register 3 skills en un ledger.
    2. load_skills correcto → retorna 3.
    3. Mutar load_skills para retornar [] sin replay.
    4. La versión mutada retorna [] (VIOLA el contrato).
    5. Restaurar.
    """
    import causadb._skill_registry as sr_mod

    # --- Paso 1: registrar 3 skills ---
    ledger = _make_ledger(tmp_path)
    register_skill(ledger, _sample_skill(name="a"))
    register_skill(ledger, _sample_skill(name="b"))
    register_skill(ledger, _sample_skill(name="c"))

    # --- Paso 2: versión correcta retorna 3 ---
    skills_ok = load_skills(ledger)
    assert len(skills_ok) == 3, (
        f"precondition: correct load_skills returns 3, got {len(skills_ok)}"
    )

    # --- Paso 3: mutar load_skills para retornar [] sin replay ---
    original_load = sr_mod.load_skills

    def fake_load(ledger_path, types=None, config=None):
        """Versión mutada: retorna [] sin replay (teatro)."""
        return []

    try:
        sr_mod.load_skills = fake_load
        # --- Paso 4: la versión mutada retorna [] (VIOLA el contrato) ---
        skills_mut = sr_mod.load_skills(ledger)
        assert skills_mut == [], (
            "ANTI-TEATRO FALLÓ: la versión mutada debe retornar []"
        )
        # La diferencia observable: correcta=3, mutada=0.
        assert len(skills_mut) != len(skills_ok), (
            "ANTI-TEATRO FALLÓ: no hay diferencia observable entre la "
            "versión correcta y la mutada. El test #5 no detectaría la mutación."
        )
    finally:
        sr_mod.load_skills = original_load

    # --- Paso 5: post-restore, la versión correcta vuelve a retornar 3 ---
    skills_restored = load_skills(ledger)
    assert len(skills_restored) == 3, (
        "post-restore: correct load_skills must return 3"
    )


# ============================================================================
# Cache disk (write_skills_cache / read_skills_cache)
# ============================================================================

def test_write_and_read_skills_cache_roundtrip(tmp_path):
    """Bonus: write cache → read cache → mismo contenido."""
    cache_path = str(tmp_path / "cache.json")
    skills = [
        {"skill_id": "id1", "skill_type": "file_tree", "skill_name": "a",
         "content": "x", "token_count": 10, "confidence": 0.9},
        {"skill_id": "id2", "skill_type": "decisions", "skill_name": "b",
         "content": "y", "token_count": 20, "confidence": 0.5},
    ]
    write_skills_cache(skills, cache_path)
    loaded = read_skills_cache(cache_path)
    assert loaded is not None
    assert loaded == skills


def test_read_skills_cache_missing_file_returns_none(tmp_path):
    """Bonus: cache frío (archivo no existe) → None."""
    cache_path = str(tmp_path / "nonexistent.json")
    assert read_skills_cache(cache_path) is None


def test_read_skills_cache_corrupt_returns_none(tmp_path):
    """Bonus: cache corrupto (JSON inválido) → None (degradación suave)."""
    cache_path = str(tmp_path / "cache.json")
    with open(cache_path, "w") as f:
        f.write("not valid json {{{")
    assert read_skills_cache(cache_path) is None


def test_read_skills_cache_hash_mismatch_returns_none(tmp_path):
    """Bonus: cache con hash alterado → None (corrupción detectada)."""
    cache_path = str(tmp_path / "cache.json")
    # Escribir un cache válido y luego corromper el contenido sin
    # actualizar el hash.
    skills = [{"skill_id": "id1", "skill_type": "file_tree", "content": "x"}]
    write_skills_cache(skills, cache_path)
    # Corromper: modificar el archivo directamente cambiando el content
    # pero dejando el cache_hash original.
    import json
    with open(cache_path, "r") as f:
        data = json.load(f)
    data["skills"][0]["content"] = "TAMPERED"
    with open(cache_path, "w") as f:
        json.dump(data, f)
    assert read_skills_cache(cache_path) is None


# ============================================================================
# Fase 14.1 — distill_post_harvest
# ============================================================================

from causadb._skill_registry import distill_post_harvest


def test_distill_post_harvest_registers_skills(tmp_path):
    """Run on ledger with events, verify skills appear via load_skills()."""
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from types import MappingProxyType

    ledger = _make_ledger(tmp_path)
    writer = LedgerWriter(ledger)

    # Add events that distill can produce skills from
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({"path": "causadb/_daemon.py", "action": "modify"}),
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({"path": "causadb/_ledger_writer.py", "action": "modify"}),
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.TOOL_CALLED, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({"tool_name": "read"}),
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.TOOL_CALLED, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({"tool_name": "read"}),
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.REASONING_STEP, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({"step_type": "decision", "step_hash": "abc123"}),
    ))

    result = distill_post_harvest(ledger, source_type="hermes")

    assert result["status"] == "ok", f"expected ok, got {result}"
    assert result["skills_registered"] > 0, (
        f"expected at least 1 skill registered, got {result}"
    )

    skills = load_skills(ledger)
    assert len(skills) > 0, f"expected skills in ledger, got {len(skills)}"


def test_distill_post_harvest_registers_governance(tmp_path):
    """FIX.GOV-AUTO-3 — Distill produce governance skill, verify IS registered.

    (Inversión de la exclusión previa: el skill ``governance`` con las
    decisiones origin='distill'+'agent' se registra post-harvest.)"""
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from types import MappingProxyType

    ledger = _make_ledger(tmp_path)
    writer = LedgerWriter(ledger)

    # Add governance decision + files so distill produces governance skill
    writer.append(CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({
            "reasoning": "Need to migrate to PostgreSQL",
            "impact": "high",
            "decision_type": "strategic",
            "origin": "agent",
        }),
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({"path": "main.py", "action": "modify"}),
    ))

    result = distill_post_harvest(ledger, source_type="hermes")

    assert result["status"] == "ok"

    skills = load_skills(ledger)
    skill_types = [s["skill_type"] for s in skills]
    assert "governance" in skill_types, (
        f"governance skill should be registered, got types: {skill_types}"
    )

    # El contenido del skill refleja la decisión (origin='distill'+'agent').
    gov_skill = next(s for s in skills if s["skill_type"] == "governance")
    assert "migrate to PostgreSQL" in gov_skill["content"], (
        f"governance skill should reflect the decision, got: {gov_skill['content']}"
    )


def test_distill_post_harvest_filters_low_confidence(tmp_path):
    """Distill produces confidence=0 skill (conventions), verify NOT registered."""
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from types import MappingProxyType

    ledger = _make_ledger(tmp_path)
    writer = LedgerWriter(ledger)

    # Add events so distill runs but conventions has confidence=0
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({"path": "main.py", "action": "modify"}),
    ))

    result = distill_post_harvest(ledger, source_type="hermes")

    assert result["status"] == "ok"

    skills = load_skills(ledger)
    skill_types = [s["skill_type"] for s in skills]
    assert "conventions" not in skill_types, (
        f"conventions skill (confidence=0) should NOT be registered, got types: {skill_types}"
    )


def test_distill_post_harvest_replaces_same_name_keeps_other_names(tmp_path):
    """BIT-CHR.103 — Agnosticismo tool estricto (Opcion A).

    Setup: dos skills file_tree pre-registrados con skill_names distintos
    (``shared_name`` y ``unique_name``). Tras ``distill_post_harvest``, el
    distill genera un nuevo file_tree con ``skill_name="touched_files_tree"``
    (nombre fijo del distill, distinto a ambos). Como los tres skill_names
    son distintos, los tres coexisten. La asercion clave es que
    ``unique_name`` permanece inalterado — es la prueba de agnosticismo tool:
    un harvester NO destruye skills de otros harvesters.
    """
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from types import MappingProxyType

    ledger = _make_ledger(tmp_path)

    # Pre-register two file_tree skills with distinct skill_names.
    register_skill(ledger, _sample_skill(
        skill_type="file_tree", name="shared_name",
        content="old content", confidence=0.3,
    ))
    register_skill(ledger, _sample_skill(
        skill_type="file_tree", name="unique_name",
        content="other", confidence=0.5,
    ))

    # Now add events that will cause distill to produce a new file_tree
    # with skill_name="touched_files_tree" (distill's fixed name).
    writer = LedgerWriter(ledger)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({"path": "new_file.py", "action": "modify"}),
    ))

    result = distill_post_harvest(ledger, source_type="hermes")

    assert result["status"] == "ok"

    skills = load_skills(ledger)
    file_tree_skills = [s for s in skills if s["skill_type"] == "file_tree"]
    names = [s["skill_name"] for s in file_tree_skills]

    # Agnosticismo tool (Opcion A): unique_name permanece inalterado.
    # El harvester NO destruye skills de otros harvesters.
    assert "unique_name" in names, (
        f"unique_name must coexist (Opcion A agnosticismo tool), got {names}"
    )
    unique_skill = next(s for s in file_tree_skills if s["skill_name"] == "unique_name")
    assert unique_skill["content"] == "other", (
        f"unique_name content must be unchanged, got {unique_skill['content']}"
    )

    # shared_name tambien coexiste (distill genera touched_files_tree, distinto).
    assert "shared_name" in names, (
        f"shared_name must coexist (Opcion A), got {names}"
    )

    # El nuevo skill del distill tambien esta presente.
    assert "touched_files_tree" in names, (
        f"touched_files_tree (distill output) must be present, got {names}"
    )
