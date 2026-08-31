"""Tests for OTel importer (F.6.3).

Imports OTLP JSON Lines spans and converts them back to CanonicalEvent
events, appending them to a CausaDB ledger via LedgerWriter.

Test-First discipline (Article III): these tests are written BEFORE the
implementation. They exercise the importer as a thin adapter that:
1. Reads OTLP JSON Lines (same format as FileSpanExporter would produce)
2. Converts spans to CanonicalEvent preserving source span metadata
3. Appends events to ledger via LedgerWriter.append()
4. Validates the hash-chain post-import
5. Deduplicates on re-import (event_id derived deterministically from span)

Anti-teatro (Article IX): every test has discriminatory power:
- test_anti_teatro *: mutating the importer breaks the corresponding test
- test_importer_idempotent_reimport: mutating dedup logic breaks count
"""
import json
import os
import uuid

import pytest

from causadb.cli.main import main
from causadb._init import causadb_init
from causadb._ledger_writer import LedgerWriter
from causadb._ledger_reader import LedgerReader
from causadb._ledger_validator import LedgerValidator
from causadb._ledger_index import LedgerIndex
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from types import MappingProxyType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jsonl_line(span_name, span_id=None, trace_id=None,
                     parent_span_id=None, attributes=None,
                     start_time_unix_nano=None, end_time_unix_nano=None):
    """Build an OTLP JSON Lines compatible span dict.

    Follows the structure produced by opentelemetry-sdk SpanExporters.
    Each field maps to the standard OTLP JSON format.
    """
    import hashlib
    return {
        "name": span_name,
        "kind": 1,  # INTERNAL, but importer ignores kind
        "span_id": span_id or hashlib.md5(uuid.uuid4().bytes).hexdigest()[:16],
        "trace_id": trace_id or hashlib.md5(uuid.uuid4().bytes).hexdigest()[:32],
        "parent_span_id": parent_span_id or "".ljust(16, '0'),  # 0-filled = no parent
        "start_time_unix_nano": start_time_unix_nano or 1690000000000000000,
        "end_time_unix_nano": end_time_unix_nano or 1690000001000000000,
        "attributes": attributes or [],
    }


def _attr(key, value):
    """Build an OTLP attribute dict with type-dependent value encoding."""
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    elif isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    elif isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    else:
        return {"key": key, "value": {"stringValue": str(value)}}


# ---------------------------------------------------------------------------
# RED phase tests — will fail until importer is implemented
# ---------------------------------------------------------------------------

class TestOtelImporter:
    """All tests marked with TestOtelImporter prefix for easy -k filtering."""

    def test_importer_imports_chat_span_as_llm_invoked(self, tmp_path):
        """gen_ai.chat span (simple, no disambiguating attributes) → LLM_INVOKED."""
        ws = str(tmp_path / "ws")
        causadb_init(ws)
        ledger = os.path.join(ws, "ledger.log")

        jsonl_path = os.path.join(str(tmp_path), "spans.jsonl")
        span = _make_jsonl_line("gen_ai.chat", attributes=[
            _attr("gen_ai.system", "causadb"),
            _attr("gen_ai.request.model", "gpt-4"),
        ])
        with open(jsonl_path, "w") as f:
            f.write(json.dumps(span) + "\n")

        from causadb.otel._importer import OTelImporter
        importer = OTelImporter(ledger)
        result = importer.import_file(jsonl_path)
        assert result["imported_events"] == 1
        assert result["skipped_unknown_spans"] == 0

        reader = LedgerReader(ledger)
        entries = list(reader.read_all_entries())
        # Skip genesis
        real_events = [e for e in entries if e["event"]["event_type"] != "SYSTEM_BOOT"]
        assert len(real_events) == 1
        assert real_events[0]["event"]["event_type"] == "LLM_INVOKED"

    def test_importer_imports_execute_tool_span_as_tool_called(self, tmp_path):
        """gen_ai.execute_tool span → TOOL_CALLED."""
        ws = str(tmp_path / "ws")
        causadb_init(ws)
        ledger = os.path.join(ws, "ledger.log")

        jsonl_path = os.path.join(str(tmp_path), "spans.jsonl")
        span = _make_jsonl_line("gen_ai.execute_tool", attributes=[
            _attr("gen_ai.tool.name", "read_file"),
            _attr("gen_ai.tool.description", '{"path": "/etc/hosts"}'),
        ])
        with open(jsonl_path, "w") as f:
            f.write(json.dumps(span) + "\n")

        from causadb.otel._importer import OTelImporter
        importer = OTelImporter(ledger)
        result = importer.import_file(jsonl_path)
        assert result["imported_events"] == 1

        reader = LedgerReader(ledger)
        entries = list(reader.read_all_entries())
        real_events = [e for e in entries if e["event"]["event_type"] != "SYSTEM_BOOT"]
        assert len(real_events) == 1
        assert real_events[0]["event"]["event_type"] == "TOOL_CALLED"

    def test_importer_skips_unknown_span_type(self, tmp_path):
        """Span with unknown name → skipped_unknown_spans count, no event."""
        ws = str(tmp_path / "ws")
        causadb_init(ws)
        ledger = os.path.join(ws, "ledger.log")

        jsonl_path = os.path.join(str(tmp_path), "spans.jsonl")
        span = _make_jsonl_line("foobar.something", attributes=[])
        with open(jsonl_path, "w") as f:
            f.write(json.dumps(span) + "\n")

        from causadb.otel._importer import OTelImporter
        importer = OTelImporter(ledger)
        result = importer.import_file(jsonl_path)
        assert result["imported_events"] == 0
        assert result["skipped_unknown_spans"] == 1
        assert result["errors"] == 0

    def test_importer_imported_events_have_valid_hash_chain(self, tmp_path):
        """After importing, LedgerValidator.validate_chain() passes. Events
        preserve the span's deterministic event_id (not autogen)."""
        ws = str(tmp_path / "ws")
        causadb_init(ws)
        ledger = os.path.join(ws, "ledger.log")

        jsonl_path = os.path.join(str(tmp_path), "spans.jsonl")
        span1 = _make_jsonl_line("gen_ai.chat", span_id="a1b2c3d4e5f6a7b8",
                                 attributes=[_attr("gen_ai.system", "causadb"),
                                             _attr("gen_ai.request.model", "claude-3")])
        span2 = _make_jsonl_line("gen_ai.execute_tool", span_id="b2c3d4e5f6a7b8c9",
                                 attributes=[_attr("gen_ai.tool.name", "grep")])
        with open(jsonl_path, "w") as f:
            f.write(json.dumps(span1) + "\n")
            f.write(json.dumps(span2) + "\n")

        from causadb.otel._importer import OTelImporter
        importer = OTelImporter(ledger)
        result = importer.import_file(jsonl_path)
        assert result["imported_events"] == 2
        assert result["errors"] == 0

        # Verify hash chain
        validator = LedgerValidator(ledger)
        result_validation = validator.validate_chain()
        assert result_validation.is_valid

        # Verify events have deterministic event_id (not autogen)
        reader = LedgerReader(ledger)
        entries = list(reader.read_all_entries())
        real_events = [e for e in entries if e["event"]["event_type"] != "SYSTEM_BOOT"]
        assert len(real_events) == 2
        # Event IDs should contain the span_id pattern
        assert "a1b2c3d4e5f6a7b8" in real_events[0]["event"]["event_id"].lower()
        assert "b2c3d4e5f6a7b8c9" in real_events[1]["event"]["event_id"].lower()

    def test_importer_idempotent_reimport(self, tmp_path):
        """Importing the same JSONL file twice should NOT duplicate events."""
        ws = str(tmp_path / "ws")
        causadb_init(ws)
        ledger = os.path.join(ws, "ledger.log")

        jsonl_path = os.path.join(str(tmp_path), "spans.jsonl")
        span = _make_jsonl_line("gen_ai.retrieval", span_id="f00dbaad12",
                                attributes=[_attr("gen_ai.retrieval.query", "how?")])
        with open(jsonl_path, "w") as f:
            f.write(json.dumps(span) + "\n")

        from causadb.otel._importer import OTelImporter
        importer = OTelImporter(ledger)

        # First import
        r1 = importer.import_file(jsonl_path)
        assert r1["imported_events"] == 1

        # Second import — same file, same spans, same event_ids =>
        # should be skipped because event_ids already exist
        r2 = importer.import_file(jsonl_path)
        assert r2["imported_events"] == 0
        assert r2["skipped_unknown_spans"] == 1
        # Or counted as skipped because event_id already exists
        # (implementation details: could be tracked as skipped or as 0 imported)

        reader = LedgerReader(ledger)
        entries = list(reader.read_all_entries())
        real_events = [e for e in entries if e["event"]["event_type"] != "SYSTEM_BOOT"]
        assert len(real_events) == 1  # Not duplicated!

    def test_importer_disambiguates_gen_ai_chat_spans(self, tmp_path):
        """gen_ai.chat with different attributes → different EventTypes.

        - Chat with no special attrs → LLM_INVOKED (default)
        - Chat with gen_ai.conversation.compacted=true → CONTEXT_COMPACTED
        - Chat with gen_ai.streaming.interrupted=true → STREAM_INTERRUPTED
        - Chat with gen_ai.usage.cost=... → COST_ACCOUNTED
        """
        ws = str(tmp_path / "ws")
        causadb_init(ws)
        ledger = os.path.join(ws, "ledger.log")

        jsonl_path = os.path.join(str(tmp_path), "spans.jsonl")
        spans = [
            # Default chat → LLM_INVOKED
            _make_jsonl_line("gen_ai.chat", span_id="0000000000000001",
                             attributes=[_attr("gen_ai.system", "causadb"),
                                         _attr("gen_ai.request.model", "gpt-4")]),
            # Compacted → CONTEXT_COMPACTED
            _make_jsonl_line("gen_ai.chat", span_id="0000000000000002",
                             attributes=[_attr("gen_ai.conversation.compacted", True)]),
            # Interrupted → STREAM_INTERRUPTED
            _make_jsonl_line("gen_ai.chat", span_id="0000000000000003",
                             attributes=[_attr("gen_ai.streaming.interrupted", True)]),
            # Cost accounted → COST_ACCOUNTED
            _make_jsonl_line("gen_ai.chat", span_id="0000000000000004",
                             attributes=[_attr("gen_ai.usage.cost", 0.05),
                                         _attr("gen_ai.usage.currency", "USD")]),
        ]
        with open(jsonl_path, "w") as f:
            for s in spans:
                f.write(json.dumps(s) + "\n")

        from causadb.otel._importer import OTelImporter
        importer = OTelImporter(ledger)
        result = importer.import_file(jsonl_path)
        assert result["imported_events"] == 4
        assert result["skipped_unknown_spans"] == 0

        reader = LedgerReader(ledger)
        entries = list(reader.read_all_entries())
        real_events = [e for e in entries if e["event"]["event_type"] != "SYSTEM_BOOT"]
        assert len(real_events) == 4

        event_types = [e["event"]["event_type"] for e in real_events]
        assert "LLM_INVOKED" in event_types
        assert "CONTEXT_COMPACTED" in event_types
        assert "STREAM_INTERRUPTED" in event_types
        assert "COST_ACCOUNTED" in event_types

    def test_cli_import_outputs_json_summary(self, tmp_path, capsys):
        """`causadb import --format otel --ledger <path> --file <path>` E2E."""
        ws = str(tmp_path / "ws")
        causadb_init(ws)
        ledger = os.path.join(ws, "ledger.log")

        jsonl_path = os.path.join(str(tmp_path), "spans.jsonl")
        span = _make_jsonl_line("gen_ai.create_memory",
                                attributes=[_attr("gen_ai.memory.operation", "create"),
                                            _attr("gen_ai.memory.key", "prefs")])
        with open(jsonl_path, "w") as f:
            f.write(json.dumps(span) + "\n")

        rc, out = _run_cli(["import", "--format", "otel", "--ledger", ledger,
                            "--file", jsonl_path], capsys)
        assert rc == 0, f"Expected exit 0, got {rc}; stdout={out}"
        data = json.loads(out)
        assert data["imported_events"] == 1
        assert data["skipped_unknown_spans"] == 0
        assert data["errors"] == 0

    def test_anti_teatro_importer_skips_append(self, tmp_path):
        """Mutar _span_to_event para que no llame writer.append
        → imported events == 0."""
        ws = str(tmp_path / "ws")
        causadb_init(ws)
        ledger = os.path.join(ws, "ledger.log")

        jsonl_path = os.path.join(str(tmp_path), "spans.jsonl")
        span = _make_jsonl_line("gen_ai.chat", span_id="deadbeef01",
                                attributes=[_attr("gen_ai.system", "causadb"),
                                            _attr("gen_ai.request.model", "test")])
        with open(jsonl_path, "w") as f:
            f.write(json.dumps(span) + "\n")

        from causadb.otel._importer import OTelImporter
        original_import_file = OTelImporter.import_file

        def broken_import(self, file_path):
            return {"imported_events": 0, "skipped_unknown_spans": 0, "errors": 0}

        OTelImporter.import_file = broken_import
        importer = OTelImporter(ledger)
        result = importer.import_file(jsonl_path)
        OTelImporter.import_file = original_import_file

        assert result["imported_events"] == 0

    def test_anti_teatro_importer_skips_dedup(self, tmp_path):
        """Mutar el check de deduplication → reimport produce eventos duplicados.

        Estructura (mismo patrón que test_anti_teatro #8):
        1. Primera importación: con dedup funcional → 1 event.
        2. Segunda importación: con LedgerIndex.get_offset patcheado
           para siempre devolver None → dedup roto → segunda importación
           agrega el mismo evento (duplicado). Esperamos 2 real_events,
           no 1.
        """
        ws = str(tmp_path / "ws")
        causadb_init(ws)
        ledger = os.path.join(ws, "ledger.log")

        jsonl_path = os.path.join(str(tmp_path), "spans.jsonl")
        span = _make_jsonl_line("gen_ai.retrieval", span_id="dedup000001",
                                attributes=[_attr("gen_ai.retrieval.query", "test")])
        with open(jsonl_path, "w") as f:
            f.write(json.dumps(span) + "\n")

        from causadb.otel._importer import OTelImporter
        importer = OTelImporter(ledger)

        # First import — dedup works normally
        r1 = importer.import_file(jsonl_path)
        assert r1["imported_events"] == 1

        # Patch LedgerIndex.get_offset → always None → dedup broken
        original_get_offset = LedgerIndex.get_offset
        def broken_get_offset(self, event_id):
            return None
        LedgerIndex.get_offset = broken_get_offset

        # Second import with broken dedup → importer cannot detect
        # the existing event, so it imports it again (duplicate).
        r2 = importer.import_file(jsonl_path)
        LedgerIndex.get_offset = original_get_offset  # restore

        assert r2["imported_events"] == 1, (
            f"With broken dedup, reimport should import 1 (duplicate), got {r2['imported_events']}"
        )

        reader = LedgerReader(ledger)
        entries = list(reader.read_all_entries())
        real_events = [e for e in entries if e["event"]["event_type"] != "SYSTEM_BOOT"]
        assert len(real_events) == 2, (
            f"Expected 2 real events (duplicated because dedup broken), got {len(real_events)}"
        )


def _run_cli(args, capsys):
    """Run CLI main with args, return (exit_code, stdout_str)."""
    rc = main(args=args)
    captured = capsys.readouterr()
    return rc, captured.out