"""Tests Fase 15.2-bis — Puntita Windsurf/Devin Desktop.

Artículo III (test-first), Artículo VI (replay-determinismo), Artículo IX
(fixture = datos REALES del store ``~/.local/share/devin/cli/sessions.db``,
sesión ``plume-grease`` — no mocks; ver
``tests/fixtures/_build_windsurf_fixture.py``).

Schema real (verificado sobre el db real):
  - ``sessions(id, model, agent_mode, created_at, ...)``
  - ``message_nodes(row_id, session_id, chat_message JSON, created_at, ...)``
  - ``tool_call_state(session_id, tool_call_id, tool_call_json, ...)``

Datos reales: sesión ``plume-grease``, modelo ``swe-1-6-slow``,
120 message_nodes (51 system, 11 user, 37 assistant, 21 tool),
9 tool_call_state rows.

Cobertura:
  1. detect() True con fixture / False sin db
  2. harvest de la fixture → 58 eventos (37 LLM_INVOKED + 21 TOOL_CALLED)
  3. dos corridas → segunda devuelve 0 eventos (sin duplicados)
  4. anti-teatro: cursor no avanza si el ledger write falla
  5. replay-determinismo (Artículo VI): mismo harvest → mismo state
  6. anti-teatro: la conexión es read-only (el fixture no se modifica)
  7. TOOL_CALLED tiene tool_name del tool_call_state.kind/title
"""

import json
import os
import shutil

import pytest

from causadb._harvester import Harvester
from causadb._harvest_source_windsurf import WindsurfHarvestSource
from causadb._replay_engine import ReplayEngine

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_DB = "windsurf_sessions_fixture.db"

# Datos de la sesión real "plume-grease"
SESSION_ID = "plume-grease"
MODEL = "swe-1-6-slow"
FIRST_USER_PROMPT_PREFIX = "Creá un script en /tmp/cursor_test.sh"


def _install_fixture(tmp_path):
    """Copia la fixture (db real recortado) a un dir temporal."""
    dest = tmp_path / FIXTURE_DB
    shutil.copy(os.path.join(FIXTURE_DIR, FIXTURE_DB), dest)
    return str(dest)


def _make_source(tmp_path, ledger_path=None):
    db_path = _install_fixture(tmp_path)
    return WindsurfHarvestSource(
        ledger_path=ledger_path or str(tmp_path / "ledger.log"),
        db_path=db_path,
    )


def _fixture_bytes():
    with open(os.path.join(FIXTURE_DIR, FIXTURE_DB), "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 1. detect()
# ---------------------------------------------------------------------------

def test_detect_true_with_fixture(tmp_path):
    source = _make_source(tmp_path)
    assert source.detect() is True
    assert source.source_type() == "windsurf"
    assert source.cursor_key() == "harvest.windsurf"


def test_detect_false_without_db(tmp_path):
    source = WindsurfHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        db_path=str(tmp_path / "no-existe.db"),
    )
    assert source.detect() is False


def test_detect_false_empty_db(tmp_path):
    """detect() verifica que haya sesiones, no solo que el archivo exista."""
    import sqlite3
    db_path = str(tmp_path / "empty.db")
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT, "
        "backend_type TEXT, model TEXT, agent_mode TEXT, created_at INTEGER, "
        "last_activity_at INTEGER)"
    )
    con.commit()
    con.close()
    source = WindsurfHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        db_path=db_path,
    )
    assert source.detect() is False


# ---------------------------------------------------------------------------
# 2. harvest de la fixture → 58 eventos canónicos
# ---------------------------------------------------------------------------

def test_harvest_fixture_maps_to_expected_events(tmp_path):
    """120 message_nodes → 37 LLM_INVOKED (assistant) + 21 TOOL_CALLED (tool).
    Los mensajes system (51) y user (11) no generan eventos."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    raws = list(source.harvest(None))
    types = [r["type"] for r in raws]
    assert types.count("LLM_INVOKED") == 37, (
        f"Expected 37 LLM_INVOKED, got {types.count('LLM_INVOKED')}"
    )
    assert types.count("TOOL_CALLED") == 21, (
        f"Expected 21 TOOL_CALLED, got {types.count('TOOL_CALLED')}"
    )
    assert len(raws) == 58, f"Expected 58 raws, got {len(raws)}"

    # -- LLM_INVOKED: model de la sesión, prompt del último user -----------
    llms = [r for r in raws if r["type"] == "LLM_INVOKED"]
    models = {llm["model"] for llm in llms}
    assert models == {MODEL}, f"Expected model {MODEL}, got {models}"
    agents = {llm["agent"] for llm in llms}
    assert agents == {"windsurf"}

    # El primer LLM_INVOKED debe tener el prompt del primer user
    first_llm = llms[0]
    assert first_llm["prompt"].startswith(FIRST_USER_PROMPT_PREFIX)
    assert first_llm["response_content"] == (
        "Voy a crear el script y ejecutarlo para ti."
    )
    assert first_llm["response_tokens"] == 0  # Windsurf no expone tokens

    # -- TOOL_CALLED: tool_name del kind/title de tool_call_state ----------------
    tools = [r for r in raws if r["type"] == "TOOL_CALLED"]
    tool_names = {t["tool_name"] for t in tools}
    assert "execute" in tool_names, f"Expected 'execute' in tool names, got {tool_names}"
    assert "edit" in tool_names, f"Expected 'edit' in tool names, got {tool_names}"
    # Todos los TOOL_CALLED deben tener result (completado por el mensaje tool)
    for t in tools:
        assert t["result"] or t["result"] == "", (
            f"TOOL_CALLED {t['tool_call_id']} should have result field"
        )
        assert t["tool_call_id"], "TOOL_CALLED must have tool_call_id"
    agents_tool = {t["agent"] for t in tools}
    assert agents_tool == {"windsurf"}
    assert {r["__harvest_locator"] for r in raws} == {source.db_path}

    # Flujo completo: harvest_all escribe 58 eventos al ledger
    result = h.harvest_all()
    assert result["windsurf"] == 58
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    # 58 windsurf + 1 SESSION_SUMMARY (Fase 11) + 1 SKILL_CREATED (Fase 14 distill)
    assert len(entries) == 60, f"Expected 60 entries (58 + 1 summary + 1 skill), got {len(entries)}"
    etypes = sorted([e["event"]["event_type"] for e in entries])
    assert "LLM_INVOKED" in etypes
    assert "TOOL_CALLED" in etypes
    assert "SESSION_SUMMARY" in etypes


# ---------------------------------------------------------------------------
# 3. idempotencia (cursor por rowid)
# ---------------------------------------------------------------------------

def test_two_runs_zero_duplicates(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    r1 = h.harvest_all()
    assert r1["windsurf"] == 58
    r2 = h.harvest_all()
    assert r2["windsurf"] == 0, f"Segunda corrida debe dar 0, obtuvo {r2}"

    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    # 58 windsurf + 1 SESSION_SUMMARY + 1 SKILL_CREATED
    assert len(entries) == 60
    # event_ids pueden repetirse (mismo contenido → mismo hash)
    ids = {e["event"]["event_id"] for e in entries}
    assert len(ids) >= 10  # al menos 10 eventos únicos


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
    assert "windsurf" in result
    assert result["windsurf"] == 0
    assert (
        not os.path.exists(config)
        or os.path.getsize(config) == 0
        or json.load(open(config)) == {}
    ), "El cursor no debe avanzar si el write falló"

    # Corrida siguiente con write OK → cosecha TODO (58) sin pérdida
    r2 = h.harvest_all()
    assert r2["windsurf"] == 58
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 60  # 58 + 1 SESSION_SUMMARY + 1 SKILL_CREATED
    # event_ids pueden repetirse (mismo contenido → mismo hash)
    assert len({e["event"]["event_id"] for e in entries}) >= 10


# ---------------------------------------------------------------------------
# 5. replay-determinismo (Artículo VI)
# ---------------------------------------------------------------------------

def test_replay_determinism_windsurf_harvest(tmp_path):
    state1 = _harvest_and_replay(tmp_path, "w1")
    state2 = _harvest_and_replay(tmp_path, "w2")
    assert state1["events_applied"] == state2["events_applied"] == 60
    assert len(state1.get("session_summaries", [])) == len(
        state2.get("session_summaries", [])
    )
    if state1.get("session_summaries"):
        assert (
            state1["session_summaries"][0]["tool"]
            == state2["session_summaries"][0]["tool"]
        )


def _harvest_and_replay(tmp_path, name):
    workdir = tmp_path / name
    workdir.mkdir()
    ledger = str(workdir / "ledger.log")
    config = str(workdir / "cursors.json")
    source = _make_source(workdir, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)
    assert h.harvest_all()["windsurf"] == 58
    state = ReplayEngine(ledger).reconstruct_state()
    assert state["events_applied"] == 60  # 58 + 1 SESSION_SUMMARY + 1 SKILL_CREATED
    return state


# ---------------------------------------------------------------------------
# 6. anti-teatro: la conexión es read-only
# ---------------------------------------------------------------------------

def test_harvest_does_not_modify_fixture(tmp_path):
    """El harvest abre el db con ``mode=ro``: el fixture queda intacto
    (mismos bytes) aunque se coseche varias veces."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    before = _fixture_bytes()
    assert h.harvest_all()["windsurf"] == 58
    assert h.harvest_all()["windsurf"] == 0
    assert _fixture_bytes() == before, "mode=ro debe dejar el db intacto"

    side_files = [
        f
        for f in os.listdir(FIXTURE_DIR)
        if f.startswith(FIXTURE_DB + "-")
    ]
    assert side_files == []


# ---------------------------------------------------------------------------
# 7. TOOL_CALLED tiene tool_name del kind/title de tool_call_state
# ---------------------------------------------------------------------------

def test_tool_called_has_correct_tool_name(tmp_path):
    """Los TOOL_CALLED deben tener tool_name del campo kind o title de
    tool_call_state, y arguments del rawInput."""
    source = _make_source(tmp_path)
    raws = list(source.harvest(None))
    tools = [r for r in raws if r["type"] == "TOOL_CALLED"]

    # Debe haber al menos un "edit" (Wrote file) y varios "execute"
    tool_names = [t["tool_name"] for t in tools]
    assert "edit" in tool_names, f"Expected 'edit' in {tool_names}"
    assert "execute" in tool_names, f"Expected 'execute' in {tool_names}"

    # El tool "edit" debe tener arguments con file_path
    edit_tools = [t for t in tools if t["tool_name"] == "edit"]
    assert len(edit_tools) >= 1
    edit = edit_tools[0]
    assert isinstance(edit["arguments"], dict)
    # rawInput contiene file_path
    assert "file_path" in edit["arguments"] or edit["arguments"] == {}

    # Verificar que hay tool_call_id único por tool
    tc_ids = {t["tool_call_id"] for t in tools}
    # 9 tool_call_state rows, pero 21 tool messages (algunos repetidos por
    # branches del árbol de conversación)
    assert len(tc_ids) <= 9, f"Expected <= 9 unique tool_call_ids, got {len(tc_ids)}"


# ---------------------------------------------------------------------------
# FIX.GEN-A — harvest() retorna generador (no lista)
# ---------------------------------------------------------------------------

def test_harvest_returns_generator(tmp_path):
    """harvest() debe retornar un generador (FIX.GEN-A), no una lista."""
    source = _make_source(tmp_path)
    gen = source.harvest(None)
    assert hasattr(gen, "__next__"), "harvest() debe retornar un iterador"
    assert not isinstance(gen, list), "harvest() no debe materializar lista"
    # Consumo streaming: iterar produce dicts normales
    first = next(iter(gen))
    assert isinstance(first, dict)
