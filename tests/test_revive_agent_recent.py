"""Q.3 — Revive: sección "Actividad reciente del agente" (GOVERNANCE_DECISION
origin=agent + FILE_MODIFIED recientes). Tests RED primero (Art. III).
"""

from types import MappingProxyType
import argparse

import pytest

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._init import causadb_init
from causadb._ledger_writer import LedgerWriter
from causadb.cli._cmd_revive import cmd_revive


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_revive_args(ledger, fmt="markdown"):
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--format", default="markdown")
    parser.add_argument("--decisions", type=int, default=10)
    parser.add_argument("--write", default=None)
    return parser.parse_args(["--ledger", ledger, "--format", fmt])


def _append(writer, event_type, payload, parent_event_id=None):
    writer.append(CanonicalEvent(
        event_type=event_type,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType(payload),
        parent_event_id=parent_event_id,
    ))


# ── tests ────────────────────────────────────────────────────────────────────


def _agent_section(output):
    """Devuelve el contenido de la sección 'Actividad reciente del agente'
    delimitado hasta el siguiente header '## ' (para no contaminar con la
    sección existente 'Decisiones de Gobernanza' que sí muestra distill)."""
    rest = output.split("## Actividad reciente del agente", 1)[1]
    for marker in ("\n## Decisiones de Gobernanza", "\n## Skills", "\n## Tools"):
        if marker in rest:
            rest = rest.split(marker, 1)[0]
            break
    return rest


def test_revive_agent_recent_decisions_section(tmp_path):
    """La sección "Actividad reciente del agente" existe y muestra el reasoning
    de la decisión origin=agent + el FILE_MODIFIED reciente."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)

    _append(writer, EventType.GOVERNANCE_DECISION, {
        "reasoning": "Close OCB rebuild gap on production",
        "impact": "high",
        "decision_type": "tactical",
        "origin": "agent",
    })
    _append(writer, EventType.FILE_MODIFIED, {
        "path": "causadb/_ocb_manager.py",
        "action": "modified",
    })

    exit_code, output = cmd_revive(_make_revive_args(ledger, "markdown"))
    assert exit_code == 0
    assert "## Actividad reciente del agente" in output
    section = _agent_section(output)
    assert "Close OCB rebuild gap on production" in section
    assert "causadb/_ocb_manager.py" in section


def test_revive_agent_recent_excludes_distill(tmp_path):
    """Las decisiones origin=distill NO aparecen en la sección agent."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)

    _append(writer, EventType.GOVERNANCE_DECISION, {
        "reasoning": "Auto-promoted heuristic decision",
        "impact": "critical",
        "decision_type": "architectural",
        "origin": "distill",
    })
    _append(writer, EventType.GOVERNANCE_DECISION, {
        "reasoning": "Explicit agent decision",
        "impact": "low",
        "decision_type": "tactical",
        "origin": "agent",
    })

    exit_code, output = cmd_revive(_make_revive_args(ledger, "markdown"))
    assert exit_code == 0
    section = _agent_section(output)
    assert "Explicit agent decision" in section
    assert "Auto-promoted heuristic decision" not in section


def test_revive_agent_recent_empty_omits_section(tmp_path):
    """Anti-teatro: sin decisiones agent ni FILE_MODIFIED, la sección NO se
    renderiza."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]

    exit_code, output = cmd_revive(_make_revive_args(ledger, "markdown"))
    assert exit_code == 0
    assert "## Actividad reciente del agente" not in output


def test_revive_agent_recent_newest_first(tmp_path):
    """Las decisiones se muestran de la más reciente a la más vieja
    (query_events retorna ascendente; revive debe invertir)."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)

    _append(writer, EventType.GOVERNANCE_DECISION, {
        "reasoning": "OLDER decision first in ledger",
        "impact": "low",
        "decision_type": "tactical",
        "origin": "agent",
    })
    _append(writer, EventType.GOVERNANCE_DECISION, {
        "reasoning": "NEWEST decision second in ledger",
        "impact": "low",
        "decision_type": "tactical",
        "origin": "agent",
    })

    exit_code, output = cmd_revive(_make_revive_args(ledger, "markdown"))
    assert exit_code == 0
    section = _agent_section(output)
    assert section.find("NEWEST decision second in ledger") < section.find(
        "OLDER decision first in ledger"
    ), "La sección debe listar la decisión más reciente primero"


def test_revive_agent_recent_degrades_on_missing_blob(tmp_path):
    """MENOR-Checker: si un blob referenciado falta en disco, la función
    _generate_agent_recent_activity degrada a [] (patrón
    _generate_governance_decisions, Art. VIII) — no lanza.
    (Nota: el pipeline revive completo tiene Capa 0 preexistente que falla
    fail-closed antes; acá se testea la unidad de Q.3.)"""
    from causadb.cli._cmd_revive import _generate_agent_recent_activity

    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)

    _append(writer, EventType.GOVERNANCE_DECISION, {
        "reasoning": "R" * 5000,
        "impact": "high",
        "decision_type": "tactical",
        "origin": "agent",
    })

    # El reasoning largo se externaliza como blob; lo borramos del disco.
    import os
    blob_dir = os.path.join(os.path.dirname(ledger), "blobs")
    deleted = 0
    for root, _dirs, files in os.walk(blob_dir):
        for f in files:
            if f.endswith(".json"):
                os.remove(os.path.join(root, f))
                deleted += 1
    assert deleted > 0, "El reasoning debió externalizarse como blob"

    activity = _generate_agent_recent_activity(ledger)
    assert activity == []
