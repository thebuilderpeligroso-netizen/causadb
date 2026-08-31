import pytest
import os
import shutil
import json
from causadb._harvester import Harvester
from causadb._harvest_source_hermes import HermesHarvestSource

@pytest.fixture
def hermes_db(tmp_path):
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "hermes_fixture.db")
    db_copy = tmp_path / "hermes.db"
    shutil.copy(fixture_path, db_copy)
    return str(db_copy)

def _make_source(db_path, ledger_path):
    return HermesHarvestSource(ledger_path=ledger_path, db_path=db_path)

def test_harvest_lifecycle_emits_started_ended(hermes_db, tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(hermes_db, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    raws = list(source.harvest(None))
    types = [r["type"] for r in raws]

    # Estas aserciones FALLARÁN en RED
    assert "SESSION_STARTED" in types
    assert "SESSION_ENDED" in types

    started = [r for r in raws if r["type"] == "SESSION_STARTED"]
    ended = [r for r in raws if r["type"] == "SESSION_ENDED"]

    # hermes_fixture.db tiene 2 sesiones, ambas con ended_at poblado
    assert len(started) == 2
    assert len(ended) == 2

    # Verificar campos de STARTED
    s1 = started[0]
    assert "session_id" in s1
    assert "started_at" in s1
    assert s1["started_at"].endswith("Z")

    # Verificar campos de ENDED
    e1 = ended[0]
    assert "session_id" in e1
    assert "ended_at" in e1
    assert "end_reason" in e1
    assert e1["ended_at"].endswith("Z")

def test_harvest_lifecycle_dedup_via_cursor(hermes_db, tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(hermes_db, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    # Primera corrida: cosecha TODO (incluyendo STARTED/ENDED si estuviera implementado)
    h.harvest_all()
    
    with open(config, "r") as f:
        cursor = json.load(f)["agent:hermes"]
    
    # Estas aserciones FALLARÁN en RED (no existen las keys en el cursor)
    assert "session_started_emitted" in cursor
    assert "session_ended_emitted" in cursor
    assert len(cursor["session_started_emitted"]) == 2
    assert len(cursor["session_ended_emitted"]) == 2

    # Segunda corrida: no debe emitir NADA nuevo (ya procesó todos los rowids)
    r2 = h.harvest_all()
    assert r2["hermes"] == 0
