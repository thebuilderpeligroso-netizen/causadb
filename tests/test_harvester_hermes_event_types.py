"""H0.2 — Bloqueante #4: registro de tipos Hermes antes del harvest.

Verifica que los 6 EventTypes de Hermes estén registrados en el registry y
que el harvester, al emitir un raw con ``type="DELEGATION"``, produzca un
evento cuyo ``event_type`` sea DELEGATION y NO se degrade a OBSERVATION.
"""

import pytest

from causadb._event_registry import is_registered
from causadb._hermes_event_types import HERMES_EVENT_TYPES


@pytest.fixture
def harvester(tmp_path):
    from causadb._init import causadb_init
    from causadb._harvester import Harvester

    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    return Harvester(result["ledger_path"])


def test_hermes_event_types_registered():
    """Los 6 tipos de Hermes deben estar registrados en el registry."""
    for name in HERMES_EVENT_TYPES:
        assert is_registered(name), f"{name} no está registrado"


def test_harvester_does_not_degrade_delegation_to_observation(harvester):
    """Un raw con type='DELEGATION' debe emitir event_type DELEGATION.

    Hoy, si el tipo no está registrado, `_event_from_raw` degrada el tipo a
    OBSERVATION silenciosamente. Con el registro previo, DELEGATION se
    preserva.
    """
    raw = {
        "type": "DELEGATION",
        "delegation_id": "d1",
        "origin_session": "s0",
        "parent_session_id": None,
        "task_json": "{}",
        "state": "started",
        "result_json": None,
        "delivery_state": "pending",
        "created_at": "2026-08-12T00:00:00Z",
        "updated_at": "2026-08-12T00:00:00Z",
    }
    event = harvester._event_from_raw("hermes", raw)
    assert event.event_type.value == "DELEGATION"
    assert event.event_type.value != "OBSERVATION"
