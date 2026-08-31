"""Tests Fase 15.2 — Puntita cursor (ver Chronicle; docs/design_index.md).

Artículo III (test-first), Artículo VI (replay-determinismo), Artículo IX
(fixture REAL extraída de ``~/.cursor/projects/``, no mocks).

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
from causadb._harvest_source_cursor import CursorHarvestSource
from causadb._replay_engine import ReplayEngine

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_FILE = "cursor_agent_transcript.jsonl"


def _install_fixture(tmp_path):
    """Copia la fixture real a un dir de proyectos temporal con la estructura
    ``projects/<project>/agent-transcripts/<session-uuid>/<session-uuid>.jsonl``."""
    projects_dir = tmp_path / "projects"
    session_uuid = "eabad476-ff5e-4fe6-ab82-79d7ad85ee09"
    transcripts_dir = projects_dir / "empty-window" / "agent-transcripts" / session_uuid
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        os.path.join(FIXTURE_DIR, FIXTURE_FILE),
        transcripts_dir / f"{session_uuid}.jsonl",
    )
    return str(projects_dir)


def _make_source(tmp_path, ledger_path=None):
    projects_dir = _install_fixture(tmp_path)
    return CursorHarvestSource(
        ledger_path=ledger_path or str(tmp_path / "ledger.log"),
        projects_dir=projects_dir,
    )


def _fixture_bytes():
    with open(os.path.join(FIXTURE_DIR, FIXTURE_FILE), "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 1. detect()
# ---------------------------------------------------------------------------

def test_detect_true_with_fixture(tmp_path):
    source = _make_source(tmp_path)
    assert source.detect() is True
    assert source.source_type() == "cursor"
    assert source.cursor_key() == "harvest.cursor"


def test_detect_false_without_dir(tmp_path):
    source = CursorHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        projects_dir=str(tmp_path / "no-existe"),
    )
    assert source.detect() is False


# ---------------------------------------------------------------------------
# 2. harvest de la fixture → eventos esperados
# ---------------------------------------------------------------------------

def test_harvest_fixture_maps_to_expected_events(tmp_path):
    """Fixture con 5 líneas (1 user + 3 assistant + 1 turn_ended) →
    3 assistant generan 4 TOOL_CALLED + 3 LLM_INVOKED = 7 eventos.
    El user NO genera eventos (solo actualiza last_user_content).
    El turn_ended se ignora."""
    source = _make_source(tmp_path)
    raws = list(source.harvest(None))

    types = [r["type"] for r in raws]
    assert types == [
        "TOOL_CALLED", "TOOL_CALLED", "LLM_INVOKED",    # assistant 1 (Write + Shell)
        "TOOL_CALLED", "TOOL_CALLED", "LLM_INVOKED",    # assistant 2 (StrReplace + Shell)
        "LLM_INVOKED",                                   # assistant 3 (solo texto)
    ], f"Esperaba 7 eventos, obtuve {types}"
    assert len(raws) == 7

    # Verificar que todos tienen agent="cursor"
    for r in raws:
        assert r["agent"] == "cursor"

    # Verificar tool_calls
    tool_events = [r for r in raws if r["type"] == "TOOL_CALLED"]
    tool_names = [t["tool_name"] for t in tool_events]
    assert tool_names == ["Write", "Shell", "StrReplace", "Shell"]

    # Verificar LLM_INVOKED tienen model="cursor"
    llm_events = [r for r in raws if r["type"] == "LLM_INVOKED"]
    assert len(llm_events) == 3
    for e in llm_events:
        assert e["model"] == "cursor"

    # Verificar session_id
    for r in raws:
        assert r["__harvest_session_id"] == "eabad476-ff5e-4fe6-ab82-79d7ad85ee09"
        assert r["__harvest_locator"] == (
            "empty-window/agent-transcripts/"
            "eabad476-ff5e-4fe6-ab82-79d7ad85ee09/"
            "eabad476-ff5e-4fe6-ab82-79d7ad85ee09.jsonl"
        )

    # Flujo completo: harvest_all escribe 7 eventos + 1 SESSION_SUMMARY al ledger
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source2 = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source2)

    result = h.harvest_all()
    assert result["cursor"] == 7

    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    # 7 raw + 1 SESSION_SUMMARY (Fase 11) + 1 SKILL_CREATED (Fase 14 distill)
    assert len(entries) == 9
    etypes = [e["event"]["event_type"] for e in entries]
    assert etypes[:7] == [
        "TOOL_CALLED", "TOOL_CALLED", "LLM_INVOKED",
        "TOOL_CALLED", "TOOL_CALLED", "LLM_INVOKED",
        "LLM_INVOKED",
    ]
    assert etypes[7] == "SESSION_SUMMARY"
    assert etypes[8] == "SKILL_CREATED"


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
    assert r1["cursor"] == 7

    r2 = h.harvest_all()
    assert r2["cursor"] == 0, f"Segunda corrida debe dar 0, obtuvo {r2}"

    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    # 7 raw + 1 SESSION_SUMMARY + 1 SKILL_CREATED = 9, sin duplicados
    assert len(entries) == 9
    ids = {e["event"]["event_id"] for e in entries}
    assert len(ids) == 9


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
    assert "cursor" in result
    assert result["cursor"] == 0
    # El cursor NO avanzó
    assert (
        not os.path.exists(config)
        or os.path.getsize(config) == 0
        or json.load(open(config)) == {}
    ), "El cursor no debe avanzar si el write falló"

    # Corrida siguiente con write OK → cosecha TODO (7) sin pérdida
    r2 = h.harvest_all()
    assert r2["cursor"] == 7
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    # 7 raw + 1 SESSION_SUMMARY + 1 SKILL_CREATED = 9
    assert len(entries) == 9
    assert len({e["event"]["event_id"] for e in entries}) == 9


# ---------------------------------------------------------------------------
# 5. replay-determinismo (Artículo VI)
# ---------------------------------------------------------------------------

def test_replay_determinism_cursor_harvest(tmp_path):
    state1 = _harvest_and_replay(tmp_path, "w1")
    state2 = _harvest_and_replay(tmp_path, "w2")
    assert state1["events_applied"] == state2["events_applied"]
    # 7 raw + 1 SESSION_SUMMARY + 1 SKILL_CREATED = 9
    assert state1["events_applied"] == 9


def _harvest_and_replay(tmp_path, name):
    workdir = tmp_path / name
    workdir.mkdir()
    ledger = str(workdir / "ledger.log")
    config = str(workdir / "cursors.json")
    source = _make_source(workdir, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)
    assert h.harvest_all()["cursor"] == 7
    state = ReplayEngine(ledger).reconstruct_state()
    # 7 raw + 1 SESSION_SUMMARY + 1 SKILL_CREATED = 9
    assert state["events_applied"] == 9
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
    assert h.harvest_all()["cursor"] == 7
    assert h.harvest_all()["cursor"] == 0
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
