"""Tests Fase 6 — Comando explicación de decisiones (ver Chronicle; docs/design_index.md)."""

from causadb.cli._cmd_explain import cmd_explain
from causadb._explain import explain_decision, _get_event_by_id, _walk_back_from_event
import json
import os
import tempfile
from causadb._ledger_writer import LedgerWriter
from causadb._event_types import EventType
from causadb._event_schema import CanonicalEvent


def test_explain_cli_missing_event_id():
    """Test que maneja correctamente event_id faltante."""
    exit_code, output = cmd_explain(type('Args', (), {'event_id': None, 'ledger': None})())
    assert exit_code == 1
    assert "Missing event_id argument" in output


def test_explain_cli_with_valid_decision():
    """Test end-to-end con decisión de gobernanza válida."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = os.path.join(tmpdir, "ledger.log")
        writer = LedgerWriter(ledger_path)
        genesis = CanonicalEvent(
            event_type=EventType.SYSTEM_BOOT,
            ctx_id="genesis",
            source="test:genesis",
            source_type="agent",
            payload={}
        )
        writer.append(genesis)
        decision = CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION,
            ctx_id="test_ctx",
            source="test:agent",
            source_type="agent",
            payload={
                "reasoning": "Test reasoning for architectural decision",
                "impact": "medium",
                "decision_type": "architectural",
                "origin": "agent"
            }
        )
        writer.append(decision)
        args = type('Args', (), {'event_id': decision.event_id, 'ledger': ledger_path})()
        exit_code, output = cmd_explain(args)
        assert exit_code == 0
        result = json.loads(output)
        assert "decision_event" in result
        assert result["decision_event"]["event_id"] == str(decision.event_id)
        assert result["decision_event"]["event_type"] == "GOVERNANCE_DECISION"
        assert "Test reasoning" in result["explanation"]


def test_explain_cli_nonexistent_event():
    """Test que maneja evento inexistente."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = os.path.join(tmpdir, "ledger.log")
        args = type('Args', (), {'event_id': "00000000-0000-0000-0000-000000000000", 'ledger': ledger_path})()
        exit_code, output = cmd_explain(args)
        assert exit_code == 1
        assert "not found" in output


def test_explain_cli_wrong_event_type():
    """Test que rechaza eventos no-GOVERNANCE_DECISION."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = os.path.join(tmpdir, "ledger.log")
        writer = LedgerWriter(ledger_path)
        genesis = CanonicalEvent(
            event_type=EventType.SYSTEM_BOOT,
            ctx_id="genesis",
            source="test:genesis",
            source_type="agent",
            payload={}
        )
        writer.append(genesis)
        file_event = CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="test_ctx",
            source="test:agent",
            source_type="agent",
            payload={"path": "test.py", "action": "create"}
        )
        writer.append(file_event)
        args = type('Args', (), {'event_id': file_event.event_id, 'ledger': ledger_path})()
        exit_code, output = cmd_explain(args)
        assert exit_code == 1
        assert "expected 'GOVERNANCE_DECISION'" in output


def test_explain_anti_teatro_type_check_disabled():
    """Anti-teatro: si explain_decision ignora event_type check, el test falla.
    
    Simula un evento no-GOVERNANCE_DECISION y verifica que el validador
    de tipo esté activo. Si se muta el chequeo de event_type, este test
    lo detecta.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = os.path.join(tmpdir, "ledger.log")
        writer = LedgerWriter(ledger_path)
        writer.append(CanonicalEvent(
            event_type=EventType.SYSTEM_BOOT,
            ctx_id="genesis",
            source="test:genesis",
            source_type="agent",
            payload={}
        ))
        fm_event = CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="test",
            source="test:agent",
            source_type="agent",
            payload={"path": "f.py", "action": "create"}
        )
        writer.append(fm_event)
        # Si explain_decision no filtra por event_type, intentaría devolver
        # una "explicación" para un FILE_MODIFIED.
        try:
            result = explain_decision(ledger_path, fm_event.event_id)
            # Si llegamos acá, el type check falló — esto es un error
            assert False, f"explain_decision debería haber lanzado ValueError para un evento FILE_MODIFIED, pero devolvió: {result}"
        except ValueError:
            pass  # Esperado: rechazar evento no-GOVERNANCE_DECISION


def test_explain_anti_teatro_ancestral_chain():
    """Anti-teatro: el explanation debe mencionar 'root decision' cuando
    no hay eventos padre — no simplemente devolver cadena vacía.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = os.path.join(tmpdir, "ledger.log")
        writer = LedgerWriter(ledger_path)
        writer.append(CanonicalEvent(
            event_type=EventType.SYSTEM_BOOT,
            ctx_id="genesis",
            source="test:genesis",
            source_type="agent",
            payload={}
        ))
        decision = CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION,
            ctx_id="test",
            source="test:agent",
            source_type="agent",
            payload={"reasoning": "Root decision test", "impact": "low", "decision_type": "tactical", "origin": "agent"}
        )
        writer.append(decision)
        result = explain_decision(ledger_path, decision.event_id)
        assert result["ancestral_chain"] == []
        assert "root decision" in result["explanation"].lower()