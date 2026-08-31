"""F.7.1 — EU AI Act Art. 12 compliance report tests.

Artículo III: 8 tests en RED phase antes de implementacion.
"""

import json
import os
import pytest
from types import MappingProxyType

from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType
from causadb._config import CausaDBConfig
from causadb._ledger_reader import LedgerReader
from causadb._ledger_validator import LedgerValidator


@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")


def _build_valid_ledger(ledger_path, n_events=3):
    """Helper: construir un ledger con N eventos encadenados + hash-chain valida."""
    writer = LedgerWriter(ledger_path)
    events = []
    for i in range(n_events):
        e = CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="test/session",
            source="causadb:test",
            payload={"path": f"/test/file_{i}.py", "action": "create"}
        )
        writer.append(e)
        events.append(e)
    return events


def _build_valid_ledger_with_cost(ledger_path):
    """Helper: construir un ledger con eventos variados que generen side-effects."""
    writer = LedgerWriter(ledger_path)
    e1 = CanonicalEvent(
        event_type=EventType.SYSTEM_BOOT,
        ctx_id="test/session",
        source="causadb:test",
        payload={"boot_id": "boot-001"}
    )
    writer.append(e1)

    e2 = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="test/session",
        source="causadb:test",
        parent_event_id=e1.event_id,
        payload={"path": "/test/app.py", "action": "create"}
    )
    writer.append(e2)

    e3 = CanonicalEvent(
        event_type=EventType.COST_ACCOUNTED,
        ctx_id="test/session",
        source="causadb:test",
        parent_event_id=e2.event_id,
        payload=MappingProxyType({
            "model": "gpt-4",
            "tokens_in": 100,
            "tokens_out": 50,
            "cost": 0.005,
            "currency": "USD",
        }),
    )
    writer.append(e3)

    e4 = CanonicalEvent(
        event_type=EventType.COMMAND_RUN,
        ctx_id="test/session",
        source="causadb:test",
        parent_event_id=e3.event_id,
        payload={"command": "pytest causadb/tests/", "exit_code": 0}
    )
    writer.append(e4)

    return [e1, e2, e3, e4]


class TestEuAiActComplianceReport:
    # ----------------------------------------------------------------
    # Test 1
    # ----------------------------------------------------------------
    def test_traceability_guaranteed_true_on_valid_ledger(self, ledger_path):
        """Ledger con N eventos + replay OK → traceability_guaranteed == true."""
        _build_valid_ledger_with_cost(ledger_path)

        from causadb.compliance._eu_ai_act import generate_eu_ai_act_report
        report = generate_eu_ai_act_report(ledger_path)

        assert report["traceability_guaranteed"] is True
        assert report["logging_facilities"]["hash_chain_validated"] is True
        assert report["logging_facilities"]["append_only"] is True
        assert report["logging_facilities"]["timestamp_precise"] is True
        assert report["logging_facilities"]["event_chain_complete"] is True

    # ----------------------------------------------------------------
    # Test 2
    # ----------------------------------------------------------------
    def test_detects_broken_hash_chain(self, ledger_path):
        """Corromper campo 'hash' → traceability_guaranteed == false."""
        _build_valid_ledger(ledger_path, n_events=3)

        # Corromper: reemplazar "hash" por "hosh" en el archivo
        with open(ledger_path, "r+") as f:
            content = f.read()
            f.seek(0)
            f.write(content.replace('"hash"', '"hosh"'))

        from causadb.compliance._eu_ai_act import generate_eu_ai_act_report
        report = generate_eu_ai_act_report(ledger_path)

        assert report["traceability_guaranteed"] is False
        assert report["logging_facilities"]["hash_chain_validated"] is False

    # ----------------------------------------------------------------
    # Test 3
    # ----------------------------------------------------------------
    def test_detects_continuity_break(self, ledger_path):
        """Borrar una linea del medio → event_chain_complete == false."""
        _build_valid_ledger(ledger_path, n_events=3)

        # Borrar la segunda linea
        with open(ledger_path, "r") as f:
            lines = f.readlines()
        lines = [lines[0]] + [lines[2]]  # saltar linea 1 (indice)
        with open(ledger_path, "w") as f:
            f.writelines(lines)

        from causadb.compliance._eu_ai_act import generate_eu_ai_act_report
        report = generate_eu_ai_act_report(ledger_path)

        assert report["logging_facilities"]["event_chain_complete"] is False
        assert report["traceability_guaranteed"] is False

    # ----------------------------------------------------------------
    # Test 4
    # ----------------------------------------------------------------
    def test_includes_event_count_and_time_range(self, ledger_path):
        """Reporte tiene events_logged (int) + time_range start/end ISO 8601."""
        events = _build_valid_ledger_with_cost(ledger_path)

        from causadb.compliance._eu_ai_act import generate_eu_ai_act_report
        report = generate_eu_ai_act_report(ledger_path)

        assert report["events_logged"] == 4
        assert "time_range" in report
        assert "start" in report["time_range"]
        assert "end" in report["time_range"]

        # Verificar que son ISO 8601 timestamps (contienen "T" y "Z" o offset)
        start = report["time_range"]["start"]
        end = report["time_range"]["end"]
        assert "T" in start
        assert "T" in end
        # start debe ser el timestamp del primer evento (system_boot)
        assert start <= end

    # ----------------------------------------------------------------
    # Test 5
    # ----------------------------------------------------------------
    def test_validates_replay_determinism(self, ledger_path):
        """Dos llamadas a reconstruct_state() → states identicos → replay_test_passed == true.
        [AMEND-2026-07-22 CRITICAL#1] Test de consistencia, NO anti-teatro."""
        _build_valid_ledger_with_cost(ledger_path)

        from causadb.compliance._eu_ai_act import generate_eu_ai_act_report
        report = generate_eu_ai_act_report(ledger_path)

        assert report["replay_test_passed"] is True
        # traceability_guaranteed debe ser true porque todo esta OK
        assert report["traceability_guaranteed"] is True

    # ----------------------------------------------------------------
    # Test 6
    # ----------------------------------------------------------------
    def test_validates_side_effects_reconstructible(self, ledger_path):
        """Ledger con events que producen side-effects → side_effects_reconstructible == true.
        Ledger sin side-effects → false."""
        # Caso A: ledger con side-effects (FILE_MODIFIED, COMMAND_RUN, etc.)
        _build_valid_ledger_with_cost(ledger_path)

        from causadb.compliance._eu_ai_act import generate_eu_ai_act_report
        report1 = generate_eu_ai_act_report(ledger_path)
        assert report1["side_effects_reconstructible"] is True

        # Caso B: ledger sin side-effects (solo eventos que no producen side-effects)
        # Usar un ledger con eventos que no tienen side-effect lists en state
        # Ej: solo SYSTEM_BOOT (que appendea a system_boots pero solo tiene 1 entry)
        # El generador checkea files_modified, commands_run, commits_made, llm_invocations,
        # cost_accounted, tools_called, queries_executed, mutations_applied.
        writer2 = LedgerWriter(str(ledger_path).replace("ledger.log", "empty_ledger.log"))
        e = CanonicalEvent(
            event_type=EventType.SYSTEM_BOOT,
            ctx_id="test",
            source="causadb:test",
            payload={"boot_id": "boot-001"}
        )
        writer2.append(e)

        report2 = generate_eu_ai_act_report(writer2.ledger_path)
        # SYSTEM_BOOT appendea a "system_boots" pero no es uno de los side-effects
        # checkeados en el generador. Deberia dar false porque files_modified,
        # commands_run, commits_made, llm_invocations, cost_accounted,
        # tools_called, queries_executed, mutations_applied estan todos vacios.
        assert report2["side_effects_reconstructible"] is False

    # ----------------------------------------------------------------
    # Test 7 — CLI
    # ----------------------------------------------------------------
    def test_cli_compliance_eu_ai_act_outputs_json_summary(self, ledger_path, capsys):
        """causadb compliance --framework eu-ai-act --ledger <path> → exit 0, json."""
        _build_valid_ledger_with_cost(ledger_path)

        from causadb.cli.main import main
        exit_code = main(["compliance", "--framework", "eu-ai-act",
                          "--ledger", ledger_path])
        captured = capsys.readouterr().out

        assert exit_code == 0
        data = json.loads(captured)
        assert "traceability_guaranteed" in data
        assert "logging_facilities" in data
        assert "events_logged" in data
        assert "time_range" in data

    # ----------------------------------------------------------------
    # Test 8 — anti-teatro
    # ----------------------------------------------------------------
    def test_anti_teatro_skips_hash_validation(self, ledger_path, monkeypatch):
        """Mutar generate_eu_ai_act_report para skipear validate_chain() →
        test_detects_broken_hash_chain falla."""
        _build_valid_ledger(ledger_path, n_events=3)

        # Corromper el ledger
        with open(ledger_path, "r+") as f:
            content = f.read()
            f.seek(0)
            f.write(content.replace('"hash"', '"hosh"'))

        from causadb.compliance._eu_ai_act import generate_eu_ai_act_report

        # Mutar: skipear validate_chain(), hardcodear hash_chain_validated = True
        import causadb.compliance._eu_ai_act as mod
        original = mod.generate_eu_ai_act_report

        def mutated(ledger_path):
            reader = LedgerReader(ledger_path)
            entries = list(reader.read_all_entries())
            # Skipear toda validacion — retornar todo como OK
            return {
                "traceability_guaranteed": True,
                "logging_facilities": {
                    "append_only": True,
                    "hash_chain_validated": True,
                    "timestamp_precise": True,
                    "event_chain_complete": True,
                },
                "events_logged": len(entries),
                "time_range": {"start": "s", "end": "e"},
                "side_effects_reconstructible": True,
                "replay_test_passed": True,
                "validation_failure_type": None,
                "validation_failure_position": None,
            }

        monkeypatch.setattr(mod, "generate_eu_ai_act_report", mutated, raising=True)

        # Ahora test_detects_broken_hash_chain DEBE fallar porque la mutacion
        # devuelve todo True a pesar del ledger corrupto
        with pytest.raises(AssertionError):
            report = mutated(ledger_path)
            assert report["traceability_guaranteed"] is False
            assert report["logging_facilities"]["hash_chain_validated"] is False

        # Restaurar (monkeypatch lo hace al salir del test)
        # Verificar que el original sigue funcionando
        report2 = original(ledger_path)
        assert report2["traceability_guaranteed"] is False