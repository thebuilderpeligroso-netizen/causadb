"""Tests Fase 15.4 — Puntita n8n.

Artículo III (test-first), Artículo VI (replay-determinismo), Artículo IX
(fixture = copia PEQUEÑA del store real ``~/.n8n/database.sqlite``,
no mocks — ver ``tests/fixtures/_build_n8n_fixture.py``).

Fixture incluye:
  - exec 1: workflow "CausaDB Fixture with Webhook" (webhook, error)
  - exec 2: workflow "CausaDB Manual Trigger" (manual, success)

Cobertura:
  1. detect() True con fixture / False sin db
  2. harvest de la fixture → 2 COMMAND_RUN + 1 OBSERVATION (el error)
  3. dos corridas → segunda devuelve 0 eventos (sin duplicados)
  4. anti-teatro: cursor no avanza si el ledger write falla
  5. replay-determinismo (Artículo VI): mismo harvest → mismo state
  6. anti-teatro: la conexión es read-only (el fixture no se modifica)
"""

import json
import os
import shutil

import pytest

from causadb._harvester import Harvester
from causadb._harvest_source_n8n import N8nHarvestSource
from causadb._replay_engine import ReplayEngine

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_DB = "n8n_fixture.sqlite"


def _install_fixture(tmp_path):
    """Copia la fixture (db real recortado) a un dir temporal."""
    dest = tmp_path / FIXTURE_DB
    shutil.copy(os.path.join(FIXTURE_DIR, FIXTURE_DB), dest)
    return str(dest)


def _make_source(tmp_path, ledger_path=None):
    db_path = _install_fixture(tmp_path)
    return N8nHarvestSource(
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
    assert source.source_type() == "n8n"
    assert source.cursor_key() == "harvest.n8n"


def test_detect_false_without_db(tmp_path):
    source = N8nHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        db_path=str(tmp_path / "no-existe.db"),
    )
    assert source.detect() is False


# ---------------------------------------------------------------------------
# 2. harvest de la fixture → 3 eventos (2 COMMAND_RUN + 1 OBSERVATION)
# ---------------------------------------------------------------------------

def test_harvest_fixture_maps_to_expected_events(tmp_path):
    """2 ejecuciones (1 error + 1 success) → 2 COMMAND_RUN + 1 OBSERVATION."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    # Raw dicts (sin escribir): exactamente 3
    raws = source.harvest(None)
    assert len(raws) == 3, f"Esperaba 3 raws (2 COMMAND_RUN + 1 OBSERVATION), obtuvo {len(raws)}"

    commands = [r for r in raws if r["type"] == "COMMAND_RUN"]
    observations = [r for r in raws if r["type"] == "OBSERVATION"]
    assert len(commands) == 2, f"Esperaba 2 COMMAND_RUN, obtuvo {len(commands)}"
    assert len(observations) == 1, f"Esperaba 1 OBSERVATION, obtuvo {len(observations)}"

    # Verificar COMMAND_RUN del webhook (error)
    webhook_cmd = [c for c in commands if c["mode"] == "webhook"]
    assert len(webhook_cmd) == 1
    wc = webhook_cmd[0]
    assert wc["command"] == "n8n:run:CausaDB Fixture with Webhook"
    assert wc["status"] == "error"
    assert wc["finished"] is False
    assert wc["execution_id"] == 1
    assert wc["__harvest_id"] == 1

    # Verificar COMMAND_RUN del manual trigger (exec 2)
    manual_cmd = [c for c in commands if c["mode"] == "manual"]
    assert len(manual_cmd) == 1
    mc = manual_cmd[0]
    assert mc["command"] == "n8n:run:CausaDB Manual Trigger"
    assert mc["status"] == "success"
    assert mc["finished"] is True
    assert mc["execution_id"] == 2
    assert mc["__harvest_id"] == 2
    # Debe tener nodes ejecutados
    assert "nodes" in mc
    assert "mt-001" in mc["nodes"]

    # Verificar OBSERVATION (del error)
    obs = observations[0]
    assert obs["severity"] == "blocker"
    assert obs["file_path"] == "n8n:execution:1"
    assert obs["line_number"] == 0
    assert "Unused Respond to Webhook" in obs["description"]
    assert obs["__harvest_id"] == 1

    # Flujo completo: harvest_all escribe 3 eventos al ledger
    result = h.harvest_all()
    assert result["n8n"] == 3
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 3
    etypes = sorted(e["event"]["event_type"] for e in entries)
    assert etypes == ["COMMAND_RUN", "COMMAND_RUN", "OBSERVATION"]


# ---------------------------------------------------------------------------
# 3. idempotencia (cursor por max_execution_id)
# ---------------------------------------------------------------------------

def test_two_runs_zero_duplicates(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    r1 = h.harvest_all()
    assert r1["n8n"] == 3
    r2 = h.harvest_all()
    assert r2["n8n"] == 0, f"Segunda corrida debe dar 0, obtuvo {r2}"

    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 3  # sin duplicados
    ids = {e["event"]["event_id"] for e in entries}
    assert len(ids) == 3


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
    # harvest_all no crashea (aislamiento por fuente)
    assert "n8n" in result
    assert result["n8n"] == 0
    # El cursor NO avanzó
    assert (
        not os.path.exists(config)
        or os.path.getsize(config) == 0
        or json.load(open(config)) == {}
    ), "El cursor no debe avanzar si el write falló"

    # Corrida siguiente con write OK → cosecha TODO (3) sin pérdida
    r2 = h.harvest_all()
    assert r2["n8n"] == 3
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 3
    assert len({e["event"]["event_id"] for e in entries}) == 3


# ---------------------------------------------------------------------------
# 5. replay-determinismo (Artículo VI)
# ---------------------------------------------------------------------------

def test_replay_determinism_n8n_harvest(tmp_path):
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
    assert h.harvest_all()["n8n"] == 3
    state = ReplayEngine(ledger).reconstruct_state()
    assert state["events_applied"] == 3
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
    assert h.harvest_all()["n8n"] == 3
    assert h.harvest_all()["n8n"] == 0
    assert _fixture_bytes() == before, "mode=ro debe dejar el db intacto"

    # No deben quedar side-files (wal/shm) junto al fixture original
    side_files = [f for f in os.listdir(FIXTURE_DIR)
                  if f.startswith(FIXTURE_DB + "-")]
    assert side_files == []