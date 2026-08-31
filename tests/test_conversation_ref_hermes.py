"""C.2 tests: Hermes is the second real conversation backend.

The database is a VERSIONED copy (``tests/fixtures/hermes_fixture.db``) of
the real Hermes v0.19.0/Ollama store.  La fixture contiene DOS sesiones del
2026-08-02 (total 9 eventos hermes):

  - ``20260802_101617_82f322``: STARTED + ENDED + REASONING_STEP +
    LLM_INVOKED = 4 eventos.
  - ``20260802_102154_c35163``: STARTED + ENDED + TOOL_CALLED +
    LLM_INVOKED x2 = 5 eventos.

La copia versionada elimina la dependencia de un state.db volátil en /tmp
(causa raíz de la rotura R3) y mantiene los tests aislados del store vivo.
"""

import json
import os
import shutil
from collections import Counter

import pytest

from causadb._harvester import Harvester
from causadb._harvest_source_hermes import HermesHarvestSource
from causadb._recover_session import recover_session


FIXTURE_DB = os.path.join(os.path.dirname(__file__), "fixtures", "hermes_fixture.db")
SESSION_FIRST = "20260802_101617_82f322"  # STARTED+ENDED+REASONING+LLM
SESSION_SECOND = "20260802_102154_c35163"  # STARTED+ENDED+TOOL+LLM x2


def _hermes_db(tmp_path):
    if not os.path.isfile(FIXTURE_DB):
        pytest.fail(f"C.2 Hermes fixture is unavailable: {FIXTURE_DB}")
    db = tmp_path / "state.db"
    shutil.copy2(FIXTURE_DB, db)
    return db


def _expected_ref(native_id):
    return {
        "provider": "hermes",
        "native_id": native_id,
        "locator_kind": "sqlite",
        "locator": "hermes_default",
        "resolver": "hermes",
        "confidence": "verified",
        "content_class": "transcript_complete",
        "privacy_class": "raw_sensitive",
    }


def test_hermes_first_harvested_event_has_exact_conversation_ref(tmp_path):
    """Solo los SESSION_STARTED llevan conversation_ref, una exacta por sesión.

    Fixture de 2 sesiones -> 9 eventos hermes (+1 SESSION_SUMMARY del propio
    Harvester en el ledger). Se aserta la descomposición ESTRUCTURAL de tipos
    (no un conteo pelado) y que las refs van POR TIPO (SESSION_STARTED) con el
    native_id nativo correcto de cada sesión, no por índice literal.
    """
    db = _hermes_db(tmp_path)
    ledger = tmp_path / "ledger.log"
    harvester = Harvester(str(ledger), str(tmp_path / "cursors.json"))
    harvester.register_source(HermesHarvestSource(str(ledger), str(db)))

    assert harvester.harvest_all()["hermes"] == 9  # 2 sesiones: 4 + 5 eventos
    events = [json.loads(line)["event"] for line in ledger.read_text().splitlines()]
    hermes_events = [
        e for e in events if e["payload"].get("agent") == "hermes"
    ]

    # Descomposición estructural exacta (mata podas de SESSION_ENDED, etc.).
    types = Counter(e["event_type"] for e in hermes_events)
    assert types == Counter({
        "SESSION_STARTED": 2,
        "SESSION_ENDED": 2,
        "LLM_INVOKED": 3,
        "REASONING_STEP": 1,
        "TOOL_CALLED": 1,
    })

    # Exactamente 2 refs en TODO el ledger (incluye el SESSION_SUMMARY),
    # ambas sobre eventos SESSION_STARTED de hermes.
    carriers = [
        e for e in events if e["payload"].get("conversation_ref") is not None
    ]
    assert len(carriers) == 2
    assert all(
        e["event_type"] == "SESSION_STARTED" and e["payload"]["agent"] == "hermes"
        for e in carriers
    )

    # Un dict EXACTO por sesión, con su native_id nativo correcto.
    refs_by_native = {
        e["payload"]["conversation_ref"]["native_id"]: e["payload"]["conversation_ref"]
        for e in carriers
    }
    assert set(refs_by_native) == {SESSION_FIRST, SESSION_SECOND}
    assert refs_by_native[SESSION_FIRST] == _expected_ref(SESSION_FIRST)
    assert refs_by_native[SESSION_SECOND] == _expected_ref(SESSION_SECOND)

    # El locator es simbólico, nunca una ruta absoluta.
    assert not os.path.isabs(refs_by_native[SESSION_FIRST]["locator"])
    assert not os.path.isabs(refs_by_native[SESSION_SECOND]["locator"])

    # Todo evento que NO es SESSION_STARTED va sin ref.
    assert all(
        e["payload"].get("conversation_ref") is None
        for e in events
        if e["event_type"] != "SESSION_STARTED"
    )


def test_hermes_recovery_uses_session_lookup_not_full_harvest(
    monkeypatch, tmp_path
):
    """An exact Hermes session must be resolved without harvesting the store."""
    db = _hermes_db(tmp_path)
    monkeypatch.setenv("CAUSADB_HERMES_DB_PATH", str(db))

    def full_harvest_is_forbidden(_source, _cursor=None):
        raise AssertionError("recover must not call Hermes harvest({})")

    monkeypatch.setattr(HermesHarvestSource, "harvest", full_harvest_is_forbidden)

    tool, storyboard = recover_session(
        str(tmp_path / "ledger.log"), SESSION_SECOND, tool="hermes"
    )

    assert tool == "hermes"
    assert storyboard["session_id"] == SESSION_SECOND
    assert storyboard["turn_count"] == 2
    assert storyboard["tool_calls"]
    assert storyboard["tool_calls"][0]["result"] == (
        '{"output": "hermes-agent\\nhermes-home\\nhermes-venv", '
        '"exit_code": 0, "error": null}'
    )
