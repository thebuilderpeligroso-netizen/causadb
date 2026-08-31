"""F.7.3 — AI Incident Response template tests.

Articulo III: 7 tests en RED phase antes de implementacion.
"""

import json
import pytest
from types import MappingProxyType

from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType


@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")


def _build_chained_ledger(ledger_path):
    """Ledger con A -> B -> C (FILE_MODIFIED siendo C un sandbox violation)."""
    writer = LedgerWriter(ledger_path)
    e_a = CanonicalEvent(
        event_type=EventType.LLM_INVOKED,
        ctx_id="session/incident",
        source="causadb:test",
        payload=MappingProxyType({
            "model": "gpt-4",
            "prompt": "write to /etc/passwd",
            "response_tokens": 42,
            "duration_ms": 800,
        }),
    )
    writer.append(e_a)

    e_b = CanonicalEvent(
        event_type=EventType.TOOL_CALLED,
        ctx_id="session/incident",
        source="causadb:test",
        parent_event_id=e_a.event_id,
        payload=MappingProxyType({
            "tool_name": "write_file",
            "arguments": {"path": "/etc/passwd"},
            "result": "ok",
            "duration_ms": 5,
        }),
    )
    writer.append(e_b)

    e_c = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="session/incident",
        source="causadb:test",
        parent_event_id=e_b.event_id,
        payload=MappingProxyType({"path": "/etc/passwd", "action": "modify"}),
    )
    writer.append(e_c)

    return [e_a, e_b, e_c]


def _build_orphan_ledger(ledger_path):
    """Ledger con 1 evento C cuyo parent_event_id apunta a un ID inexistente."""
    writer = LedgerWriter(ledger_path)
    e_c = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="session/orphan",
        source="causadb:test",
        parent_event_id="non-existent-parent-id-123",
        payload=MappingProxyType({"path": "/etc/shadow", "action": "modify"}),
    )
    writer.append(e_c)
    return [e_c]


def _build_ledger_with_revert(ledger_path):
    """Ledger con mutacion aplicada + MUTATION_REVERTED apuntando a la mutacion."""
    writer = LedgerWriter(ledger_path)

    e_a = CanonicalEvent(
        event_type=EventType.SYSTEM_BOOT,
        ctx_id="session/revert",
        source="causadb:test",
        payload={"boot_id": "boot-001"}
    )
    writer.append(e_a)

    e_b = CanonicalEvent(
        event_type=EventType.MUTATION_APPLIED,
        ctx_id="session/revert",
        source="causadb:test",
        parent_event_id=e_a.event_id,
        payload=MappingProxyType({"mutation_id": "mut-001", "change": "added file foo.py"}),
    )
    writer.append(e_b)

    e_revert = CanonicalEvent(
        event_type=EventType.MUTATION_REVERTED,
        ctx_id="session/revert",
        source="causadb:test",
        parent_event_id=e_b.event_id,
        payload=MappingProxyType({"revert_target_event_id": e_b.event_id}),
    )
    writer.append(e_revert)

    return [e_a, e_b, e_revert]


class TestIncidentResponseReport:
    # ----------------------------------------------------------------
    # Test 1
    # ----------------------------------------------------------------
    def test_has_4_sections(self, ledger_path):
        """Reporte tiene what_happened, why_it_happened, remediation, prevention."""
        events = _build_chained_ledger(ledger_path)
        incident_id = events[2].event_id

        from causadb.compliance._incident_response import generate_incident_report
        report = generate_incident_report(ledger_path, incident_id)

        assert "what_happened" in report
        assert "why_it_happened" in report
        assert "remediation" in report
        assert "prevention" in report

    # ----------------------------------------------------------------
    # Test 2 (combinado: cadena completa + orphan)
    # ----------------------------------------------------------------
    def test_causal_chain_traces_to_root_and_truncates_on_orphan(self, tmp_path):
        """(a) Cadena A->B->C (C es incident) -> causal_chain 3 elementos, root == A.
        (b) Orphan C -> causal_chain 1 elemento (solo C), root == C, chain_length == 1.
        [AMEND-2026-07-22 NIT#2] test combinado.
        """
        from causadb.compliance._incident_response import generate_incident_report

        # (a) Cadena completa
        lp1 = str(tmp_path / "ledger_full.log")
        events = _build_chained_ledger(lp1)
        incident_id = events[2].event_id
        report = generate_incident_report(lp1, incident_id)

        chain = report["why_it_happened"]["causal_chain"]
        assert report["why_it_happened"]["chain_length"] == 3
        assert report["why_it_happened"]["root_cause_event_id"] == events[0].event_id
        assert len(chain) == 3
        # El primer elemento de la cadena debe ser la raiz (A)
        assert chain[0]["event_id"] == events[0].event_id

        # (b) Orphan (parent_event_id inexistente)
        lp2 = str(tmp_path / "ledger_orphan.log")
        events_o = _build_orphan_ledger(lp2)
        orphan_id = events_o[0].event_id
        report2 = generate_incident_report(lp2, orphan_id)

        chain2 = report2["why_it_happened"]["causal_chain"]
        assert report2["why_it_happened"]["chain_length"] == 1
        assert report2["why_it_happened"]["root_cause_event_id"] == orphan_id
        assert len(chain2) == 1
        assert chain2[0]["event_id"] == orphan_id

    # ----------------------------------------------------------------
    # Test 3
    # ----------------------------------------------------------------
    def test_what_happened_includes_event_type_and_timestamp(self, ledger_path):
        """what_happened describe el event_type y timestamp del incident."""
        events = _build_chained_ledger(ledger_path)
        incident_id = events[2].event_id

        from causadb.compliance._incident_response import generate_incident_report
        report = generate_incident_report(ledger_path, incident_id)

        what = report["what_happened"]
        # Debe contener el event_type (FILE_MODIFIED) y el timestamp
        assert "FILE_MODIFIED" in what
        assert events[2].timestamp in what

    # ----------------------------------------------------------------
    # Test 4
    # ----------------------------------------------------------------
    def test_revert_possible_when_mutation_reverted_exists(self, ledger_path):
        """incident_event_id tiene MUTATION_REVERTED apuntandolo -> revert_possible == True."""
        events = _build_ledger_with_revert(ledger_path)
        # El incident es la MUTATION_APPLIED (events[1])
        incident_id = events[1].event_id

        from causadb.compliance._incident_response import generate_incident_report
        report = generate_incident_report(ledger_path, incident_id)

        assert report["remediation"]["revert_possible"] is True
        assert report["remediation"]["revert_target_event_id"] is not None

    # ----------------------------------------------------------------
    # Test 5
    # ----------------------------------------------------------------
    def test_revert_not_possible_when_no_revert(self, ledger_path):
        """Sin MUTATION_REVERTED -> remediation.revert_possible == False."""
        events = _build_chained_ledger(ledger_path)
        incident_id = events[2].event_id

        from causadb.compliance._incident_response import generate_incident_report
        report = generate_incident_report(ledger_path, incident_id)

        assert report["remediation"]["revert_possible"] is False
        assert report["remediation"]["revert_target_event_id"] is None

    # ----------------------------------------------------------------
    # Test 6 — CLI
    # ----------------------------------------------------------------
    def test_cli_incident_outputs_json_summary(self, ledger_path, capsys):
        """causadb incident --ledger <path> --event-id <uuid> -> exit 0, JSON con 4 secciones."""
        events = _build_chained_ledger(ledger_path)
        incident_id = events[2].event_id

        from causadb.cli.main import main
        exit_code = main(["incident", "--ledger", ledger_path,
                          "--event-id", incident_id])
        captured = capsys.readouterr().out

        assert exit_code == 0
        data = json.loads(captured)
        assert "what_happened" in data
        assert "why_it_happened" in data
        assert "remediation" in data
        assert "prevention" in data

    # ----------------------------------------------------------------
    # Test 7 — anti-teatro
    # ----------------------------------------------------------------
    def test_anti_teatro_skips_causal_chain(self, ledger_path, monkeypatch):
        """Mutar generate_incident_report para que causal_chain = [] y
        root_cause_event_id = None -> test causal_chain_traces_to_root_and_truncates_on_orphan falla."""
        events = _build_chained_ledger(ledger_path)
        incident_id = events[2].event_id

        import causadb.compliance._incident_response as mod
        original = mod.generate_incident_report

        def mutated(ledger_path, event_id):
            return {
                "what_happened": "incident",
                "why_it_happened": {
                    "triggering_event": "",
                    "causal_chain": [],
                    "root_cause_event_id": None,
                    "chain_length": 0,
                    "chain_truncated": False,
                },
                "remediation": {
                    "action_taken": "",
                    "revert_possible": False,
                    "revert_target_event_id": None,
                },
                "prevention": {
                    "sentinel_rules_triggered": [],
                    "recommended_rules": [],
                },
                "_estimated": False,
            }

        monkeypatch.setattr(mod, "generate_incident_report", mutated, raising=True)

        with pytest.raises(AssertionError):
            report = mutated(ledger_path, incident_id)
            # Test target (a): chain_length == 3 y root == events[0].event_id
            assert report["why_it_happened"]["chain_length"] == 3
            assert report["why_it_happened"]["root_cause_event_id"] == events[0].event_id

        # Verificar que el original sigue funcionando
        report2 = original(ledger_path, incident_id)
        assert report2["why_it_happened"]["chain_length"] == 3
        assert report2["why_it_happened"]["root_cause_event_id"] == events[0].event_id

    # ----------------------------------------------------------------
    # Test 8 — loop / cycle detection (chain_truncated flag)
    # ----------------------------------------------------------------
    def test_causal_chain_truncated_on_loop(self, tmp_path, monkeypatch):
        """Ciclo A->B->A: _trace_causal_chain debe setear chain_truncated == True.
        El ledger no se puede construir via LedgerWriter normal (que no permitiria
        el ciclo porque genera event_id nuevos en cada append), asi que construyo
        entries raw con parent_event_id apuntando cruzados y los escribo al archivo.
        """
        from causadb.compliance._incident_response import generate_incident_report
        import causadb.compliance._incident_response as mod
        from causadb._event_types import EventType

        lp = str(tmp_path / "cycle.log")

        # Construir entries raw con ciclo A -> B -> A (parent_event_id cruzados)
        from causadb._event_schema import CanonicalEvent
        from types import MappingProxyType
        import json
        import hashlib

        # Crear dos eventos raw con IDs fijos
        ev_a = {
            "event_id": "aaaaaaaa-0000-0000-0000-000000000001",
            "event_type": "FILE_MODIFIED",
            "timestamp": "2026-07-22T10:00:00Z",
            "ctx_id": "session/cycle",
            "source": "causadb:test",
            "parent_event_id": "bbbbbbbb-0000-0000-0000-000000000002",
            "source_type": "agent",
            "schema_version": "0.1.0",
            "payload": {"path": "/a.py", "action": "create"},
            "metadata": None,
        }
        ev_b = {
            "event_id": "bbbbbbbb-0000-0000-0000-000000000002",
            "event_type": "TOOL_CALLED",
            "timestamp": "2026-07-22T10:01:00Z",
            "ctx_id": "session/cycle",
            "source": "causadb:test",
            "parent_event_id": "aaaaaaaa-0000-0000-0000-000000000001",
            "source_type": "agent",
            "schema_version": "0.1.0",
            "payload": {"tool_name": "t", "arguments": {}, "result": "ok"},
            "metadata": None,
        }

        # Construir entries con hash-chain valida (prev_hash + hash)
        entries_raw = []
        prev = "GENESIS"
        for ev in [ev_a, ev_b]:
            ev_json = json.dumps(ev, sort_keys=True)
            h = hashlib.sha256((ev_json + prev).encode()).hexdigest()
            entries_raw.append({"event": ev, "prev_hash": prev, "hash": h})
            prev = h

        with open(lp, "w") as f:
            for entry in entries_raw:
                f.write(json.dumps(entry, sort_keys=True) + "\n")

        # Incident = ev_a (su parent apunta a ev_b, cuyo parent apunta a ev_a = ciclo)
        report = generate_incident_report(lp, ev_a["event_id"])

        assert report["why_it_happened"]["chain_truncated"] is True
        # La cadena debe ser no vacia (se llego a ev_a pero se corto al detectar el loop)
        assert report["why_it_happened"]["chain_length"] >= 1