"""Tests Fase 11 — SESSION_SUMMARY (círculo multi-capa).

Artículo III: RED first — tests escritos antes de la implementación.
Artículo IX: anti-teatro — los tests verifican comportamiento real.
"""

import json
import os
import pytest
from datetime import datetime, timezone

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._harvest_source import HarvestSource
from causadb._harvester import Harvester
from causadb._replay_engine import ReplayEngine


# ===================================================================
# 11.2 — _session_summarizer tests
# ===================================================================

class TestSummarizeSession:
    """Tests para _summarize_session(raw_events, tool)."""

    def test_summarize_session_vacia_retorna_none(self):
        """Lista vacía → retorna None (no generar summary vacío)."""
        from causadb._session_summarizer import summarize_session
        result = summarize_session([], tool="gemini")
        assert result is None

    def test_summarize_session_basica(self):
        """3 LLM_INVOKED + 2 TOOL_CALLED → turn_count=3, tokens_used sumado."""
        from causadb._session_summarizer import summarize_session

        raw_events = [
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:00Z",
             "response_tokens": 100, "response_content": "Hello world",
             "prompt": "Say hello", "__harvest_session_id": "s1"},
            {"type": "TOOL_CALLED", "timestamp": "2026-08-01T10:00:01Z",
             "tool_name": "read_file", "error": "", "__harvest_session_id": "s1"},
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:02Z",
             "response_tokens": 200, "response_content": "File contents...",
             "prompt": "Read the file", "__harvest_session_id": "s1"},
            {"type": "TOOL_CALLED", "timestamp": "2026-08-01T10:00:03Z",
             "tool_name": "write_file", "error": "", "__harvest_session_id": "s1"},
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:04Z",
             "response_tokens": 150, "response_content": "Done",
             "prompt": "Write the result", "__harvest_session_id": "s1"},
        ]

        result = summarize_session(raw_events, tool="gemini")
        assert result is not None
        assert result.event_type == EventType.SESSION_SUMMARY
        payload = dict(result.payload)
        assert payload["tool"] == "gemini"
        assert payload["session_id"] == "s1"
        assert payload["turn_count"] == 3
        assert payload["tokens_used"] == 450  # 100 + 200 + 150
        assert len(payload["summary_lines"]) == 3

    def test_summarize_session_extrae_files_touched(self):
        """Raw events con FILE_MODIFIED → paths únicos en files_touched."""
        from causadb._session_summarizer import summarize_session

        raw_events = [
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:00Z",
             "response_tokens": 10, "response_content": "ok",
             "prompt": "test", "__harvest_session_id": "s1"},
            {"type": "FILE_MODIFIED", "timestamp": "2026-08-01T10:00:01Z",
             "path": "src/main.py", "action": "create", "__harvest_session_id": "s1"},
            {"type": "FILE_MODIFIED", "timestamp": "2026-08-01T10:00:02Z",
             "path": "src/main.py", "action": "modify", "__harvest_session_id": "s1"},
            {"type": "FILE_MODIFIED", "timestamp": "2026-08-01T10:00:03Z",
             "path": "tests/test.py", "action": "create", "__harvest_session_id": "s1"},
        ]

        result = summarize_session(raw_events, tool="gemini")
        assert result is not None
        payload = dict(result.payload)
        assert sorted(payload["files_touched"]) == ["src/main.py", "tests/test.py"]

    def test_summarize_session_extrae_errors(self):
        """TOOL_CALLED con error no vacío → errors list."""
        from causadb._session_summarizer import summarize_session

        raw_events = [
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:00Z",
             "response_tokens": 10, "response_content": "ok",
             "prompt": "test", "__harvest_session_id": "s1"},
            {"type": "TOOL_CALLED", "timestamp": "2026-08-01T10:00:01Z",
             "tool_name": "bash", "error": "Permission denied", "__harvest_session_id": "s1"},
            {"type": "TOOL_CALLED", "timestamp": "2026-08-01T10:00:02Z",
             "tool_name": "read_file", "error": "", "__harvest_session_id": "s1"},
            {"type": "TOOL_CALLED", "timestamp": "2026-08-01T10:00:03Z",
             "tool_name": "write_file", "error": "Disk full", "__harvest_session_id": "s1"},
        ]

        result = summarize_session(raw_events, tool="gemini")
        assert result is not None
        payload = dict(result.payload)
        errors = payload["errors"]
        assert len(errors) == 2
        assert errors[0]["tool_name"] == "bash"
        assert errors[0]["error"] == "Permission denied"
        assert errors[1]["tool_name"] == "write_file"
        assert errors[1]["error"] == "Disk full"

    def test_summarize_session_extrae_decisions(self):
        """REASONING_STEP con step_type que contiene 'decision' → extrae."""
        from causadb._session_summarizer import summarize_session

        raw_events = [
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:00Z",
             "response_tokens": 10, "response_content": "ok",
             "prompt": "test", "__harvest_session_id": "s1"},
            {"type": "REASONING_STEP", "timestamp": "2026-08-01T10:00:01Z",
             "step_type": "decision", "reasoning": "Use SQLite",
             "__harvest_session_id": "s1"},
            {"type": "REASONING_STEP", "timestamp": "2026-08-01T10:00:02Z",
             "step_type": "plan", "reasoning": "First do X",
             "__harvest_session_id": "s1"},
            {"type": "REASONING_STEP", "timestamp": "2026-08-01T10:00:03Z",
             "step_type": "decision", "reasoning": "Refactor module",
             "__harvest_session_id": "s1"},
        ]

        result = summarize_session(raw_events, tool="gemini")
        assert result is not None
        payload = dict(result.payload)
        decisions = payload["decisions"]
        assert len(decisions) == 2
        assert decisions[0]["step_type"] == "decision"
        assert decisions[0]["reasoning"] == "Use SQLite"
        assert decisions[1]["step_type"] == "decision"
        assert decisions[1]["reasoning"] == "Refactor module"

    def test_summarize_session_trunca_summary_lines(self):
        """Prompts y responses largos se truncan a 60 chars con '...'."""
        from causadb._session_summarizer import summarize_session

        long_prompt = "A" * 100
        long_response = "B" * 100

        raw_events = [
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:00Z",
             "response_tokens": 10, "response_content": long_response,
             "prompt": long_prompt, "__harvest_session_id": "s1"},
        ]

        result = summarize_session(raw_events, tool="gemini")
        assert result is not None
        payload = dict(result.payload)
        lines = payload["summary_lines"]
        assert len(lines) == 1
        line = lines[0]
        assert line.startswith("user: ")
        assert "assistant: " in line
        # Debe truncar — el formato es "user: <trunc> assistant: <trunc>"
        assert len(line) <= 150  # "user: " + 63 + "..." + " assistant: " + 63 + "..."
        assert "..." in line
        assert "..." in line

    def test_summarize_session_duration_s(self):
        """Verifica diff entre primer y último timestamp."""
        from causadb._session_summarizer import summarize_session

        raw_events = [
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:00Z",
             "response_tokens": 10, "response_content": "ok",
             "prompt": "test", "__harvest_session_id": "s1"},
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:30Z",
             "response_tokens": 10, "response_content": "ok",
             "prompt": "test", "__harvest_session_id": "s1"},
        ]

        result = summarize_session(raw_events, tool="gemini")
        assert result is not None
        payload = dict(result.payload)
        assert payload["duration_s"] == 30

    def test_summarize_session_sin_session_id_usa_unknown(self):
        """Si no hay __harvest_session_id, usa 'unknown'."""
        from causadb._session_summarizer import summarize_session

        raw_events = [
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:00Z",
             "response_tokens": 10, "response_content": "ok",
             "prompt": "test"},
        ]

        result = summarize_session(raw_events, tool="gemini")
        assert result is not None
        payload = dict(result.payload)
        assert payload["session_id"] == "unknown"

    def test_summarize_session_sin_llm_invoked_retorna_none(self):
        """Si no hay LLM_INVOKED, retorna None (no hay turnos que resumir)."""
        from causadb._session_summarizer import summarize_session

        raw_events = [
            {"type": "TOOL_CALLED", "timestamp": "2026-08-01T10:00:00Z",
             "tool_name": "bash", "error": "", "__harvest_session_id": "s1"},
        ]

        result = summarize_session(raw_events, tool="gemini")
        assert result is None


# ===================================================================
# 11.4 — ReplayEngine apply() tests
# ===================================================================

class TestReplaySessionSummary:
    """Tests para ReplayEngine.apply() con SESSION_SUMMARY."""

    def test_apply_session_summary_en_replay(self, tmp_path):
        """Escribe evento SESSION_SUMMARY real al ledger, replay, verifica."""
        from causadb._init import causadb_init
        from causadb._ledger_writer import LedgerWriter
        from causadb._config import CausaDBConfig
        from types import MappingProxyType

        result = causadb_init(str(tmp_path / "ws"))
        ledger_path = result["ledger_path"]
        config = CausaDBConfig(ledger_path=ledger_path)
        writer = LedgerWriter(ledger_path, config)

        event = CanonicalEvent(
            event_type=EventType.SESSION_SUMMARY,
            ctx_id="test",
            source="harvester:gemini",
            source_type="agent",
            payload=MappingProxyType({
                "tool": "gemini",
                "session_id": "s1",
                "turn_count": 3,
                "summary_lines": ["user: hello assistant: hi"],
                "decisions": [],
                "errors": [],
                "files_touched": ["main.py"],
                "tokens_used": 450,
                "duration_s": 30,
            }),
        )
        writer.append(event)

        engine = ReplayEngine(ledger_path)
        state = engine.reconstruct_state()
        summaries = state.get("session_summaries", [])
        assert len(summaries) >= 1
        # El último summary debe ser el nuestro
        last = summaries[-1]
        assert last["tool"] == "gemini"
        assert last["session_id"] == "s1"
        assert last["turn_count"] == 3
        assert last["tokens_used"] == 450
        assert last["duration_s"] == 30
        assert last["files_touched"] == ["main.py"]


# ===================================================================
# 11.4b — Revive muestra session_summaries
# ===================================================================

class TestReviveSessionSummaries:
    def test_revive_muestra_session_summaries(self, tmp_path):
        """Revive output incluye sección 'Sesiones Recientes'."""
        from causadb._init import causadb_init
        from causadb._ledger_writer import LedgerWriter
        from causadb._config import CausaDBConfig
        from causadb.cli._cmd_revive import _run_revive, _generate_revive_markdown
        from types import MappingProxyType

        result = causadb_init(str(tmp_path / "ws"))
        ledger_path = result["ledger_path"]
        config = CausaDBConfig(ledger_path=ledger_path)
        writer = LedgerWriter(ledger_path, config)

        # Write a SESSION_SUMMARY event
        event = CanonicalEvent(
            event_type=EventType.SESSION_SUMMARY,
            ctx_id="test",
            source="harvester:gemini",
            source_type="agent",
            payload=MappingProxyType({
                "tool": "gemini",
                "session_id": "session-abc",
                "turn_count": 5,
                "summary_lines": ["user: test prompt... assistant: test response..."],
                "decisions": [],
                "errors": [],
                "files_touched": ["src/main.py"],
                "tokens_used": 1200,
                "duration_s": 45,
            }),
        )
        writer.append(event)

        # Run revive
        exit_code, output = _run_revive(ledger_path, output_format="markdown")
        assert exit_code == 0
        assert "Sesiones Recientes" in output
        assert "gemini" in output
        assert "session-abc" in output
        assert "5" in output  # turn_count
        assert "1200" in output  # tokens_used
        assert "45" in output  # duration_s


# ===================================================================
# 11.3 — Harvester genera SESSION_SUMMARY
# ===================================================================

class MockAgentSource(HarvestSource):
    """Fuente mock que simula una fuente de agente (source_type en _AGENT_SOURCES)."""

    def __init__(self, ledger_path, events=None, detect_result=True):
        super().__init__(ledger_path)
        self._events = list(events or [])
        self._detect_result = detect_result

    def source_type(self):
        return "gemini"

    def cursor_key(self):
        return "agent:gemini"

    def detect(self):
        return self._detect_result

    def harvest(self, cursor=None):
        if not self._events:
            return []
        if cursor is None:
            return list(self._events)
        idx = cursor.get("index", 0)
        if idx >= len(self._events):
            return []
        return list(self._events[idx:])

    def advance_cursor(self, cursor, harvested_raw_events):
        old_index = cursor.get("index", 0) if cursor else 0
        return {"index": old_index + len(harvested_raw_events)}


class TestHarvestOneGeneraSummary:
    def test_harvest_one_genera_summary(self, tmp_path):
        """Harvest con fuente agente mock → se genera SESSION_SUMMARY en el ledger."""
        from causadb._harvester import Harvester
        from causadb._ledger_reader import LedgerReader

        ledger_path = str(tmp_path / "ledger.log")
        config_path = str(tmp_path / ".harvester_cursors.json")

        harvester = Harvester(ledger_path, config_path=config_path)

        raw_events = [
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:00Z",
             "response_tokens": 100, "response_content": "hello",
             "prompt": "say hello", "__harvest_session_id": "s1"},
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:05Z",
             "response_tokens": 50, "response_content": "bye",
             "prompt": "say bye", "__harvest_session_id": "s1"},
        ]

        source = MockAgentSource(ledger_path, events=raw_events)
        harvester.register_source(source)

        results = harvester.harvest_all()
        assert results.get("gemini", 0) > 0

        # Leer el ledger y buscar SESSION_SUMMARY
        reader = LedgerReader(ledger_path)
        summary_found = False
        for entry in reader.read_all_entries():
            if entry["event"]["event_type"] == "SESSION_SUMMARY":
                summary_found = True
                payload = entry["event"]["payload"]
                assert payload["tool"] == "gemini"
                assert payload["session_id"] == "s1"
                assert payload["turn_count"] == 2
                assert payload["tokens_used"] == 150
                break

        assert summary_found, "No se encontró SESSION_SUMMARY en el ledger después del harvest"