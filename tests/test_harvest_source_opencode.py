"""Tests Fase 3 — Puntita opencode (ver docs/design_index.md).

Artículo III (test-first), Artículo VI (replay-determinismo), Artículo IX
(fixture = copia PEQUEÑA del store real ``~/.local/share/opencode/opencode.db``,
no mocks — ver ``tests/fixtures/_build_opencode_fixture.py``).

Desviaciones del plan descubiertas en la auditoría de schema (ver reporte):
  - los parts de razonamiento son ``type="reasoning"`` (no "thinking");
  - los tool parts son ``type="tool"`` (no "tool_use");
  - ``part.id`` es texto (``prt_...``) → cursor por ``rowid`` (existe).

Cobertura:
  1. detect() True con fixture / False sin db
  2. harvest de la fixture → exactamente 2 eventos: REASONING_STEP (part
     reasoning) + TOOL_CALLED (part tool) — los parts text/step-* no generan
  3. dos corridas → segunda devuelve 0 eventos (sin duplicados)
  4. anti-teatro: cursor no avanza si el ledger write falla
  5. replay-determinismo (Artículo VI): mismo harvest → mismo state
  6. anti-teatro: la conexión es read-only (el fixture no se modifica)
"""

import hashlib
import json
import os
import shutil

import pytest

from causadb._harvester import Harvester
from causadb._harvest_source_opencode import OpenCodeHarvestSource
from causadb._replay_engine import ReplayEngine

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_DB = "opencode_fixture.db"

REASONING_TEXT = (
    "The user wants me to run several test commands in sequence to verify the "
    "LedgerIndex fix works. Let me start by loading the testing-strategies "
    "skill, then run the tests as specified."
)
SUBJECT_SYNTH = "The user wants me to run several test"  # primeras 8 palabras
TOOL_CALL_ID = "call_00_rfioiYYb2zmsv5R0CpKN7122"
REASONING_TS_MS = 1785103951154  # data.time.start (part reasoning)
TOOL_TS_MS = 1785103951852  # data.state.time.start (part tool)


def _install_fixture(tmp_path):
    """Copia la fixture (db real recortado) a un dir temporal."""
    dest = tmp_path / FIXTURE_DB
    shutil.copy(os.path.join(FIXTURE_DIR, FIXTURE_DB), dest)
    return str(dest)


def _make_source(tmp_path, ledger_path=None):
    db_path = _install_fixture(tmp_path)
    return OpenCodeHarvestSource(
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
    assert source.source_type() == "opencode"  # SIN colon (fix de namespace)
    assert source.cursor_key() == "agent:opencode"


def test_detect_false_without_db(tmp_path):
    source = OpenCodeHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        db_path=str(tmp_path / "no-existe.db"),
    )
    assert source.detect() is False


# ---------------------------------------------------------------------------
# 2. harvest de la fixture → 2 eventos canónicos
# ---------------------------------------------------------------------------

def test_harvest_fixture_maps_to_expected_events(tmp_path):
    """5 parts (text, step-start, reasoning, tool, step-finish) → solo los
    parts reasoning y tool generan eventos: REASONING_STEP + TOOL_CALLED."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    # Raw dicts (sin escribir): exactamente 2
    raws = list(source.harvest(None))
    assert len(raws) == 2, f"Esperaba 2 raws (reasoning + tool), obtuvo {len(raws)}"

    reasoning = [r for r in raws if r["type"] == "REASONING_STEP"]
    tool = [r for r in raws if r["type"] == "TOOL_CALLED"]
    assert len(reasoning) == 1 and len(tool) == 1

    r = reasoning[0]
    assert r["description"] == REASONING_TEXT
    assert r["subject"] == SUBJECT_SYNTH          # sujeto sintetizado (opencode no trae)
    assert r["step_type"] == "analysis"           # heurística del motor universal
    assert r["step_hash"] == hashlib.sha256(REASONING_TEXT.encode("utf-8")).hexdigest()
    assert r["agent"] == "testing"
    # timestamp: data.time.start (ms) → ISO
    assert r["timestamp"] == "2026-07-26T22:12:31.154000Z"

    t = tool[0]
    assert t["tool_name"] == "skill"
    assert t["arguments"] == {"name": "testing-strategies"}
    assert t["tool_call_id"] == TOOL_CALL_ID
    assert t["agent"] == "testing"
    assert t["timestamp"] == "2026-07-26T22:12:31.852000Z"
    # result: output de la tool (recortado en fixture, íntegro en prod)
    assert t["result"].startswith('<skill_content name="testing-strategies">')
    assert t["result"].endswith("[recortado-fixture]")

    # Flujo completo: harvest_all escribe 2 eventos al ledger
    result = h.harvest_all()
    assert result["opencode"] == 2
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 2
    etypes = sorted(e["event"]["event_type"] for e in entries)
    assert etypes == ["REASONING_STEP", "TOOL_CALLED"]
    agent_ids = {e["event"]["payload"]["agent"] for e in entries}
    assert agent_ids == {"testing"}


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
    assert r1["opencode"] == 2
    r2 = h.harvest_all()
    assert r2["opencode"] == 0, f"Segunda corrida debe dar 0, obtuvo {r2}"

    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 2  # sin duplicados
    ids = {e["event"]["event_id"] for e in entries}
    assert len(ids) == 2


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
    assert "opencode" in result
    assert result["opencode"] == 0
    # El cursor NO avanzó (mismo contrato que las otras fuentes)
    assert (
        not os.path.exists(config)
        or os.path.getsize(config) == 0
        or json.load(open(config)) == {}
    ), "El cursor no debe avanzar si el write falló"

    # Corrida siguiente con write OK → cosecha TODO (2) sin pérdida
    r2 = h.harvest_all()
    assert r2["opencode"] == 2
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 2
    assert len({e["event"]["event_id"] for e in entries}) == 2


# ---------------------------------------------------------------------------
# 5. replay-determinismo (Artículo VI)
# ---------------------------------------------------------------------------

def test_replay_determinism_opencode_harvest(tmp_path):
    state1 = _harvest_and_replay(tmp_path, "w1")
    state2 = _harvest_and_replay(tmp_path, "w2")
    assert state1 == state2, "Mismo harvest → mismo state de replay (Art. VI)"


def _harvest_and_replay(tmp_path, name):
    workdir = tmp_path / name
    workdir.mkdir()
    ledger = str(workdir / "ledger.log")
    config = str(workdir / "cursors.json")
    source = _make_source(workdir, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)
    assert h.harvest_all()["opencode"] == 2
    state = ReplayEngine(ledger).reconstruct_state()
    assert state["events_applied"] == 2
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
    assert h.harvest_all()["opencode"] == 2
    assert h.harvest_all()["opencode"] == 0
    assert _fixture_bytes() == before, "mode=ro debe dejar el db intacto"

    # No deben quedar side-files (wal/shm) junto al fixture original
    side_files = [f for f in os.listdir(FIXTURE_DIR)
                  if f.startswith(FIXTURE_DB + "-")]
    assert side_files == []


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


# ---------------------------------------------------------------------------
# GAP-01 — forward-compat: config del agente declara el store (t12)
# ---------------------------------------------------------------------------

def test_db_path_from_agent_config_forward_compat(tmp_path, monkeypatch):
    """Sin db_path explícito: ~/.config/opencode/opencode.json key 'data'
    declara el store (forward-compat). Env override gana."""
    home = tmp_path / "home"
    cfg_dir = home / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    custom_db = tmp_path / "custom.db"
    shutil.copy(os.path.join(FIXTURE_DIR, FIXTURE_DB), custom_db)
    (cfg_dir / "opencode.json").write_text(json.dumps({"data": str(custom_db)}))
    monkeypatch.setenv("HOME", str(home))

    source = OpenCodeHarvestSource(ledger_path=str(tmp_path / "ledger.log"))
    assert source.db_path == str(custom_db)
    assert source.detect() is True
    raws = list(source.harvest(None))
    assert len(raws) == 2

    # env override gana sobre el config
    env_db = tmp_path / "env.db"
    shutil.copy(os.path.join(FIXTURE_DIR, FIXTURE_DB), env_db)
    monkeypatch.setenv("CAUSADB_OPENCODE_DB_PATH", str(env_db))
    source2 = OpenCodeHarvestSource(ledger_path=str(tmp_path / "ledger.log"))
    assert source2.db_path == str(env_db)
