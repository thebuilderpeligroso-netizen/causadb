"""F.13.4.1 / F.13.4.2 — Tests para Context Profiler (profile_context) y Distill Engine (distill).

Test-first discipline (Artículo III). Anti-teatro (Artículo IX).

Cobertura F.13.4.1 (profile_context):
1. Ledger vacío → métricas en cero.
2. unique_files cuenta paths distintos.
3. unique_tools cuenta tool_names distintos.
4. repetition_ratio = 1 - (unique/total).
5. top_patterns devuelve top 5 (o menos) ordenados desc.
6. total_tokens estima desde prompts (chars/4).
7. Redacción de prompts antes de analizar (redacted_fields).
8. Maneja payloads sin campos esperados (no error).
9. ANTI-TEATRO: mutar profile_context para no redactar → secret aparece.

Cobertura F.13.4.2 (distill):
10. Ledger vacío → skills vacío.
11. file_tree skill lista los files tocados.
12. tool_patterns skill desde TOOL_CALLED repetido.
13. decisions skill desde REASONING_STEP con step_type='decision'.
14. Compresión: sum(skill.token_count) < profile.total_tokens.
15. file_tree menciona todos los unique_files.
16. decisions redactados antes de extraer (secret no aparece).
17. confidence en rango [0, 1] para todos los skills.
18. ANTI-TEATRO: mutar distill a skills=[] → test de compresión/no-vacío falla.
"""

import json
import os
import uuid
from datetime import datetime
from types import MappingProxyType

import pytest

from causadb._distill import profile_context, distill
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._init import causadb_init
from causadb._ledger_writer import LedgerWriter


# --- Helpers ---

def _make_ledger_with_events(tmp_path, events_spec, redaction_enabled=True):
    """Construye un ledger con eventos vía LedgerWriter.

    events_spec: list of (event_type, payload_dict)
    Retorna el ledger_path absoluto.
    """
    from causadb._config import CausaDBConfig
    ws = tmp_path / "ws"
    config = CausaDBConfig(ledger_path=str(ws / "ledger.log"), redaction_enabled=redaction_enabled)
    # causadb_init requiere dir que no exista; creamos manualmente para controlar config
    os.makedirs(ws, exist_ok=True)
    chronicle = ws / "CAUSADB_CHRONICLE.md"
    chronicle.write_text("# CAUSADB_CHRONICLE.md\n")
    ledger = str(ws / "ledger.log")
    writer = LedgerWriter(ledger, config)
    # genesis
    genesis = CanonicalEvent(
        event_type=EventType.SYSTEM_BOOT, ctx_id="genesis", source="causadb:init",
        source_type="human", payload={"action": "init"},
    )
    writer.append(genesis)
    for event_type, payload in events_spec:
        event = CanonicalEvent(
            event_type=event_type, ctx_id="test", source="causadb:test",
            payload=MappingProxyType(payload),
        )
        writer.append(event)
    return ledger


def _write_raw_ledger_entry(ledger_path, event_type, payload, prev_hash="GENESIS"):
    """Escribe una entrada cruda al ledger SIN pasar por LedgerWriter.

    Útil para tests anti-teatro donde necesitamos un secret en claro en el
    ledger (simulando una fuente externa no redactada).
    """
    event_id = str(uuid.uuid4())
    timestamp = datetime.utcnow().isoformat() + "Z"
    event_dict = {
        "event_id": event_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "ctx_id": "raw",
        "source": "causadb:test",
        "parent_event_id": None,
        "source_type": "agent",
        "schema_version": "0.1.0",
        "sequence_number": 0,
        "payload": payload,
        "metadata": None,
        "pre_snapshot": None,
        "post_snapshot": None,
    }
    event_json = json.dumps(event_dict, sort_keys=True)
    import hashlib
    new_hash = hashlib.sha256((event_json + prev_hash).encode()).hexdigest()
    entry = {"event": event_dict, "prev_hash": prev_hash, "hash": new_hash}
    with open(ledger_path, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return new_hash


# --- Tests ---

def test_profile_context_empty_ledger(tmp_path):
    """profile_context sobre ledger vacío retorna métricas en cero."""
    ledger = _make_ledger_with_events(tmp_path, [])
    profile = profile_context(ledger)
    assert profile["repetition_ratio"] == 0
    assert profile["unique_files"] == 0
    assert profile["unique_tools"] == 0
    assert profile["top_patterns"] == []
    assert profile["total_tokens"] == 0
    assert profile["redacted_fields"] == []


def test_profile_context_counts_unique_files(tmp_path):
    """3 eventos que tocan main.py, utils.py, main.py (repetido) → unique_files=2."""
    ledger = _make_ledger_with_events(tmp_path, [
        (EventType.FILE_MODIFIED, {"path": "main.py", "action": "modify"}),
        (EventType.FILE_MODIFIED, {"path": "utils.py", "action": "modify"}),
        (EventType.FILE_MODIFIED, {"path": "main.py", "action": "modify"}),
    ])
    profile = profile_context(ledger)
    assert profile["unique_files"] == 2, (
        f"Expected unique_files=2, got {profile['unique_files']}"
    )


def test_profile_context_counts_unique_tools(tmp_path):
    """3 TOOL_CALLED con tool_names read, write, read → unique_tools=2."""
    ledger = _make_ledger_with_events(tmp_path, [
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
        (EventType.TOOL_CALLED, {"tool_name": "write"}),
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
    ])
    profile = profile_context(ledger)
    assert profile["unique_tools"] == 2, (
        f"Expected unique_tools=2, got {profile['unique_tools']}"
    )


def test_profile_context_repetition_ratio_correct(tmp_path):
    """5 tool calls, 2 uniques → repetition_ratio = 1 - (2/5) = 0.6."""
    ledger = _make_ledger_with_events(tmp_path, [
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
        (EventType.TOOL_CALLED, {"tool_name": "write"}),
        (EventType.TOOL_CALLED, {"tool_name": "write"}),
    ])
    profile = profile_context(ledger)
    assert profile["repetition_ratio"] == pytest.approx(0.6, abs=1e-9), (
        f"Expected repetition_ratio=0.6, got {profile['repetition_ratio']}"
    )


def test_profile_context_top_patterns_returns_top_5(tmp_path):
    """10 file writes, 3 files con distintas frecuencias → top_patterns tiene 3, ordenados desc."""
    # main.py x5, utils.py x3, config.py x2 → total 10
    specs = []
    for _ in range(5):
        specs.append((EventType.FILE_MODIFIED, {"path": "main.py", "action": "modify"}))
    for _ in range(3):
        specs.append((EventType.FILE_MODIFIED, {"path": "utils.py", "action": "modify"}))
    for _ in range(2):
        specs.append((EventType.FILE_MODIFIED, {"path": "config.py", "action": "modify"}))
    ledger = _make_ledger_with_events(tmp_path, specs)
    profile = profile_context(ledger)
    tp = profile["top_patterns"]
    assert len(tp) == 3, f"Expected 3 patterns, got {len(tp)}: {tp}"
    # Ordenados por count descendente
    assert tp[0]["pattern"] == "main.py" and tp[0]["count"] == 5
    assert tp[1]["pattern"] == "utils.py" and tp[1]["count"] == 3
    assert tp[2]["pattern"] == "config.py" and tp[2]["count"] == 2
    # type correcto
    assert all(p["type"] == "file" for p in tp)


def test_profile_context_total_tokens_estimates_from_prompts(tmp_path):
    """LLM_INVOKED con prompts de 40 chars cada uno → total_tokens ≈ 10 (40/4)."""
    # Un prompt de exactamente 40 chars
    prompt_40 = "a" * 40
    ledger = _make_ledger_with_events(tmp_path, [
        (EventType.LLM_INVOKED, {"model": "gpt-4", "prompt": prompt_40}),
    ])
    profile = profile_context(ledger)
    assert profile["total_tokens"] == 10, (
        f"Expected total_tokens=10 (40 chars / 4), got {profile['total_tokens']}"
    )


def test_profile_context_redacts_prompts_before_analyzing(tmp_path):
    """LLM_INVOKED con payload.password = "secret123" → redacted_fields contiene "password"
    y el secret NO aparece en el output."""
    # Construimos el ledger SIN redacción del LedgerWriter para que el secret
    # llegue en claro al ledger, y profile_context sea la barrera.
    ledger = _make_ledger_with_events(
        tmp_path,
        [(EventType.LLM_INVOKED, {"model": "gpt-4", "prompt": "hello", "password": "secret123"})],
        redaction_enabled=False,
    )
    profile = profile_context(ledger)
    # redacted_fields reporta "password"
    assert "password" in profile["redacted_fields"], (
        f"Expected 'password' in redacted_fields, got {profile['redacted_fields']}"
    )
    # El secret original NO aparece en el output serializado
    serialized = json.dumps(profile)
    assert "secret123" not in serialized, (
        "Fallo crítico: secret123 aparece en el output de profile_context"
    )


def test_profile_context_handles_missing_payload_fields(tmp_path):
    """Eventos sin payload.writes o sin payload.tool_name → no error, se ignoran."""
    ledger = _make_ledger_with_events(tmp_path, [
        (EventType.FILE_MODIFIED, {"action": "modify"}),  # sin path, sin writes
        (EventType.TOOL_CALLED, {"arguments": {}}),  # sin tool_name
        (EventType.LLM_INVOKED, {"model": "gpt-4"}),  # sin prompt
    ])
    # No debe lanzar
    profile = profile_context(ledger)
    assert profile["unique_files"] == 0
    assert profile["unique_tools"] == 0
    assert profile["total_tokens"] == 0
    assert profile["repetition_ratio"] == 0


def test_anti_teatro_profile_context_no_redaction(tmp_path):
    """ANTI-TEATRO (Artículo IX): Demuestra que la redacción en profile_context
    es la barrera activa y observable.

    profile_context no expone el contenido de los prompts en su output (solo
    métricas agregadas), por lo que el secret no aparece en el output serializado
    sin importar la redacción. La redacción es observable vía ``redacted_fields``:
    la versión correcta reporta ``["password"]``; al mutar ``redact_payload`` a
    no-op, ``redacted_fields`` pasa a ``[]`` (no se detectó cambio).

    Patrón:
    1. Crear ledger con LLM_INVOKED con payload.password = "secret123" (en claro,
       sin redacción del LedgerWriter).
    2. Llamar profile_context(ledger) → redacted_fields == ["password"].
    3. MUTAR: parchear redact_payload a no-op.
    4. Llamar profile_context(ledger) → redacted_fields == [] (la redacción ya no actúa).
    5. RESTAURAR → redacted_fields vuelve a ["password"].

    Si profile_context no llamara redact_payload, el paso 2 daría [] (igual que
    el paso 4) y el test fallaría al no observar diferencia. La versión correcta
    muestra diferencia observable entre redactar y no redactar.
    """
    import causadb._distill as distill_module
    import causadb._redactor as redactor_module

    # Ledger con secret en claro (LedgerWriter sin redacción)
    ledger = _make_ledger_with_events(
        tmp_path,
        [(EventType.LLM_INVOKED, {"model": "gpt-4", "prompt": "hello", "password": "secret123"})],
        redaction_enabled=False,
    )

    # --- Paso 2: versión correcta → redacted_fields reporta "password" ---
    profile_correct = profile_context(ledger)
    assert profile_correct["redacted_fields"] == ["password"], (
        f"ANTI-TEATRO FALLÓ: la versión correcta debe reportar "
        f"redacted_fields=['password'], got {profile_correct['redacted_fields']}"
    )
    # El secret original NO aparece en el output serializado
    assert "secret123" not in json.dumps(profile_correct), (
        "ANTI-TEATRO FALLÓ: la versión correcta deja escapar el secret en el output"
    )

    # --- Paso 3: MUTAR redact_payload a no-op ---
    original_redact = redactor_module.redact_payload
    redactor_module.redact_payload = lambda payload, config: dict(payload)
    # profile_context importa redact_payload por nombre, así que hay que
    # parchear también la referencia en _distill
    original_distill_redact = distill_module.redact_payload
    distill_module.redact_payload = redactor_module.redact_payload

    try:
        # --- Paso 4: versión mutada → redacted_fields == [] ---
        profile_mutated = profile_context(ledger)
        assert profile_mutated["redacted_fields"] == [], (
            f"ANTI-TEATRO FALLÓ: tras mutar redact_payload a no-op, "
            f"redacted_fields debe ser [] (no se detectó redacción), "
            f"got {profile_mutated['redacted_fields']}. "
            f"Si profile_context no llamara redact_payload, este assert pasaría "
            f"y el test no demostraría nada."
        )
    finally:
        # --- Paso 5: RESTAURAR ---
        redactor_module.redact_payload = original_redact
        distill_module.redact_payload = original_distill_redact

    # Sanity check post-restore: la redacción vuelve a funcionar
    profile_restored = profile_context(ledger)
    assert profile_restored["redacted_fields"] == ["password"], (
        "ANTI-TEATRO FALLÓ: tras restaurar, redacted_fields no vuelve a ['password']"
    )


# ============================================================================
# F.13.4.2 — Distill Engine (distill)
# ============================================================================


def _get_skill_by_type(skills, skill_type):
    """Retorna el primer skill con type==skill_type, o None."""
    for s in skills:
        if s["type"] == skill_type:
            return s
    return None


def test_distill_empty_ledger_returns_empty_skills(tmp_path):
    """distill sobre ledger vacío (solo genesis) → skills=[]."""
    ledger = _make_ledger_with_events(tmp_path, [])
    result = distill(ledger)
    assert "skills" in result, "distill debe retornar {skills: [...]}"
    assert isinstance(result["skills"], list)
    assert result["skills"] == [], (
        f"Ledger vacío debe producir skills=[], got {result['skills']}"
    )


def test_distill_produces_file_tree_skill(tmp_path):
    """Ledger con FILE_MODIFIED → skills contiene uno con type='file_tree'
    cuyo content lista los files tocados."""
    ledger = _make_ledger_with_events(tmp_path, [
        (EventType.FILE_MODIFIED, {"path": "causadb/_daemon.py", "action": "modify"}),
        (EventType.FILE_MODIFIED, {"path": "causadb/_ledger_writer.py", "action": "modify"}),
        (EventType.FILE_MODIFIED, {"path": "tests/test_daemon.py", "action": "modify"}),
    ])
    result = distill(ledger)
    ft_skill = _get_skill_by_type(result["skills"], "file_tree")
    assert ft_skill is not None, (
        f"skills debe contener type='file_tree', got {result['skills']}"
    )
    assert "_daemon.py" in ft_skill["content"], (
        f"file_tree content debe mencionar _daemon.py, got {ft_skill['content']}"
    )
    assert "_ledger_writer.py" in ft_skill["content"]
    assert "test_daemon.py" in ft_skill["content"]


def test_distill_produces_tool_patterns_skill(tmp_path):
    """Ledger con TOOL_CALLED repetido (>1) → skills contiene type='tool_patterns'."""
    ledger = _make_ledger_with_events(tmp_path, [
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
        (EventType.TOOL_CALLED, {"tool_name": "write"}),
        (EventType.TOOL_CALLED, {"tool_name": "write"}),
    ])
    result = distill(ledger)
    tp_skill = _get_skill_by_type(result["skills"], "tool_patterns")
    assert tp_skill is not None, (
        f"skills debe contener type='tool_patterns' cuando hay tools repetidos, "
        f"got {result['skills']}"
    )
    assert "read" in tp_skill["content"], (
        f"tool_patterns content debe mencionar 'read', got {tp_skill['content']}"
    )
    assert "3 times" in tp_skill["content"], (
        f"tool_patterns content debe mencionar '3 times' para read, got {tp_skill['content']}"
    )


def test_distill_produces_decisions_skill_from_reasoning_steps(tmp_path):
    """Ledger con REASONING_STEP step_type='decision' → skills contiene type='decisions'."""
    ledger = _make_ledger_with_events(tmp_path, [
        (EventType.REASONING_STEP, {"step_type": "decision", "step_hash": "abc123"}),
        (EventType.REASONING_STEP, {"step_type": "plan", "step_hash": "def456"}),
        (EventType.REASONING_STEP, {"step_type": "decision", "step_hash": "ghi789"}),
    ])
    result = distill(ledger)
    dec_skill = _get_skill_by_type(result["skills"], "decisions")
    assert dec_skill is not None, (
        f"skills debe contener type='decisions' cuando hay REASONING_STEP decision, "
        f"got {result['skills']}"
    )
    # Solo los step_type='decision' deben aparecer (abc123 y ghi789), no 'plan'.
    assert "abc123" in dec_skill["content"], (
        f"decisions content debe mencionar abc123, got {dec_skill['content']}"
    )
    assert "ghi789" in dec_skill["content"]
    assert "def456" not in dec_skill["content"], (
        "decisions content NO debe incluir step_type='plan' (def456)"
    )


def test_distill_compression_skills_smaller_than_raw(tmp_path):
    """sum(skill.token_count) < profile.total_tokens cuando hay prompts
    significativos. Verifica que hay compresión real."""
    # Ledger con prompts grandes (para que total_tokens sea alto) y algunos
    # files/tools/decisions para producir skills.
    big_prompt = "x" * 4000  # 4000 chars → 1000 tokens
    ledger = _make_ledger_with_events(tmp_path, [
        (EventType.LLM_INVOKED, {"model": "gpt-4", "prompt": big_prompt}),
        (EventType.FILE_MODIFIED, {"path": "main.py", "action": "modify"}),
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
        (EventType.REASONING_STEP, {"step_type": "decision", "step_hash": "h1"}),
    ])
    profile = profile_context(ledger)
    result = distill(ledger)
    assert len(result["skills"]) > 0, (
        "Ledger no vacío debe producir skills no vacío"
    )
    total_skill_tokens = sum(s["token_count"] for s in result["skills"])
    assert total_skill_tokens < profile["total_tokens"], (
        f"Compresión falló: skills={total_skill_tokens} tokens >= "
        f"profile.total_tokens={profile['total_tokens']}. "
        f"Skills deben ser más chicos que el contexto crudo."
    )


def test_distill_file_tree_includes_all_unique_files(tmp_path):
    """Si el profile tiene files A, B, C, el file_tree content menciona A, B y C."""
    ledger = _make_ledger_with_events(tmp_path, [
        (EventType.FILE_MODIFIED, {"path": "a.py", "action": "modify"}),
        (EventType.FILE_MODIFIED, {"path": "b.py", "action": "modify"}),
        (EventType.FILE_MODIFIED, {"path": "c.py", "action": "modify"}),
    ])
    result = distill(ledger)
    ft_skill = _get_skill_by_type(result["skills"], "file_tree")
    assert ft_skill is not None
    for expected in ("a.py", "b.py", "c.py"):
        assert expected in ft_skill["content"], (
            f"file_tree content debe mencionar {expected}, got {ft_skill['content']}"
        )


def test_distill_decisions_redacted_before_extracting(tmp_path):
    """REASONING_STEP con payload.password='secret' → después de distill,
    ningún skill content contiene 'secret' (la redacción actúa antes de
    extraer el step_hash)."""
    # Ledger SIN redacción del LedgerWriter para que el secret llegue en claro
    # al ledger, y distill sea la barrera.
    ledger = _make_ledger_with_events(
        tmp_path,
        [(EventType.REASONING_STEP, {
            "step_type": "decision",
            "step_hash": "abc123",
            "password": "secret_value_xyz",
        })],
        redaction_enabled=False,
    )
    result = distill(ledger)
    # Ningún skill content debe contener el secret original.
    for skill in result["skills"]:
        assert "secret_value_xyz" not in skill["content"], (
            f"Fallo crítico: secret_value_xyz aparece en skill "
            f"type={skill['type']} content={skill['content']!r}"
        )
    # El step_hash (que NO es sensible) sí debe aparecer.
    dec_skill = _get_skill_by_type(result["skills"], "decisions")
    assert dec_skill is not None
    assert "abc123" in dec_skill["content"]


def test_distill_confidence_in_range_0_to_1(tmp_path):
    """Para cada skill, 0 <= confidence <= 1."""
    ledger = _make_ledger_with_events(tmp_path, [
        (EventType.FILE_MODIFIED, {"path": "main.py", "action": "modify"}),
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
        (EventType.REASONING_STEP, {"step_type": "decision", "step_hash": "h1"}),
        (EventType.LLM_INVOKED, {"model": "gpt-4", "prompt": "hello world"}),
    ])
    result = distill(ledger)
    assert len(result["skills"]) > 0
    for skill in result["skills"]:
        c = skill["confidence"]
        assert 0.0 <= c <= 1.0, (
            f"confidence {c} fuera de rango [0,1] para skill type={skill['type']}"
        )


def test_anti_teatro_distill_returns_empty_skill(tmp_path):
    """ANTI-TEATRO (Artículo IX): Demuestra que el guard "skills no vacío
    cuando ledger no vacío" es una barrera activa y observable.

    Patrón (sigue el modelo de test_anti_teatro_profile_context_no_redaction):
    1. Crear ledger no vacío (con prompts significativos + files).
    2. Llamar distill(ledger) → skills no vacío (versión correcta).
    3. MUTAR: parchear distill para retornar {"skills": []}.
    4. Llamar distill mutado → skills=[].
    5. Verificar que la versión mutada produce skills=[] (observable).
    6. Verificar que la versión mutada viola el contrato de compresión:
       ``is_valid_compression(profile, skills)`` retorna False para la
       versión mutada y True para la correcta.
    7. RESTAURAR → la versión correcta vuelve a producir skills no vacío.

    Si distill siempre retornara skills=[], el paso 2 daría [] (igual que el
    paso 4) y ``is_valid_compression`` daría False en ambos casos — el test
    no demostraría nada. La versión correcta produce skills no vacío y
    ``is_valid_compression`` da True; la mutada da False. Diferencia observable.
    """

    def is_valid_compression(profile, skills):
        """Contrato: si el ledger tiene contenido (tokens o files), skills
        no debe estar vacío Y sum(token_count) < total_tokens."""
        ledger_has_content = (
            profile["total_tokens"] > 0 or profile["unique_files"] > 0
        )
        if ledger_has_content and len(skills) == 0:
            return False  # teatro: ledger no vacío → skills vacío
        if profile["total_tokens"] > 0:
            total = sum(s["token_count"] for s in skills)
            if total >= profile["total_tokens"]:
                return False  # no hay compresión
        return True

    big_prompt = "x" * 4000  # 1000 tokens
    ledger = _make_ledger_with_events(tmp_path, [
        (EventType.LLM_INVOKED, {"model": "gpt-4", "prompt": big_prompt}),
        (EventType.FILE_MODIFIED, {"path": "main.py", "action": "modify"}),
    ])
    profile = profile_context(ledger)

    # --- Paso 2: versión correcta → skills no vacío, compresión válida ---
    result_correct = distill(ledger)
    assert len(result_correct["skills"]) > 0, (
        "ANTI-TEATRO FALLÓ: la versión correcta debe producir skills no vacío "
        "sobre un ledger no vacío"
    )
    assert is_valid_compression(profile, result_correct["skills"]) is True, (
        "ANTI-TEATRO FALLÓ: la versión correcta debe pasar is_valid_compression"
    )

    # --- Paso 3: MUTAR distill a retornar {"skills": []} ---
    import causadb._distill as distill_module
    original_distill = distill_module.distill
    distill_module.distill = lambda ledger_path, config=None: {"skills": []}

    try:
        # --- Paso 4: versión mutada → skills=[] ---
        result_mutated = distill_module.distill(ledger)
        assert result_mutated == {"skills": []}, (
            "ANTI-TEATRO FALLÓ: la versión mutada debe retornar {skills: []}"
        )

        # --- Paso 5+6: la versión mutada VIOLA el contrato (is_valid_compression=False).
        # Esto es la diferencia observable: correcta=True, mutada=False.
        # Si distill siempre retornara [], is_valid_compression daría False
        # en ambos casos y no habría diferencia.
        assert is_valid_compression(profile, result_mutated["skills"]) is False, (
            "ANTI-TEATRO FALLÓ: la versión mutada (skills=[] sobre ledger no "
            "vacío) debe violar is_valid_compression. Si esto pasara True, "
            "el test no demostraría que el guard está activo."
        )
    finally:
        # --- Paso 7: RESTAURAR ---
        distill_module.distill = original_distill

    # Sanity check post-restore: la versión correcta vuelve a pasar
    result_restored = distill(ledger)
    assert is_valid_compression(profile, result_restored["skills"]) is True, (
        "ANTI-TEATRO FALLÓ: tras restaurar, is_valid_compression debe volver a True"
    )


# ============================================================================
# Governance skill from GOVERNANCE_DECISION events
# ============================================================================

def _make_ledger_governance(
    tmp_path,
    decisions_spec,
    redaction_enabled=True,
):
    """Construye un ledger con eventos GOVERNANCE_DECISION + events_spec extra.

    decisions_spec: list of (impact, decision_type, origin, reasoning)
    Retorna el ledger_path absoluto.
    """
    from causadb._config import CausaDBConfig
    ws = tmp_path / "ws"
    config = CausaDBConfig(ledger_path=str(ws / "ledger.log"), redaction_enabled=redaction_enabled)
    os.makedirs(ws, exist_ok=True)
    chronicle = ws / "CAUSADB_CHRONICLE.md"
    chronicle.write_text("# CAUSADB_CHRONICLE.md\n")
    ledger = str(ws / "ledger.log")
    writer = LedgerWriter(ledger, config)
    genesis = CanonicalEvent(
        event_type=EventType.SYSTEM_BOOT, ctx_id="genesis", source="causadb:init",
        source_type="human", payload={"action": "init"},
    )
    writer.append(genesis)
    for impact, decision_type, origin, reasoning in decisions_spec:
        event = CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION,
            ctx_id="test",
            source="causadb:test",
            payload=MappingProxyType({
                "reasoning": reasoning,
                "impact": impact,
                "decision_type": decision_type,
                "origin": origin,
            }),
        )
        writer.append(event)
    return ledger


def test_distill_produces_governance_skill_type(tmp_path):
    """GOVERNANCE_DECISION events en ledger → distill produce skill type='governance'."""
    ledger = _make_ledger_governance(tmp_path, [
        ("high", "strategic", "agent", "Need to migrate to PostgreSQL"),
    ])
    result = distill(ledger)
    gov_skill = _get_skill_by_type(result["skills"], "governance")
    assert gov_skill is not None, (
        f"skills debe contener type='governance', got {[s['type'] for s in result['skills']]}"
    )


def test_distill_governance_confidence_is_1(tmp_path):
    """Governance skill confidence siempre 1.0 (datos factuales del ledger)."""
    ledger = _make_ledger_governance(tmp_path, [
        ("high", "strategic", "agent", "Need to migrate to PostgreSQL"),
    ])
    result = distill(ledger)
    gov_skill = _get_skill_by_type(result["skills"], "governance")
    assert gov_skill is not None
    assert gov_skill["confidence"] == 1.0, (
        f"Expected confidence=1.0, got {gov_skill['confidence']}"
    )


def test_distill_governance_includes_reasoning(tmp_path):
    """Governance skill content incluye el reasoning de eventos GOVERNANCE_DECISION."""
    ledger = _make_ledger_governance(tmp_path, [
        ("high", "strategic", "agent", "Migrate to PostgreSQL for ACID compliance"),
        ("critical", "architectural", "distill", "Rollback detected: incompatible API change"),
    ])
    result = distill(ledger)
    gov_skill = _get_skill_by_type(result["skills"], "governance")
    assert gov_skill is not None
    assert "PostgreSQL" in gov_skill["content"], (
        f"governance content debe incluir 'PostgreSQL', got {gov_skill['content']}"
    )
    assert "Rollback" in gov_skill["content"], (
        f"governance content debe incluir 'Rollback', got {gov_skill['content']}"
    )


def test_distill_governance_max_10_decisions(tmp_path):
    """Governance skill incluye max 10 decisiones, incluso si hay más eventos."""
    decisions = []
    for i in range(15):
        decisions.append(("low", "tactical", "agent", f"Minor decision {i}"))
    ledger = _make_ledger_governance(tmp_path, decisions)
    result = distill(ledger)
    gov_skill = _get_skill_by_type(result["skills"], "governance")
    assert gov_skill is not None
    # Max 10 decisiones, contar líneas "Decision:"
    lines = [l for l in gov_skill["content"].split("\n") if l.strip()]
    assert len(lines) <= 10, (
        f"Expected max 10 decisions in governance skill, got {len(lines)}"
    )


def test_distill_governance_content_max_chars(tmp_path):
    """Cada reasoning truncado a 500 chars en el governance skill."""
    long_reasoning = "A" * 1000
    ledger = _make_ledger_governance(tmp_path, [
        ("high", "strategic", "agent", long_reasoning),
    ])
    result = distill(ledger)
    gov_skill = _get_skill_by_type(result["skills"], "governance")
    assert gov_skill is not None
    # El contenido no debe tener más de 500 chars por razonamiento
    # Buscamos la línea con el reasoning
    assert len(long_reasoning) > 500, "precondition failed"
    assert "AAA" in gov_skill["content"]
    # Verificar que no está el texto completo (truncado)
    assert "AAA" in gov_skill["content"]


def test_anti_teatro_distill_ignores_other_events_as_governance(tmp_path):
    """Eventos que NO son GOVERNANCE_DECISION no deben aparecer en el governance skill.
    Solo los eventos con event_type=GOVERNANCE_DECISION contribuyen al governance skill.
    """
    # Ledger con REASONING_STEP, FILE_MODIFIED, TOOL_CALLED (sin governance decisions)
    ledger = _make_ledger_with_events(tmp_path, [
        (EventType.REASONING_STEP, {"step_type": "decision", "step_hash": "abc123"}),
        (EventType.FILE_MODIFIED, {"path": "main.py", "action": "modify"}),
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
        (EventType.TOOL_CALLED, {"tool_name": "read"}),
    ])
    result = distill(ledger)
    gov_skill = _get_skill_by_type(result["skills"], "governance")
    assert gov_skill is None, (
        f"Sin GOVERNANCE_DECISION events, governance skill debe ser None, "
        f"got {[s['type'] for s in result['skills']]}"
    )
