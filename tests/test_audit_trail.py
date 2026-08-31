"""F.7.4 — Audit trail export tests.

Articulo III: 8 tests en RED phase antes de implementacion.
"""

import json
import os
import pytest
from datetime import datetime
from types import MappingProxyType

from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_reader import LedgerReader
from causadb._ledger_validator import LedgerValidator


@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")


def _build_ledger(ledger_path, n=3):
    """Ledger con N eventos de tipos variados."""
    writer = LedgerWriter(ledger_path)
    events = []
    types_payloads = [
        (EventType.FILE_MODIFIED, {"path": "/foo.py", "action": "create"}),
        (EventType.COMMAND_RUN, {"command": "ls", "exit_code": 0}),
        (EventType.LLM_INVOKED, {"model": "gpt-4", "prompt": "hi", "response_tokens": 10, "duration_ms": 200}),
    ]
    for i in range(n):
        t, p = types_payloads[i % len(types_payloads)]
        e = CanonicalEvent(
            event_type=t,
            ctx_id="test/audit",
            source="causadb:test",
            payload=MappingProxyType(p),
        )
        writer.append(e)
        events.append(e)
    return events


def _is_iso8601(s):
    """Quick check: string looks like ISO 8601 (has 'T' and is parseable)."""
    if not s or "T" not in s:
        return False
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except (ValueError, TypeError):
        return False


class TestAuditTrailExport:
    # ----------------------------------------------------------------
    # Test 1
    # ----------------------------------------------------------------
    def test_json_export_includes_all_events(self, ledger_path):
        """JSON export: array 'entries' has same length as LedgerReader."""
        _build_ledger(ledger_path, n=3)

        from causadb.cli._cmd_audit_trail import cmd_audit_trail
        exit_code, output = cmd_audit_trail(type("A", (), {
            "ledger": ledger_path, "format": "json", "output": None
        })())

        assert exit_code == 0
        data = json.loads(output)
        # entries length must match LedgerReader
        reader = LedgerReader(ledger_path)
        n_entries = sum(1 for _ in reader.read_all_entries())
        assert len(data["entries"]) == n_entries == 3
        # generated_at must be present and ISO 8601 parseable (NIT #3: not value)
        assert "generated_at" in data
        assert _is_iso8601(data["generated_at"])

    # ----------------------------------------------------------------
    # Test 2
    # ----------------------------------------------------------------
    def test_json_export_includes_hash_chain_validation(self, ledger_path):
        """JSON export: valid ledger -> validation.is_valid == true;
        corrupted ledger -> validation.is_valid == false."""
        _build_ledger(ledger_path, n=3)

        from causadb.cli._cmd_audit_trail import cmd_audit_trail
        # Caso A: ledger valido
        exit_code, output = cmd_audit_trail(type("A", (), {
            "ledger": ledger_path, "format": "json", "output": None
        })())
        data = json.loads(output)
        assert data["validation"]["is_valid"] is True

        # Caso B: corromper ledger
        with open(ledger_path, "r+") as f:
            content = f.read()
            f.seek(0)
            f.write(content.replace('"hash"', '"hosh"'))

        exit_code2, output2 = cmd_audit_trail(type("A", (), {
            "ledger": ledger_path, "format": "json", "output": None
        })())
        data2 = json.loads(output2)
        assert data2["validation"]["is_valid"] is False

    # ----------------------------------------------------------------
    # Test 3
    # ----------------------------------------------------------------
    def test_text_export_includes_header_and_summary(self, ledger_path):
        """Text export: contains header, Summary:, Entries, Hash-chain validation: PASS."""
        _build_ledger(ledger_path, n=3)

        from causadb.cli._cmd_audit_trail import cmd_audit_trail
        exit_code, output = cmd_audit_trail(type("A", (), {
            "ledger": ledger_path, "format": "text", "output": None
        })())

        assert exit_code == 0
        assert "=== CausaDB Audit Trail ===" in output
        assert "Summary:" in output
        assert "Entries" in output
        assert "Hash-chain validation: PASS" in output
        # Check Generated line has ISO 8601 timestamp (NIT #3: not exact value)
        gen_line = [l for l in output.splitlines() if l.startswith("Generated: ")]
        assert len(gen_line) == 1
        ts = gen_line[0].replace("Generated: ", "").strip()
        assert _is_iso8601(ts)

    # ----------------------------------------------------------------
    # Test 4
    # ----------------------------------------------------------------
    def test_text_export_includes_each_event_on_its_own_block(self, ledger_path):
        """Text export with 3 events -> 3 numbered blocks [#1], [#2], [#3]."""
        _build_ledger(ledger_path, n=3)

        from causadb.cli._cmd_audit_trail import cmd_audit_trail
        exit_code, output = cmd_audit_trail(type("A", (), {
            "ledger": ledger_path, "format": "text", "output": None
        })())

        # Each event has its own block
        assert "[#1]" in output
        assert "[#2]" in output
        assert "[#3]" in output
        # No [#4]
        assert "[#4]" not in output

    # ----------------------------------------------------------------
    # Test 5
    # ----------------------------------------------------------------
    def test_export_summary_includes_event_types_count(self, ledger_path):
        """JSON export: event_types_count dict has keys per type, sum(total) == total_events."""
        _build_ledger(ledger_path, n=3)

        from causadb.cli._cmd_audit_trail import cmd_audit_trail
        exit_code, output = cmd_audit_trail(type("A", (), {
            "ledger": ledger_path, "format": "json", "output": None
        })())
        data = json.loads(output)

        event_types_count = data["summary"]["event_types_count"]
        total_events = data["summary"]["total_events"]
        # sum of counts == total events
        assert sum(event_types_count.values()) == total_events == 3
        # Each key is a known event type
        for et in event_types_count:
            assert isinstance(et, str)

    # ----------------------------------------------------------------
    # Test 6 (roundtrip — Alternativa B)
    # ----------------------------------------------------------------
    def test_roundtrip_json_import_validates(self, tmp_path):
        """Export JSON -> re-import via CLI 'log' command-by-command -> validate new ledger is_valid.
        [AMEND-2026-07-22 CRITICAL#2] Alternativa B: chain valida, no identica (sequence_number se recalcula).
        """
        lp = str(tmp_path / "ledger.log")
        _build_ledger(lp, n=3)

        from causadb.cli._cmd_audit_trail import cmd_audit_trail
        exit_code, output = cmd_audit_trail(type("A", (), {
            "ledger": lp, "format": "json", "output": None
        })())
        exported = json.loads(output)

        # New ledger path for re-import
        new_lp = str(tmp_path / "imported.log")
        writer = LedgerWriter(new_lp)
        for entry in exported["entries"]:
            ev = entry["event"]
            # Re-create CanonicalEvent from entry (preserves event_id, timestamp, etc.).
            # CanonicalEvent.from_dict does NOT preserve sequence_number (not a field);
            # LedgerWriter.append recalculates it -> hash-chain differs but is still valid.
            event = CanonicalEvent(
                event_type=EventType(ev["event_type"]),
                ctx_id=ev["ctx_id"],
                source=ev["source"],
                event_id=ev["event_id"],
                timestamp=ev["timestamp"],
                parent_event_id=ev.get("parent_event_id"),
                source_type=ev.get("source_type", "agent"),
                payload=MappingProxyType(ev.get("payload", {}) or {}),
            )
            writer.append(event)

        # Validate the new ledger
        validator = LedgerValidator(new_lp)
        vr = validator.validate_chain()
        assert vr.is_valid is True

    # ----------------------------------------------------------------
    # Test 7 — CLI dual behavior (output file + stdout)
    # ----------------------------------------------------------------
    def test_cli_writes_to_output_file_and_stdout(self, ledger_path, capsys, tmp_path):
        """(a) --output /tmp/audit.json -> file exists, JSON parseable.
        (b) sin --output -> stdout tiene el reporte text legible.
        [AMEND-2026-07-22 NIT#6] test combinado.
        """
        _build_ledger(ledger_path, n=3)

        from causadb.cli.main import main

        # (a) Con --output
        out_file = str(tmp_path / "audit.json")
        exit_code1 = main([
            "audit-trail", "--ledger", ledger_path,
            "--format", "json", "--output", out_file,
        ])
        captured1 = capsys.readouterr().out

        assert exit_code1 == 0
        assert os.path.exists(out_file)
        with open(out_file) as f:
            data = json.load(f)
        assert "entries" in data
        # return value es metadata JSON
        meta = json.loads(captured1)
        assert meta["written_to"] == out_file
        assert meta["format"] == "json"
        assert meta["bytes"] > 0

        # (b) Sin --output (stdout directo)
        exit_code2 = main([
            "audit-trail", "--ledger", ledger_path,
            "--format", "text",
        ])
        captured2 = capsys.readouterr().out

        assert exit_code2 == 0
        assert "=== CausaDB Audit Trail ===" in captured2

    # ----------------------------------------------------------------
    # Test 8 — anti-teatro
    # ----------------------------------------------------------------
    def test_anti_teatro_skips_hash_validation(self, ledger_path, monkeypatch):
        """Mutar _cmd_audit_trail.py:cmd_audit_trail para skipear validate_chain() y
        hardcodear validation.is_valid = true -> test_json_export_includes_hash_chain_validation cae."""
        _build_ledger(ledger_path, n=3)

        # Corromper ledger
        with open(ledger_path, "r+") as f:
            content = f.read()
            f.seek(0)
            f.write(content.replace('"hash"', '"hosh"'))

        import causadb.cli._cmd_audit_trail as mod
        original_cmd = mod.cmd_audit_trail

        def mutated_cmd(args):
            # Skipear validate_chain() y hardcodear validation.is_valid = true
            import json
            from causadb._ledger_reader import LedgerReader
            reader = LedgerReader(args.ledger)
            entries = list(reader.read_all_entries())
            fake_vr = type("VR", (), {
                "is_valid": True,
                "failure_type": None,
                "position": None,
            })
            # Llamar al helper _format_json interno pasando el VR falso
            content = mod._format_json(entries, fake_vr, args.ledger)
            return (0, content)

        monkeypatch.setattr(mod, "cmd_audit_trail", mutated_cmd, raising=True)

        # Si el cmd es mutado a hardcodear True, el test_json_export_includes_hash_chain_validation cae
        with pytest.raises(AssertionError):
            exit_code, output = mutated_cmd(type("A", (), {
                "ledger": ledger_path, "format": "json", "output": None
            })())
            data = json.loads(output)
            # Esta asercion (que tendria que dar False) da con la mutacion True
            assert data["validation"]["is_valid"] is False

        # Restaurar y verificar que el original sigue atrapando el ledger corrupto
        exit_code, output = original_cmd(type("A", (), {
            "ledger": ledger_path, "format": "json", "output": None
        })())
        data = json.loads(output)
        assert data["validation"]["is_valid"] is False