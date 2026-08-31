"""H8.6 — RED tests: la proyección del ReplayEngine debe exponer ``source``.

Contexto: H8.5 (Agent Activity Report) descubrió que
``agents_sources_observed`` siempre devuelve ``["unknown"]`` porque la
proyección del ReplayEngine NO proyecta ``source`` (los eventos crudos SÍ lo
llevan en el wrapper, ``event_data["source"]``, pero se descarta al hacer el
``.append({...})`` de cada entry). H8.6: exponer ``source`` en TODAS las
entries proyectadas.

Contrato (Artículo III — RED primero, Artículo IX — aserciones reales):
- Cada entry proyectada lleva ``"source": event_data.get("source")``.
- ``build_agent_activity_report`` detecta esos sources (NO ``["unknown"]``).
- Back-compat: events sin ``source`` → entry con ``source=None`` y el reporte
  sigue con el fallback ``["unknown"]``.

Discriminación payload-vs-wrapper: los payloads incluyen un decoy
``"source"``; si la implementación leyera de ``payload`` en vez de
``event_data``, los asserts fallan.
"""

import hashlib
import json

import pytest

from causadb._agent_activity_report import build_agent_activity_report
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._replay_engine import ReplayEngine


@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")


def _payload(**kwargs):
    """Payload con decoy ``source`` para discriminar wrapper vs payload."""
    return {"source": "payload:decoy", **kwargs}


def test_source_projected_for_representative_event_types(ledger_path):
    """12 tipos representativos: cada entry proyectada expone el ``source``
    del wrapper del evento crudo (no el del payload)."""
    writer = LedgerWriter(ledger_path)
    specs = [
        (EventType.FILE_MODIFIED, "hermes", _payload(path="/a.py", action="create")),
        (EventType.COMMAND_RUN, "opencode:agent", _payload(command="ls", exit_code=0)),
        (EventType.COMMIT_MADE, "gemini-cli", _payload(commit_hash="abc", message="m")),
        (EventType.TOOL_CALLED, "hermes", _payload(tool_name="read_file")),
        (EventType.SESSION_STARTED, "opencode:agent", _payload(session_id="s1")),
        (EventType.LLM_INVOKED, "hermes", _payload(model="m1", prompt="p")),
        (EventType.COST_ACCOUNTED, "gemini-cli", _payload(model="m1", tokens_in=1, tokens_out=1, cost=0.01)),
        (EventType.MEMORY_OP, "opencode:agent", _payload(operation="store", key="k", value="v")),
        (EventType.REASONING_STEP, "opencode:agent", _payload(step_type="plan", step_hash="h1")),
        (EventType.GOVERNANCE_DECISION, "hermes", _payload(reasoning="r", impact="low", decision_type="tactical", origin="agent")),
        (EventType.API_ATTEMPT, "gemini-cli", _payload(hermes_session_id="s1", status="success", model="m1")),
        (EventType.OBSERVATION, "harvester:browser", _payload(file_path="/x", line_number=1, description="d", severity="info")),
    ]
    for event_type, source, payload in specs:
        writer.append(CanonicalEvent(event_type=event_type, ctx_id="ctx", source=source, payload=payload))

    state = ReplayEngine(ledger_path).reconstruct_state()

    assert state["files_modified"][0]["source"] == "hermes"
    assert state["commands_run"][0]["source"] == "opencode:agent"
    assert state["commits_made"][0]["source"] == "gemini-cli"
    assert state["tools_called"][0]["source"] == "hermes"
    assert state["sessions"][0]["source"] == "opencode:agent"
    assert state["llm_invocations"][0]["source"] == "hermes"
    assert state["cost_accounted"][0]["source"] == "gemini-cli"
    assert state["memory_ops"][0]["source"] == "opencode:agent"
    assert state["reasoning_steps"][0]["source"] == "opencode:agent"
    assert state["governance_decisions"][0]["source"] == "hermes"
    assert state["api_attempts"][0]["source"] == "gemini-cli"
    assert state["observations"][0]["source"] == "harvester:browser"


def test_agent_activity_report_detects_projected_sources(ledger_path):
    """End-to-end: ledger real → replay → report. agents_sources_observed
    incluye los sources de las entries proyectadas y NO es ``["unknown"]``."""
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(EventType.FILE_MODIFIED, "ctx", "hermes", payload=_payload(path="/a.py", action="create")))
    writer.append(CanonicalEvent(EventType.COMMAND_RUN, "ctx", "opencode:agent", payload=_payload(command="ls", exit_code=0)))
    writer.append(CanonicalEvent(EventType.COMMIT_MADE, "ctx", "gemini-cli", payload=_payload(commit_hash="c1", message="m")))
    writer.append(CanonicalEvent(EventType.LLM_INVOKED, "ctx", "hermes", payload=_payload(model="m", prompt="p")))
    writer.append(CanonicalEvent(EventType.REASONING_STEP, "ctx", "opencode:agent", payload=_payload(step_type="plan", step_hash="h")))
    writer.append(CanonicalEvent(EventType.COST_ACCOUNTED, "ctx", "gemini-cli", payload=_payload(model="m", tokens_in=1, tokens_out=1, cost=0.0)))
    writer.append(CanonicalEvent(EventType.API_ATTEMPT, "ctx", "hermes", payload=_payload(hermes_session_id="s", status="success", model="m")))

    state = ReplayEngine(ledger_path).reconstruct_state()
    report = build_agent_activity_report(state)["agent_activity_report"]

    observed = report["summary"]["agents_sources_observed"]
    assert set(observed) == {"hermes", "opencode:agent", "gemini-cli"}
    assert "unknown" not in observed


def _write_raw_entry(ledger_path, event_dict, prev_hash):
    """Appendea una entry raw replicando el hash chain del LedgerWriter."""
    event_json = json.dumps(event_dict, sort_keys=True)
    new_hash = hashlib.sha256((event_json + prev_hash).encode()).hexdigest()
    entry = {"event": event_dict, "prev_hash": prev_hash, "hash": new_hash}
    with open(ledger_path, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return new_hash


def test_backcompat_entry_without_source_projects_none(ledger_path):
    """Evento legacy (wrapper sin key ``source``) → la entry proyectada tiene
    ``source is None``; el replay NO crashea (Art. V, back-compat)."""
    event_dict = {
        "event_id": "legacy-1",
        "event_type": "FILE_MODIFIED",
        "timestamp": "2026-08-15T10:00:00Z",
        "ctx_id": "ctx",
        "parent_event_id": None,
        "source_type": "agent",
        "schema_version": "0.1.0",
        "payload": {"path": "/legacy.py", "action": "create"},
        "sequence_number": 0,
    }
    _write_raw_entry(ledger_path, event_dict, "GENESIS")

    state = ReplayEngine(ledger_path).reconstruct_state()

    assert len(state["files_modified"]) == 1
    assert state["files_modified"][0]["source"] is None


def test_backcompat_report_unknown_fallback_without_sources(ledger_path):
    """Ledger con entries pero sin ``source`` en ninguna → el reporte sigue
    funcionando y agents_sources_observed cae al fallback ``["unknown"]``."""
    fm = {
        "event_id": "legacy-1",
        "event_type": "FILE_MODIFIED",
        "timestamp": "2026-08-15T10:00:00Z",
        "ctx_id": "ctx",
        "payload": {"path": "/a.py", "action": "create"},
        "sequence_number": 0,
    }
    last = _write_raw_entry(ledger_path, fm, "GENESIS")
    cmd = {
        "event_id": "legacy-2",
        "event_type": "COMMAND_RUN",
        "timestamp": "2026-08-15T10:01:00Z",
        "ctx_id": "ctx",
        "payload": {"command": "ls", "exit_code": 0},
        "sequence_number": 1,
    }
    _write_raw_entry(ledger_path, cmd, last)

    state = ReplayEngine(ledger_path).reconstruct_state()
    report = build_agent_activity_report(state)["agent_activity_report"]

    assert report["files_modified"]["count"] == 1
    assert report["commands_run"]["count"] == 1
    assert report["summary"]["agents_sources_observed"] == ["unknown"]
