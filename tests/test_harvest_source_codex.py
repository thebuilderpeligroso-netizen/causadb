"""Tests Fase 15.2-ter — Puntita codex (BIT-CHR.16; docs/design_index.md).

Artículo III (test-first), Artículo VI (replay-determinismo), Artículo IX
(fixture REAL extraída de ``~/.codex/sessions/``, no mocks).

Cobertura:
  1. detect() True con fixture / False sin el dir
  2. harvest de la fixture → eventos esperados
  3. dos corridas → segunda devuelve 0 eventos (sin duplicados)
  4. anti-teatro: cursor no avanza si el ledger write falla
  5. replay-determinismo (Artículo VI): mismo harvest → mismo state
  6. anti-teatro: la lectura es read-only (el fixture no se modifica)
"""

import json
import os
import shutil

import pytest

from causadb._harvester import Harvester
from causadb._harvest_source_codex import CodexHarvestSource
from causadb._replay_engine import ReplayEngine

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_FILE = "codex_rollout_fixture.jsonl"


def _install_fixture(tmp_path):
    """Copia la fixture real a un dir de sesiones temporal con la estructura
    ``sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl``."""
    sessions_dir = tmp_path / "sessions" / "2026" / "08" / "02"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        os.path.join(FIXTURE_DIR, FIXTURE_FILE),
        sessions_dir / "rollout-fixture-001.jsonl",
    )
    return str(tmp_path)


def _make_source(tmp_path, ledger_path=None):
    codex_dir = _install_fixture(tmp_path)
    return CodexHarvestSource(
        ledger_path=ledger_path or str(tmp_path / "ledger.log"),
        codex_dir=codex_dir,
    )


def _fixture_bytes():
    with open(os.path.join(FIXTURE_DIR, FIXTURE_FILE), "rb") as f:
        return f.read()


def _write_current_codex_session(tmp_path):
    """Current Codex rollout shape observed in a live 2026-08-12 session."""
    sessions = tmp_path / "sessions" / "2026" / "08" / "12"
    sessions.mkdir(parents=True)
    path = sessions / "rollout-live-001.jsonl"
    lines = [
        {"timestamp": "2026-08-12T15:06:40Z", "type": "session_meta",
         "payload": {"session_id": "live-codex-session"}},
        {"timestamp": "2026-08-12T15:06:41Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-luna", "selected_model": "gpt-5.6-luna"}},
        {"timestamp": "2026-08-12T15:06:42Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "ping"}]}},
        {"timestamp": "2026-08-12T15:06:43Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "pong"}]}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return str(tmp_path)


# ---------------------------------------------------------------------------
# 1. detect()
# ---------------------------------------------------------------------------

def test_detect_true_with_fixture(tmp_path):
    source = _make_source(tmp_path)
    assert source.detect() is True
    assert source.source_type() == "codex"
    assert source.cursor_key() == "harvest.codex"


def test_detect_false_without_dir(tmp_path):
    source = CodexHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        codex_dir=str(tmp_path / "no-existe"),
    )
    assert source.detect() is False


# ---------------------------------------------------------------------------
# 2. harvest de la fixture → eventos esperados
# ---------------------------------------------------------------------------

def test_harvest_fixture_maps_to_expected_events(tmp_path):
    """Fixture con 5 response_items (3 developer + 2 user) →
    los 3 developer generan LLM_INVOKED cada uno (asistente + model).
    Los 2 user NO generan eventos (solo actualizan last_user_content).
    Total esperado: 3 LLM_INVOKED."""
    source = _make_source(tmp_path)
    raws = list(source.harvest(None))

    types = [r["type"] for r in raws]
    # 3 developer messages → 3 LLM_INVOKED (asistente + model presente)
    assert types == ["LLM_INVOKED", "LLM_INVOKED", "LLM_INVOKED"], (
        f"Esperaba 3 LLM_INVOKED, obtuve {types}"
    )
    assert len(raws) == 3

    # Verificar que todos tienen agent="codex"
    for r in raws:
        assert r["agent"] == "codex"

    # El modelo viene del turn_context
    for r in raws:
        assert r["model"] == "gpt-5.6-sol"

    # Verificar session_id
    for r in raws:
        assert r["__harvest_session_id"] == "fixture-session-001"

    # Flujo completo: harvest_all escribe 3 eventos + 1 SESSION_SUMMARY al ledger
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source2 = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source2)

    result = h.harvest_all()
    assert result["codex"] == 3

    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    # 3 raw + 1 SESSION_SUMMARY (Fase 11)
    assert len(entries) == 4
    etypes = [e["event"]["event_type"] for e in entries]
    assert etypes[:3] == ["LLM_INVOKED", "LLM_INVOKED", "LLM_INVOKED"]
    assert etypes[3] == "SESSION_SUMMARY"


def test_harvest_current_live_codex_shape(tmp_path):
    """C1: current live Codex uses assistant/output_text, not developer."""
    codex_dir = _write_current_codex_session(tmp_path)
    source = CodexHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"), codex_dir=codex_dir
    )
    raws = list(source.harvest(None))

    assert [raw["type"] for raw in raws] == ["LLM_INVOKED"]
    assert raws[0]["model"] == "gpt-5.6-luna"
    assert raws[0]["response_content"] == "pong"
    assert raws[0]["__harvest_session_id"] == "live-codex-session"
    assert raws[0]["__harvest_locator"] == "2026/08/12/rollout-live-001.jsonl"


# ---------------------------------------------------------------------------
# 3. idempotencia (cursor por offset)
# ---------------------------------------------------------------------------

def test_two_runs_zero_duplicates(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    r1 = h.harvest_all()
    assert r1["codex"] == 3

    r2 = h.harvest_all()
    assert r2["codex"] == 0, f"Segunda corrida debe dar 0, obtuvo {r2}"

    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    # 3 raw + 1 SESSION_SUMMARY = 4, sin duplicados
    assert len(entries) == 4
    ids = {e["event"]["event_id"] for e in entries}
    assert len(ids) == 4


# ---------------------------------------------------------------------------
# 4. anti-teatro: cursor no avanza si el write falla
# ---------------------------------------------------------------------------

def test_cursor_not_advanced_on_write_failure(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    import unittest.mock as um
    with um.patch.object(h._writer, "append", side_effect=OSError("disk full")):
        result = h.harvest_all()
    # harvest_all no crashea (aislamiento por fuente, auditoría I.2)
    assert "codex" in result
    assert result["codex"] == 0
    # El cursor NO avanzó
    assert (
        not os.path.exists(config)
        or os.path.getsize(config) == 0
        or json.load(open(config)) == {}
    ), "El cursor no debe avanzar si el write falló"

    # Corrida siguiente con write OK → cosecha TODO (3) sin pérdida
    r2 = h.harvest_all()
    assert r2["codex"] == 3
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    # 3 raw + 1 SESSION_SUMMARY = 4
    assert len(entries) == 4
    assert len({e["event"]["event_id"] for e in entries}) == 4


# ---------------------------------------------------------------------------
# 5. replay-determinismo (Artículo VI)
# ---------------------------------------------------------------------------

def test_replay_determinism_codex_harvest(tmp_path):
    state1 = _harvest_and_replay(tmp_path, "w1")
    state2 = _harvest_and_replay(tmp_path, "w2")
    assert state1["events_applied"] == state2["events_applied"]
    # 3 raw + 1 SESSION_SUMMARY = 4
    assert state1["events_applied"] == 4


def _harvest_and_replay(tmp_path, name):
    workdir = tmp_path / name
    workdir.mkdir()
    ledger = str(workdir / "ledger.log")
    config = str(workdir / "cursors.json")
    source = _make_source(workdir, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)
    assert h.harvest_all()["codex"] == 3
    state = ReplayEngine(ledger).reconstruct_state()
    # 3 raw + 1 SESSION_SUMMARY = 4
    assert state["events_applied"] == 4
    return state


# ---------------------------------------------------------------------------
# 6. anti-teatro: la lectura es read-only
# ---------------------------------------------------------------------------

def test_harvest_does_not_modify_fixture(tmp_path):
    """El harvest solo lee archivos JSONL: el fixture queda intacto
    (mismos bytes) aunque se coseche varias veces."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    before = _fixture_bytes()
    assert h.harvest_all()["codex"] == 3
    assert h.harvest_all()["codex"] == 0
    assert _fixture_bytes() == before, "El fixture no debe modificarse"


# ---------------------------------------------------------------------------
# FIX.GEN-B — harvest() retorna generador (no lista)
# ---------------------------------------------------------------------------

def test_harvest_returns_generator(tmp_path):
    """harvest() debe retornar un generador (FIX.GEN-B), no una lista."""
    source = _make_source(tmp_path)
    gen = source.harvest(None)
    assert hasattr(gen, "__next__"), "harvest() debe retornar un iterador"
    assert not isinstance(gen, list), "harvest() no debe materializar lista"
    # Consumo streaming: iterar produce dicts normales
    first = next(iter(gen))
    assert isinstance(first, dict)
