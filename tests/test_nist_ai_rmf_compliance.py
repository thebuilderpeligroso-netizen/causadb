"""F.7.2 — NIST AI RMF compliance report tests.

Artículo III: 8 tests en RED phase antes de implementacion.
"""

import json
import pytest
from types import MappingProxyType

from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._config import CausaDBConfig
from causadb._ledger_reader import LedgerReader


@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")


def _build_valid_ledger(ledger_path):
    """Ledger con side-effects (llm_invocations, cost_accounted, etc.)."""
    writer = LedgerWriter(ledger_path)
    e1 = CanonicalEvent(
        event_type=EventType.SYSTEM_BOOT,
        ctx_id="test/session",
        source="causadb:test",
        payload={"boot_id": "boot-001"}
    )
    writer.append(e1)

    e2 = CanonicalEvent(
        event_type=EventType.LLM_INVOKED,
        ctx_id="test/session",
        source="causadb:test",
        parent_event_id=e1.event_id,
        payload=MappingProxyType({
            "model": "gpt-4",
            "prompt": "analyze code",
            "response_tokens": 150,
            "duration_ms": 1200,
        }),
    )
    writer.append(e2)

    e3 = CanonicalEvent(
        event_type=EventType.TOOL_CALLED,
        ctx_id="test/session",
        source="causadb:test",
        parent_event_id=e2.event_id,
        payload=MappingProxyType({
            "tool_name": "read_file",
            "arguments": {"path": "/test.py"},
            "result": "file contents",
            "duration_ms": 45,
        }),
    )
    writer.append(e3)

    e4 = CanonicalEvent(
        event_type=EventType.RETRIEVAL_DONE,
        ctx_id="test/session",
        source="causadb:test",
        parent_event_id=e3.event_id,
        payload=MappingProxyType({
            "query": "what are the rules?",
            "chunks": [{"text": "Rule 1: ..."}, {"text": "Rule 2: ..."}],
            "scores": [0.95, 0.87],
        }),
    )
    writer.append(e4)

    e5 = CanonicalEvent(
        event_type=EventType.COST_ACCOUNTED,
        ctx_id="test/session",
        source="causadb:test",
        parent_event_id=e4.event_id,
        payload=MappingProxyType({
            "model": "gpt-4",
            "tokens_in": 100,
            "tokens_out": 150,
            "cost": 0.075,
            "currency": "USD",
        }),
    )
    writer.append(e5)

    return [e1, e2, e3, e4, e5]


class TestNistAiRmfComplianceReport:
    # ----------------------------------------------------------------
    # Test 1
    # ----------------------------------------------------------------
    def test_has_4_functions(self, ledger_path):
        """Reporte tiene las 4 keys top-level: govern, map, measure, manage."""
        _build_valid_ledger(ledger_path)

        from causadb.compliance._nist_ai_rmf import generate_nist_report
        report = generate_nist_report(ledger_path)

        assert "govern" in report
        assert "map" in report
        assert "measure" in report
        assert "manage" in report

    # ----------------------------------------------------------------
    # Test 2
    # ----------------------------------------------------------------
    def test_govern_audit_trail_complete_on_valid_ledger(self, ledger_path):
        """Ledger valido → govern.audit_trail_complete == True."""
        _build_valid_ledger(ledger_path)

        from causadb.compliance._nist_ai_rmf import generate_nist_report
        report = generate_nist_report(ledger_path)

        assert report["govern"]["audit_trail_complete"] is True
        assert report["govern"]["policy_enforced"] is True

    # ----------------------------------------------------------------
    # Test 3
    # ----------------------------------------------------------------
    def test_govern_audit_trail_complete_false_on_corrupt_ledger(self, ledger_path):
        """Corromper hash chain → govern.audit_trail_complete == False."""
        _build_valid_ledger(ledger_path)

        with open(ledger_path, "r+") as f:
            content = f.read()
            f.seek(0)
            f.write(content.replace('"hash"', '"hosh"'))

        from causadb.compliance._nist_ai_rmf import generate_nist_report
        report = generate_nist_report(ledger_path)

        assert report["govern"]["audit_trail_complete"] is False
        assert report["govern"]["policy_enforced"] is False

    # ----------------------------------------------------------------
    # Test 4
    # ----------------------------------------------------------------
    def test_map_components_identified_includes_llm_tool_retrieval(self, ledger_path):
        """Ledger con LLM_INVOKED + TOOL_CALLED + RETRIEVAL_DONE →
        map.components_identified contiene llm, tool, retrieval."""
        _build_valid_ledger(ledger_path)

        from causadb.compliance._nist_ai_rmf import generate_nist_report
        report = generate_nist_report(ledger_path)

        components = report["map"]["components_identified"]
        assert "llm" in components
        assert "tool" in components
        assert "retrieval" in components

    # ----------------------------------------------------------------
    # Test 5
    # ----------------------------------------------------------------
    def test_measure_includes_cost_and_tokens(self, ledger_path):
        """Ledger con COST_ACCOUNTED → measure.total_cost_usd > 0, total_tokens > 0."""
        _build_valid_ledger(ledger_path)

        from causadb.compliance._nist_ai_rmf import generate_nist_report
        report = generate_nist_report(ledger_path)

        assert report["measure"]["total_cost_usd"] > 0
        assert report["measure"]["total_tokens"] > 0
        assert report["measure"]["total_events"] == 5

    # ----------------------------------------------------------------
    # Test 6
    # ----------------------------------------------------------------
    def test_manage_detects_incidents_via_sentinel(self, ledger_path):
        """Ledger con sandbox_violations → manage.incidents_detected >= 1."""
        writer = LedgerWriter(ledger_path)
        e1 = CanonicalEvent(
            event_type=EventType.SYSTEM_BOOT,
            ctx_id="test/session",
            source="causadb:test",
            payload={"boot_id": "boot-001"}
        )
        writer.append(e1)

        e2 = CanonicalEvent(
            event_type=EventType.SANDBOX_STATE,
            ctx_id="test/session",
            source="causadb:test",
            parent_event_id=e1.event_id,
            payload=MappingProxyType({
                "mutation_type": "file_modify",
                "path_or_resource": "/etc/passwd",
                "sandbox_boundary": "/workspace",
                "violates_boundary": True,
                "process_pid": 1234,
                "process_name": "agent.py",
            }),
        )
        writer.append(e2)

        from causadb.compliance._nist_ai_rmf import generate_nist_report
        report = generate_nist_report(ledger_path)

        assert report["manage"]["incidents_detected"] >= 1
        assert "sentinel_rules_passing" in report["manage"]

    # ----------------------------------------------------------------
    # Test 7 — CLI
    # ----------------------------------------------------------------
    def test_cli_compliance_nist_ai_rmf_outputs_json_summary(self, ledger_path, capsys):
        """causadb compliance --framework nist-ai-rmf --ledger <path> → exit 0, JSON."""
        _build_valid_ledger(ledger_path)

        from causadb.cli.main import main
        exit_code = main(["compliance", "--framework", "nist-ai-rmf",
                          "--ledger", ledger_path])
        captured = capsys.readouterr().out

        assert exit_code == 0
        data = json.loads(captured)
        assert "govern" in data
        assert "map" in data
        assert "measure" in data
        assert "manage" in data

    # ----------------------------------------------------------------
    # Test 8 — anti-teatro
    # ----------------------------------------------------------------
    def test_anti_teatro_nist_skips_audit_trail_check(self, ledger_path, monkeypatch):
        """Mutar generate_nist_report para skipear validate_chain() →
        test_govern_audit_trail_complete_false_on_corrupt_ledger falla."""
        _build_valid_ledger(ledger_path)

        with open(ledger_path, "r+") as f:
            content = f.read()
            f.seek(0)
            f.write(content.replace('"hash"', '"hosh"'))

        from causadb.compliance._nist_ai_rmf import generate_nist_report

        import causadb.compliance._nist_ai_rmf as mod
        original = mod.generate_nist_report

        def mutated(ledger_path):
            reader = LedgerReader(ledger_path)
            entries = list(reader.read_all_entries())
            event_types_seen = sorted({e["event"]["event_type"] for e in entries})
            components = []
            if "LLM_INVOKED" in event_types_seen: components.append("llm")
            if "TOOL_CALLED" in event_types_seen: components.append("tool")
            if "RETRIEVAL_DONE" in event_types_seen: components.append("retrieval")
            return {
                "govern": {
                    "policy_enforced": True,
                    "audit_trail_complete": True,
                },
                "map": {
                    "context_documented": len(entries) > 0,
                    "components_identified": components,
                },
                "measure": {
                    "total_events": len(entries),
                    "total_cost_usd": 0.0,
                    "total_tokens": 0,
                    "replay_determinism_verified": True,
                },
                "manage": {
                    "incidents_detected": 0,
                    "sentinel_rules_passing": True,
                },
                "_validation": {
                    "failure_type": None,
                    "failure_position": None,
                },
            }

        monkeypatch.setattr(mod, "generate_nist_report", mutated, raising=True)

        with pytest.raises(AssertionError):
            report = mutated(ledger_path)
            assert report["govern"]["audit_trail_complete"] is False
            assert report["govern"]["policy_enforced"] is False

        report2 = original(ledger_path)
        assert report2["govern"]["audit_trail_complete"] is False