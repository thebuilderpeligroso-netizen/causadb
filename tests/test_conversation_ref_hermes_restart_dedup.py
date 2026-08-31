"""C.2.3 RED/GREEN test: dedup de conversation_ref Hermes entre reinicios.

Regresión del hallazgo de auditoría 2026-08-12: la persistencia de
``conversation_ref_sessions`` en el cursor era exclusiva de opencode
(``_harvester.py:338-341``). Hermes emitía ``__conversation_ref`` pero su
dedup NO sobrevivía al reinicio del harvester, produciendo refs duplicadas y
dejando los eventos nuevos sin ref.

Este test reproduce el escenario con la fixture VERSIONADA
(``tests/fixtures/hermes_fixture.db``, state.db real de Hermes v0.19.0) que
contiene DOS sesiones del 2026-08-02 -> 9 eventos hermes:
  1. harvest_all() sobre la copia del db -> 9 eventos, una ref por sesión
     (solo en los SESSION_STARTED).
  2. Se verifica que el cursor de Hermes persiste AMBAS sesiones en
     ``conversation_ref_sessions``.
  3. La sesión c35163 crece: se inserta una fila nueva de esa sesión.
  4. Un NUEVO Harvester con el mismo archivo de cursores hace harvest_all().
  5. Los eventos nuevos NO deben llevar conversation_ref (la sesión ya fue
     referenciada en la corrida 1).
"""

import json
import os
import shutil
import sqlite3

import pytest

from causadb._harvester import Harvester
from causadb._harvest_source_hermes import HermesHarvestSource


FIXTURE_DB = os.path.join(os.path.dirname(__file__), "fixtures", "hermes_fixture.db")
SESSION_FIRST = "20260802_101617_82f322"  # STARTED+ENDED+REASONING+LLM
SESSION_SECOND = "20260802_102154_c35163"  # STARTED+ENDED+TOOL+LLM x2


def _hermes_db(tmp_path):
    if not os.path.isfile(FIXTURE_DB):
        pytest.fail(f"C.2 Hermes fixture is unavailable: {FIXTURE_DB}")
    db = tmp_path / "state.db"
    shutil.copy2(FIXTURE_DB, db)
    return db


def _events_with_agent(ledger, agent="hermes"):
    events = []
    for line in ledger.read_text().splitlines():
        event = json.loads(line)["event"]
        if event.get("payload", {}).get("agent") == agent:
            events.append(event)
    return events


def _insert_new_message(db, session_id, reasoning_content):
    """Simula que la sesión crece tras reiniciar el harvester.

    Inserta una fila assistant con reasoning_content (que produce un
    REASONING_STEP con __harvest_session_id y conversation_ref). Usa un
    timestamp epoch REAL estrictamente posterior al max actual.
    """
    con = sqlite3.connect(db)
    try:
        max_ts = con.execute("SELECT MAX(timestamp) FROM messages").fetchone()[0]
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, "
            "reasoning_content) VALUES (?, 'assistant', ?, ?, ?)",
            (session_id, "post-restart reasoning", max_ts + 1.0, reasoning_content),
        )
        con.commit()
    finally:
        con.close()


def _load_cursor(cursor_file, source_type):
    data = json.loads(cursor_file.read_text())
    for key, value in data.items():
        if key == source_type or key == f"agent:{source_type}":
            return value
        if isinstance(value, dict) and value.get("source_type") == source_type:
            return value
    return None


def test_hermes_conversation_ref_dedup_survives_restart(tmp_path):
    """Una sesión Hermes que crece tras reinicio NO duplica la conversation_ref."""
    db = _hermes_db(tmp_path)
    cursor_file = tmp_path / "cursors.json"
    ledger = tmp_path / "ledger.log"

    # Corrida 1: harvest inicial sobre la copia del db (2 sesiones -> 9 eventos).
    harvester1 = Harvester(str(ledger), str(cursor_file))
    harvester1.register_source(HermesHarvestSource(str(ledger), str(db)))
    assert harvester1.harvest_all()["hermes"] == 9

    events1 = _events_with_agent(ledger)
    assert len(events1) == 9
    # Una ref por sesión, SOLO sobre los SESSION_STARTED.
    carriers1 = [e for e in events1 if e["payload"].get("conversation_ref")]
    assert len(carriers1) == 2
    assert all(e["event_type"] == "SESSION_STARTED" for e in carriers1)
    assert {
        e["payload"]["conversation_ref"]["native_id"] for e in carriers1
    } == {SESSION_FIRST, SESSION_SECOND}

    # El cursor de Hermes debe persistir AMBAS sesiones ya referenciadas
    # (ANTES del fix C.2.3 esta clave NO existía para hermes).
    hermes_cursor = _load_cursor(cursor_file, "hermes")
    assert hermes_cursor is not None
    assert "conversation_ref_sessions" in hermes_cursor
    assert set(hermes_cursor["conversation_ref_sessions"]) == {
        SESSION_FIRST,
        SESSION_SECOND,
    }

    # La sesión c35163 crece tras el reinicio del harvester.
    _insert_new_message(db, SESSION_SECOND, "post-restart reasoning")

    # Corrida 2: nuevo Harvester, MISMO archivo de cursores (rehidrata el dedup).
    harvester2 = Harvester(str(ledger), str(cursor_file))
    harvester2.register_source(HermesHarvestSource(str(ledger), str(db)))
    # La fila assistant nueva emite 2 eventos (REASONING_STEP + LLM_INVOKED).
    assert harvester2.harvest_all()["hermes"] == 2

    events2 = _events_with_agent(ledger)
    assert len(events2) == 11
    # Los eventos NUEVOS (post-reinicio) NO deben llevar conversation_ref: la
    # sesión ya fue referenciada en la corrida 1 (invariante "primer evento").
    assert all(e["payload"].get("conversation_ref") is None for e in events2[9:])
    # Y el ledger NO debe contener una segunda ref para ninguna sesión.
    total_refs = [e for e in events2 if e["payload"].get("conversation_ref")]
    assert len(total_refs) == 2


def test_hermes_conversation_ref_key_survives_deduped_batch(tmp_path):
    """Regresión Checker 2026-08-12: un batch 100% deduped no debe perder la
    clave del cursor.

    Sin el fix correcto, tras una corrida que solo cosecha sesiones ya
    referenciadas (batch 100% deduped), ``conversation_ref_sessions`` se pierde
    del cursor y la corrida siguiente vuelve a duplicar la ref.
    """
    db = _hermes_db(tmp_path)
    cursor_file = tmp_path / "cursors.json"
    ledger = tmp_path / "ledger.log"

    def new_harvester():
        h = Harvester(str(ledger), str(cursor_file))
        h.register_source(HermesHarvestSource(str(ledger), str(db)))
        return h

    # Corrida 1: harvest inicial (2 sesiones -> 9 eventos), persiste la clave.
    assert new_harvester().harvest_all()["hermes"] == 9

    # Corrida 2: la sesión c35163 crece con una fila de una sesión YA
    # referenciada (batch 100% deduped: emite eventos SIN ref). Con el fix
    # viejo, la persistencia condicionada al batch pierde la clave del cursor.
    _insert_new_message(db, SESSION_SECOND, "post-deduped-batch-1")
    assert new_harvester().harvest_all()["hermes"] == 2
    cursor2 = _load_cursor(cursor_file, "hermes")
    assert cursor2 is not None
    # La clave debe SOBREVIVIR a un batch 100% deduped (antes se perdía),
    # y seguir conteniendo ambas sesiones originales.
    assert "conversation_ref_sessions" in cursor2
    assert set(cursor2["conversation_ref_sessions"]) == {SESSION_FIRST, SESSION_SECOND}

    # La sesión vuelve a crecer.
    _insert_new_message(db, SESSION_SECOND, "post-deduped-batch-2")

    # Corrida 3: la sesión crece -> el evento nuevo NO debe duplicar la ref
    # (si la clave se perdió en la corrida 2, aquí se re-agrega la ref).
    assert new_harvester().harvest_all()["hermes"] == 2
    events3 = _events_with_agent(ledger)
    assert len(events3) == 13
    # Las únicas refs siguen siendo las 2 originales, solo en SESSION_STARTED
    # y con el native_id nativo correcto de cada sesión.
    carriers3 = [e for e in events3 if e["payload"].get("conversation_ref")]
    assert len(carriers3) == 2
    assert all(e["event_type"] == "SESSION_STARTED" for e in carriers3)
    assert {
        e["payload"]["conversation_ref"]["native_id"] for e in carriers3
    } == {SESSION_FIRST, SESSION_SECOND}
    # Todo lo cosechado después de la corrida 1 va sin ref.
    assert all(e["payload"].get("conversation_ref") is None for e in events3[9:])
