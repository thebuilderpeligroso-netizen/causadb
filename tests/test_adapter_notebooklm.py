"""
Tests para el adapter NotebookLM / Gemini (G.1).

Verifica que:
  1. ``query()`` retorna eventos reales del ledger (anti-teatro).
  2. ``format_for_notebooklm()`` incluye los event_ids en el markdown.
"""

import pytest
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb.adapters.notebooklm.adapter import query, format_for_notebooklm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger_path(tmp_path):
    """Crea un ledger limpio en un directorio temporal."""
    return str(tmp_path / "ledger.log")


@pytest.fixture
def seeded_ledger(ledger_path):
    """Escribe 3 eventos de distintos tipos en el ledger y devuelve la ruta."""
    writer = LedgerWriter(ledger_path)

    e1 = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="test_g1",
        source="notebooklm:test",
        payload={"path": "/tmp/a.txt", "action": "create"},
    )
    writer.append(e1)

    e2 = CanonicalEvent(
        event_type=EventType.COMMAND_RUN,
        ctx_id="test_g1",
        source="notebooklm:test",
        payload={"command": "ls -la", "exit_code": 0},
    )
    writer.append(e2)

    e3 = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test_g1",
        source="notebooklm:test",
        payload={"reasoning": "approve", "impact": "low", "decision_type": "tactical", "origin": "agent"},
    )
    writer.append(e3)

    return ledger_path, [e1, e2, e3]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNotebookLMAdapter:
    """Test suite para el adapter NotebookLM / Gemini."""

    def test_notebooklm_query_e2e(self, seeded_ledger):
        """
        G.1 — E2E: escribe 3 eventos al ledger, usa query() del adapter,
        verifica que retorna eventos reales con event_id, type, timestamp.
        """
        ledger_path, original_events = seeded_ledger

        # Consultar todos los eventos del contexto test_g1
        results = query({"ctx_id": "test_g1"}, ledger_path=ledger_path)

        # Debe retornar exactamente 3 eventos
        assert len(results) == 3, (
            f"Expected 3 events, got {len(results)}. "
            f"Anti-teatro: si el adapter retorna datos fijos, esto falla."
        )

        # Cada evento debe tener event_id, event_type, timestamp reales
        for ev in results:
            inner = ev.get("event", {})
            assert "event_id" in inner, (
                f"Missing event_id in nested event — real ledger event expected. "
                f"Got keys: {list(ev.keys())}"
            )
            assert "event_type" in inner, "Missing event_type"
            assert "timestamp" in inner, "Missing timestamp"

        # Los event_ids deben coincidir con los que generó LedgerWriter
        returned_ids = {ev["event"]["event_id"] for ev in results}
        expected_ids = {e.event_id for e in original_events}
        assert returned_ids == expected_ids, (
            f"event_ids mismatch. "
            f"Expected {expected_ids}, got {returned_ids}"
        )

    def test_notebooklm_format_cites_event_id(self, seeded_ledger):
        """
        G.1 — Format: llama format_for_notebooklm() con eventos reales
        y verifica que el markdown contiene los event_ids (respuesta citada).
        """
        ledger_path, original_events = seeded_ledger

        # Consultar y formatear
        results = query({"ctx_id": "test_g1"}, ledger_path=ledger_path)
        markdown = format_for_notebooklm(results)

        # El markdown debe contener cada event_id
        for ev in original_events:
            assert ev.event_id in markdown, (
                f"event_id {ev.event_id} not found in formatted output:\n{markdown}"
            )

        # El markdown debe contener los tipos de evento
        assert "FILE_MODIFIED" in markdown
        assert "COMMAND_RUN" in markdown
        assert "GOVERNANCE_DECISION" in markdown

        # Debe haber un bullet point por evento (3 en total)
        bullet_count = markdown.count("- **")
        assert bullet_count == 3, (
            f"Expected 3 bullet points in markdown, got {bullet_count}"
        )

    def test_notebooklm_format_empty_events(self):
        """
        G.1 — Format con lista vacía devuelve mensaje apropiado.
        """
        markdown = format_for_notebooklm([])
        assert "No events found" in markdown

    def test_notebooklm_query_no_results(self, ledger_path):
        """
        G.1 — Query sin resultados retorna lista vacía.
        """
        # Inicializar ledger con un evento de otro ctx_id
        writer = LedgerWriter(ledger_path)
        writer.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="other",
            source="test",
            payload={},
        ))

        # Consultar con ctx_id que no existe
        results = query({"ctx_id": "nonexistent"}, ledger_path=ledger_path)
        assert results == [], (
            f"Expected empty list for no-match query, got {results}"
        )

    def test_notebooklm_query_invalid_ledger_raises(self):
        """
        G.1 — Anti-teatro: ledger que no existe debe lanzar ValueError.
        """
        with pytest.raises(ValueError, match="No ledger path"):
            query({"ctx_id": "test"}, ledger_path=None)

        with pytest.raises((ValueError, FileNotFoundError, OSError)):
            query({"ctx_id": "test"}, ledger_path="/no/existe/ledger.log")
