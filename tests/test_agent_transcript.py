"""Tests Fase 1 — Motor universal de transcripción de agentes.

Motor universal (ver docs/design_index.md). Artículo III (test-first), Artículo VI
(determinismo de replay), Artículo VIII (funciones puras, sin clases),
Artículo IX (anti-teatro: datos reales, nada hardcodeado).

Cobertura:
  1. thought → REASONING_STEP con step_type correcto según subject
  2. step_hash determinístico (mismo input → mismo hash)
  3. tool_call → TOOL_CALLED con campos completos (sin recortar)
  4. assistant+model → LLM_INVOKED con prompt del user anterior y duration_ms
  5. anti-teatro: mismo input → mismos event_ids vía _event_from_raw
  6. step_type fallback cuando no hay subject
  7. usuario sin thoughts/tool_calls → 0 eventos (pero actualiza estado)
  8. contenido grande NO se recorta (decisión del operador 2026-07-31)
  9. determinismo puro: mismo input → mismas raws (Artículo VI)
"""

import hashlib

import pytest

from causadb._agent_transcript import agent_message_to_raw, infer_step_type
from causadb._harvester import Harvester


def _msg(**over):
    base = {
        "kind": "assistant",
        "model": None,
        "timestamp": "2026-07-02T18:18:50.511Z",
        "content": "",
        "thoughts": [],
        "tool_calls": [],
        "tokens": None,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1. thought → REASONING_STEP
# ---------------------------------------------------------------------------

def test_thought_maps_to_reasoning_step_plan():
    """Thought con subject de planificación → step_type='plan'."""
    msg = _msg(
        thoughts=[{
            "subject": "Planning the implementation steps",
            "description": "We need to design the module boundary before coding.",
        }],
    )
    raws = agent_message_to_raw("gemini", msg)
    assert len(raws) == 1
    r = raws[0]
    assert r["type"] == "REASONING_STEP"
    assert r["step_type"] == "plan"
    assert r["subject"] == "Planning the implementation steps"
    assert r["description"] == "We need to design the module boundary before coding."
    assert r["timestamp"] == "2026-07-02T18:18:50.511Z"
    assert r["agent"] == "gemini"


def test_thought_step_type_heuristic_variants():
    """La heurística cubre los 4 step_type del enum cerrado
    (plan/analysis/decision/reflection)."""
    cases = [
        ("Planning the migration strategy", "plan"),
        ("Analyzing the failure rate of the harvest loop", "analysis"),
        ("Deciding between two ledger formats", "decision"),
        ("Reflecting on what went wrong last session", "reflection"),
    ]
    for subject, expected in cases:
        raws = agent_message_to_raw(
            "gemini",
            _msg(thoughts=[{"subject": subject, "description": "d"}]),
        )
        assert raws[0]["step_type"] == expected, (
            f"subject={subject!r} → esperaba {expected}, obtuvo {raws[0]['step_type']}"
        )
    # Y la función auxiliar directamente
    assert infer_step_type("Analyzing the data") == "analysis"


def test_step_type_fallback_without_subject():
    """Sin subject → fallback 'analysis' (Art. VIII: sin abstracción,
    comportamiento por defecto documentado)."""
    msg = _msg(thoughts=[{"description": "No subject here, just reasoning."}])
    raws = agent_message_to_raw("opencode", msg)
    assert raws[0]["type"] == "REASONING_STEP"
    assert raws[0]["step_type"] == "analysis"
    assert infer_step_type(None) == "analysis"
    assert infer_step_type("") == "analysis"


# ---------------------------------------------------------------------------
# 2. step_hash determinístico
# ---------------------------------------------------------------------------

def test_step_hash_deterministic():
    """Mismo input → mismo step_hash (sha256 del contenido completo)."""
    msg = _msg(thoughts=[{"subject": "S", "description": "razonamiento completo"}])
    raws1 = agent_message_to_raw("gemini", msg)
    raws2 = agent_message_to_raw("gemini", msg)
    expected = hashlib.sha256("razonamiento completo".encode()).hexdigest()
    assert raws1[0]["step_hash"] == expected
    assert raws1[0]["step_hash"] == raws2[0]["step_hash"]

    # Contenido distinto → hash distinto (anti-teatro: no hardcodeado)
    msg2 = _msg(thoughts=[{"subject": "S", "description": "otro razonamiento"}])
    raws3 = agent_message_to_raw("gemini", msg2)
    assert raws3[0]["step_hash"] != raws1[0]["step_hash"]


# ---------------------------------------------------------------------------
# 3. tool_call → TOOL_CALLED completo
# ---------------------------------------------------------------------------

def test_tool_call_maps_to_tool_called_complete():
    """tool_call → TOOL_CALLED con tool_name, arguments y result COMPLETOS."""
    big_result = {"output": "x" * 100_000}  # 100KB — NO se recorta
    msg = _msg(
        tool_calls=[{
            "name": "read_file",
            "arguments": {"file_path": "/tmp/bitacora.md"},
            "result": big_result,
            "timestamp": "2026-07-02T18:18:50.519Z",
        }],
    )
    raws = agent_message_to_raw("gemini", msg)
    assert len(raws) == 1
    r = raws[0]
    assert r["type"] == "TOOL_CALLED"
    assert r["tool_name"] == "read_file"
    assert r["arguments"] == {"file_path": "/tmp/bitacora.md"}
    assert r["result"]["output"] == big_result["output"]  # íntegro
    assert r["timestamp"] == "2026-07-02T18:18:50.519Z"  # usa el ts del tool_call


# ---------------------------------------------------------------------------
# 4. assistant+model → LLM_INVOKED
# ---------------------------------------------------------------------------

def test_assistant_model_maps_to_llm_invoked():
    """Mensaje assistant con model → LLM_INVOKED con prompt del user anterior,
    response_tokens y duration_ms entre timestamps."""
    msg = _msg(
        model="gemini-3.1-flash-lite",
        content="He leído el archivo.",
        tokens={"input": 15933, "output": 28, "total": 16795},
    )
    raws = agent_message_to_raw(
        "gemini",
        msg,
        last_user_content="Quedamos acá: lee la bitácora.",
        prev_timestamp="2026-07-02T18:18:45.753Z",
    )
    llm = [r for r in raws if r["type"] == "LLM_INVOKED"]
    assert len(llm) == 1
    r = llm[0]
    assert r["model"] == "gemini-3.1-flash-lite"
    assert r["prompt"] == "Quedamos acá: lee la bitácora."  # completo, sin recortar
    assert r["response_tokens"] == 28
    # 18:18:50.511 - 18:18:45.753 = 4.758s = 4758 ms
    assert r["duration_ms"] == 4758
    assert r["response_content"] == "He leído el archivo."  # respuesta completa


def test_llm_invoked_prompt_falls_back_to_empty():
    """Sin user anterior → prompt vacío, no crashea."""
    raws = agent_message_to_raw("gemini", _msg(model="m", tokens={"output": 5}))
    llm = [r for r in raws if r["type"] == "LLM_INVOKED"]
    assert len(llm) == 1
    assert llm[0]["prompt"] == ""


def test_user_message_produces_no_events():
    """Mensaje user sin thoughts ni tool_calls → 0 eventos (solo actualiza
    estado del caller)."""
    raws = agent_message_to_raw(
        "gemini",
        _msg(kind="user", content="hola"),
    )
    assert raws == []


def test_assistant_without_model_produces_no_llm_invoked():
    """Mensaje assistant sin model → NO LLM_INVOKED (los thoughts sí)."""
    msg = _msg(thoughts=[{"subject": "Analyzing X", "description": "d"}])
    raws = agent_message_to_raw("opencode", msg)
    types = {r["type"] for r in raws}
    assert types == {"REASONING_STEP"}


# ---------------------------------------------------------------------------
# 8. contenido grande NO se recorta
# ---------------------------------------------------------------------------

def test_large_thought_not_truncated():
    """Decisión del operador (2026-07-31): NO recortar el razonamiento.
    El contenido completo viaja en el payload (el LedgerWriter lo
    blob-ifica cuando está habilitado)."""
    long_reasoning = "razonamiento " + ("detalle " * 20_000)  # ~180KB
    msg = _msg(thoughts=[{"subject": "Planning X", "description": long_reasoning}])
    raws = agent_message_to_raw("gemini", msg)
    assert raws[0]["description"] == long_reasoning
    assert len(raws[0]["description"]) > 2000  # anti-recorte


# ---------------------------------------------------------------------------
# 5 + 9. anti-teatro: determinismo de event_ids (Artículo VI)
# ---------------------------------------------------------------------------

def test_anti_teatro_same_input_same_event_ids(tmp_path):
    """Mismo input → mismos event_ids vía _event_from_raw. Y el motor es
    puro: dos llamadas idénticas producen raws idénticas."""
    ledger_path = str(tmp_path / "ledger.log")
    h = Harvester(ledger_path)

    msg = _msg(
        model="gemini-3.1-flash-lite",
        thoughts=[{"subject": "Planning X", "description": "d1"}],
        tool_calls=[{"name": "bash", "arguments": {"cmd": "ls"}, "result": {"ok": True}}],
        tokens={"output": 10},
    )
    raws = agent_message_to_raw("gemini", msg, last_user_content="prompt")

    events1 = [h._event_from_raw("gemini", r) for r in raws]
    events2 = [h._event_from_raw("gemini", r) for r in raws]
    ids1 = [e.event_id for e in events1]
    ids2 = [e.event_id for e in events2]
    assert ids1 == ids2  # determinístico

    # 3 eventos: REASONING_STEP + TOOL_CALLED + LLM_INVOKED, ids únicos
    assert len(set(ids1)) == 3

    # Input distinto → ids distintos (anti-teatro: no hardcodeado)
    msg2 = _msg(
        model="gemini-3.1-flash-lite",
        thoughts=[{"subject": "Planning Y", "description": "d2"}],
        tool_calls=[{"name": "bash", "arguments": {"cmd": "pwd"}, "result": {"ok": False}}],
        tokens={"output": 11},
    )
    raws2 = agent_message_to_raw("gemini", msg2, last_user_content="otro")
    events3 = [h._event_from_raw("gemini", r) for r in raws2]
    assert not set(ids1) & {e.event_id for e in events3}

    # Los sources son válidos según el namespace de _attribution
    for e in events1:
        assert e.source == "harvester:gemini"
        assert e.source_type == "agent"
        assert e.ctx_id == "harvester:gemini"


def test_anti_teatro_event_types_registered():
    """Los EventTypes generados deben estar registrados (SCHEMA_RULES ya
    los define — no se tocan _event_types ni _schema_validator)."""
    from causadb._event_registry import is_registered
    for et in ("REASONING_STEP", "TOOL_CALLED", "LLM_INVOKED"):
        assert is_registered(et), f"{et} debe estar registrado"
