"""H2.2 — Tests RED del harvester Hermes para tokens, cache, reasoning tokens,
coste y pricing granular por llamada/model/task.

Contrato (DONE H2.2, CAUSADB_ROADMAP_HERMES_TRACEABILITY.md):
- ``session_model_usage`` (store Hermes) trae columnas adicionales que el
  harvester debe correlacionar por ``session_id`` al emitir ``API_ATTEMPT``:
  ``task``, ``cache_read_tokens``, ``cache_write_tokens``, ``reasoning_tokens``,
  ``estimated_cost_usd``, ``actual_cost_usd``, ``cost_status``, ``cost_source``,
  ``input_tokens``, ``output_tokens``.
- El log ya aporta ``latency`` (→ ``latency_ms``) y ``tokens_in/out`` por request.
- Schemas legacy (tabla sin columnas nuevas) → guard de columnas, sin crash.
- Sin fila de billing → defaults seguros (ausencia de campos opcionales), sin crash.
- Art. V: NINGUNA credencial de ``billing_base_url`` en el payload emitido.
- Replay lossless: ``cache_read``, ``cache_write``, ``reasoning_tokens``,
  ``cost_usd`` sobreviven la proyección ``state["api_attempts"]``.
"""

import json
import sqlite3

import pytest

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._harvest_source_hermes import HermesHarvestSource
from causadb._ledger_writer import LedgerWriter
from causadb._replay_engine import ReplayEngine
from tests.helpers._build_synthetic_hermes_store import build_synthetic_hermes_store
from tests.helpers._synthetic_agent_log import create_synthetic_agent_log

SESSION = "20260802_101617_82f322"
MODEL = "qwen3.5:4b"


def _usage_row_columns() -> list[str]:
    """Columnas de la tabla session_model_usage del store sintético (schema v22+)."""
    return [
        "session_id", "model", "billing_provider", "billing_base_url",
        "billing_mode", "task", "api_call_count", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
        "estimated_cost_usd", "actual_cost_usd", "cost_status", "cost_source",
        "first_seen", "last_seen",
    ]


def _update_usage(db_path: str, **fields) -> None:
    """Actualiza la fila de session_model_usage de la sesión de test."""
    con = sqlite3.connect(db_path)
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values()) + [SESSION, MODEL]
        con.execute(
            f"UPDATE session_model_usage SET {sets} WHERE session_id=? AND model=?",
            params,
        )
        con.commit()
    finally:
        con.close()


def _build(db_path: str, logs_dir: str, log_content: str,
           usage_fields: dict | None = None) -> list[dict]:
    """Store sintético + log → eventos raw del harvester (API_ATTEMPT incluidos).

    ``usage_fields`` (opcional): columnas de session_model_usage a setear en la
    fila de la sesión de test DESPUÉS de construir el store (el store debe
    existir antes de UPDATE).
    """
    build_synthetic_hermes_store(db_path)
    if usage_fields:
        _update_usage(db_path, **usage_fields)
    create_synthetic_agent_log(logs_dir, log_content)
    source = HermesHarvestSource(ledger_path="/tmp/ledger.log", db_path=db_path)
    return list(source.harvest())


def _api_attempts(events: list[dict]) -> list[dict]:
    return [e for e in events if e["type"] == "API_ATTEMPT"]


_COMPLETED_LINE = (
    f"2026-08-13 10:00:00,000 INFO [{SESSION}] agent.conversation_loop: "
    f"API call #1: model={MODEL} provider=custom in=100 out=50 total=150 latency=1.5s\n"
)


# ---------------------------------------------------------------------------
# 1. Correlación granular desde session_model_usage
# ---------------------------------------------------------------------------

def test_granular_fields_correlated_from_session_model_usage(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    events = _build(db_path, logs_dir, _COMPLETED_LINE, usage_fields={
        "task": "coder_loop", "cache_read_tokens": 1200,
        "cache_write_tokens": 340, "reasoning_tokens": 512,
        "estimated_cost_usd": 0.05, "actual_cost_usd": 0.0,
        "cost_status": "estimated", "cost_source": "pricing_cache",
    })

    attempts = _api_attempts(events)
    assert len(attempts) == 1
    ev = attempts[0]
    assert ev["cache_read"] == 1200
    assert ev["cache_write"] == 340
    assert ev["reasoning_tokens"] == 512
    assert ev["cost_usd"] == pytest.approx(0.05)
    assert ev["task"] == "coder_loop"
    assert ev["cost_status"] == "estimated"
    assert ev["cost_source"] == "pricing_cache"


def test_task_correlated_if_exists(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    events = _build(db_path, logs_dir, _COMPLETED_LINE, usage_fields={"task": "agentic_coding"})

    ev = _api_attempts(events)[0]
    assert ev["task"] == "agentic_coding"


# ---------------------------------------------------------------------------
# 2. latency_ms se conserva del log
# ---------------------------------------------------------------------------

def test_latency_ms_preserved_from_log(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    events = _build(db_path, logs_dir, _COMPLETED_LINE)

    ev = _api_attempts(events)[0]
    assert ev["latency_ms"] == 1500


# ---------------------------------------------------------------------------
# 3. Prioridad de coste: actual > estimated
# ---------------------------------------------------------------------------

def test_cost_priority_actual_over_estimated(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    events = _build(db_path, logs_dir, _COMPLETED_LINE, usage_fields={
        "estimated_cost_usd": 0.05, "actual_cost_usd": 0.07,
        "cost_status": "billed", "cost_source": "provider_invoice",
    })

    ev = _api_attempts(events)[0]
    assert ev["cost_usd"] == pytest.approx(0.07)


def test_cost_fallback_estimated_when_actual_zero(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    events = _build(db_path, logs_dir, _COMPLETED_LINE, usage_fields={
        "estimated_cost_usd": 0.05, "actual_cost_usd": 0.0,
    })

    ev = _api_attempts(events)[0]
    assert ev["cost_usd"] == pytest.approx(0.05)


def test_cost_usd_omitted_when_both_costs_zero(tmp_path):
    """Store sintético con estimated=0.0 / actual=0.0 (default del store =
    "costo no computado") → cost_usd queda AUSENTE, no se fabrica un $0."""
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    events = _build(db_path, logs_dir, _COMPLETED_LINE)

    ev = _api_attempts(events)[0]
    assert "cost_usd" not in ev
    # ... pero el status del store sí se reporta tal cual (metadato honesto)
    assert ev["cost_status"] == "unknown"


# ---------------------------------------------------------------------------
# 4. Fallback de tokens desde session_model_usage
# ---------------------------------------------------------------------------

def test_token_fallback_from_session_model_usage_when_log_zero(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    # La fila sintética de SESSION ya trae input_tokens=2050/output_tokens=1339
    zero_line = (
        f"2026-08-13 10:00:00,000 INFO [{SESSION}] agent.conversation_loop: "
        f"API call #1: model={MODEL} provider=custom in=0 out=0 total=0 latency=0.8s\n"
    )
    events = _build(db_path, logs_dir, zero_line)

    ev = _api_attempts(events)[0]
    assert ev["tokens_in"] == 2050
    assert ev["tokens_out"] == 1339


def test_token_fallback_not_applied_when_log_has_tokens(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    events = _build(db_path, logs_dir, _COMPLETED_LINE)

    ev = _api_attempts(events)[0]
    assert ev["tokens_in"] == 100
    assert ev["tokens_out"] == 50


# ---------------------------------------------------------------------------
# 5. Schemas legacy: sin columnas granulares → sin crash
# ---------------------------------------------------------------------------

def test_legacy_schema_without_granular_columns_no_crash(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    build_synthetic_hermes_store(db_path)
    con = sqlite3.connect(db_path)
    try:
        con.execute("DROP TABLE session_model_usage")
        con.execute(
            "CREATE TABLE session_model_usage ("
            " session_id TEXT, model TEXT, billing_provider TEXT,"
            " billing_base_url TEXT, billing_mode TEXT, api_call_count INTEGER)"
        )
        con.execute(
            "INSERT INTO session_model_usage VALUES (?, ?, ?, ?, ?, ?)",
            (SESSION, MODEL, "custom", "http://127.0.0.1:11434/v1", "", 1),
        )
        con.commit()
    finally:
        con.close()
    create_synthetic_agent_log(logs_dir, _COMPLETED_LINE)

    source = HermesHarvestSource(ledger_path="/tmp/ledger.log", db_path=db_path)
    events = list(source.harvest())  # no debe crashear

    ev = _api_attempts(events)[0]
    assert ev["hermes_session_id"] == SESSION
    assert ev["provider"] == "custom"
    for field in ("cache_read", "cache_write", "reasoning_tokens", "cost_usd",
                  "task", "cost_status", "cost_source"):
        assert field not in ev


def test_no_session_model_usage_table_no_crash(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    build_synthetic_hermes_store(db_path)
    con = sqlite3.connect(db_path)
    try:
        con.execute("DROP TABLE session_model_usage")
        con.commit()
    finally:
        con.close()
    create_synthetic_agent_log(logs_dir, _COMPLETED_LINE)

    source = HermesHarvestSource(ledger_path="/tmp/ledger.log", db_path=db_path)
    events = list(source.harvest())  # no debe crashear

    ev = _api_attempts(events)[0]
    assert ev["hermes_session_id"] == SESSION
    assert ev["mode"] == "unknown"
    for field in ("cache_read", "cache_write", "reasoning_tokens", "cost_usd"):
        assert field not in ev


# ---------------------------------------------------------------------------
# 6. Sin fila de billing → defaults seguros, sin crash
# ---------------------------------------------------------------------------

def test_no_billing_row_safe_defaults(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    # Sesión presente SOLO en el log (no existe en el store ni en usage)
    log_only = (
        "2026-08-13 10:00:00,000 INFO [20260812_194440_69d79b] agent.conversation_loop: "
        "API call #1: model=qwen provider=custom in=10 out=10 total=20 latency=0.1s\n"
    )
    events = _build(db_path, logs_dir, log_only)

    ev = _api_attempts(events)[0]
    assert ev["status"] == "completed"
    assert ev["tokens_in"] == 10
    assert ev["tokens_out"] == 10
    assert ev["mode"] == "unknown"
    for field in ("cache_read", "cache_write", "reasoning_tokens", "cost_usd",
                  "task", "cost_status", "cost_source"):
        assert field not in ev


# ---------------------------------------------------------------------------
# 7. Privacidad (Art. V): ninguna credencial de billing_base_url en el payload
# ---------------------------------------------------------------------------

def test_no_credentials_leaked_via_billing_base_url(tmp_path):
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    events = _build(db_path, logs_dir, _COMPLETED_LINE, usage_fields={
        "billing_base_url": "http://user:hunter2secret@localhost:11434/v1",
    })

    ev = _api_attempts(events)[0]
    assert ev["billing_base_url"] == "http://***@localhost:11434/v1"
    serialized = json.dumps(ev, sort_keys=True)
    assert "hunter2secret" not in serialized
    assert "user:" not in serialized


# ---------------------------------------------------------------------------
# 8. Replay lossless de los campos nuevos
# ---------------------------------------------------------------------------

def test_replay_lossless_granular_fields(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger)
    payload = {
        "hermes_session_id": SESSION,
        "provider": "custom",
        "model": MODEL,
        "mode": "chat",
        "status": "completed",
        "request_ref": "req1",
        "tokens_in": 100,
        "tokens_out": 50,
        "cache_read": 1200,
        "cache_write": 340,
        "reasoning_tokens": 512,
        "cost_usd": 0.05,
        "latency_ms": 1500,
    }
    event = CanonicalEvent(
        event_type=EventType.API_ATTEMPT,
        ctx_id="test_ctx",
        source="test",
        payload=payload,
    )
    writer.append(event)

    state = ReplayEngine(ledger).reconstruct_state()
    assert len(state["api_attempts"]) == 1
    entry = state["api_attempts"][0]
    assert entry["cache_read"] == 1200
    assert entry["cache_write"] == 340
    assert entry["reasoning_tokens"] == 512
    assert entry["cost_usd"] == pytest.approx(0.05)
    assert entry["latency_ms"] == 1500


def test_replay_lossless_granular_fields_end_to_end(tmp_path):
    """El evento emitido por el harvester (con granular) sobrevive el ciclo
    completo: raw → CanonicalEvent (LedgerWriter) → replay."""
    db_path = str(tmp_path / "state.db")
    logs_dir = str(tmp_path / "logs")
    events = _build(db_path, logs_dir, _COMPLETED_LINE, usage_fields={
        "task": "coder_loop", "cache_read_tokens": 1200,
        "cache_write_tokens": 340, "reasoning_tokens": 512,
        "estimated_cost_usd": 0.05, "actual_cost_usd": 0.0,
        "cost_status": "estimated", "cost_source": "pricing_cache",
    })
    raw = _api_attempts(events)[0]

    from causadb._harvester import Harvester

    ledger = str(tmp_path / "ledger.log")
    cursors = str(tmp_path / "cursors.json")
    harvester = Harvester(ledger, cursors)
    source = HermesHarvestSource(ledger, db_path)
    harvester.register_source(source)
    harvester.harvest_all()

    state = ReplayEngine(ledger).reconstruct_state()
    entries = [a for a in state["api_attempts"] if a["request_ref"] == raw["request_ref"]]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["cache_read"] == 1200
    assert entry["cache_write"] == 340
    assert entry["reasoning_tokens"] == 512
    assert entry["cost_usd"] == pytest.approx(0.05)
