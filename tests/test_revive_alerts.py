"""E-Causal — Tests for Proactive Alert Injection in Revive.

Verifica:
1. Alerta de deuda de trazabilidad inyectada al principio del markdown de revive en total colapso (0 SESSION_SUMMARY).
2. Supresión de la alerta cuando la distilación pasiva o manual ya saldó la deuda.
"""

import pytest
from datetime import datetime

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._config import CausaDBConfig
from causadb._ledger_writer import LedgerWriter
from causadb.cli._cmd_revive import _run_revive


@pytest.fixture
def temp_ledger(tmp_path):
    ledger_file = tmp_path / "test_ledger_alert.log"
    config = CausaDBConfig(ledger_path=str(ledger_file))
    writer = LedgerWriter(str(ledger_file), config)
    writer.append(CanonicalEvent.from_dict({
        "event_id": "genesis-event-12345",
        "event_type": "SYSTEM_BOOT",
        "timestamp": datetime.now().isoformat(),
        "source": "causadb:init",
        "ctx_id": "genesis",
        "payload": {"version": "1.0.0"}
    }))
    return str(ledger_file)


@pytest.mark.unit
def test_revive_alert_injection_on_total_collapse(temp_ledger, monkeypatch):
    """Test RED 3: Si la última sesión cerró abruptamente y no hay summary,
    inyecta la ALERTA DE TRAZABILIDAD en el markdown de revive.
    """
    # temp_ledger ya es el path string
    ledger_path = temp_ledger
    
    # Forzar que generate_resume retorne un session_type="abrupt_close"
    # sin agregar ningún summary en el ledger (colapso total).
    def mock_generate_resume(ledger_path, state=None):
        return {
            "events_count": 1,
            "last_timestamp": datetime.now().isoformat(),
            "session_type": "abrupt_close",
            "preloaded_partitions": ["OCB_PARTITION_1786809705.log"],
            "last_session_id": "test-session-123",
            "entry_summary": None  # No summary = total collapse
        }

    monkeypatch.setattr("causadb.cli._cmd_resume.generate_resume", mock_generate_resume)

    exit_code, output = _run_revive(
        ledger_path=ledger_path,
        output_format="markdown"
    )

    assert exit_code == 0
    # La alerta debe inyectarse en el markdown
    # Nota: El estado de los daemons ahora se reporta según DaemonManager,
    # ajustar el mock para que sea consistente.
    assert "⚠️ **ALERTA DE TRAZABILIDAD:**" in output

    assert "deuda de trazabilidad (gobernanza pendiente)" in output


def test_revive_alert_suppressed_when_reconciled(temp_ledger, monkeypatch):
    """Test RED 4: Si ya existe una GOVERNANCE_DECISION que saldó la deuda,
    no inyecta la alerta en el revive markdown.
    """
    ledger_path = temp_ledger
    config = CausaDBConfig(ledger_path=ledger_path)
    writer = LedgerWriter(ledger_path, config)

    # 1. Registrar una GOVERNANCE_DECISION de cierre (manual o distilada previa)
    writer.append(CanonicalEvent.from_dict({
        "event_id": "closure-decision-abc",
        "event_type": EventType.GOVERNANCE_DECISION.value,
        "timestamp": datetime.now().isoformat(),
        "source": "causadb:agent",
        "ctx_id": "closure",
        "payload": {
            "decision_type": "tactical",
            "impact": "low",
            "origin": "agent",
            "reasoning": "Cierre manual de la sesión para saldar deuda."
        }
    }))

    # Forzar abrupt_close
    def mock_generate_resume(ledger_path, state=None):
        return {
            "events_count": 2,
            "last_timestamp": datetime.now().isoformat(),
            "session_type": "abrupt_close",
            "preloaded_partitions": ["OCB_PARTITION_1786809705.log"]
        }

    monkeypatch.setattr("causadb.cli._cmd_resume.generate_resume", mock_generate_resume)

    exit_code, output = _run_revive(
        ledger_path=temp_ledger,
        output_format="markdown"
    )

    assert exit_code == 0
    # La alerta no debe estar presente porque ya hay una decisión de cierre
    assert "⚠️ **ALERTA DE TRAZABILIDAD:**" not in output
