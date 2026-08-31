"""H2.3 — Tests RED: sin duplicación de ``sessions.output_tokens`` por
assistant + auditoría de consistencia de las 3 fuentes + coexistencia en replay.

Contrato H2.3 (CAUSADB_ROADMAP_HERMES_TRACEABILITY.md):
- ``LLM_INVOKED`` (per-message): ``response_tokens`` = token count REAL del
  mensaje (``messages.token_count``) si es int > 0; si es NULL/0 → 0 honesto
  (patrón windsurf/grok/openjarvis). NUNCA el agregado ``sessions.output_tokens``.
- ``API_ATTEMPT`` (per-request) y ``COST_ACCOUNTED`` (agregado sesión) son los
  únicos que transportan el agregado por-sesión.
- ``CostRollup.validate_hermes_consistency`` (función pura de auditoría)
  agrupa por ``hermes_session_id`` y cruza las 3 fuentes: detecta duplicación
  (llm_invoked >= 2× cost_accounted) y discrepancia (>10% api vs cost).
- Replay: los 3 EventTypes conviven proyectados por separado en el estado,
  sin mezclarse ni anularse.

Artículo III (tests RED primero), Artículo IX (aserciones reales y
discriminatorias — nada de ``assert True``).
"""

import os
import sqlite3

import pytest

from causadb._cost_rollup import CostRollup
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._harvest_source_hermes import HermesHarvestSource
from causadb._ledger_writer import LedgerWriter
from causadb._replay_engine import ReplayEngine
from tests.helpers._build_synthetic_hermes_store import build_synthetic_hermes_store

# Sesiones de la fixture (copiada por el helper sintético H2.0)
SESSION_QWEN = "20260802_101617_82f322"
SESSION_LLAMA = "20260802_102154_c35163"
MODEL_QWEN = "qwen3.5:4b"
MODEL_LLAMA = "llama3.1:8b"
# Agregados por-sesión reales del store (SOLO viven en API_ATTEMPT/COST_ACCOUNTED)
QWEN_SESSION_OUTPUT_TOKENS = 1339
LLAMA_SESSION_OUTPUT_TOKENS = 215


# ---------------------------------------------------------------------------
# Decisión 1 — LLM_INVOKED.response_tokens honesto per-message (harvester)
# ---------------------------------------------------------------------------

def _harvest_llms(tmp_path) -> list[dict]:
    """Store sintético (copia de la fixture real, token_count NULL) → raws
    LLM_INVOKED del harvester, sin pasar por el Harvester/ledger (raw puro)."""
    db_path = str(tmp_path / "state.db")
    build_synthetic_hermes_store(db_path)
    source = HermesHarvestSource(ledger_path=str(tmp_path / "ledger.log"), db_path=db_path)
    raws = list(source.harvest(None))
    return [r for r in raws if r["type"] == "LLM_INVOKED"]


def test_llm_invoked_response_tokens_zero_when_message_token_null(tmp_path):
    """messages.token_count es NULL en la práctica (verificado en la fixture).
    → cada LLM_INVOKED debe llevar response_tokens == 0, NO el agregado
    sessions.output_tokens repetido por mensaje.

    Sesión llama = 2 mensajes assistant → con el bug (pre-H2.3) ambos llevaban
    215 (suma 430 = 2× el agregado). Post-H2.3: ambos 0, suma 0.
    """
    llms = _harvest_llms(tmp_path)

    # La sesión llama tiene 2 assistant con model → 2 LLM_INVOKED
    llama_llms = [l for l in llms if l["model"] == MODEL_LLAMA]
    assert len(llama_llms) == 2, f"esperaba 2 LLM_INVOKED llama, got {len(llama_llms)}"
    for llm in llama_llms:
        assert llm["response_tokens"] == 0, (
            f"token_count NULL → response_tokens debe ser 0, got {llm['response_tokens']} "
            f"(no repetir sessions.output_tokens={LLAMA_SESSION_OUTPUT_TOKENS})"
        )
    # La suma NO es el agregado multiplicado por N (2×215=430)
    assert sum(l["response_tokens"] for l in llama_llms) != 2 * LLAMA_SESSION_OUTPUT_TOKENS
    assert sum(l["response_tokens"] for l in llama_llms) == 0

    # Sesión qwen (1 assistant) tampoco repite su agregado
    qwen_llms = [l for l in llms if l["model"] == MODEL_QWEN]
    assert len(qwen_llms) == 1
    assert qwen_llms[0]["response_tokens"] == 0
    # Ningún LLM_INVOKED de la sesión puede llevar el agregado por-sesión
    assert qwen_llms[0]["response_tokens"] != QWEN_SESSION_OUTPUT_TOKENS


def test_llm_invoked_uses_real_message_token_count_when_present(tmp_path):
    """Cuando messages.token_count es int > 0, el LLM_INVOKED correspondiente
    lleva ESE valor (per-message real), no el agregado y no 0.

    UPDATEs: message id 6 (qwen assistant) → 42; message id 22 (llama assistant
    final) → 77. El otro llama (id 20, sin token_count) sigue en 0. Esto prueba
    granularidad POR-MENSAJE: cada LLM_INVOKED refleja solo su mensaje.
    """
    db_path = str(tmp_path / "state.db")
    build_synthetic_hermes_store(db_path)
    con = sqlite3.connect(db_path)
    try:
        con.execute("UPDATE messages SET token_count=42 WHERE id=6")   # qwen assistant
        con.execute("UPDATE messages SET token_count=77 WHERE id=22")  # llama assistant final
        con.commit()
    finally:
        con.close()

    source = HermesHarvestSource(ledger_path=str(tmp_path / "ledger.log"), db_path=db_path)
    raws = list(source.harvest(None))
    llms = [r for r in raws if r["type"] == "LLM_INVOKED"]

    qwen_llm = [l for l in llms if l["model"] == MODEL_QWEN][0]
    assert qwen_llm["message_id"] == 6
    assert qwen_llm["response_tokens"] == 42

    llama_by_msg = {l["message_id"]: l for l in llms if l["model"] == MODEL_LLAMA}
    assert set(llama_by_msg) == {20, 22}, f"esperaba messages 20 y 22, got {sorted(llama_by_msg)}"
    assert llama_by_msg[20]["response_tokens"] == 0  # sin token_count → 0 honesto
    assert llama_by_msg[22]["response_tokens"] == 77  # token_count real del mensaje


# ---------------------------------------------------------------------------
# Decisión 2 — CostRollup.validate_hermes_consistency (auditoría pura)
# ---------------------------------------------------------------------------

def test_costrollup_validate_hermes_consistency_clean():
    """API_ATTEMPT (100) + LLM_INVOKED (50) + COST_ACCOUNTED (100) de la misma
    sesión → consistente: sin duplicación ni discrepancia."""
    events = [
        {"type": "API_ATTEMPT", "hermes_session_id": "s1", "tokens_out": 100},
        {"type": "LLM_INVOKED", "hermes_session_id": "s1", "response_tokens": 50},
        {"type": "COST_ACCOUNTED", "hermes_session_id": "s1", "tokens_out": 100},
    ]
    result = CostRollup.validate_hermes_consistency(events)

    assert "s1" in result
    r = result["s1"]
    assert r["api_attempt_tokens_out"] == 100
    assert r["cost_accounted_tokens_out"] == 100
    assert r["llm_invoked_response_tokens"] == 50
    assert r["duplication_detected"] is False
    assert r["discrepancy_detected"] is False


def test_costrollup_validate_hermes_consistency_detects_duplication():
    """3 LLM_INVOKED con response_tokens=215 cada uno (el agregado repetido por
    mensaje — el bug H2.3) + 1 COST_ACCOUNTED tokens_out=215 → duplicación."""
    events = [
        {"type": "LLM_INVOKED", "hermes_session_id": "s1", "response_tokens": 215},
        {"type": "LLM_INVOKED", "hermes_session_id": "s1", "response_tokens": 215},
        {"type": "LLM_INVOKED", "hermes_session_id": "s1", "response_tokens": 215},
        {"type": "COST_ACCOUNTED", "hermes_session_id": "s1", "tokens_out": 215},
    ]
    result = CostRollup.validate_hermes_consistency(events)

    r = result["s1"]
    assert r["llm_invoked_response_tokens"] == 645
    assert r["cost_accounted_tokens_out"] == 215
    assert r["api_attempt_tokens_out"] == 0  # fuente ausente → 0, sin excepción
    assert r["duplication_detected"] is True
    assert r["discrepancy_detected"] is False


def test_costrollup_validate_hermes_consistency_detects_discrepancy():
    """API_ATTEMPT tokens_out=100 vs COST_ACCOUNTED tokens_out=150 → diff 33%
    > umbral ~10% → discrepancia; sin duplicación."""
    events = [
        {"type": "API_ATTEMPT", "hermes_session_id": "s1", "tokens_out": 100},
        {"type": "COST_ACCOUNTED", "hermes_session_id": "s1", "tokens_out": 150},
        {"type": "LLM_INVOKED", "hermes_session_id": "s1", "response_tokens": 100},
    ]
    result = CostRollup.validate_hermes_consistency(events)

    r = result["s1"]
    assert r["api_attempt_tokens_out"] == 100
    assert r["cost_accounted_tokens_out"] == 150
    assert r["discrepancy_detected"] is True
    assert r["duplication_detected"] is False  # 100 >= 2*150? no


def test_costrollup_validate_hermes_consistency_multiple_sessions_and_empty():
    """Agrupación por sesión: cada sesión con sus propios totales. Lista vacía
    → dict vacío, sin excepción."""
    events = [
        {"type": "API_ATTEMPT", "hermes_session_id": "a", "tokens_out": 10},
        {"type": "API_ATTEMPT", "hermes_session_id": "a", "tokens_out": 20},
        {"type": "COST_ACCOUNTED", "hermes_session_id": "b", "tokens_out": 30},
        {"type": "LLM_INVOKED", "hermes_session_id": "b", "response_tokens": 5},
    ]
    result = CostRollup.validate_hermes_consistency(events)

    assert result["a"]["api_attempt_tokens_out"] == 30
    assert result["a"]["cost_accounted_tokens_out"] == 0
    assert result["a"]["llm_invoked_response_tokens"] == 0
    assert result["a"]["duplication_detected"] is False
    assert result["b"]["cost_accounted_tokens_out"] == 30
    assert result["b"]["llm_invoked_response_tokens"] == 5
    assert result["b"]["discrepancy_detected"] is False  # api ausente (0) vs 30

    assert CostRollup.validate_hermes_consistency([]) == {}
    assert CostRollup.validate_hermes_consistency([{"type": "TOOL_CALLED", "tool_name": "x"}]) == {}


# ---------------------------------------------------------------------------
# Decisión 3 — Replay: los 3 EventTypes conviven sin mezclarse
# ---------------------------------------------------------------------------

def test_replay_three_sources_coexist(tmp_path):
    """Un ledger con API_ATTEMPT + LLM_INVOKED + COST_ACCOUNTED de la misma
    sesión proyecta los 3 por separado en state: api_attempts, llm_invocations,
    cost_accounted — sin mezclarse ni anularse (cada uno 1 entrada)."""
    ledger = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger)

    writer.append(CanonicalEvent(
        event_type=EventType.API_ATTEMPT,
        ctx_id="test_ctx",
        source="hermes",
        payload={
            "hermes_session_id": SESSION_QWEN,
            "provider": "custom",
            "model": MODEL_QWEN,
            "mode": "chat",
            "status": "completed",
            "request_ref": "s1#req#call1",
            "tokens_in": 2050,
            "tokens_out": 1339,
        },
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.LLM_INVOKED,
        ctx_id="test_ctx",
        source="hermes",
        payload={
            "model": MODEL_QWEN,
            "prompt": "p",
            "response_tokens": 0,  # per-message honesto (token_count NULL)
            "duration_ms": 100,
        },
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.COST_ACCOUNTED,
        ctx_id="test_ctx",
        source="hermes",
        payload={
            "model": MODEL_QWEN,
            "tokens_in": 2050,
            "tokens_out": 1339,
            "cost": 0.0,
            "currency": "USD",
        },
    ))

    state = ReplayEngine(ledger).reconstruct_state()

    # Los 3 conviven, cada uno en su proyección, sin anularse
    assert len(state["api_attempts"]) == 1
    assert len(state["llm_invocations"]) == 1
    assert len(state["cost_accounted"]) == 1

    # Cada entrada conserva los campos de SU fuente
    assert state["api_attempts"][0]["request_ref"] == "s1#req#call1"
    assert state["api_attempts"][0]["tokens_out"] == 1339
    assert state["llm_invocations"][0]["model"] == MODEL_QWEN
    assert state["llm_invocations"][0]["response_tokens"] == 0
    assert state["cost_accounted"][0]["tokens_out"] == 1339
    assert state["cost_accounted"][0]["cost"] == 0.0

    # No se contaminan: el agregado de COST_ACCOUNTED no viaja al LLM_INVOKED
    assert "tokens_out" not in state["llm_invocations"][0]
    assert "response_tokens" not in state["cost_accounted"][0]
